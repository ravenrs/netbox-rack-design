#!/usr/bin/env python3
"""Deterministic, self-provisioning Playwright e2e regression: drop a device
from the palette (both a full-depth and a plain type), then sweep the newly
added tile across every 0.5U row of both rack faces (including cross-face
hops), asserting a fixed set of invariants after every step.

This is the "add" counterpart to ``tests/e2e/test_editor_sweep.py`` (which
sweeps an EXISTING device). The ADD code path is different: a palette drop
creates a ``kind: "add"`` tile with ``device_id: null`` and no server-side
placement yet, exercising ``onPaletteDrop`` / the add-tile styling /
add-specific opposite-face-hatch derivation instead of the existing-device
move path.

DETERMINISM: no step uses a real pixel-based Playwright mouse drag. Every
move is driven through the in-page ``window.__rdAdd`` shim injected below,
which fires editor.js's REAL GridStack event handlers with EXACT grid
coordinates -- the same technique as ``test_editor_sweep.py`` (see that
file's harness for detailed commentary on ``moveTile``/``moveTileToFace``).
The palette-drop shim additionally sets ``data-is-full-depth`` on the
synthesised clone before dropping it (the earlier discovery-era shim did
not, so full-depth adds were never actually exercised by it) -- editor.js's
``onPaletteDrop`` reads that attribute to seed the new widget's
``is_full_depth`` flag, which is what makes the opposite-face shadow hatch
appear for a full-depth add at all.

SELF-PROVISIONING: ``setUpClass`` creates its OWN throwaway manufacturer,
device role, site, two device types (a 1U half-depth type and a 3U
full-depth type), and a fresh, otherwise-EMPTY, modest rack (16U by
default). A throwaway design with NO placements is created over that rack --
the sweep's own palette drops are the only content. ``tearDownClass``
deletes the design, then the created device types/rack/role/manufacturer/
site, in dependency order, best-effort. If DCIM creation is blocked
(permissions/validation), the suite SKIPS cleanly with a clear reason (there
is no meaningful "discover an existing empty rack to add into" fallback, so
unlike the existing-device sweep this one does not attempt one).

Prerequisites (auto-detected, SKIPS cleanly when missing): the
``playwright`` package + a Chrome channel, and a reachable dev server on
``RD_BASE``.

Run via ``dev/e2e.sh tests.e2e.test_editor_add_sweep``.
"""
import json
import os
import unittest
import urllib.error
import urllib.request
import uuid

# ---------------------------------------------------------------------------
# Configuration (matches dev/config.sh)
# ---------------------------------------------------------------------------
BASE = os.environ.get("RD_BASE", "http://127.0.0.1:8000").rstrip("/")
USER = os.environ.get("RD_USER", "rd_shot")
PASS = os.environ.get("RD_PASS", "ShotPass12345!")

# Modest so a FULL (non-sampled) 0.5U sweep of both faces stays fast.
RACK_U_HEIGHT = int(os.environ.get("RD_ADD_SWEEP_RACK_U_HEIGHT", "16"))
STEP_SETTLE_MS = 60


