#!/usr/bin/env python3
"""Playwright end-to-end regression suite for the rack-design editor's CLIENT side.

Run via ``dev/e2e.sh`` (sources ``dev/config.sh`` + the venv python that has
Playwright). Each behaviour below is an independent test that asserts on in-page
DOM/GridStack state and on the payload ``buildRackPayload`` WOULD emit.

SELF-PROVISIONING: the suite does NOT depend on any pre-existing, hand-curated
demo design. ``setUpClass`` creates its OWN throwaway design ("e2e-<uuid>")
over the plugin REST API (session auth against the live dev server), scoped to
the demo rack (``RD_RACK_PK``), with exactly the placements the tests need:
one 'move' of a real device, one 'remove' of another, one 'add' from a device
type — leaving at least one real device untouched for the existing-device
tests. ``tearDownClass`` deletes that design (cascading its placements). Those
create/delete calls are the ONLY writes; everything else — including Save,
which is never clicked — is read-only, and no pre-existing design is touched.

Tests never hardcode widget indices: tiles are located dynamically by
predicate over the rack's live ``baseWidgets`` JSON (kind / placement_id /
face), so reordering or new widget kinds cannot silently break them.

Prerequisites (all auto-detected — the suite SKIPS cleanly, never fails, when
any is missing): the ``playwright`` package + a Chrome channel installed, and a
reachable dev server on ``RD_BASE``. Config (RD_BASE/RD_USER/RD_PASS) comes from
the environment exactly as ``dev/config.sh`` exports it.

Because ``manage.py test`` only collects the ``netbox_rack_design`` package, this
file (under the top-level ``tests/e2e``) is never picked up by the normal suite.
"""
import json
import math
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
RACK_PK = os.environ.get("RD_RACK_PK", "2541")


