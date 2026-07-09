#!/usr/bin/env python3
"""Deterministic, self-provisioning Playwright e2e check of the 0.9.0
non-racked tray (spec §9): real position-less DCIM devices (0U/vertical PDUs,
etc.) must render in their rack's editor tray as ``existing`` tiles on load,
and a rack with none must render an empty tray (negative case).

DETERMINISM: no drag/mouse interaction here (that is covered incrementally as
the interactive tray moves land) -- this is a pure load-and-inspect check,
same self-provisioning discipline as test_editor_sweep.py's primary fixture.

SELF-PROVISIONING: setUpClass creates its own throwaway manufacturer, device
role, site, device type, and TWO racks: one gets a real position-less device
(the tray-1 positive case), the other gets none (the negative case). A single
design scopes both racks so one page load (the multi-rack workspace) covers
both assertions. tearDownClass deletes everything it created, best-effort.

Run via ``dev/e2e.sh tests.e2e.test_editor_tray``.
"""
import json
import os
import unittest
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("RD_BASE", "http://127.0.0.1:8000").rstrip("/")
USER = os.environ.get("RD_USER", "rd_shot")
PASS = os.environ.get("RD_PASS", "ShotPass12345!")


def _check_prereqs():
    try:
        import playwright.sync_api  # noqa: F401
    except Exception as exc:  # pragma: no cover - env-dependent
        return False, f"playwright not importable ({exc})"

    try:
        req = urllib.request.Request(f"{BASE}/login/", method="GET")
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status >= 500:
                return False, f"dev server at {BASE} returned {resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            return False, f"dev server at {BASE} returned {exc.code}"
    except Exception as exc:
        return False, f"dev server at {BASE} not reachable ({exc})"

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            b = p.chromium.launch(channel="chrome", headless=True)
            b.close()
    except Exception as exc:  # pragma: no cover - env-dependent
        return False, f"headless Chrome unavailable ({exc})"

    return True, ""


_PREREQ_OK, _PREREQ_REASON = _check_prereqs()


# ---------------------------------------------------------------------------
# In-page interactive shim (spec §9.3 moves), same discipline as
# test_editor_sweep.py's window.__rdX: fires editor.js's REAL GridStack event
# handlers with exact grid coordinates -- no pixel geometry, no real mouse.
# `hostFor`/`gridFor` build ids "nbx-rd-grid-<face>-<rackId>"; passing
# face="tray" resolves to the tray host/grid exactly like "front"/"rear"
# resolve to a face grid -- the tray needs no special-cased lookup.
# ---------------------------------------------------------------------------
TRAY_HARNESS_JS_TEMPLATE = r"""
window.__rdTray = (function () {
    var RACKS = [%(rack_a)s, %(rack_b)s];
    var root = document.getElementById("rd-editor");

    function blockFor(rackId) {
        return document.querySelector('.nbx-rd-rack-block[data-rack-id="' + rackId + '"]');
    }
    function hostFor(rackId, face) {
        return document.getElementById("nbx-rd-grid-" + face + "-" + rackId);
    }
    function gridFor(rackId, face) { return hostFor(rackId, face).gridstack; }

    function fireHandler(grid, name, arg) {
        var handlers = grid._gsEventHandler && grid._gsEventHandler[name];
        var list = Array.isArray(handlers) ? handlers : (handlers ? [handlers] : []);
        list.forEach(function (h) { h({ type: name }, arg); });
    }
    function fireDropped(grid, newNode) {
        var handlers = grid._gsEventHandler && grid._gsEventHandler["dropped"];
        var list = Array.isArray(handlers) ? handlers : (handlers ? [handlers] : []);
        list.forEach(function (h) { h({ type: "dropped" }, null, newNode); });
    }
    function fastSetY(grid, el, newGsY) {
        var node = el.gridstackNode;
        node.x = 0;
        node.y = newGsY;
        node._orig = { x: node.x, y: node.y };
        grid._writePosAttr(el, node);
    }

    function tileByLabel(label) {
        var hit = null;
        root.querySelectorAll(".grid-stack-item").forEach(function (el) {
            if (hit) { return; }
            if (el.getAttribute("data-rd-derived-opp")) { return; }
            if (el.hasAttribute("data-rd-temp-ghost")) { return; }
            if (el.classList.contains("nbx-rd-state-move_out_ghost")) { return; }
            var span = el.querySelector(".nbx-rd-label");
            if (span && span.textContent === label) { hit = el; }
        });
        return hit;
    }

    function whereIs(el) {
        var host = el.closest(".grid-stack");
        if (!host) { return { rackId: null, face: null }; }
        var block = el.closest(".nbx-rd-rack-block");
        return {
            rackId: block ? parseInt(block.getAttribute("data-rack-id"), 10) : null,
            face: host.getAttribute("data-face") || "",
        };
    }

    // Move the subject (by label) to (rackId, faceParam, gsY). faceParam is
    // "front"/"rear" for a face grid or "tray" for the tray grid -- the tray
    // host id embeds the literal string "tray" (see hostFor above), so no
    // special-casing is needed to target it.
    function moveTo(label, rackId, faceParam, gsY) {
        var el = tileByLabel(label);
        if (!el) { return { ok: false, reason: "subject not found" }; }
        var at = whereIs(el);
        var destGrid = gridFor(rackId, faceParam);
        var destHost = hostFor(rackId, faceParam);
        var destDataFace = destHost.getAttribute("data-face") || "";
        if (at.rackId === rackId && at.face === destDataFace) {
            var grid = el.gridstackNode.grid;
            fireHandler(grid, "dragstart", el);
            fastSetY(grid, el, gsY);
            fireHandler(grid, "change", []);
            fireHandler(grid, "dragstop", el);
            return { ok: true, mode: "same-grid" };
        }
        var srcGrid = el.gridstackNode.grid;
        fireHandler(srcGrid, "dragstart", el);
        srcGrid.removeWidget(el, false);
        if (el.parentNode !== destHost) { destHost.appendChild(el); }
        el.setAttribute("gs-x", "0");
        el.setAttribute("gs-y", String(gsY));
        destGrid.makeWidget(el);
        if (el.parentNode === destHost && el.gridstackNode
                && el.gridstackNode.y !== gsY) {
            fastSetY(destGrid, el, gsY);
        }
        var newNode = el.gridstackNode || { el: el };
        fireDropped(destGrid, newNode);
        return { ok: true, mode: "cross-grid" };
    }

    function countRenameDialogs() {
        return document.querySelectorAll(".nbx-rd-move-modal").length;
    }

    function answerDialogs() {
        var out = { displaced: 0, renamed: 0 };
        document.querySelectorAll(".nbx-rd-displace-modal [data-rd-displace-confirm]").forEach(function (btn) {
            btn.click();
            out.displaced++;
        });
        document.querySelectorAll(".nbx-rd-move-modal [data-rd-move-apply]").forEach(function (btn) {
            btn.click();
            out.renamed++;
        });
        return out;
    }

    function subjectInfo(label) {
        var el = tileByLabel(label);
        if (!el) { return null; }
        var at = whereIs(el);
        var n = el.gridstackNode;
        return {
            rackId: at.rackId, face: at.face,
            y: (n && n.y != null) ? n.y : parseInt(el.getAttribute("gs-y"), 10),
            classes: Array.prototype.slice.call(el.classList),
        };
    }

    function ghostFor(label) {
        var hit = null;
        RACKS.forEach(function (rackId) {
            var block = blockFor(rackId);
            if (!block || hit) { return; }
            block.querySelectorAll(".grid-stack-item.nbx-rd-state-move_out_ghost").forEach(function (el) {
                if (hit) { return; }
                var span = el.querySelector(".nbx-rd-label");
                if (span && span.textContent === label) { hit = el; }
            });
        });
        return hit;
    }

    // Does the DESTINATION tray grid's own acceptWidgets policy accept a
    // foreign real device tile? This is EXACTLY the gate GridStack itself
    // runs before a native mouse drag is even allowed to transfer across
    // grids -- if false, a real drag never fires `dropped`/`added` at all
    // (rejected natively, tile snaps back to its source grid on release).
    function wouldAcceptForeignTile(label, destRackId, destFace) {
        var el = tileByLabel(label);
        if (!el) { return { ok: false, reason: "not found" }; }
        var destHost = hostFor(destRackId, destFace);
        var destGrid = destHost.gridstack;
        var fn = destGrid.opts && destGrid.opts.acceptWidgets;
        if (typeof fn !== "function") { return { ok: false, reason: "acceptWidgets not a function" }; }
        return { ok: true, accepted: !!fn(el) };
    }

    return {
        moveTo: moveTo,
        countRenameDialogs: countRenameDialogs,
        answerDialogs: answerDialogs,
        subjectInfo: subjectInfo,
        ghostFor: ghostFor,
        wouldAcceptForeignTile: wouldAcceptForeignTile,
    };
})();
"""