# ---------------------------------------------------------------------------
# Prerequisite guard -- SKIP CLEANLY (do not fail) when the environment is
# not ready. Mirrors test_editor_e2e._check_prereqs.
# ---------------------------------------------------------------------------
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
# In-page harness, built per-class once the fixture's rack id is known. It
# combines: (a) the buildRackPayload()/dropPaletteItem() shim from
# test_editor_e2e.py (here fixing the bug that shim had -- no
# data-is-full-depth on the synthesised clone), and (b) the moveTile /
# moveTileToFace / snapshot primitives from test_editor_sweep.py.
# ---------------------------------------------------------------------------
HARNESS_JS_TEMPLATE = r"""
window.__rdAdd = (function () {
    var RACK_PK = "%(rack_pk)s";
    var root = document.getElementById("rd-rack-" + RACK_PK);
    var rackId = parseInt(root.getAttribute("data-rack-id"), 10);
    var rackUHeight = parseInt(root.getAttribute("data-u-height"), 10);
    var descUnits = root.getAttribute("data-desc-units") === "true";

    var baseWidgets = JSON.parse(
        (document.getElementById("rd-editor-data-" + RACK_PK) || {}).textContent || "[]");

    function frontGrid() { return document.getElementById("nbx-rd-grid-front-" + RACK_PK).gridstack; }
    function rearGrid() { return document.getElementById("nbx-rd-grid-rear-" + RACK_PK).gridstack; }
    function trayGrid() { return document.getElementById("nbx-rd-grid-tray-" + RACK_PK).gridstack; }
    function gridFor(face) { return face === "front" ? frontGrid() : rearGrid(); }
    function hostFor(face) { return document.getElementById("nbx-rd-grid-" + face + "-" + RACK_PK); }

    function tileEl(idx) {
        return root.querySelector('.grid-stack-item[data-widget-index="' + idx + '"]');
    }

    function gsYToUPosition(gsY, gsH) {
        var y = gsY / 2;
        var uHeight = gsH / 2;
        if (descUnits) { return y + 1; }
        if (uHeight > 1) { return rackUHeight - y - uHeight + 1; }
        return rackUHeight - y;
    }

    // Resolve the "widget" for a tile the way editor.js's `state[idx].widget`
    // does: base JSON for pre-existing tiles, or the stashed dtid/uheight for
    // a palette-dropped add.
    function widgetForTile(el) {
        // An owned full-depth shadow / ghost-mirror hatch (editor.js Phase 3:
        // placeOrMoveShadow) now carries its OWNER's state class (spec §3 --
        // e.g. an add's shadow is styled "nbx-rd-state-add" too, not a fixed
        // generic style) so the legend filters it correctly. It must still
        // never be mistaken for a real tile here, same as editor.js's real
        // pushItem excludes it implicitly (no state[idx] entry for a hatch).
        if (el.getAttribute("data-rd-derived-opp")) { return null; }
        var idx = parseInt(el.getAttribute("data-widget-index"), 10);
        if (!isNaN(idx) && baseWidgets[idx]) { return baseWidgets[idx]; }
        if (el.classList.contains("nbx-rd-state-add") ||
            (el.getAttribute("data-rd-test-dtid") != null)) {
            var dtid = el.getAttribute("data-rd-test-dtid");
            return {
                kind: "add",
                device_id: null,
                device_type_id: dtid != null ? parseInt(dtid, 10) : null,
                placement_id: null,
                u_height: parseFloat(el.getAttribute("data-rd-test-uheight")) || 1,
            };
        }
        return null;
    }

    function isFlaggedRemoved(el) {
        return el.classList.contains("nbx-rd-state-remove");
    }

    // Faithful reconstruction of editor.js's buildRackPayload() (see
    // test_editor_e2e.py for detailed commentary -- kept line-for-line
    // aligned with that file's copy).
    function buildRackPayload() {
        var buckets = { front: [], rear: [], other: [] };
        var seenPlacement = {};

        function pushItem(itemEl, faceKey) {
            if (itemEl.getAttribute("data-rd-temp-ghost")) { return; }
            var w = widgetForTile(itemEl);
            if (!w) { return; }
            if (w.kind === "move_out_ghost") { return; }
            if (w.opposite_face) { return; }

            var placementId = (w.placement_id !== undefined) ? w.placement_id : null;
            if (placementId != null) {
                if (seenPlacement[placementId]) { return; }
                seenPlacement[placementId] = true;
            }

            var isAdd = w.kind === "add";
            var item = {
                kind: null,
                device_id: (w.device_id != null) ? w.device_id : null,
                device_type_id: (w.device_type_id != null) ? w.device_type_id : null,
                placement_id: placementId,
                u_position: null,
                face: "",
            };
            if (isAdd) {
                item.proposed_name = (w.proposed_name != null) ? w.proposed_name : "";
            } else if (w.proposed_name) {
                item.proposed_name = w.proposed_name;
            }

            var removed = isFlaggedRemoved(itemEl);
            if (removed && isAdd) {
                item.kind = "add"; item.cancel = true;
                buckets[faceKey].push(item); return;
            }
            if (removed && item.device_id != null) {
                item.kind = "remove";
                buckets[faceKey].push(item); return;
            }
            if (faceKey === "other") {
                item.kind = isAdd ? "add" : "move";
                buckets.other.push(item); return;
            }

            var node = itemEl.gridstackNode;
            var gsY = (node && node.y != null) ? node.y : parseInt(itemEl.getAttribute("gs-y"), 10);
            var gsH = (node && node.h != null) ? node.h : parseInt(itemEl.getAttribute("gs-h"), 10);
            item.u_position = gsYToUPosition(gsY, gsH);
            item.face = faceKey;
            item.kind = isAdd ? "add" : "existing";
            buckets[faceKey].push(item);
        }

        function walkGrid(grid, faceKey) {
            if (!grid) { return; }
            grid.getGridItems().forEach(function (itemEl) { pushItem(itemEl, faceKey); });
        }

        walkGrid(frontGrid(), "front");
        walkGrid(rearGrid(), "rear");
        walkGrid(trayGrid(), "other");

        return { rack_id: rackId, front: buckets.front, rear: buckets.rear, other: buckets.other };
    }

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

    // Drive a palette-style drop by invoking the SAME GridStack `dropped`
    // handler a real HTML5 drop fires. FIX vs. the earlier discovery-era
    // shim: this stamps `data-is-full-depth` on the synthesised clone before
    // dropping it -- a real palette <li> always carries this attribute
    // (editor.js:1940), and onPaletteDrop reads it (editor.js:1572) to seed
    // the new widget's `is_full_depth`, which is what makes the
    // opposite-face shadow hatch get derived for a full-depth add at all.
    // Without it every add silently behaved as non-full-depth.
    function dropPaletteItem(dtId, uHeight, label, isFullDepth) {
        var grid = frontGrid();
        var clone = document.createElement("div");
        clone.className = "grid-stack-item nbx-rd-palette-item";
        clone.setAttribute("data-device-type-id", String(dtId));
        clone.setAttribute("data-u-height", String(uHeight));
        clone.setAttribute("data-label", label);
        clone.setAttribute("data-is-full-depth", isFullDepth ? "true" : "false");
        // Stash copies our reconstruction can still read after onPaletteDrop
        // strips the palette attrs from the live tile.
        clone.setAttribute("data-rd-test-dtid", String(dtId));
        clone.setAttribute("data-rd-test-uheight", String(uHeight));
        var content = document.createElement("div");
        content.className = "grid-stack-item-content";
        clone.appendChild(content);

        var gsH = Math.max(1, Math.round(uHeight * 2));
        var added = grid.addWidget(clone, { x: 0, y: 0, w: 1, h: gsH });
        var el = added || clone;
        var newNode = el.gridstackNode || { el: el };
        fireDropped(grid, newNode);
        return el.getAttribute("data-widget-index");
    }

    // Low-level, collision-engine-BYPASSING position write (see
    // test_editor_sweep.py for the full rationale): the public
    // `grid.update()` recurses into a call-stack overflow in this vendored
    // GridStack build when the destination cell is blocked by a
    // frozen/locked neighbour (every other tile is frozen for the duration
    // of a "drag" -- see freezeAllTiles/onDragStart). A real mouse drag
    // never routes through `update()` mid-drag so never hits this. Safe for
    // our purposes because editor.js's own overlap detection
    // (`tileOverlapsOther`) does its own independent manual scan over live
    // grid items -- it does not rely on GridStack's engine to have resolved
    // collisions, so the reject/snap-back path still runs correctly.
    function fastSetY(grid, el, newGsY) {
        var node = el.gridstackNode;
        node.x = 0;
        node.y = newGsY;
        // These grids run GridStack in `float:true` mode, which tracks each
        // node's `_orig` (x/y) snapshot and silently "packs" it back toward
        // `_orig` the next time ANY repack pass runs (e.g. triggered by
        // editor.js's own `thaw()` calling `grid.update()` on an unrelated,
        // previously-frozen tile). Sync `_orig` to the position we just
        // wrote so a later repack is a no-op (see test_editor_sweep.py for
        // the full incident this fixes: a cross-face move landed at the
        // right y synchronously, then silently snapped back once thaw()'s
        // repack ran moments later).
        node._orig = { x: node.x, y: node.y };
        grid._writePosAttr(el, node);
    }

    // Deterministic SAME-GRID move (see test_editor_sweep.py for detailed
    // commentary; identical primitive).
    function moveTile(idx, newGsY) {
        var el = tileEl(idx);
        if (!el) { return false; }
        var grid = el.gridstackNode.grid;
        fireHandler(grid, "dragstart", el);
        fastSetY(grid, el, newGsY);
        fireHandler(grid, "change", []);
        fireHandler(grid, "dragstop", el);
        return true;
    }

    // Deterministic CROSS-FACE move mirroring editor.js's own `homeInto()`
    // primitive + the real cross-grid `dropped` sequence (see
    // test_editor_sweep.py for the detailed step-by-step rationale,
    // including why the tile is adopted at a known-free row-0 slot first).
    function moveTileToFace(idx, face, newGsY) {
        var el = tileEl(idx);
        if (!el) { return false; }
        var srcGrid = el.gridstackNode.grid;
        fireHandler(srcGrid, "dragstart", el);
        var destGrid = gridFor(face);
        var destHost = hostFor(face);
        srcGrid.removeWidget(el, false);
        if (el.parentNode !== destHost) { destHost.appendChild(el); }
        el.setAttribute("gs-x", "0");
        el.setAttribute("gs-y", "0");
        destGrid.makeWidget(el);
        fastSetY(destGrid, el, newGsY);
        var newNode = el.gridstackNode || { el: el };
        fireDropped(destGrid, newNode);
        return true;
    }

    function faceOf(el) {
        if (el.closest("#nbx-rd-grid-front-" + RACK_PK)) { return "front"; }
        if (el.closest("#nbx-rd-grid-rear-" + RACK_PK)) { return "rear"; }
        return "other";
    }

    function isLiveTile(el) {
        if (el.getAttribute("data-rd-derived-opp")) { return false; }
        if (el.classList.contains("nbx-rd-state-move_out_ghost")) { return false; }
        if (el.hasAttribute("data-rd-temp-ghost")) { return false; }
        return true;
    }

    function tileInfo(idx) {
        var el = tileEl(idx);
        if (!el) { return null; }
        var n = el.gridstackNode;
        return {
            classes: Array.prototype.slice.call(el.classList),
            y: n ? n.y : null, h: n ? n.h : null, face: faceOf(el),
        };
    }

    // Full invariant snapshot for the add tile at `idx`, matched
    // additionally by `label` for the opposite-face hatch lookup.
    function snapshot(idx, label) {
        var tiles = Array.prototype.slice.call(
            root.querySelectorAll('.grid-stack-item[data-widget-index="' + idx + '"]')
        ).filter(isLiveTile);
        var tile = tiles[0] || null;

        var dis = 0, tot = 0, disIdx = [];
        root.querySelectorAll(".grid-stack-item").forEach(function (el) {
            if (el.getAttribute("data-rd-derived-opp")) { return; }
            if (el.classList.contains("nbx-rd-state-move_out_ghost")) { return; }
            if (el.classList.contains("nbx-rd-state-remove")) { return; }
            tot++;
            if (el.classList.contains("ui-draggable-disabled")) {
                dis++; disIdx.push(el.getAttribute("data-widget-index"));
            }
        });

        var out = {
            existsCount: tiles.length, frozen: dis, frozenTotal: tot, frozenIdx: disIdx,
            face: null, y: null, h: null, isAdd: null, overlap: [],
            hatchLive: [], hatchGhost: [],
        };
        if (tile) {
            out.face = faceOf(tile);
            out.isAdd = tile.classList.contains("nbx-rd-state-add");
            var n = tile.gridstackNode;
            out.y = n ? n.y : null;
            out.h = n ? n.h : null;
            var grid = gridFor(out.face);
            if (grid && n) {
                grid.getGridItems().forEach(function (el) {
                    if (el === tile) { return; }
                    if (el.getAttribute("data-rd-derived-opp")) { return; }
                    if (el.classList.contains("nbx-rd-state-move_out_ghost")) { return; }
                    if (el.hasAttribute("data-rd-temp-ghost")) { return; }
                    var on = el.gridstackNode;
                    if (!on) { return; }
                    if (n.y < on.y + on.h && on.y < n.y + n.h) {
                        out.overlap.push(el.getAttribute("data-widget-index"));
                    }
                });
            }
        }
        root.querySelectorAll("[data-rd-derived-opp]").forEach(function (el) {
            var l = (el.querySelector(".nbx-rd-label") || {}).textContent || null;
            if (l !== label) { return; }
            var n = el.gridstackNode;
            var rec = {
                face: faceOf(el), y: n ? n.y : null,
                classes: Array.prototype.slice.call(el.classList),
            };
            if (el.classList.contains("nbx-rd-opposite-ghost")) { out.hatchGhost.push(rec); }
            else { out.hatchLive.push(rec); }
        });
        return out;
    }

    return {
        baseWidgets: baseWidgets,
        buildRackPayload: buildRackPayload,
        dropPaletteItem: dropPaletteItem,
        moveTile: moveTile,
        moveTileToFace: moveTileToFace,
        tileInfo: tileInfo,
        snapshot: snapshot,
    };
})();
"""