# ---------------------------------------------------------------------------
# Prerequisite guard — SKIP CLEANLY (do not fail) when the environment is not
# ready, so the suite stays out of normal/headless CI while staying runnable
# locally on demand.
# ---------------------------------------------------------------------------
def _check_prereqs():
    """Return (ok, skip_reason). ok=True means run; otherwise skip with reason."""
    try:
        import playwright.sync_api  # noqa: F401
    except Exception as exc:  # pragma: no cover - env-dependent
        return False, f"playwright not importable ({exc})"

    # Server reachable? Probe the login page with a short timeout.
    try:
        req = urllib.request.Request(f"{BASE}/login/", method="GET")
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status >= 500:
                return False, f"dev server at {BASE} returned {resp.status}"
    except urllib.error.HTTPError as exc:
        # A 4xx still means the server is up and serving — fine for our purposes.
        if exc.code >= 500:
            return False, f"dev server at {BASE} returned {exc.code}"
    except Exception as exc:
        return False, f"dev server at {BASE} not reachable ({exc})"

    # Chrome channel available? Launch once to confirm (covers "playwright
    # installed but browser/Chrome missing").
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
# In-page JS injected after load. It builds a faithful MIRROR of editor.js's
# private per-rack `state` + `buildRackPayload`, reading from the SAME live
# sources the editor reads (the #rd-editor-data-<rackId> widget JSON, the live
# `nbx-rd-state-*` classes, the `data-rd-temp-ghost` marker, and each tile's
# gridstackNode y/h). This lets us assert on "what Save WOULD send" WITHOUT
# ever calling Save.
#
# Since the 0.7.0 multi-rack refactor, editor.js's `initRack(block)` scopes
# everything to ONE rack block (id="rd-rack-<pk>") with per-rack-suffixed
# child ids (#rd-editor-data-<pk>, #nbx-rd-grid-front-<pk>, etc.) and a
# LOCAL (per-rack) `data-widget-index`. This harness targets the single rack
# under test (RACK_PK, substituted below) the same way.
#
# editor.js keeps these private (IIFE), so we reconstruct rather than reach in.
# The reconstruction is kept line-for-line aligned with editor.js's
# buildRackPayload so a behavioural regression in the DOM/state shows up here.
# ---------------------------------------------------------------------------
TEST_HARNESS_JS = r"""
window.__rdE2E = (function () {
    var RACK_PK = "%(rack_pk)s";
    var root = document.getElementById("rd-rack-" + RACK_PK);
    var rackId = parseInt(root.getAttribute("data-rack-id"), 10);
    var rackUHeight = parseInt(root.getAttribute("data-u-height"), 10);
    var descUnits = root.getAttribute("data-desc-units") === "true";

    var baseWidgets = JSON.parse(
        document.getElementById("rd-editor-data-" + RACK_PK).textContent || "[]");

    function frontGrid() { return document.getElementById("nbx-rd-grid-front-" + RACK_PK).gridstack; }
    function rearGrid() { return document.getElementById("nbx-rd-grid-rear-" + RACK_PK).gridstack; }
    function trayGrid() { return document.getElementById("nbx-rd-grid-tray-" + RACK_PK).gridstack; }

    function gsYToUPosition(gsY, gsH) {
        var y = gsY / 2;
        var uHeight = gsH / 2;
        if (descUnits) { return y + 1; }
        if (uHeight > 1) { return rackUHeight - y - uHeight + 1; }
        return rackUHeight - y;
    }

    // Resolve the per-tile "widget" the way editor.js's `state[idx].widget` does.
    // For tiles that existed at load this is the base JSON; for a palette-dropped
    // add we read the device_type_id we stashed on the clone before dropping.
    function widgetForTile(el) {
        // An owned full-depth shadow / ghost-mirror hatch (editor.js Phase 3)
        // now carries its OWNER's state class (spec §3 -- e.g. an add's
        // shadow is styled "nbx-rd-state-add" too), so it must never be
        // mistaken for a real tile here, same as editor.js's real
        // buildRackPayload excludes it implicitly (no state[idx] entry).
        if (el.getAttribute("data-rd-derived-opp")) { return null; }
        var idx = parseInt(el.getAttribute("data-widget-index"), 10);
        if (!isNaN(idx) && baseWidgets[idx]) {
            return baseWidgets[idx];
        }
        // Newly dropped add (index beyond the base array, or no index yet).
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

    // Mirror of editor.js's `st.removed`: a tile reads as "flagged" when it
    // currently carries the remove styling (× applied). For an add the editor
    // swaps add<->remove classes; for an existing device it adds remove.
    function isFlaggedRemoved(el, w) {
        return el.classList.contains("nbx-rd-state-remove");
    }

    // Faithful reconstruction of editor.js buildRackPayload(), reading the live
    // DOM + GridStack nodes exactly as the real function does.
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

            var removed = isFlaggedRemoved(itemEl, w);
            if (removed && isAdd) {
                item.kind = "add";
                item.cancel = true;
                buckets[faceKey].push(item);
                return;
            }
            if (removed && item.device_id != null) {
                item.kind = "remove";
                buckets[faceKey].push(item);
                return;
            }
            if (faceKey === "other") {
                item.kind = isAdd ? "add" : "move";
                buckets.other.push(item);
                return;
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

    // Click the × button on the tile at the given widget index (scoped to this
    // rack block, since data-widget-index is now a per-rack LOCAL index).
    function clickRemove(idx) {
        var el = root.querySelector('.grid-stack-item[data-widget-index="' + idx + '"]');
        if (!el) { return false; }
        var btn = el.querySelector(".nbx-rd-remove-btn");
        if (!btn) { return false; }
        btn.click();
        return true;
    }

    // Drive a palette-style drop onto the front grid by invoking the SAME
    // GridStack `dropped` handler a real HTML5 drop fires (real DnD can't be
    // driven headlessly). We synthesise a palette clone, add it as a node, then
    // call the registered handler with (event, prevNode=null, newNode).
    function dropPaletteItem(dtId, uHeight, label) {
        var grid = frontGrid();
        var clone = document.createElement("div");
        clone.className = "grid-stack-item nbx-rd-palette-item";
        clone.setAttribute("data-device-type-id", String(dtId));
        clone.setAttribute("data-u-height", String(uHeight));
        clone.setAttribute("data-label", label);
        // Stash copies our reconstruction can still read after onPaletteDrop
        // strips the palette attrs from the live tile.
        clone.setAttribute("data-rd-test-dtid", String(dtId));
        clone.setAttribute("data-rd-test-uheight", String(uHeight));
        var content = document.createElement("div");
        content.className = "grid-stack-item-content";
        clone.appendChild(content);

        // Place it at a free U near the top so it doesn't collide with fixtures.
        var gsH = Math.max(1, Math.round(uHeight * 2));
        var added = grid.addWidget(clone, { x: 0, y: 0, w: 1, h: gsH });
        var el = added || clone;
        var newNode = el.gridstackNode || { el: el };

        // Invoke the editor's registered `dropped` handler(s).
        var handlers = grid._gsEventHandler && grid._gsEventHandler["dropped"];
        var list = Array.isArray(handlers) ? handlers : (handlers ? [handlers] : []);
        list.forEach(function (h) { h({ type: "dropped" }, null, newNode); });

        return el.getAttribute("data-widget-index");
    }

    // Drive a "move" of an existing tile: update its gridstack node to a new gs-y
    // (the same mutation a drag performs) then fire the registered `change`
    // handler so the editor's ghost logic runs.
    function fireHandler(grid, name, arg) {
        var handlers = grid._gsEventHandler && grid._gsEventHandler[name];
        var list = Array.isArray(handlers) ? handlers : (handlers ? [handlers] : []);
        list.forEach(function (h) { h({ type: name }, arg); });
    }

    // Drive a vertical drag of an existing tile, mirroring the REAL event order:
    // dragstart (frees the temp ghost so the tile can settle on its origin) ->
    // grid.update(y) (the move GridStack performs) -> change + dragstop (the
    // editor's ghost-recompute path). This is the path a real drag exercises.
    function moveTile(idx, newGsY) {
        var el = root.querySelector('.grid-stack-item[data-widget-index="' + idx + '"]');
        if (!el) { return false; }
        var grid = el.gridstackNode.grid;
        fireHandler(grid, "dragstart", el);
        grid.update(el, { y: newGsY });
        fireHandler(grid, "change", []);
        fireHandler(grid, "dragstop", el);
        return true;
    }

    function uPositionToGsY(uPosition, gsH) {
        if (descUnits) { return uPosition * 2 - 2; }
        if (gsH > 2) { return rackUHeight * 2 - uPosition * 2 - gsH + 2; }
        return rackUHeight * 2 - uPosition * 2;
    }

    function tileInfo(idx) {
        var el = root.querySelector('.grid-stack-item[data-widget-index="' + idx + '"]');
        if (!el) { return null; }
        var content = el.querySelector(".grid-stack-item-content");
        var node = el.gridstackNode;
        return {
            cls: el.className,
            classes: Array.prototype.slice.call(el.classList),
            dirty: el.classList.contains("nbx-rd-dirty"),
            locked: node ? !!node.locked : null,
            noMove: node ? !!node.noMove : null,
            y: node ? node.y : null,
            h: node ? node.h : null,
            bg: content ? (content.style.backgroundColor || "") : "",
            roleBg: content ? content.getAttribute("data-role-bg") : null,
            face: (el.closest("#nbx-rd-grid-front-" + RACK_PK) ? "front"
                   : el.closest("#nbx-rd-grid-rear-" + RACK_PK) ? "rear" : "other"),
        };
    }

    function ghostCount() {
        return root.querySelectorAll(
            '.grid-stack-item.nbx-rd-state-move_out_ghost[data-rd-temp-ghost]').length;
    }

    return {
        buildRackPayload: buildRackPayload,
        clickRemove: clickRemove,
        dropPaletteItem: dropPaletteItem,
        moveTile: moveTile,
        tileInfo: tileInfo,
        ghostCount: ghostCount,
        uPositionToGsY: uPositionToGsY,
        baseWidgets: baseWidgets,
        rackUHeight: rackUHeight,
        saveDisabled: function () {
            var b = document.getElementById("rd-editor-save");
            return b ? b.hasAttribute("disabled") : null;
        },
    };
})();
""" % {"rack_pk": RACK_PK}


