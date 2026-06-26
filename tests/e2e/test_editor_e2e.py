#!/usr/bin/env python3
"""Playwright end-to-end regression suite for the rack-design editor's CLIENT side.

Run via ``dev/e2e.sh`` (sources ``dev/config.sh`` + the venv python that has
Playwright). Each behaviour below is an independent test that asserts on in-page
DOM/GridStack state and on the payload ``buildRackPayload`` WOULD emit.

CRITICAL: this suite is strictly READ-ONLY against the live dev DB. It NEVER
clicks Save / POSTs to save-layout / mutates design 5. The server-side
create/move/remove/cancel persistence is covered by the Python ``test_api.py``
suite; this file covers only the JS/DOM behaviour that real drag-and-drop drives.

Prerequisites (all auto-detected — the suite SKIPS cleanly, never fails, when
any is missing): the ``playwright`` package + a Chrome channel installed, and a
reachable dev server on ``RD_BASE``. Config (RD_BASE/RD_USER/RD_PASS) comes from
the environment exactly as ``dev/config.sh`` exports it.

Because ``manage.py test`` only collects the ``netbox_rack_design`` package, this
file (under the top-level ``tests/e2e``) is never picked up by the normal suite.
"""
import os
import unittest
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Configuration (matches dev/config.sh)
# ---------------------------------------------------------------------------
BASE = os.environ.get("RD_BASE", "http://127.0.0.1:8000").rstrip("/")
USER = os.environ.get("RD_USER", "rd_shot")
PASS = os.environ.get("RD_PASS", "ShotPass12345!")
DESIGN_PK = os.environ.get("RD_DESIGN_PK", "5")
RACK_PK = os.environ.get("RD_RACK_PK", "2541")
EDITOR_URL = f"{BASE}/plugins/rack-design/designs/{DESIGN_PK}/editor/{RACK_PK}/"


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
# private `state` + `buildRackPayload`, reading from the SAME live sources the
# editor reads (the #rd-editor-data widget JSON, the live `nbx-rd-state-*`
# classes, the `data-rd-temp-ghost` marker, and each tile's gridstackNode y/h).
# This lets us assert on "what Save WOULD send" WITHOUT ever calling Save.
#
# editor.js keeps these private (IIFE), so we reconstruct rather than reach in.
# The reconstruction is kept line-for-line aligned with editor.js's
# buildRackPayload so a behavioural regression in the DOM/state shows up here.
# ---------------------------------------------------------------------------
TEST_HARNESS_JS = r"""
window.__rdE2E = (function () {
    var root = document.getElementById("rd-editor");
    var rackId = parseInt(root.getAttribute("data-rack-id"), 10);
    var rackUHeight = parseInt(root.getAttribute("data-u-height"), 10);
    var descUnits = root.getAttribute("data-desc-units") === "true";

    var baseWidgets = JSON.parse(
        document.getElementById("rd-editor-data").textContent || "[]");

    function frontGrid() { return document.getElementById("nbx-rd-grid-front").gridstack; }
    function rearGrid() { return document.getElementById("nbx-rd-grid-rear").gridstack; }
    function trayGrid() { return document.getElementById("nbx-rd-grid-tray").gridstack; }

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

    // Click the × button on the tile at the given widget index.
    function clickRemove(idx) {
        var el = document.querySelector('.grid-stack-item[data-widget-index="' + idx + '"]');
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
        var el = document.querySelector('.grid-stack-item[data-widget-index="' + idx + '"]');
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
        var el = document.querySelector('.grid-stack-item[data-widget-index="' + idx + '"]');
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
            face: (el.closest("#nbx-rd-grid-front") ? "front"
                   : el.closest("#nbx-rd-grid-rear") ? "rear" : "other"),
        };
    }

    function ghostCount() {
        return document.querySelectorAll(
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
"""