@unittest.skipUnless(_PREREQ_OK, f"editor add-sweep prerequisites not met: {_PREREQ_REASON}")
class EditorAddSweepTestCase(unittest.TestCase):
    """Drops a device from the palette then sweeps the added tile across
    every 0.5U row of both rack faces via the deterministic ``__rdAdd``
    shim, once for a full-depth type and once for a plain type."""

    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Fixture: our OWN manufacturer/role/site/device-types + an otherwise
    # EMPTY fresh rack. No pre-placed devices are needed: the add-sweep's own
    # dropped tile is the only content, so it never has anything else to
    # collide with (the "no overlap" invariant still runs -- it is a
    # regression guard even when trivially satisfied).
    # ------------------------------------------------------------------
    @classmethod
    def _provision_fixture(cls):
        suffix = uuid.uuid4().hex[:8]
        mfr = cls._api("POST", "/api/dcim/manufacturers/", {
            "name": f"E2E AddSweep Mfr {suffix}", "slug": f"e2e-addsweep-mfr-{suffix}"})
        role = cls._api("POST", "/api/dcim/device-roles/", {
            "name": f"E2E AddSweep Role {suffix}", "slug": f"e2e-addsweep-role-{suffix}",
            "color": "9e9e9e"})
        site = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E AddSweep Site {suffix}", "slug": f"e2e-addsweep-site-{suffix}",
            "status": "active"})
        dt_half1 = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-AddSweep-1U-Half-{suffix}",
            "slug": f"e2e-addsweep-1u-half-{suffix}", "u_height": 1,
            "is_full_depth": False})
        dt_full3 = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-AddSweep-3U-Full-{suffix}",
            "slug": f"e2e-addsweep-3u-full-{suffix}", "u_height": 3,
            "is_full_depth": True})
        rack = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E AddSweep Rack {suffix}", "site": site["id"],
            "status": "active", "u_height": RACK_U_HEIGHT})

        cls._created = dict(
            manufacturer=mfr["id"], role=role["id"], site=site["id"], rack=rack["id"],
            device_types=[dt_half1["id"], dt_full3["id"]],
        )
        cls._rack_id = rack["id"]
        cls._rack_u_height = RACK_U_HEIGHT
        cls._dt_half1 = dt_half1
        cls._dt_full3 = dt_full3

        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"addsweep-{suffix}", "site": site["id"], "racks": [rack["id"]]})
        cls._design_id = design["id"]
        cls.editor_url = (
            f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/{rack['id']}/")

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
                for tid in created.get("device_types", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/device-types/{tid}/")
                    except Exception:
                        pass
                if created.get("rack") is not None:
                    try:
                        cls._api("DELETE", f"/api/dcim/racks/{created['rack']}/")
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
        cls._api_ctx = cls._browser.new_context(viewport={"width": 1600, "height": 1400})
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
            cls.HARNESS_JS = HARNESS_JS_TEMPLATE % {"rack_pk": cls._rack_id}
        except BaseException:
            cls._cleanup_class()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_class()

    # ------------------------------------------------------------------
    def _load_editor(self):
        self.ctx = self._browser.new_context(
            storage_state=self._storage, viewport={"width": 1600, "height": 1400})
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on(
            "console",
            lambda m: self.errors.append(f"{m.type}: {m.text}")
            if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"PAGEERROR: {e}"))
        resp = self.page.goto(self.editor_url, wait_until="networkidle")
        self.assertIsNotNone(resp, "no response loading the editor URL")
        self.assertEqual(resp.status, 200, f"editor URL returned {resp.status}")
        self.page.wait_for_selector("#rd-editor", timeout=15000)
        self.page.wait_for_timeout(1000)  # let GridStack finish init
        self.page.add_script_tag(content=self.HARNESS_JS)

    def tearDown(self):
        if getattr(self, "ctx", None):
            self.ctx.close()

    # ------------------------------------------------------------------
    # Per-step invariant check, shared by both sweeps below.
    # ------------------------------------------------------------------
    def _check(self, idx, label, full_depth, face_hint, row, phase, violations):
        if self.errors:
            for e in self.errors:
                violations.append(dict(
                    phase=phase, face=face_hint, row=row,
                    kind="page_error", detail=e))
            self.errors = []

        snap = self.page.evaluate(
            f"() => window.__rdAdd.snapshot('{idx}', {json.dumps(label)})")

        if snap["existsCount"] != 1:
            violations.append(dict(
                phase=phase, face=face_hint, row=row, kind="tile_count",
                detail=f"expected 1 live tile, found {snap['existsCount']}"))
        if snap["frozen"] > 0:
            violations.append(dict(
                phase=phase, face=face_hint, row=row, kind="lockup",
                detail=f"{snap['frozen']}/{snap['frozenTotal']} tiles left "
                       f"ui-draggable-disabled: {snap['frozenIdx']}"))
        if snap["overlap"]:
            violations.append(dict(
                phase=phase, face=face_hint, row=row, kind="overlap",
                detail=f"add tile overlaps real tile(s): {snap['overlap']}"))
        if snap.get("isAdd") is False:
            violations.append(dict(
                phase=phase, face=face_hint, row=row, kind="not_add",
                detail="tile lost its nbx-rd-state-add styling mid-sweep"))
        if full_depth:
            if snap["face"] is not None:
                opp = "rear" if snap["face"] == "front" else "front"
                live = snap["hatchLive"]
                if len(live) != 1:
                    violations.append(dict(
                        phase=phase, face=face_hint, row=row, kind="hatch_count",
                        detail=f"expected 1 live opposite-face shadow, found "
                               f"{len(live)}: {live}"))
                else:
                    h0 = live[0]
                    if h0["face"] != opp:
                        violations.append(dict(
                            phase=phase, face=face_hint, row=row, kind="hatch_face",
                            detail=f"live shadow on {h0['face']}, expected {opp}: {h0}"))
                    elif h0["y"] != snap["y"]:
                        violations.append(dict(
                            phase=phase, face=face_hint, row=row, kind="hatch_y",
                            detail=f"live shadow y={h0['y']} != tile y={snap['y']}: {h0}"))
                    # Phase 3 (spec §3, §7 goal 3): an add's owned shadow must
                    # carry the "add" state class throughout the sweep (green-
                    # tinted, per rack_design.css's add accent) -- tightens the
                    # position-only check above to position+class.
                    elif "nbx-rd-state-add" not in h0["classes"]:
                        violations.append(dict(
                            phase=phase, face=face_hint, row=row, kind="hatch_state_class",
                            detail=f"live shadow missing 'nbx-rd-state-add' "
                                   f"class: {h0['classes']}"))
        else:
            if snap["hatchLive"] or snap["hatchGhost"]:
                violations.append(dict(
                    phase=phase, face=face_hint, row=row, kind="unexpected_hatch",
                    detail=f"non-full-depth add should have no opposite-face "
                           f"shadow, found live={snap['hatchLive']} "
                           f"ghost={snap['hatchGhost']}"))

        # Phase 1 read-model cross-check (docs/editor-behavior-spec.md §2/§6):
        # independently rebuild the model from the settled DOM and assert its
        # own I1/I2 invariants agree with the hand-rolled checks above.
        model_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : ['window.__rdModel missing'])")
        for mv in model_violations:
            violations.append(dict(
                phase=phase, face=face_hint, row=row, kind="rd_model", detail=mv))
        return snap

    def _assert_clean(self, violations):
        by_kind = {}
        for v in violations:
            by_kind.setdefault(v["kind"], []).append(v)
        lines = [f"{len(violations)} invariant violation(s); by kind: "
                 f"{ {k: len(vs) for k, vs in by_kind.items()} }"]
        for kind, vs in by_kind.items():
            ex = vs[0]
            lines.append(f"  [{kind}] x{len(vs)} — e.g. {ex}")
        self.assertEqual(violations, [], "\n" + "\n".join(lines))

    # ------------------------------------------------------------------
    # Shared sweep routine: drop `dt` from the palette (flagging
    # `is_full_depth`), assert the drop's precondition state, then sweep the
    # added tile across every 0.5U row of front, hop to rear, sweep rear,
    # hop back to front -- checking invariants at every step -- and finally
    # re-assert the tile is still a well-formed unsaved add.
    # ------------------------------------------------------------------
    def _add_and_sweep(self, dt, label, is_full_depth):
        self._load_editor()
        u_height = float(dt["u_height"])
        idx = self.page.evaluate(
            f"() => window.__rdAdd.dropPaletteItem("
            f"{dt['id']}, {u_height}, {json.dumps(label)}, "
            f"{'true' if is_full_depth else 'false'})")
        self.assertIsNotNone(idx, "dropPaletteItem did not stamp a widget index")

        info = self.page.evaluate(f"() => window.__rdAdd.tileInfo('{idx}')")
        self.assertIn(
            "nbx-rd-state-add", info["classes"],
            f"dropped tile not styled as add: {info['classes']}")

        pl = self.page.evaluate("() => window.__rdAdd.buildRackPayload()")
        adds = [it for it in pl["front"]
                if it["kind"] == "add" and it["placement_id"] is None]
        self.assertEqual(len(adds), 1, f"expected exactly one unsaved add: {adds}")
        self.assertIsNone(adds[0].get("device_id"), f"add has a device_id: {adds[0]}")
        self.assertEqual(adds[0]["device_type_id"], dt["id"])

        h_gs = round(u_height * 2)
        max_row = self._rack_u_height * 2 - h_gs
        self.assertGreaterEqual(max_row, 0, f"device type too tall for rack: {dt}")
        violations = []
        steps = 0

        for row in range(0, max_row + 1):
            self.page.evaluate(f"() => window.__rdAdd.moveTile('{idx}', {row})")
            self.page.wait_for_timeout(STEP_SETTLE_MS)
            self._check(idx, label, is_full_depth, "front", row, "front_sweep",
                        violations)
            steps += 1

        self.page.evaluate(
            f"() => window.__rdAdd.moveTileToFace('{idx}', 'rear', {max_row})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self._check(idx, label, is_full_depth, "rear", max_row, "cross_to_rear",
                    violations)
        steps += 1

        for row in range(0, max_row + 1):
            self.page.evaluate(f"() => window.__rdAdd.moveTile('{idx}', {row})")
            self.page.wait_for_timeout(STEP_SETTLE_MS)
            self._check(idx, label, is_full_depth, "rear", row, "rear_sweep",
                        violations)
            steps += 1

        self.page.evaluate(
            f"() => window.__rdAdd.moveTileToFace('{idx}', 'front', 0)")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self._check(idx, label, is_full_depth, "front", 0, "cross_back", violations)
        steps += 1

        # Final integrity check: still exactly one unsaved add, kind
        # unchanged, device_id/placement_id still null.
        info2 = self.page.evaluate(f"() => window.__rdAdd.tileInfo('{idx}')")
        if "nbx-rd-state-add" not in info2["classes"]:
            violations.append(dict(
                phase="final", face="front", row=0, kind="not_add",
                detail=f"tile lost nbx-rd-state-add after full sweep: "
                       f"{info2['classes']}"))
        pl2 = self.page.evaluate("() => window.__rdAdd.buildRackPayload()")
        all_items2 = pl2["front"] + pl2["rear"] + pl2["other"]
        adds2 = [it for it in all_items2
                 if it["kind"] == "add" and it.get("placement_id") is None]
        if len(adds2) != 1 or adds2[0].get("device_id") is not None:
            violations.append(dict(
                phase="final", face="front", row=0, kind="payload",
                detail=f"expected exactly one add with null placement_id/"
                       f"device_id at end of sweep, got {adds2}"))

        print(f"\n=== ADD-SWEEP SUMMARY ({label}) ===")
        print(f"  device_type={dt.get('model')!r} u_height={u_height} "
              f"is_full_depth={is_full_depth}")
        print(f"  total steps: {steps}")
        print(f"  total violations: {len(violations)}")
        self._assert_clean(violations)

    # =====================================================================
    # Full-depth add: guards Bug A (full-depth adds must get an
    # opposite-face shadow that follows the tile at every position).
    # =====================================================================
    def test_add_and_sweep_full_depth(self):
        self._add_and_sweep(self._dt_full3, "addsweep-full-depth", True)

    # =====================================================================
    # Plain (non-full-depth) add: base add integrity + confirms no spurious
    # opposite-face shadow is ever derived for it.
    # =====================================================================
    def test_add_and_sweep_one_u(self):
        self._add_and_sweep(self._dt_half1, "addsweep-one-u", False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