@unittest.skipUnless(_PREREQ_OK, f"editor tray prerequisites not met: {_PREREQ_REASON}")
class EditorTrayTestCase(unittest.TestCase):
    """T-tray-1 (spec §9.6): a real 0U device renders in the tray as
    'existing' on load; a rack with zero such devices renders an empty tray."""

    @classmethod
    def _api(cls, method, path, payload=None):
        headers = {"X-CSRFToken": cls._csrf, "Accept": "application/json"}
        kwargs = {"method": method, "headers": headers}
        if payload is not None:
            kwargs["data"] = payload
        resp = cls._api_ctx.request.fetch(f"{BASE}{path}", **kwargs)
        body = resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"{method} {path} -> HTTP {resp.status}: {body[:500]}")
        return json.loads(body) if body.strip() else None

    @classmethod
    def _provision_fixture(cls):
        suffix = uuid.uuid4().hex[:8]
        mfr = cls._api("POST", "/api/dcim/manufacturers/", {
            "name": f"E2E Tray Mfr {suffix}", "slug": f"e2e-tray-mfr-{suffix}"})
        role = cls._api("POST", "/api/dcim/device-roles/", {
            "name": f"E2E Tray Role {suffix}", "slug": f"e2e-tray-role-{suffix}",
            "color": "9e9e9e"})
        site = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E Tray Site {suffix}", "slug": f"e2e-tray-site-{suffix}",
            "status": "active"})
        dt = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-PDU-{suffix}",
            "slug": f"e2e-pdu-{suffix}", "u_height": 0, "is_full_depth": False})
        dt_racked = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-Racked-{suffix}",
            "slug": f"e2e-racked-{suffix}", "u_height": 1, "is_full_depth": False})
        rack_with_tray = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E Tray Rack With {suffix}", "site": site["id"],
            "status": "active", "u_height": 12})
        rack_without_tray = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E Tray Rack Without {suffix}", "site": site["id"],
            "status": "active", "u_height": 12})

        # This dev instance has a pre-existing global custom field
        # ("warranty_type", text-typed) whose configured default is a
        # non-string value, so leaving it unset on a POST 400s.
        cf_override = {"custom_fields": {"warranty_type": ""}}

        cls._pdu_name = f"e2e-tray-pdu-{suffix}"
        pdu = cls._api("POST", "/api/dcim/devices/", {
            "name": cls._pdu_name, "device_type": dt["id"], "role": role["id"],
            "site": site["id"], "rack": rack_with_tray["id"],
            "position": None, "face": "rear", "status": "active", **cf_override})
        # A SECOND real tray device, so the layout test has two pre-existing
        # tray tiles to check for non-overlap + bystander stability.
        cls._pdu2_name = f"e2e-tray-pdu2-{suffix}"
        pdu2 = cls._api("POST", "/api/dcim/devices/", {
            "name": cls._pdu2_name, "device_type": dt["id"], "role": role["id"],
            "site": site["id"], "rack": rack_with_tray["id"],
            "position": None, "face": "front", "status": "active", **cf_override})

        # A real RACKED device (spec §9.3 units<->tray moves need a mounted
        # subject) at a known U/face on the tray-having rack.
        cls._racked_name = f"e2e-tray-rackeddev-{suffix}"
        cls._racked_u = 5
        racked = cls._api("POST", "/api/dcim/devices/", {
            "name": cls._racked_name, "device_type": dt_racked["id"], "role": role["id"],
            "site": site["id"], "rack": rack_with_tray["id"],
            "position": str(cls._racked_u), "face": "front", "status": "active", **cf_override})

        cls._created = dict(
            manufacturer=mfr["id"], role=role["id"], site=site["id"],
            racks=[rack_with_tray["id"], rack_without_tray["id"]],
            device_types=[dt["id"], dt_racked["id"]],
            devices=[pdu["id"], pdu2["id"], racked["id"]],
        )
        cls._rack_with_tray_id = rack_with_tray["id"]
        cls._rack_without_tray_id = rack_without_tray["id"]
        cls._rack_u_height = 12
        cls._pdu_id = pdu["id"]
        cls._pdu2_id = pdu2["id"]
        cls._racked_id = racked["id"]
        cls._dt_id = dt["id"]

        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"tray-{suffix}", "site": site["id"],
            "racks": [rack_with_tray["id"], rack_without_tray["id"]]})
        cls._design_id = design["id"]
        cls.editor_url = (
            f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/")
        cls.HARNESS_JS = TRAY_HARNESS_JS_TEMPLATE % {
            "rack_a": rack_with_tray["id"], "rack_b": rack_without_tray["id"],
        }

    @classmethod
    def _cleanup_class(cls):
        try:
            if getattr(cls, "_design_id", None) is not None:
                try:
                    cls._api(
                        "DELETE",
                        f"/api/plugins/rack-design/designs/{cls._design_id}/")
                except Exception:
                    pass
                cls._design_id = None
            created = getattr(cls, "_created", None)
            if created:
                for did in created.get("devices", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/devices/{did}/")
                    except Exception:
                        pass
                for tid in created.get("device_types", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/device-types/{tid}/")
                    except Exception:
                        pass
                for rid in created.get("racks", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/racks/{rid}/")
                    except Exception:
                        pass
                if created.get("role") is not None:
                    try:
                        cls._api(
                            "DELETE", f"/api/dcim/device-roles/{created['role']}/")
                    except Exception:
                        pass
                if created.get("manufacturer") is not None:
                    try:
                        cls._api(
                            "DELETE",
                            f"/api/dcim/manufacturers/{created['manufacturer']}/")
                    except Exception:
                        pass
                if created.get("site") is not None:
                    try:
                        cls._api("DELETE", f"/api/dcim/sites/{created['site']}/")
                    except Exception:
                        pass
        finally:
            for closer in (
                lambda: cls._api_ctx.close(),
                lambda: cls._browser.close(),
                lambda: cls._pw.stop(),
            ):
                try:
                    closer()
                except Exception:
                    pass

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(channel="chrome", headless=True)
        cls._design_id = None
        cls._created = None
        cls._api_ctx = cls._browser.new_context(
            viewport={"width": 1600, "height": 1400})
        try:
            pg = cls._api_ctx.new_page()
            pg.goto(f"{BASE}/login/", wait_until="networkidle")
            pg.fill("#id_username", USER)
            pg.fill("#id_password", PASS)
            pg.click("button[type=submit]")
            pg.wait_for_load_state("networkidle")
            pg.close()
            cls._storage = cls._api_ctx.storage_state()
            cls._csrf = next(
                (c["value"] for c in cls._api_ctx.cookies()
                 if c["name"] == "csrftoken"), "")
            cls._provision_fixture()
        except BaseException:
            cls._cleanup_class()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_class()

    def setUp(self):
        self.ctx = self._browser.new_context(
            storage_state=self._storage, viewport={"width": 1600, "height": 1400})
        self.page = self.ctx.new_page()
        self.console_errors = []
        self.page.on(
            "console",
            lambda m: self.console_errors.append(f"{m.type}: {m.text}")
            if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.console_errors.append(f"PAGEERROR: {e}"))
        resp = self.page.goto(self.editor_url, wait_until="networkidle")
        self.assertIsNotNone(resp, "no response loading the editor URL")
        self.assertEqual(resp.status, 200, f"editor URL returned {resp.status}")
        self.page.wait_for_selector("#rd-editor", timeout=15000)
        self.page.wait_for_timeout(1000)  # let GridStack finish init
        self.page.add_script_tag(content=self.HARNESS_JS)

    def tearDown(self):
        if getattr(self, "ctx", None):
            self.ctx.close()

    def _tray_tile_labels(self, rack_id):
        return self.page.evaluate(
            """(rackId) => {
                const tray = document.getElementById('nbx-rd-grid-tray-' + rackId);
                if (!tray) { return null; }
                return Array.from(tray.querySelectorAll('.grid-stack-item'))
                    .map(el => ({
                        label: el.querySelector('.nbx-rd-label')?.textContent || '',
                        state: (el.className.match(/nbx-rd-state-([a-z_]+)/) || [])[1] || '',
                    }));
            }""",
            rack_id,
        )

    def test_tray_1_real_device_renders_as_existing(self):
        tiles = self._tray_tile_labels(self._rack_with_tray_id)
        self.assertIsNotNone(tiles, "tray grid not found for the fixture rack")
        matches = [t for t in tiles if t["label"] == self._pdu_name]
        self.assertEqual(
            len(matches), 1,
            f"expected exactly one tray tile for {self._pdu_name}, got {tiles}")
        self.assertEqual(matches[0]["state"], "existing")

    def test_tray_1_negative_rack_without_tray_devices_is_empty(self):
        tiles = self._tray_tile_labels(self._rack_without_tray_id)
        self.assertIsNotNone(tiles, "tray grid not found for the negative-case rack")
        self.assertEqual(tiles, [])

    @staticmethod
    def _u_to_gsy(rack_u_height, u_position, gs_h):
        if gs_h > 2:
            return rack_u_height * 2 - u_position * 2 - gs_h + 2
        return rack_u_height * 2 - u_position * 2

    def test_tray_2_units_to_tray_then_homecoming_is_silent(self):
        """T-tray-2/T-tray-4 (spec §9.3): a racked device dragged into the
        tray gets its origin ghost (crossed marker) and renders move_in in
        the tray (after the rename dialog, spec §9.3 "rename dialog per
        naming feature"); dragging it back onto its own origin U is a SILENT
        homecoming (spec §4.4, extended by §9.2) -- no new dialog, ghost
        gone, back to 'existing'."""
        label = self._racked_name
        rack_id = self._rack_with_tray_id

        before = self.page.evaluate(f"() => window.__rdTray.subjectInfo({json.dumps(label)})")
        self.assertIsNotNone(before)
        self.assertEqual(before["face"], "front")
        orig_gsy = before["y"]  # the tile's REAL server-rendered row -- trust the DOM, not a re-derived formula.

        r = self.page.evaluate(
            f"() => window.__rdTray.moveTo({json.dumps(label)}, {rack_id}, 'tray', 0)")
        self.assertTrue(r.get("ok"), f"moveTo (units->tray) failed: {r}")
        self.page.wait_for_timeout(150)

        dialogs = self.page.evaluate("() => window.__rdTray.countRenameDialogs()")
        self.assertGreaterEqual(
            dialogs, 1,
            "moving an existing device into the tray must open the §4a "
            "rename dialog (spec §9.3: 'rename dialog per naming feature')")
        self.page.evaluate("() => window.__rdTray.answerDialogs()")
        # The applied modal fades out asynchronously (Bootstrap transition);
        # wait for it to fully leave the DOM so the homecoming leg below can
        # only see a dialog if ITS OWN gesture opened one.
        self.page.wait_for_function(
            "() => window.__rdTray.countRenameDialogs() === 0", timeout=5000)
        self.page.wait_for_timeout(150)

        info = self.page.evaluate(f"() => window.__rdTray.subjectInfo({json.dumps(label)})")
        self.assertIsNotNone(info, "subject lost after units->tray move")
        self.assertEqual(info["rackId"], rack_id, info)
        self.assertEqual(info["face"], "", f"tray tile must have no face: {info}")
        self.assertIn("nbx-rd-state-move_in", info["classes"], info)

        ghost = self.page.evaluate(f"() => !!window.__rdTray.ghostFor({json.dumps(label)})")
        self.assertTrue(ghost, "origin ghost must remain at the vacated U (spec §9.3/§4.4)")

        violations = self.page.evaluate("() => window.__rdModel.check()")
        self.assertEqual(violations, [], f"invariant violations after units->tray: {violations}")

        # ---- homecoming: drag it back onto its own origin U -- SILENT ----
        r2 = self.page.evaluate(
            f"() => window.__rdTray.moveTo({json.dumps(label)}, {rack_id}, 'front', {orig_gsy})")
        self.assertTrue(r2.get("ok"), f"moveTo (tray->units homecoming) failed: {r2}")
        self.page.wait_for_timeout(150)

        dialogs_after = self.page.evaluate("() => window.__rdTray.countRenameDialogs()")
        self.assertEqual(
            dialogs_after, 0,
            "homecoming onto the device's own origin ghost must be silent (spec §4.4/§9.3)")

        final = self.page.evaluate(f"() => window.__rdTray.subjectInfo({json.dumps(label)})")
        self.assertIsNotNone(final, "subject lost after homecoming")
        self.assertEqual(final["rackId"], rack_id, final)
        self.assertEqual(final["face"], "front", final)
        self.assertEqual(final["y"], orig_gsy, final)
        self.assertIn("nbx-rd-state-existing", final["classes"], final)

        ghost_after = self.page.evaluate(f"() => !!window.__rdTray.ghostFor({json.dumps(label)})")
        self.assertFalse(ghost_after, "origin ghost must be gone after a full homecoming")

        final_violations = self.page.evaluate("() => window.__rdModel.check()")
        self.assertEqual(final_violations, [], f"invariant violations after homecoming: {final_violations}")
        self.assertEqual(self.console_errors, [], f"console errors: {self.console_errors}")

    def test_tray_6_palette_add_into_tray(self):
        """T-tray-6 (spec §9.3/§9.6): a palette add dropped into the tray
        registers a position-less 'add' entry -- add styling, no U/face, no
        dialog (adds have their own inline name field)."""
        # Synthesize a palette clone directly in the tray grid, mirroring
        # what a real drag does BEFORE `dropped` fires (GridStack clones the
        # dragged palette source element into the destination grid's DOM).
        label = "e2e-tray-palette-add"
        tray_host_id = f"nbx-rd-grid-tray-{self._rack_with_tray_id}"
        result = self.page.evaluate(
            """({trayHostId, dtId, dtLabel}) => {
                const trayHost = document.getElementById(trayHostId);
                const el = document.createElement('div');
                el.className = 'grid-stack-item nbx-rd-palette-item';
                el.setAttribute('data-device-type-id', dtId);
                el.setAttribute('data-u-height', '0');
                el.setAttribute('data-is-full-depth', 'false');
                el.setAttribute('data-label', dtLabel);
                el.setAttribute('data-model', dtLabel);
                el.setAttribute('gs-w', '1');
                el.setAttribute('gs-h', '2');
                trayHost.appendChild(el);
                const grid = trayHost.gridstack;
                grid.makeWidget(el);
                const handlers = grid._gsEventHandler && grid._gsEventHandler['dropped'];
                const list = Array.isArray(handlers) ? handlers : (handlers ? [handlers] : []);
                const newNode = el.gridstackNode || {el: el};
                list.forEach(h => h({type: 'dropped'}, null, newNode));
                return true;
            }""",
            {"trayHostId": tray_host_id, "dtId": self._dt_id, "dtLabel": label},
        )
        self.assertTrue(result)
        self.page.wait_for_timeout(150)
        # A brand-new add has device_id=null; find it by its add-state class
        # inside the tray host with the synthesized label.
        added = self.page.evaluate(
            """(trayHostId) => {
                const tray = document.getElementById(trayHostId);
                const el = tray.querySelector('.nbx-rd-state-add');
                if (!el) { return null; }
                return {
                    classes: Array.from(el.classList),
                    hasUPosition: el.hasAttribute('gs-y') && el.getAttribute('gs-y') !== null,
                };
            }""",
            tray_host_id,
        )
        self.assertIsNotNone(added, "palette add into the tray did not register")
        self.assertIn("nbx-rd-state-add", added["classes"])
        violations = self.page.evaluate("() => window.__rdModel.check()")
        self.assertEqual(violations, [], f"invariant violations after palette->tray add: {violations}")
        self.assertEqual(self.console_errors, [], f"console errors: {self.console_errors}")

    def test_tray_4_cross_rack_accept_widgets_permits_foreign_tile(self):
        """The reported blocker ("ПДУ не перетаскивается в соседнюю стойку"):
        GridStack's own acceptWidgets policy on a tray grid must accept a
        foreign real device tile from another rack -- otherwise a real mouse
        drag never even fires `dropped`/`added` (native rejection, before any
        of editor.js's own code runs)."""
        accepted = self.page.evaluate(
            f"() => window.__rdTray.wouldAcceptForeignTile("
            f"{json.dumps(self._pdu_name)}, {self._rack_without_tray_id}, 'tray')")
        self.assertTrue(accepted.get("ok"), accepted)
        self.assertTrue(
            accepted.get("accepted"),
            "a tray grid must accept a foreign real device tile "
            "(spec §9.3 'tray -> tray (cross-rack)')")

    def test_tray_4_cross_rack_reassociation_drag_and_homecoming(self):
        """T-tray-4 (spec §9.3): dragging a real tray device from rack A's
        tray onto rack B's tray fires the cross-rack move dialog, leaves a
        list-style ghost (no rows, no shadow) at A's tray, and I4 stays
        clean; dragging it back onto A's own tray ghost is a silent
        homecoming (single entity, world-wide)."""
        label = self._pdu2_name
        rack_a = self._rack_with_tray_id
        rack_b = self._rack_without_tray_id

        before = self.page.evaluate(f"() => window.__rdTray.subjectInfo({json.dumps(label)})")
        self.assertEqual(before["rackId"], rack_a, before)

        r = self.page.evaluate(
            f"() => window.__rdTray.moveTo({json.dumps(label)}, {rack_b}, 'tray', 0)")
        self.assertTrue(r.get("ok"), f"cross-rack tray->tray moveTo failed: {r}")
        self.page.wait_for_timeout(200)

        dialogs = self.page.evaluate("() => window.__rdTray.countRenameDialogs()")
        self.assertGreaterEqual(
            dialogs, 1,
            "a committed cross-rack tray->tray move must open the §4a rename dialog")
        self.page.evaluate("() => window.__rdTray.answerDialogs()")
        self.page.wait_for_function(
            "() => window.__rdTray.countRenameDialogs() === 0", timeout=5000)
        self.page.wait_for_timeout(200)

        info = self.page.evaluate(f"() => window.__rdTray.subjectInfo({json.dumps(label)})")
        self.assertIsNotNone(info, "subject lost after cross-rack tray->tray move")
        self.assertEqual(info["rackId"], rack_b, info)
        self.assertEqual(info["face"], "", info)
        self.assertIn("nbx-rd-state-move_in", info["classes"], info)

        ghost = self.page.evaluate(f"() => !!window.__rdTray.ghostFor({json.dumps(label)})")
        self.assertTrue(
            ghost, "origin rack's tray must keep a list-style ghost entry (spec §9.3)")

        violations = self.page.evaluate("() => window.__rdModel.check()")
        self.assertEqual(violations, [], f"invariant violations after cross-rack tray move: {violations}")

        # ---- homecoming: drag it back onto rack A's own tray ghost -- SILENT ----
        r2 = self.page.evaluate(
            f"() => window.__rdTray.moveTo({json.dumps(label)}, {rack_a}, 'tray', 0)")
        self.assertTrue(r2.get("ok"), f"homecoming moveTo failed: {r2}")
        self.page.wait_for_timeout(200)

        dialogs_after = self.page.evaluate("() => window.__rdTray.countRenameDialogs()")
        self.assertEqual(
            dialogs_after, 0,
            "homecoming onto the device's own origin tray ghost must be silent")

        final = self.page.evaluate(f"() => window.__rdTray.subjectInfo({json.dumps(label)})")
        self.assertIsNotNone(final, "subject lost after homecoming")
        self.assertEqual(final["rackId"], rack_a, final)
        self.assertEqual(final["face"], "", final)
        self.assertIn("nbx-rd-state-existing", final["classes"], final)

        ghost_after = self.page.evaluate(f"() => !!window.__rdTray.ghostFor({json.dumps(label)})")
        self.assertFalse(ghost_after, "origin ghost must be gone after a full homecoming")

        final_violations = self.page.evaluate("() => window.__rdModel.check()")
        self.assertEqual(
            final_violations, [],
            f"I4/I1/I2 violations after the round trip: {final_violations}")
        self.assertEqual(self.console_errors, [], f"console errors: {self.console_errors}")

    def test_tray_4_origin_ghost_row_and_style_parity(self):
        """Live-acceptance regression (design 6, dra4-pdu-rf11-a1): after a
        tray->tray cross-rack move, the ORIGIN tray's ghost must (a) occupy
        its OWN row -- never the same rows as a remaining tray tile (the
        ghost's 12%-alpha background over a solid dark neighbour composites
        into what reads as 'a solid role-colored ghost') -- and (b) carry the
        standard move_out_ghost visual: translucent grey background (never
        the role color), dashed border, italic. Moves the FIRST tray tile
        (row 0) so the remaining tile (row 2) exposes the append-row bug."""
        label = self._pdu_name          # first tray tile (row 0; names sort pdu < pdu2)
        bystander = self._pdu2_name     # stays behind at row 2
        rack_a = self._rack_with_tray_id
        rack_b = self._rack_without_tray_id

        r = self.page.evaluate(
            f"() => window.__rdTray.moveTo({json.dumps(label)}, {rack_b}, 'tray', 0)")
        self.assertTrue(r.get("ok"), f"cross-rack tray->tray moveTo failed: {r}")
        self.page.wait_for_timeout(200)
        self.page.evaluate("() => window.__rdTray.answerDialogs()")
        self.page.wait_for_function(
            "() => window.__rdTray.countRenameDialogs() === 0", timeout=5000)
        self.page.wait_for_timeout(200)

        tray = self.page.evaluate(
            """({rackId, ghostLabel}) => {
                const tray = document.getElementById('nbx-rd-grid-tray-' + rackId);
                const out = {ghost: null, others: []};
                tray.querySelectorAll('.grid-stack-item').forEach(el => {
                    const span = el.querySelector('.nbx-rd-label');
                    const lbl = span ? span.textContent : '';
                    const n = el.gridstackNode;
                    const y = (n && n.y != null) ? n.y : parseInt(el.getAttribute('gs-y'), 10);
                    const h = (n && n.h != null) ? n.h : parseInt(el.getAttribute('gs-h'), 10);
                    const rec = {label: lbl, y: y, h: h,
                                 classes: Array.from(el.classList)};
                    if (el.classList.contains('nbx-rd-state-move_out_ghost')
                            && lbl === ghostLabel) {
                        const content = el.querySelector('.grid-stack-item-content');
                        const cs = content ? getComputedStyle(content) : null;
                        rec.style = cs ? {
                            backgroundColor: cs.backgroundColor,
                            borderTopStyle: cs.borderTopStyle,
                            fontStyle: cs.fontStyle,
                        } : null;
                        rec.inlineStyle = content ? content.getAttribute('style') : null;
                        out.ghost = rec;
                    } else {
                        out.others.push(rec);
                    }
                });
                return out;
            }""",
            {"rackId": rack_a, "ghostLabel": label},
        )
        ghost = tray["ghost"]
        self.assertIsNotNone(ghost, f"origin tray ghost not found: {tray}")

        # (a) The ghost occupies its OWN rows -- no overlap with any remaining
        # tray tile (the reported live bug: ghost landed on the bystander's row).
        for other in tray["others"]:
            overlap = (ghost["y"] < other["y"] + other["h"]
                       and other["y"] < ghost["y"] + ghost["h"])
            self.assertFalse(
                overlap,
                f"origin tray ghost (rows {ghost['y']}..{ghost['y'] + ghost['h'] - 1}) "
                f"overlaps remaining tile {other['label']} "
                f"(rows {other['y']}..{other['y'] + other['h'] - 1}): {tray}")
        # The bystander stays in the tray at ITS OWN (possibly reflowed) row.
        # Spec §9.4 (coordinator ruling 2026-07-09): the tray is a COMPACT
        # list -- rows renumber to contiguous after a removal; §4.1's
        # no-bystander-movement rule constrains rack positions (U), not list
        # reflow. So the bystander may legitimately move UP to row 0 here.
        bystander_rec = next(
            (o for o in tray["others"] if o["label"] == bystander), None)
        self.assertIsNotNone(bystander_rec, tray)

        # (b) Style parity with face ghosts (spec §3 via §9.4): the standard
        # move_out_ghost class + translucent grey bg (alpha < 1, so never a
        # solid role color), dashed border, italic; no inline role background.
        self.assertIn("nbx-rd-state-move_out_ghost", ghost["classes"], ghost)
        self.assertIsNotNone(ghost.get("style"), ghost)
        self.assertIn(
            "rgba", ghost["style"]["backgroundColor"],
            f"ghost background must be translucent (rgba), not a solid role "
            f"color: {ghost}")
        self.assertEqual(ghost["style"]["borderTopStyle"], "dashed", ghost)
        self.assertEqual(ghost["style"]["fontStyle"], "italic", ghost)
        self.assertFalse(
            ghost.get("inlineStyle") and "background" in ghost["inlineStyle"],
            f"ghost must not carry an inline role background: {ghost}")

        violations = self.page.evaluate("() => window.__rdModel.check()")
        self.assertEqual(violations, [], f"invariant violations: {violations}")
        self.assertEqual(self.console_errors, [], f"console errors: {self.console_errors}")

    def _move_tray_tile(self, label, rack_id, answer=True):
        """Drive one tray->tray move of `label` to `rack_id` and (optionally)
        apply its rename dialog, waiting for the modal to fully close."""
        r = self.page.evaluate(
            f"() => window.__rdTray.moveTo({json.dumps(label)}, {rack_id}, 'tray', 0)")
        self.assertTrue(r.get("ok"), f"moveTo({label} -> rack {rack_id}) failed: {r}")
        self.page.wait_for_timeout(200)
        if answer:
            self.page.evaluate("() => window.__rdTray.answerDialogs()")
        self.page.wait_for_function(
            "() => window.__rdTray.countRenameDialogs() === 0", timeout=5000)
        self.page.wait_for_timeout(200)

    def _assert_tray_compact(self, rack_id, expected_labels, context):
        """The rack's tray holds exactly `expected_labels` at contiguous rows
        0,2,4,... (a list has no holes -- spec §9.4 compaction ruling)."""
        state = self._tray_rows(rack_id)
        rows = state["rows"]
        self.assertEqual(
            sorted(rows.keys()), sorted(expected_labels),
            f"{context}: unexpected tray membership: {state}")
        self.assertEqual(
            sorted(rows.values()), [i * 2 for i in range(len(expected_labels))],
            f"{context}: tray rows must be contiguous from 0 (no holes): {state}")
        return state

    def test_tray_compaction_after_cancel_return(self):
        """Compaction scenario (a): move one tray tile out (its ghost takes a
        list row), then bring it home -- the tray must end compact from row 0
        with no dead gap, and the container must shrink back to content."""
        rack_a = self._rack_with_tray_id
        rack_b = self._rack_without_tray_id
        label = self._pdu_name

        height_on_load = self._tray_rows(rack_a)["containerHeight"]

        self._move_tray_tile(label, rack_b)
        # Home again (silent homecoming, no dialog to answer).
        r = self.page.evaluate(
            f"() => window.__rdTray.moveTo({json.dumps(label)}, {rack_a}, 'tray', 0)")
        self.assertTrue(r.get("ok"), f"homecoming moveTo failed: {r}")
        self.page.wait_for_timeout(300)

        state = self._assert_tray_compact(
            rack_a, [self._pdu_name, self._pdu2_name], "after out-and-home round trip")
        self.assertEqual(
            state["containerHeight"], height_on_load,
            f"tray container must shrink back to content height: "
            f"load={height_on_load}, after={state['containerHeight']}")

        violations = self.page.evaluate("() => window.__rdModel.check()")
        self.assertEqual(violations, [], f"invariant violations: {violations}")
        self.assertEqual(self.console_errors, [], f"console errors: {self.console_errors}")

    def test_tray_compaction_after_double_round_trip(self):
        """Compaction scenario (b) -- the user's exact live repro: move BOTH
        tray tiles cross-rack, then both back home. The tray must end with
        both tiles at contiguous rows (0,2) in a stable order and no dead gap
        above (pre-fix: both returned BELOW their then-live ghosts' rows, and
        the destroyed ghosts left rows 0..3 empty)."""
        rack_a = self._rack_with_tray_id
        rack_b = self._rack_without_tray_id

        height_on_load = self._tray_rows(rack_a)["containerHeight"]

        # Both out...
        self._move_tray_tile(self._pdu_name, rack_b)
        self._move_tray_tile(self._pdu2_name, rack_b)
        # ...both home (silent homecomings).
        for label in (self._pdu_name, self._pdu2_name):
            r = self.page.evaluate(
                f"() => window.__rdTray.moveTo({json.dumps(label)}, {rack_a}, 'tray', 0)")
            self.assertTrue(r.get("ok"), f"homecoming moveTo({label}) failed: {r}")
            self.page.wait_for_timeout(300)

        state = self._assert_tray_compact(
            rack_a, [self._pdu_name, self._pdu2_name], "after double round trip")
        # Order stable: the return order (pdu first, then pdu2).
        self.assertLess(
            state["rows"][self._pdu_name], state["rows"][self._pdu2_name],
            f"return order must be preserved: {state}")
        self.assertEqual(
            state["containerHeight"], height_on_load,
            f"tray container must shrink back to content height: "
            f"load={height_on_load}, after={state['containerHeight']}")
        # Rack B's tray is empty again.
        state_b = self._tray_rows(rack_b)
        self.assertEqual(state_b["rows"], {}, f"rack B tray must be empty: {state_b}")

        violations = self.page.evaluate("() => window.__rdModel.check()")
        self.assertEqual(violations, [], f"invariant violations: {violations}")
        self.assertEqual(self.console_errors, [], f"console errors: {self.console_errors}")

    def test_tray_1_model_check_is_clean(self):
        # window.__rdModel.check() runs I1/I2/I4 over the whole page; a plain
        # load with an existing tray tile and an untouched empty tray must be
        # invariant-clean, and the load itself must be console-error-free.
        violations = self.page.evaluate("() => window.__rdModel.check()")
        self.assertEqual(violations, [], f"invariant violations on load: {violations}")
        self.assertEqual(
            self.console_errors, [],
            f"unexpected console errors: {self.console_errors}")

    def _tray_rows(self, rack_id):
        """{label: gs-y} for every LIVE (non-temp-ghost) tile in the rack's
        tray grid, plus the tray container's own content height."""
        return self.page.evaluate(
            """(rackId) => {
                const tray = document.getElementById('nbx-rd-grid-tray-' + rackId);
                const rows = {};
                tray.querySelectorAll('.grid-stack-item').forEach(el => {
                    const label = el.querySelector('.nbx-rd-label')?.textContent || '';
                    const n = el.gridstackNode;
                    rows[label] = (n && n.y != null) ? n.y : parseInt(el.getAttribute('gs-y'), 10);
                });
                return {rows: rows, containerHeight: tray.getBoundingClientRect().height};
            }""",
            rack_id,
        )

    def test_tray_layout_appends_without_overlap_or_bystander_movement(self):
        """T-tray-layout: the tray is a LIST (spec §9.2/§9.4) -- every item
        (2 pre-existing real devices + a palette add + a units->tray move)
        gets its own row; existing items' rows never move as a side effect
        of a later drop (spec §4.1 no-bystander-movement extends to the
        tray); the container grows to fit all 4 rows."""
        rack_id = self._rack_with_tray_id
        before = self._tray_rows(rack_id)
        self.assertEqual(
            len(before["rows"]), 2,
            f"expected the 2 fixture PDUs pre-loaded in the tray: {before}")
        pdu_y_before = before["rows"][self._pdu_name]
        pdu2_y_before = before["rows"][self._pdu2_name]
        self.assertNotEqual(
            pdu_y_before, pdu2_y_before,
            f"the 2 pre-existing tray tiles must already occupy distinct rows on load: {before}")

        # ---- drop 1: palette add into the tray (same synthesis as T-tray-6) ----
        tray_host_id = f"nbx-rd-grid-tray-{rack_id}"
        add_label = "e2e-tray-layout-palette-add"
        self.page.evaluate(
            """({trayHostId, dtId, dtLabel}) => {
                const trayHost = document.getElementById(trayHostId);
                const el = document.createElement('div');
                el.className = 'grid-stack-item nbx-rd-palette-item';
                el.setAttribute('data-device-type-id', dtId);
                el.setAttribute('data-u-height', '0');
                el.setAttribute('data-is-full-depth', 'false');
                el.setAttribute('data-label', dtLabel);
                el.setAttribute('data-model', dtLabel);
                el.setAttribute('gs-w', '1');
                el.setAttribute('gs-h', '2');
                trayHost.appendChild(el);
                const grid = trayHost.gridstack;
                grid.makeWidget(el);
                const handlers = grid._gsEventHandler && grid._gsEventHandler['dropped'];
                const list = Array.isArray(handlers) ? handlers : (handlers ? [handlers] : []);
                const newNode = el.gridstackNode || {el: el};
                list.forEach(h => h({type: 'dropped'}, null, newNode));
            }""",
            {"trayHostId": tray_host_id, "dtId": self._dt_id, "dtLabel": add_label},
        )
        self.page.wait_for_timeout(150)

        # ---- drop 2: units -> tray move of the real racked device ----
        r = self.page.evaluate(
            f"() => window.__rdTray.moveTo({json.dumps(self._racked_name)}, {rack_id}, 'tray', 0)")
        self.assertTrue(r.get("ok"), f"moveTo (units->tray) failed: {r}")
        self.page.wait_for_timeout(150)
        self.page.evaluate("() => window.__rdTray.answerDialogs()")
        self.page.wait_for_function(
            "() => window.__rdTray.countRenameDialogs() === 0", timeout=5000)
        self.page.wait_for_timeout(150)

        after = self._tray_rows(rack_id)
        self.assertEqual(
            len(after["rows"]), 4,
            f"expected 4 distinct tray tiles after 2 drops: {after}")

        # Bystander rule (spec §4.1): the 2 pre-existing tiles keep their rows.
        self.assertEqual(after["rows"][self._pdu_name], pdu_y_before, after)
        self.assertEqual(after["rows"][self._pdu2_name], pdu2_y_before, after)

        # All 4 rows are pairwise distinct (no overlap).
        rows = list(after["rows"].values())
        self.assertEqual(
            len(set(rows)), 4,
            f"tray tiles must occupy 4 DISTINCT rows (no overlap): {after}")

        # The container's rendered height covers all 4 rows (grows with content).
        self.assertGreater(
            after["containerHeight"], before["containerHeight"],
            f"tray container must grow to fit its new content: before={before}, after={after}")

        violations = self.page.evaluate("() => window.__rdModel.check()")
        self.assertEqual(violations, [], f"invariant violations after tray layout drops: {violations}")
        self.assertEqual(self.console_errors, [], f"console errors: {self.console_errors}")