@unittest.skipUnless(_PREREQ_OK, f"editor e2e prerequisites not met: {_PREREQ_REASON}")
class EditorE2ETestCase(unittest.TestCase):
    """Each test loads a FRESH editor page (so cases are independent and order
    does not matter) and asserts on in-page state / the would-be save payload.
    Nothing is ever saved."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(channel="chrome", headless=True)
        # Authenticate once and reuse the storage state across tests.
        ctx = cls._browser.new_context(viewport={"width": 1400, "height": 1200})
        pg = ctx.new_page()
        pg.goto(f"{BASE}/login/", wait_until="networkidle")
        pg.fill("#id_username", USER)
        pg.fill("#id_password", PASS)
        pg.click("button[type=submit]")
        pg.wait_for_load_state("networkidle")
        cls._storage = ctx.storage_state()
        ctx.close()

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

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

        resp = self.page.goto(EDITOR_URL, wait_until="networkidle")
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

    def assert_no_console_errors(self):
        self.assertEqual(
            self.console_errors, [],
            f"unexpected console errors: {self.console_errors}")

    # =====================================================================
    # 1. Editor loads clean
    # =====================================================================
    def test_01_editor_loads_clean(self):
        info = self.page.evaluate(
            """() => ({
                gridstack: typeof window.GridStack,
                frontGrid: !!document.getElementById('nbx-rd-grid-front').gridstack,
                rearGrid: !!document.getElementById('nbx-rd-grid-rear').gridstack,
                trayGrid: !!document.getElementById('nbx-rd-grid-tray').gridstack,
                nWidgets: window.__rdE2E.baseWidgets.length,
                saveDisabled: window.__rdE2E.saveDisabled(),
            })""")
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
        DTID = 669
        new_idx = self.page.evaluate(
            "() => window.__rdE2E.dropPaletteItem(669, 1, 'e2e-drop')")
        self.assertIsNotNone(new_idx, "dropPaletteItem did not stamp a widget index")

        # The dropped tile became an `add` tile.
        info = self.page.evaluate(
            f"() => window.__rdE2E.tileInfo('{new_idx}')")
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
        self.assertEqual(add["device_type_id"], DTID, f"wrong device_type_id: {add}")
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
        IDX = 0  # existing, front, has a role color
        before = self.page.evaluate(f"() => window.__rdE2E.tileInfo('{IDX}')")
        self.assertIn("nbx-rd-state-existing", before["classes"])
        self.assertTrue(before["roleBg"], "fixture idx0 should have a role color")
        device_id = self.page.evaluate(
            f"() => window.__rdE2E.baseWidgets[{IDX}].device_id")
        self.assertIsNotNone(device_id, "fixture idx0 should be a real device")

        # First × -> flagged for removal, dirty, Save enabled.
        self.page.evaluate(f"() => window.__rdE2E.clickRemove('{IDX}')")
        flagged = self.page.evaluate(f"() => window.__rdE2E.tileInfo('{IDX}')")
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
        self.page.evaluate(f"() => window.__rdE2E.clickRemove('{IDX}')")
        restored = self.page.evaluate(f"() => window.__rdE2E.tileInfo('{IDX}')")
        self.assertNotIn("nbx-rd-state-remove", restored["classes"],
                         "second × should un-flag removal")
        self.assertIn("nbx-rd-state-existing", restored["classes"])
        self.assertTrue(restored["bg"], "un-flagged existing device should regain its role color")
        self.assert_no_console_errors()

    # =====================================================================
    # 4b. × (cancel) on a pre-existing move_in -> restores device to its REAL
    #     slot in existing styling (role color), payload sends it as `existing`
    #     at the real U.
    # =====================================================================
    def test_04b_cancel_move_on_preexisting_move_in(self):
        IDX = 1  # move_in, front, placement_id 5, real U lives on ghost idx4 (U5)
        mv = self.find_item(self.payload()["front"], placement_id=5)
        self.assertIsNotNone(mv, "fixture move_in (placement 5) missing from payload")

        ghosts_before = self.page.evaluate("() => window.__rdE2E.ghostCount()")
        self.page.evaluate(f"() => window.__rdE2E.clickRemove('{IDX}')")
        info = self.page.evaluate(f"() => window.__rdE2E.tileInfo('{IDX}')")
        self.assertIn("nbx-rd-state-existing", info["classes"],
                      "cancelled move should be restyled existing")
        self.assertNotIn("nbx-rd-state-move_in", info["classes"])
        self.assertFalse(info["dirty"], "restored existing tile should drop the per-tile dirty outline")
        # Role color (not grey): the device type 670 has a role bg in this fixture.
        if info["roleBg"]:
            self.assertTrue(info["bg"], "cancelled move should regain its real role color")

        # Payload: placement 5 now sent as `existing` at the REAL U (5), front.
        pl5 = self.find_item(self.payload()["front"], placement_id=5)
        self.assertIsNotNone(pl5, "cancelled move should still be in the payload")
        self.assertEqual(pl5["kind"], "existing",
                         f"cancelled move should serialize as existing: {pl5}")
        self.assertEqual(pl5["u_position"], 5,
                         f"cancelled move should sit at its real U (5): {pl5}")
        self.assertEqual(pl5["face"], "front")
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
            "() => window.__rdE2E.dropPaletteItem(669, 1, 'e2e-drop')")
        # Confirm it is present first.
        adds = [it for it in self.payload()["front"]
                if it["kind"] == "add" and it["placement_id"] is None]
        self.assertEqual(len(adds), 1, "precondition: one unsaved add present")

        self.page.evaluate(f"() => window.__rdE2E.clickRemove('{new_idx}')")
        gone = self.page.evaluate(
            f"() => !!document.querySelector('.grid-stack-item[data-widget-index=\"{new_idx}\"]')")
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
        IDX = 0  # existing, front, U41, h1 -> origin gsY 2
        orig_gsY = self.page.evaluate(f"() => window.__rdE2E.tileInfo('{IDX}').y")
        self.assertEqual(orig_gsY, 2, "fixture idx0 should start at gsY 2")

        # Drag it down to a free slot (gsY 50 — clear of other fixtures).
        self.page.evaluate("() => window.__rdE2E.moveTile('0', 50)")
        self.page.wait_for_timeout(50)  # ghost logic runs on a 0ms timer
        moved = self.page.evaluate(f"() => window.__rdE2E.tileInfo('{IDX}')")
        self.assertIn("nbx-rd-state-move_in", moved["classes"],
                      "dragged-off existing should restyle move_in")
        self.assertTrue(moved["dirty"], "moved tile should be nbx-rd-dirty")
        ghosts = self.page.evaluate(
            """() => [...document.querySelectorAll(
                '.nbx-rd-state-move_out_ghost[data-rd-temp-ghost]')].map(g => ({
                    locked: g.gridstackNode ? !!g.gridstackNode.locked : null,
                    noMove: g.gridstackNode ? !!g.gridstackNode.noMove : null,
                    y: g.gridstackNode ? g.gridstackNode.y : null,
                }))""")
        self.assertEqual(len(ghosts), 1, f"expected one temp ghost at origin, got {ghosts}")
        self.assertTrue(ghosts[0]["locked"], "move_out_ghost must be locked")
        self.assertTrue(ghosts[0]["noMove"], "move_out_ghost must be noMove")
        self.assertEqual(ghosts[0]["y"], orig_gsY, "ghost should sit at the vacated origin slot")

        # Return it to origin -> ghost removed, existing restored, dirty cleared.
        self.page.evaluate("() => window.__rdE2E.moveTile('0', 2)")
        self.page.wait_for_timeout(50)
        restored = self.page.evaluate(f"() => window.__rdE2E.tileInfo('{IDX}')")
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
        # idx4 = static move_out_ghost; idx3 = pre-existing remove.
        ghost = self.page.evaluate("() => window.__rdE2E.tileInfo('4')")
        self.assertIn("nbx-rd-state-move_out_ghost", ghost["classes"])
        self.assertTrue(ghost["locked"], "static move_out_ghost should be locked")
        self.assertTrue(ghost["noMove"], "static move_out_ghost should be noMove")

        rem = self.page.evaluate("() => window.__rdE2E.tileInfo('3')")
        self.assertIn("nbx-rd-state-remove", rem["classes"])
        self.assertTrue(rem["locked"], "pre-existing remove tile should be locked")
        self.assertTrue(rem["noMove"], "pre-existing remove tile should be noMove")
        self.assert_no_console_errors()


if __name__ == "__main__":
    unittest.main(verbosity=2)
