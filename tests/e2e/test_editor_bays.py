#!/usr/bin/env python3
"""Playwright end-to-end coverage for device bays / blades (spec §10).

Run via ``dev/e2e.sh`` like the other editor e2e suites.

What this locks in, none of which a Django test can reach: the rack view REPORTS
bay occupancy without trying to edit it (§10.4); the BLADE LAYER renders each
chassis as a degenerate rack and drives it with the unmodified rack editor
(§10.3); the palette there is filtered to child device types so a rack-mountable
device can never be offered as a blade; and switching layers with unsaved work
asks rather than discards.

SELF-PROVISIONING, like the sibling suites: the rack is DISCOVERED via the API
(a rack holding a parent device with a free bay), never hardcoded, so a
deployment without blade hardware SKIPS cleanly. Save is intercepted and
answered locally, so the only writes are the design create/delete.
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
    """Return (ok, skip_reason) — mirrors the sibling suites' guard."""
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


@unittest.skipUnless(_PREREQ_OK, _PREREQ_REASON)
class EditorBayE2ETestCase(unittest.TestCase):
    """§10.3/§10.4 — the rack view reports, the blade layer edits."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(channel="chrome", headless=True)
        cls._design_id = None
        cls._api_ctx = cls._browser.new_context(viewport={"width": 1700, "height": 1000})
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
                (c["value"] for c in cls._api_ctx.cookies() if c["name"] == "csrftoken"), "")
            cls._provision()
        except BaseException:
            cls._cleanup_class()
            raise

    # -- provisioning -------------------------------------------------------

    @classmethod
    def _api(cls, method, path, payload=None):
        r = cls._api_ctx.request.fetch(
            f"{BASE}{path}",
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-CSRFToken": cls._csrf,
                "Referer": BASE,
            },
            data=json.dumps(payload) if payload is not None else None,
        )
        if r.status >= 400:
            raise AssertionError(f"{method} {path} -> {r.status}: {r.text()[:300]}")
        return r.json() if r.status != 204 else None

    @classmethod
    def _provision(cls):
        bays = cls._api(
            "GET", "/api/dcim/device-bays/?installed_device_id=null&limit=50")
        chosen = None
        for bay in bays.get("results", []):
            dev = cls._api("GET", f"/api/dcim/devices/{bay['device']['id']}/")
            if dev.get("rack"):
                chosen = (bay, dev)
                break
        if not chosen:
            raise unittest.SkipTest("no empty device bay on a racked chassis in this data")
        bay, chassis = chosen
        cls.bay_id = bay["id"]
        cls.bay_name = bay["name"]
        cls.rack_pk = chassis["rack"]["id"]

        blades = cls._api("GET", "/api/dcim/device-types/?subdevice_role=child&limit=1")
        if not blades.get("results"):
            raise unittest.SkipTest("no child (blade) device type in this data")

        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"e2e-bays-{uuid.uuid4().hex[:8]}",
            "site": chassis["site"]["id"],
            "status": "draft",
            "racks": [cls.rack_pk],
        })
        cls._design_id = design["id"]
        cls.rack_url = (
            f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/{cls.rack_pk}/")
        cls.blade_url = f"{BASE}/plugins/rack-design/designs/{cls._design_id}/blades/"

    @classmethod
    def _cleanup_class(cls):
        try:
            if getattr(cls, "_design_id", None):
                cls._api("DELETE", f"/api/plugins/rack-design/designs/{cls._design_id}/")
        except Exception:
            pass
        for attr, close in (("_api_ctx", "close"), ("_browser", "close"), ("_pw", "stop")):
            obj = getattr(cls, attr, None)
            if obj is not None:
                try:
                    getattr(obj, close)()
                except Exception:
                    pass

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_class()

    # -- per-test -----------------------------------------------------------

    def setUp(self):
        self.ctx = self._browser.new_context(
            storage_state=self._storage, viewport={"width": 1700, "height": 1000})
        self.page = self.ctx.new_page()
        self.console_errors = []
        self.page.on(
            "console",
            lambda m: self.console_errors.append(f"{m.type}: {m.text}")
            if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.console_errors.append(f"PAGEERROR: {e}"))

    def tearDown(self):
        errs = [e for e in self.console_errors if "favicon" not in e]
        try:
            self.ctx.close()
        finally:
            self.assertEqual(errs, [], f"console errors: {errs}")

    # -- helpers ------------------------------------------------------------

    def _open(self, url):
        self.page.goto(url, wait_until="networkidle")
        self.page.wait_for_selector(".grid-stack", timeout=30000)
        self.page.wait_for_timeout(900)
        # The debug toolbar overlays the toolbar buttons on a dev server. Remove
        # BOTH ids: NetBox 4.4 renders the panel as #djDebug, while 4.6 wraps it
        # in a full-page #djDebugRoot that swallows the clicks by itself.
        # HIDDEN, not removed: 4.6's toolbar script keeps reaching into its own
        # shadowRoot afterwards and throws a page error if the node is gone.
        self.page.evaluate(
            "() => ['djDebug', 'djDebugRoot'].forEach(function (id) {"
            "  const d = document.getElementById(id);"
            "  if (d) { d.style.display = 'none'; d.style.pointerEvents = 'none'; }"
            "})")

    def _open_palette(self):
        box = self.page.query_selector("#nbx-rd-palette-search")
        if not (box and box.is_visible()):
            self.page.click('[data-rd-section-toggle="device"]')
        self.page.wait_for_selector("#nbx-rd-palette-search", state="visible", timeout=15000)
        self.page.wait_for_timeout(2500)

    def _free_bay_target(self):
        """(grid id, y-offset) of a FREE bay row, or (None, None).

        Targets a free bay rather than an empty chassis: the provisioned rack is
        chosen for having one free bay, which usually sits in a chassis that is
        otherwise occupied. Looking for a wholly empty column skipped exactly the
        tests worth running.
        """
        found = self.page.evaluate("""() => {
            for (const b of document.querySelectorAll('.nbx-rd-chassis-block')) {
                const total = parseInt(b.dataset.uHeight, 10);
                const taken = new Set([...b.querySelectorAll('.grid-stack-item')]
                    .map(t => t.gridstackNode ? t.gridstackNode.y : -1));
                for (let i = 0; i < total; i++) {
                    if (!taken.has(i * 2)) {
                        return {gid: 'nbx-rd-grid-front-' + b.dataset.rackId, index: i};
                    }
                }
            }
            return null;
        }""")
        if not found:
            return None, None
        # One bay is one whole "U" == 2 grid rows of 11px; aim at its middle.
        return found["gid"], found["index"] * 22 + 11

    def _drag_palette_onto(self, selector, offset_y=11):
        row = self.page.query_selector("#nbx-rd-palette-list .nbx-rd-palette-item")
        self.assertIsNotNone(row, "palette returned no device types")
        row.scroll_into_view_if_needed()
        self.page.wait_for_timeout(250)
        target = self.page.query_selector(selector)
        self.assertIsNotNone(target, f"no drop target for {selector}")
        s, d = row.bounding_box(), target.bounding_box()
        self.page.mouse.move(s["x"] + s["width"] / 2, s["y"] + s["height"] / 2)
        self.page.mouse.down()
        self.page.mouse.move(d["x"] + d["width"] / 2, d["y"] + offset_y, steps=25)
        self.page.wait_for_timeout(350)

    def _capture_save(self):
        """Click Save, capture the body, and answer locally so nothing persists."""
        sent = {}

        def _route(route, request):
            sent["body"] = request.post_data_json
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"created": 0, "updated": 0, "deleted": 0}))

        self.page.route("**/save-layout/", _route)
        self.page.evaluate("() => document.getElementById('rd-editor-save')?.click()")
        self.page.wait_for_timeout(2500)
        self.page.unroute("**/save-layout/")
        return sent.get("body") or {}

    # -- rack view: report, don't edit (§10.4) ------------------------------

    def test_rack_view_reports_bay_occupancy_and_has_no_bay_strip(self):
        self._open(self.rack_url)
        self.assertEqual(
            self.page.eval_on_selector_all(".nbx-rd-bays", "e => e.length"), 0,
            "the rack view must not render an in-tile bay strip (rejected, §10.3)")
        tile = self.page.query_selector("[data-bays-total]")
        self.assertIsNotNone(tile, "a chassis tile must report its bay occupancy")
        self.assertIsNotNone(tile.get_attribute("data-bays-used"))

    def test_rack_view_offers_the_blade_layer(self):
        self._open(self.rack_url)
        self.assertIsNotNone(
            self.page.query_selector("[data-rd-layer-switch]"),
            "a design with a chassis in scope must offer the blade layer")

    # -- blade layer (§10.3) -------------------------------------------------

    def test_layer_renders_each_chassis_as_a_column_of_bays(self):
        self._open(self.blade_url)
        self.assertGreater(
            self.page.eval_on_selector_all(".nbx-rd-chassis-block", "e => e.length"), 0,
            "no chassis columns rendered")
        # A chassis IS a rack here, and one bay is one whole "U" -- so the grid's
        # max-row is twice the bay count, exactly as a rack's is twice its height.
        geometry = self.page.evaluate("""() => {
            const b = document.querySelector('.nbx-rd-chassis-block');
            const g = document.getElementById('nbx-rd-grid-front-' + b.dataset.rackId);
            return {bays: parseInt(b.dataset.uHeight, 10),
                    maxRow: parseInt(g.getAttribute('gs-max-row'), 10),
                    hasGridstack: !!g.gridstack};
        }""")
        self.assertTrue(geometry["hasGridstack"], "the bay column must be a live grid")
        self.assertEqual(geometry["maxRow"], geometry["bays"] * 2)

    def test_layer_palette_offers_only_child_device_types(self):
        self._open(self.blade_url)
        self._open_palette()
        roles = self.page.eval_on_selector_all(
            "#nbx-rd-palette-list .nbx-rd-palette-item",
            "els => els.map(e => e.getAttribute('data-subdevice-role'))")
        self.assertGreater(len(roles), 0, "palette returned nothing")
        self.assertEqual(set(roles), {"child"},
                         "a non-child type must never be offered as a blade")

    def test_dropping_a_blade_names_it_and_saves_it_as_a_bay_item(self):
        """The §10.3 gesture end to end, plus §10.6's payload contract."""
        self._open(self.blade_url)
        self._open_palette()
        gid, offset = self._free_bay_target()
        if not gid:
            self.skipTest("every chassis in scope is already full")
        self._drag_palette_onto(f"#{gid}", offset_y=offset)
        self.page.mouse.up()
        self.page.wait_for_timeout(2500)      # the naming preview is async

        self.assertIsNotNone(
            self.page.query_selector(f"#{gid} .nbx-rd-state-add"),
            "the blade must land in the bay column")
        name = self.page.evaluate(
            f"""() => document.querySelector(
                '#{gid} .nbx-rd-state-add .nbx-rd-name-input')?.value""")
        self.assertTrue(name, "a dropped blade must be auto-named by the naming engine")

        payload = self._capture_save()
        bays = [b for r in payload.get("racks", []) for b in (r.get("bays") or [])]
        added = [b for b in bays if b.get("kind") == "add"]
        self.assertEqual(len(added), 1, f"expected exactly one bay add, got {bays}")
        self.assertTrue(
            added[0].get("target_bay_id") or added[0].get("parent_placement_id"),
            "a bay item must address a real bay or a planned chassis")
        self.assertIsNone(added[0].get("u_position"), "a blade takes no rack position")

    def test_a_clean_load_posts_nothing(self):
        """Untouched blades must not be re-sent as edits: Save stays disabled, so
        loading and clicking it produces no request at all."""
        self._open(self.blade_url)
        self.assertTrue(
            self.page.evaluate(
                "() => document.getElementById('rd-editor-save').hasAttribute('disabled')"),
            "a clean load must not arm Save")
        self.assertEqual(self._capture_save(), {}, "a clean load must post nothing")

    # -- layer switching -----------------------------------------------------

    def test_switching_with_unsaved_work_asks_first(self):
        self._open(self.blade_url)
        self._open_palette()
        gid, offset = self._free_bay_target()
        if not gid:
            self.skipTest("every chassis in scope is already full")
        self._drag_palette_onto(f"#{gid}", offset_y=offset)
        self.page.mouse.up()
        self.page.wait_for_timeout(1200)
        self.assertFalse(
            self.page.evaluate(
                "() => document.getElementById('rd-editor-save').hasAttribute('disabled')"),
            "the drop should have armed Save")

        self.page.click("[data-rd-layer-switch]")
        self.page.wait_for_timeout(900)
        modal = self.page.query_selector(".nbx-rd-switch-modal")
        self.assertIsNotNone(modal, "switching with unsaved work must ask first")
        self.assertIn("unsaved", modal.inner_text().lower())
        self.assertIn("/blades/", self.page.url, "must not navigate until answered")

        self.page.click(".nbx-rd-switch-modal [data-bs-dismiss=modal]")
        self.page.wait_for_timeout(700)
        self.assertIn("/blades/", self.page.url, "Cancel must keep us where we are")

    def test_switching_with_no_changes_goes_straight_through(self):
        self._open(self.blade_url)
        self.page.click("[data-rd-layer-switch]")
        self.page.wait_for_load_state("networkidle")
        self.assertIn("/editor", self.page.url)
        self.assertIsNone(self.page.query_selector(".nbx-rd-switch-modal"))


if __name__ == "__main__":
    unittest.main()