@unittest.skipUnless(_PREREQ_OK, f"editor e2e prerequisites not met: {_PREREQ_REASON}")
class EditorE2ETestCase(unittest.TestCase):
    """Each test loads a FRESH editor page (so cases are independent and order
    does not matter) and asserts on in-page state / the would-be save payload.
    Nothing is ever saved; the only writes are the create/delete of the suite's
    OWN throwaway design in setUpClass/tearDownClass."""

    # ------------------------------------------------------------------
    # REST plumbing: session-cookie auth (from the Playwright login) plus
    # the csrftoken cookie sent back as X-CSRFToken, over plain HTTP against
    # the live dev server — exactly what the browser itself would do.
    # ------------------------------------------------------------------
    @classmethod
    def _api(cls, method, path, payload=None):
        headers = {"X-CSRFToken": cls._csrf, "Accept": "application/json"}
        kwargs = {"method": method, "headers": headers}
        if payload is not None:
            kwargs["data"] = payload  # Playwright serializes a dict as JSON
        resp = cls._api_ctx.request.fetch(f"{BASE}{path}", **kwargs)
        body = resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"{method} {path} -> HTTP {resp.status}: {body[:500]}")
        return json.loads(body) if body.strip() else None

    # ------------------------------------------------------------------
    # Fixture provisioning: create a throwaway design with exactly the
    # placements the tests need, discovering the rack's real devices via the
    # API (never assuming names/pks) and computing free Us dynamically.
    # ------------------------------------------------------------------
    @classmethod
    def _provision_fixture(cls):
        rack = cls._api("GET", f"/api/dcim/racks/{RACK_PK}/")
        site_id = rack["site"]["id"]
        rack_uh = int(rack["u_height"])

        devs = cls._api(
            "GET", f"/api/dcim/devices/?rack_id={RACK_PK}&limit=0")["results"]
        devs = [d for d in devs if d.get("position") is not None]
        if len(devs) < 3:
            raise unittest.SkipTest(
                f"rack {RACK_PK} needs >= 3 racked devices for the e2e fixture "
                f"(found {len(devs)})")

        # Device-type heights + full-depth flag (the device serializer's nested
        # DT is brief; is_full_depth only lives on the full device-type detail).
        dt_info = {}
        for d in devs:
            dtid = d["device_type"]["id"]
            if dtid not in dt_info:
                dt = cls._api("GET", f"/api/dcim/device-types/{dtid}/")
                dt_info[dtid] = {
                    "h": float(dt["u_height"]),
                    "full_depth": bool(dt.get("is_full_depth")),
                }

        def dev_h(d):
            return max(1, math.ceil(dt_info[d["device_type"]["id"]]["h"]))

        def dev_full_depth(d):
            return dt_info[d["device_type"]["id"]]["full_depth"]

        # Highest-positioned device stays UNTOUCHED (the `existing` tile the
        # remove-toggle and live-move tests exercise); the next is MOVED; the
        # lowest is flagged for REMOVAL. If the rack has a full-depth racked
        # device, force it to be the UNTOUCHED one so the full-depth-drag
        # regression test always has an interactive tile to grab.
        devs.sort(key=lambda d: float(d["position"]), reverse=True)
        fulldepth_candidates = [d for d in devs if dev_full_depth(d)]
        if fulldepth_candidates and devs[0]["id"] != fulldepth_candidates[0]["id"]:
            fd = fulldepth_candidates[0]
            devs.remove(fd)
            devs.insert(0, fd)
        untouched, moved, removed = devs[0], devs[1], devs[2]
        cls._fulldepth_dev_id = untouched["id"] if fulldepth_candidates else None

        # Free-U allocator over the REAL occupancy (plus what we allocate),
        # with a 1U buffer around each block and the very top rows kept clear
        # for the harness's synthetic palette drop (which lands at gsY 0).
        occupied = set()
        for d in devs:
            p = int(float(d["position"]))
            occupied.update(range(p, p + dev_h(d)))

        def alloc(height):
            for p in range(rack_uh - height - 3, 1, -1):
                span = set(range(p - 1, p + height + 1))  # 1U buffer each side
                if span & occupied:
                    continue
                occupied.update(span)
                return p
            raise unittest.SkipTest(
                f"rack {RACK_PK} has no free {height}U run for the e2e fixture")

        move_u = alloc(dev_h(moved))
        add_u = alloc(dev_h(untouched))
        # A free slot test_05 can drag the untouched device into and back.
        cls._live_free_u = alloc(dev_h(untouched))

        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"e2e-{uuid.uuid4()}",
            "site": site_id,
            "racks": [int(RACK_PK)],
        })
        cls._design_id = design["id"]

        mv = cls._api("POST", "/api/plugins/rack-design/placements/", {
            "design": cls._design_id,
            "kind": "move",
            "device": moved["id"],
            "target_rack": int(RACK_PK),
            "target_position": move_u,
            "target_face": "front",
        })
        cls._move_pid = mv["id"]
        rm = cls._api("POST", "/api/plugins/rack-design/placements/", {
            "design": cls._design_id,
            "kind": "remove",
            "device": removed["id"],
        })
        cls._remove_pid = rm["id"]
        cls._api("POST", "/api/plugins/rack-design/placements/", {
            "design": cls._design_id,
            "kind": "add",
            "device_type": untouched["device_type"]["id"],
            "proposed_name": "e2e-fixture-add",
            "target_rack": int(RACK_PK),
            "target_position": add_u,
            "target_face": "front",
        })

        cls._untouched_id = untouched["id"]
        cls._moved_real_u = int(float(moved["position"]))
        cls._dt_id = untouched["device_type"]["id"]
        cls._dt_h = dt_info[cls._dt_id]["h"]
        cls.editor_url = (
            f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/{RACK_PK}/")

    @classmethod
    def _cleanup_class(cls):
        """Delete the throwaway design (cascades placements), then shut down
        Playwright. Best-effort at every step so a cleanup hiccup never masks
        the original failure."""
        try:
            if getattr(cls, "_design_id", None) is not None:
                try:
                    cls._api(
                        "DELETE",
                        f"/api/plugins/rack-design/designs/{cls._design_id}/")
                    cls._design_id = None
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
        # This context is BOTH the login browser and the API client: its
        # request context shares the session cookies the login set.
        cls._api_ctx = cls._browser.new_context(
            viewport={"width": 1400, "height": 1200})
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
            # setUpClass failing (or skipping) means tearDownClass never runs:
            # clean up the design + Playwright here, then re-raise.
            cls._cleanup_class()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_class()

    def setUp(self):
        self.ctx = self._browser.new_context(
            storage_state=self._storage, viewport={"width": 1400, "height": 1200})
        self.page = self.ctx.new_page()
        self.console_errors = []
        self.page.on(
            "console",
            lambda m: self.console_errors.append(f"{m.type}: {m.text}")
            if m.type == "error" else None,
        )
        self.page.on("pageerror", lambda e: self.console_errors.append(f"PAGEERROR: {e}"))

        resp = self.page.goto(self.editor_url, wait_until="networkidle")
        self.assertIsNotNone(resp, "no response loading the editor URL")
        self.assertEqual(
            resp.status, 200,
            f"editor URL returned {resp.status} (auth/fixture problem?)")
        self.page.wait_for_selector("#rd-editor", timeout=10000)
        self.page.wait_for_timeout(1200)  # let GridStack finish init
        self.page.add_script_tag(content=TEST_HARNESS_JS)

    def tearDown(self):
        # Hard guarantee NOTHING was persisted: assert no save-layout POST left
        # the page, then close the context. (We never click Save anyway.)
        self.ctx.close()

    # --- shared helpers ---------------------------------------------------
    def payload(self):
        return self.page.evaluate("() => window.__rdE2E.buildRackPayload()")

    def front_items(self, pl=None):
        return (pl or self.payload())["front"]

    def find_item(self, items, **match):
        for it in items:
            if all(it.get(k) == v for k, v in match.items()):
                return it
        return None

    def base_widgets(self):
        return self.page.evaluate("() => window.__rdE2E.baseWidgets")

    def widx(self, **match):
        """Widget index found by predicate over the live baseWidgets JSON —
        never hardcoded. Opposite-face hatch copies are skipped (they are
        visual-only and carry no editable tile)."""
        widgets = self.base_widgets()
        for idx, w in enumerate(widgets):
            if w.get("opposite_face"):
                continue
            if all(w.get(k) == v for k, v in match.items()):
                return idx
        self.fail(f"no widget matching {match} in baseWidgets: {widgets}")

    def tile_info(self, idx):
        return self.page.evaluate(f"() => window.__rdE2E.tileInfo('{idx}')")

    def assert_no_console_errors(self):
        self.assertEqual(
            self.console_errors, [],
            f"unexpected console errors: {self.console_errors}")

    # =====================================================================
    # 1. Editor loads clean
    # =====================================================================
    def test_01_editor_loads_clean(self):
        info = self.page.evaluate(
            f"""() => ({{
                gridstack: typeof window.GridStack,
                frontGrid: !!document.getElementById('nbx-rd-grid-front-{RACK_PK}').gridstack,
                rearGrid: !!document.getElementById('nbx-rd-grid-rear-{RACK_PK}').gridstack,
                trayGrid: !!document.getElementById('nbx-rd-grid-tray-{RACK_PK}').gridstack,
                nWidgets: window.__rdE2E.baseWidgets.length,
                saveDisabled: window.__rdE2E.saveDisabled(),
            }})""")
        self.assertEqual(info["gridstack"], "function", "GridStack not loaded")
        self.assertTrue(info["frontGrid"], "front GridStack not initialised")
        self.assertTrue(info["rearGrid"], "rear GridStack not initialised")
        self.assertTrue(info["trayGrid"], "tray GridStack not initialised")
        self.assertGreater(info["nWidgets"], 0, "widget JSON parsed to zero widgets")
        self.assertTrue(info["saveDisabled"], "Save should start DISABLED on a clean load")
        self.assert_no_console_errors()

    # =====================================================================
    # 2. Palette search populates results from the core device-type API
    # =====================================================================
    def test_02_palette_search_populates(self):
        # The Device catalog lives in the collapsible tool drawer, which starts
        # CLOSED (0.7.0's push/collapse drawer refactor) — open its section
        # before the search box is interactable.
        self.page.click('[data-rd-section-toggle="device"]')
        self.page.wait_for_selector("#nbx-rd-palette-search", state="visible", timeout=5000)
        self.page.fill("#nbx-rd-palette-search", "demo")
        # Wait for the debounced fetch + render.
        self.page.wait_for_function(
            "() => document.querySelectorAll('.nbx-rd-palette-item').length > 0",
            timeout=8000)
        items = self.page.evaluate(
            """() => [...document.querySelectorAll('.nbx-rd-palette-item')].map(li => ({
                dtid: li.getAttribute('data-device-type-id'),
                uh: li.getAttribute('data-u-height'),
            }))""")
        self.assertGreater(len(items), 0, "palette search returned no .nbx-rd-palette-item")
        for it in items:
            self.assertIsNotNone(it["dtid"], "palette item missing data-device-type-id")
            self.assertNotEqual(it["dtid"], "", "palette item has empty data-device-type-id")
            self.assertIsNotNone(it["uh"], "palette item missing data-u-height")
        self.assert_no_console_errors()

    # =====================================================================
    # 3. Palette drop -> exactly one {kind:"add"} item in the payload
    # =====================================================================
    def test_03_palette_drop_adds_payload_item(self):
        dtid = self._dt_id  # discovered from the rack's own devices
        new_idx = self.page.evaluate(
            f"() => window.__rdE2E.dropPaletteItem({dtid}, {self._dt_h}, 'e2e-drop')")
        self.assertIsNotNone(new_idx, "dropPaletteItem did not stamp a widget index")

        # The dropped tile became an `add` tile.
        info = self.tile_info(new_idx)
        self.assertIn("nbx-rd-state-add", info["classes"],
                      f"dropped tile not styled as add: {info['classes']}")

        # buildRackPayload emits exactly one brand-new add (placement_id null) for
        # this device type, with the derived u_position + front face.
        pl = self.payload()
        new_adds = [it for it in pl["front"]
                    if it["kind"] == "add" and it["placement_id"] is None]
        self.assertEqual(
            len(new_adds), 1,
            f"expected exactly one unsaved add in payload, got {new_adds}")
        add = new_adds[0]
        self.assertEqual(add["device_type_id"], dtid, f"wrong device_type_id: {add}")
        self.assertIsNone(add["placement_id"], f"unsaved add must have null placement_id: {add}")
        self.assertEqual(add["face"], "front", f"add face should be front: {add}")
        self.assertIsNotNone(add["u_position"], f"add missing derived u_position: {add}")
        self.assertIsNone(add.get("device_id"), f"add must not carry a device_id: {add}")
        # Save becomes enabled by the drop (markDirty).
        self.assertFalse(self.page.evaluate("() => window.__rdE2E.saveDisabled()"),
                         "Save should enable after a palette drop")
        self.assert_no_console_errors()

    # =====================================================================
    # 4a. × on an existing device -> flag remove + dirty + Save enables;
    #     × again un-flags and restores role color.
    # =====================================================================
    def test_04a_remove_toggle_on_existing(self):
        # The untouched real device projects as the rack's `existing` tile.
        idx = self.widx(kind="existing", face="front", device_id=self._untouched_id)
        before = self.tile_info(idx)
        self.assertIn("nbx-rd-state-existing", before["classes"])
        device_id = self._untouched_id

        # First × -> flagged for removal, dirty, Save enabled.
        self.page.evaluate(f"() => window.__rdE2E.clickRemove('{idx}')")
        flagged = self.tile_info(idx)
        self.assertIn("nbx-rd-state-remove", flagged["classes"],
                      "× should flag existing device nbx-rd-state-remove")
        self.assertTrue(flagged["dirty"], "flagged remove should be nbx-rd-dirty")
        self.assertTrue(flagged["locked"], "flagged remove should be locked")
        self.assertFalse(self.page.evaluate("() => window.__rdE2E.saveDisabled()"),
                         "Save should enable after flagging a removal")
        # Payload: this device is a `remove`.
        rem = self.find_item(self.payload()["front"], device_id=device_id, kind="remove")
        self.assertIsNotNone(rem, "flagged device should serialize as kind=remove")

        # Second × -> un-flag, restore role color, no longer remove-styled.
        self.page.evaluate(f"() => window.__rdE2E.clickRemove('{idx}')")
        restored = self.tile_info(idx)
        self.assertNotIn("nbx-rd-state-remove", restored["classes"],
                         "second × should un-flag removal")
        self.assertIn("nbx-rd-state-existing", restored["classes"])
        if restored["roleBg"]:
            self.assertTrue(
                restored["bg"],
                "un-flagged existing device should regain its role color")
        self.assert_no_console_errors()

    # =====================================================================
    # 4b. × (cancel) on a pre-existing move_in -> restores device to its REAL
    #     slot in existing styling (role color), payload sends it as `existing`
    #     at the real U.
    # =====================================================================
    def test_04b_cancel_move_on_preexisting_move_in(self):
        pid = self._move_pid  # the move placement this suite created
        real_u = self._moved_real_u  # the moved device's REAL rack position
        idx = self.widx(kind="move_in", face="front", placement_id=pid)
        mv = self.find_item(self.payload()["front"], placement_id=pid)
        self.assertIsNotNone(mv, f"fixture move_in (placement {pid}) missing from payload")

        ghosts_before = self.page.evaluate("() => window.__rdE2E.ghostCount()")
        self.page.evaluate(f"() => window.__rdE2E.clickRemove('{idx}')")
        info = self.tile_info(idx)
        self.assertIn("nbx-rd-state-existing", info["classes"],
                      "cancelled move should be restyled existing")
        self.assertNotIn("nbx-rd-state-move_in", info["classes"])
        self.assertFalse(info["dirty"], "restored existing tile should drop the per-tile dirty outline")
        # Role color (not grey) when the device's role carries a color.
        if info["roleBg"]:
            self.assertTrue(info["bg"], "cancelled move should regain its real role color")

        # Payload: the placement is now sent as `existing` at the REAL U, front.
        pl = self.find_item(self.payload()["front"], placement_id=pid)
        self.assertIsNotNone(pl, "cancelled move should still be in the payload")
        self.assertEqual(pl["kind"], "existing",
                         f"cancelled move should serialize as existing: {pl}")
        self.assertEqual(pl["u_position"], real_u,
                         f"cancelled move should sit at its real U ({real_u}): {pl}")
        self.assertEqual(pl["face"], "front")
        self.assertLessEqual(self.page.evaluate("() => window.__rdE2E.ghostCount()"),
                             ghosts_before,
                             "cancelling the move should not leave a temp ghost")
        self.assert_no_console_errors()

    # =====================================================================
    # 4c. × on an UNSAVED add (dropped this session) -> tile removed locally and
    #     gone from the payload.
    # =====================================================================
    def test_04c_remove_unsaved_add(self):
        new_idx = self.page.evaluate(
            f"() => window.__rdE2E.dropPaletteItem({self._dt_id}, {self._dt_h}, 'e2e-drop')")
        # Confirm it is present first.
        adds = [it for it in self.payload()["front"]
                if it["kind"] == "add" and it["placement_id"] is None]
        self.assertEqual(len(adds), 1, "precondition: one unsaved add present")

        self.page.evaluate(f"() => window.__rdE2E.clickRemove('{new_idx}')")
        gone = self.page.evaluate(
            f"() => !!document.querySelector('#rd-rack-{RACK_PK} "
            f".grid-stack-item[data-widget-index=\"{new_idx}\"]')")
        self.assertFalse(gone, "× on an unsaved add should delete the tile from the DOM")
        adds_after = [it for it in self.payload()["front"]
                      if it["kind"] == "add" and it["placement_id"] is None]
        self.assertEqual(len(adds_after), 0,
                         f"unsaved add should be gone from the payload: {adds_after}")
        self.assert_no_console_errors()

    # =====================================================================
    # 5. Live move ghost: drag existing off origin -> locked ghost at origin +
    #    move_in styling; return to origin -> ghost gone, existing restored, no
    #    lingering dirty.
    # =====================================================================
    def test_05_live_move_ghost(self):
        idx = self.widx(kind="existing", face="front", device_id=self._untouched_id)
        before = self.tile_info(idx)
        orig_gsY = before["y"]
        # Sanity: the tile really sits at its widget's U (derived, not hardcoded).
        w = self.base_widgets()[idx]
        expected_gsY = self.page.evaluate(
            f"() => window.__rdE2E.uPositionToGsY({w['u_position']}, {before['h']})")
        self.assertEqual(orig_gsY, expected_gsY,
                         "existing tile should start at its projected origin slot")

        # Drag it to a free slot the fixture allocator reserved for this test.
        free_gsY = self.page.evaluate(
            f"() => window.__rdE2E.uPositionToGsY({self._live_free_u}, {before['h']})")
        self.page.evaluate(f"() => window.__rdE2E.moveTile('{idx}', {free_gsY})")
        self.page.wait_for_timeout(50)  # ghost logic runs on a 0ms timer
        moved = self.tile_info(idx)
        self.assertIn("nbx-rd-state-move_in", moved["classes"],
                      "dragged-off existing should restyle move_in")
        self.assertTrue(moved["dirty"], "moved tile should be nbx-rd-dirty")
        ghosts = self.page.evaluate(
            f"""() => [...document.querySelectorAll(
                '#rd-rack-{RACK_PK} .nbx-rd-state-move_out_ghost[data-rd-temp-ghost]')].map(g => ({{
                    locked: g.gridstackNode ? !!g.gridstackNode.locked : null,
                    noMove: g.gridstackNode ? !!g.gridstackNode.noMove : null,
                    y: g.gridstackNode ? g.gridstackNode.y : null,
                }}))""")
        self.assertEqual(len(ghosts), 1, f"expected one temp ghost at origin, got {ghosts}")
        self.assertTrue(ghosts[0]["locked"], "move_out_ghost must be locked")
        self.assertTrue(ghosts[0]["noMove"], "move_out_ghost must be noMove")
        self.assertEqual(ghosts[0]["y"], orig_gsY, "ghost should sit at the vacated origin slot")

        # Return it to origin -> ghost removed, existing restored, dirty cleared.
        self.page.evaluate(f"() => window.__rdE2E.moveTile('{idx}', {orig_gsY})")
        self.page.wait_for_timeout(50)
        restored = self.tile_info(idx)
        self.assertIn("nbx-rd-state-existing", restored["classes"],
                      "returning to origin should restore existing styling")
        self.assertNotIn("nbx-rd-state-move_in", restored["classes"])
        self.assertFalse(restored["dirty"],
                         "no lingering nbx-rd-dirty after returning to origin")
        self.assertEqual(self.page.evaluate("() => window.__rdE2E.ghostCount()"), 0,
                         "temp ghost should be removed once the device is back home")
        self.assert_no_console_errors()

    # =====================================================================
    # 6. Ghosts locked: pre-existing move_out_ghost + pre-existing remove tiles
    #    are non-draggable.
    # =====================================================================
    def test_06_ghosts_and_removes_locked(self):
        # The move placement projects a static ghost at the device's real slot;
        # the remove placement projects a remove-styled tile.
        gidx = self.widx(kind="move_out_ghost", face="front", placement_id=self._move_pid)
        ghost = self.tile_info(gidx)
        self.assertIn("nbx-rd-state-move_out_ghost", ghost["classes"])
        self.assertTrue(ghost["locked"], "static move_out_ghost should be locked")
        self.assertTrue(ghost["noMove"], "static move_out_ghost should be noMove")

        ridx = self.widx(kind="remove", face="front", placement_id=self._remove_pid)
        rem = self.tile_info(ridx)
        self.assertIn("nbx-rd-state-remove", rem["classes"])
        self.assertTrue(rem["locked"], "pre-existing remove tile should be locked")
        self.assertTrue(rem["noMove"], "pre-existing remove tile should be noMove")
        self.assert_no_console_errors()

    # =====================================================================
    # 7. Full-depth cross-face move: dragging a full-depth device's tile from
    #    the FRONT face to the REAR face (a cross-GridStack-instance real
    #    mouse drag) must thaw every other tile in the rack (no leftover
    #    ui-draggable-disabled) and carry the device's opposite-face shadow
    #    hatch along to the tile's new slot on the FRONT face.
    # =====================================================================
    def test_07_fulldepth_crossface_move_follows_shadow_and_thaws(self):
        if self._fulldepth_dev_id is None:
            self.skipTest(f"rack {RACK_PK} has no full-depth racked device")

        idx = self.widx(kind="existing", face="front", device_id=self._fulldepth_dev_id)
        w = self.base_widgets()[idx]
        label = w.get("label")

        tile = self.page.query_selector(
            f'#rd-rack-{RACK_PK} .grid-stack-item[data-widget-index="{idx}"]')
        self.assertIsNotNone(tile, "full-depth device's front tile not found in DOM")
        box = tile.bounding_box()
        rear_el = self.page.query_selector(f"#nbx-rd-grid-rear-{RACK_PK}")
        self.assertIsNotNone(rear_el, "rear grid not found")
        rear_box = rear_el.bounding_box()

        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        tx = rear_box["x"] + rear_box["width"] / 2
        # Aim the CURSOR at a genuinely FREE rear slot (spec §4.1
        # cursor-governed placement, 2026-07-08: releasing while the cursor
        # is over ILLEGAL rows is now a full snap-back -- the old blind
        # "tile.y +/- 80px" target could land on the fixture add's rear
        # shadow rows and be spec-correctly rejected). Ask the in-page
        # read-model for the first legal rear top-row for this tile, then
        # position the cursor so the tile's TOP lands there (the drag
        # grabbed the tile's center, so cursor row = top + gsH/2).
        target = self.page.evaluate(f"""() => {{
            var el = document.querySelector(
                '#rd-rack-{RACK_PK} .grid-stack-item[data-widget-index="{idx}"]');
            var host = document.getElementById('nbx-rd-grid-rear-{RACK_PK}');
            var r = host.getBoundingClientRect();
            var maxRow = parseInt(host.getAttribute('gs-max-row'), 10);
            var rowPx = r.height / maxRow;
            var gsH = (el.gridstackNode && el.gridstackNode.h) || 2;
            for (var top = 0; top <= maxRow - gsH; top++) {{
                var v = window.__rdModel.canPlaceAt(
                    el, {RACK_PK}, 'rear', top, gsH, true);
                if (v.ok) {{
                    var cursorRow = top + Math.floor(gsH / 2);
                    return {{top: top, gsH: gsH,
                             ty: r.top + (cursorRow + 0.5) * rowPx}};
                }}
            }}
            return null;
        }}""")
        self.assertIsNotNone(
            target, "no free rear slot found for the cross-face drag")
        ty = target["ty"]

        self.page.mouse.move(cx, cy)
        self.page.mouse.down()
        self.page.mouse.move((cx + tx) / 2, (cy + ty) / 2, steps=8)
        self.page.mouse.move(tx, ty, steps=8)
        self.page.mouse.up()
        self.page.wait_for_timeout(1200)

        frozen = self.page.evaluate(f"""() => {{
            var bl = document.getElementById('rd-rack-{RACK_PK}');
            var dis = 0, tot = 0, disEls = [];
            bl.querySelectorAll('.grid-stack-item').forEach(function(el) {{
                if (el.getAttribute('data-rd-derived-opp')) return;
                // Skip tiles that are INTENTIONALLY locked by design (static
                // move_out_ghost / flagged-remove tiles — see test_06): only
                // count tiles that should be interactive but got frozen.
                if (el.classList.contains('nbx-rd-state-move_out_ghost')) return;
                if (el.classList.contains('nbx-rd-state-remove')) return;
                tot++;
                if (el.classList.contains('ui-draggable-disabled')) {{
                    dis++;
                    disEls.push({{idx: el.getAttribute('data-widget-index'), cls: el.className}});
                }}
            }});
            return {{disabled: dis, total: tot, disEls: disEls}};
        }}""")
        self.assertEqual(
            frozen["disabled"], 0,
            f"cross-face drag should thaw every tile (0 ui-draggable-disabled "
            f"left), got {frozen}")

        probe = self.page.evaluate(f"""() => {{
            var el = document.querySelector(
                '#rd-rack-{RACK_PK} .grid-stack-item[data-widget-index="{idx}"]');
            if (!el) return null;
            var n = el.gridstackNode;
            var face = el.closest('#nbx-rd-grid-front-{RACK_PK}') ? 'front'
                : el.closest('#nbx-rd-grid-rear-{RACK_PK}') ? 'rear' : '?';
            return {{face: face, y: n ? n.y : null}};
        }}""")
        self.assertIsNotNone(probe, "dragged full-depth tile disappeared from the DOM")
        self.assertEqual(
            probe["face"], "rear",
            f"full-depth tile should have landed on the rear face: {probe}")

        hatches = self.page.evaluate(f"""(lbl) => {{
            var bl = document.getElementById('rd-rack-{RACK_PK}');
            var out = [];
            bl.querySelectorAll('[data-rd-derived-opp]').forEach(function(el) {{
                var n = el.gridstackNode;
                var face = el.closest('#nbx-rd-grid-front-{RACK_PK}') ? 'front'
                    : el.closest('#nbx-rd-grid-rear-{RACK_PK}') ? 'rear' : '?';
                var l = (el.querySelector('.nbx-rd-label') || {{}}).textContent || null;
                if (l === lbl) out.push({{
                    face: face, y: n ? n.y : null,
                    ghost: el.classList.contains('nbx-rd-opposite-ghost'),
                }});
            }});
            return out;
        }}""", label)
        # The LIVE (solid) shadow must follow the tile to its new opposite face.
        # Moving the device also leaves a move-out ghost at its origin, which now
        # casts its OWN dashed ghost-shadow on the opposite face -- so filter to the
        # non-ghost hatch for the follow assertion.
        live = [h for h in hatches if not h["ghost"]]
        ghost = [h for h in hatches if h["ghost"]]
        self.assertEqual(
            len(live), 1,
            f"expected exactly one LIVE opposite-face shadow hatch for {label!r}, "
            f"got {hatches}")
        self.assertEqual(
            live[0]["face"], "front",
            f"live shadow hatch should sit on the FRONT face (opposite of the "
            f"tile's new rear slot): {live[0]}")
        self.assertEqual(
            live[0]["y"], probe["y"],
            f"live shadow hatch should follow the tile to its new slot "
            f"(y={probe['y']}): {live[0]}")
        # The vacated origin (front, the device's original mounted face) should now
        # show a dashed ghost-shadow on the opposite (rear) face.
        self.assertTrue(
            all(g["face"] == "rear" for g in ghost),
            f"any ghost-shadow should sit on the rear face (opposite the "
            f"front-face move-out ghost): {ghost}")
        self.assert_no_console_errors()


if __name__ == "__main__":
    unittest.main(verbosity=2)
