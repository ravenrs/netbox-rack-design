#!/usr/bin/env python3
"""Playwright coverage for the Design-racks panel's per-user visibility toggle.

The eye button beside each rack in the **Racks** drawer hides that rack from
*this user's* workspace (``HiddenDesignRack``). It is documented as
reload-free: the POST records the state and the JS toggles the rack block's
``hidden`` class in place (editor_panels.js §2).

The server side was covered by ``netbox_rack_design.tests.test_views``; the
DOM effect was not, and it silently regressed when planned racks landed
(07ec760). ``rack_block.html`` renders ``data-rack-id`` through
``rack_dom_id``, which deliberately keeps a REAL rack's **bare pk** and
prefixes only a planned one (``p-<pk>``) -- while the panel's JS looked the
block up as ``r-<pk>``. The query never matched, so clicking the eye updated
the row and the database but never the rack itself: the rack stayed on
screen when hidden, and -- after a reload rendered it hidden server-side --
clicking the eye again did not bring it back.

SELF-PROVISIONING: creates its own design scoped to two real racks and
deletes it at the end. Same scaffolding as test_editor_planned_rack.py.
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
        with urllib.request.urlopen(f"{BASE}/login/", timeout=4) as resp:
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
class EditorRackVisibilityTestCase(unittest.TestCase):
    """Hiding and showing a rack must take effect without a page reload."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(channel="chrome", headless=True)
        cls._design_id = None
        cls._ctx = cls._browser.new_context(viewport={"width": 1800, "height": 1100})
        try:
            pg = cls._ctx.new_page()
            pg.goto(f"{BASE}/login/", wait_until="networkidle")
            pg.fill("#id_username", USER)
            pg.fill("#id_password", PASS)
            pg.click("button[type=submit]")
            pg.wait_for_load_state("networkidle")
            pg.close()
            cls._csrf = next(
                (c["value"] for c in cls._ctx.cookies() if c["name"] == "csrftoken"), "")
            cls._provision()
        except BaseException:
            cls._cleanup_class()
            raise

    @classmethod
    def _api(cls, method, path, payload=None):
        r = cls._ctx.request.fetch(
            f"{BASE}{path}", method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     "X-CSRFToken": cls._csrf, "Referer": BASE},
            data=json.dumps(payload) if payload is not None else None)
        if r.status >= 400:
            raise AssertionError(f"{method} {path} -> {r.status}: {r.text()[:300]}")
        return r.json() if r.status != 204 else None

    @classmethod
    def _provision(cls):
        # Two racks of one site: hiding one must leave the other alone.
        racks = cls._api("GET", "/api/dcim/racks/?limit=200")["results"]
        by_site = {}
        for rack in racks:
            by_site.setdefault(rack["site"]["id"], []).append(rack)
        pair = next((v for v in by_site.values() if len(v) >= 2), None)
        if not pair:
            raise unittest.SkipTest("need a site with two racks")
        cls.rack_a, cls.rack_b = pair[0], pair[1]
        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"e2e-visibility-{uuid.uuid4().hex[:8]}",
            "sites": [cls.rack_a["site"]["id"]], "status": "draft",
            "racks": [cls.rack_a["id"], cls.rack_b["id"]],
        })
        cls._design_id = design["id"]
        cls.editor_url = (
            f"{BASE}/plugins/rack-design/designs/{cls._design_id}"
            f"/editor/{cls.rack_a['id']}/")

    @classmethod
    def _cleanup_class(cls):
        try:
            if getattr(cls, "_design_id", None):
                cls._api("DELETE", f"/api/plugins/rack-design/designs/{cls._design_id}/")
        except Exception:
            pass
        for attr, close in (("_ctx", "close"), ("_browser", "close"), ("_pw", "stop")):
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
            storage_state=self._ctx.storage_state(),
            viewport={"width": 1800, "height": 1100})
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on("console", lambda m: self.errors.append(m.text)
                     if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"PAGEERROR: {e}"))
        # Each test states its own starting visibility, so one failing test
        # never decides what the next one sees.
        self._show_all()

    def tearDown(self):
        errs = [e for e in self.errors if "Failed to load resource" not in e]
        try:
            self.assertEqual(errs, [], f"console errors: {errs}")
        finally:
            self.ctx.close()

    # -- helpers ------------------------------------------------------------

    def _show_all(self):
        self._api("POST", "/api/plugins/rack-design/hidden-design-racks/show-all/",
                  {"design_id": self._design_id})

    def _open_editor(self):
        self.page.goto(self.editor_url, wait_until="networkidle")
        self.page.wait_for_timeout(1200)
        self.page.add_style_tag(
            content="#djDebug,#djDebugToolbarHandle{display:none !important}")
        # The drawer remembers it was open (a reload inside one test), so
        # open it only when it is closed.
        toggle = self.page.locator("[data-rd-section-toggle='racks']")
        if toggle.get_attribute("aria-expanded") != "true":
            toggle.click()
            self.page.wait_for_timeout(300)

    def _block_visible(self, rack_id):
        """Is this rack's block on screen? (reads the DOM, not the class)"""
        return self.page.evaluate(
            """(id) => {
                const b = document.querySelector(
                    '.nbx-rd-rack-block[data-rack-id="' + id + '"]');
                if (!b) { return null; }
                return getComputedStyle(b).display !== 'none'
                    && b.getBoundingClientRect().width > 0;
            }""", str(rack_id))

    def _click_eye(self, rack_id):
        self.page.click(f"[data-rd-visi-toggle='{rack_id}']", force=True)
        self.page.wait_for_timeout(700)

    # -- tests --------------------------------------------------------------

    def test_hide_then_show_without_reload(self):
        """The eye hides the rack in place, and shows it again in place."""
        self._open_editor()
        a, b = self.rack_a["id"], self.rack_b["id"]
        self.assertTrue(self._block_visible(a), "rack A should start visible")
        self.assertTrue(self._block_visible(b), "rack B should start visible")

        self._click_eye(a)
        self.assertFalse(self._block_visible(a),
                         "rack A must disappear when its eye is clicked -- no reload")
        self.assertTrue(self._block_visible(b), "hiding A must not touch B")

        self._click_eye(a)
        self.assertTrue(self._block_visible(a),
                        "rack A must come back when its eye is clicked again -- no reload")

    def test_show_after_reload_of_a_hidden_rack(self):
        """A rack hidden in an earlier session is shown again by one click.

        This is the shape the bug was reported in: the rack is hidden when
        the page loads (server-rendered), and clicking the eye left it
        hidden until the user reloaded.
        """
        self._api("POST", "/api/plugins/rack-design/hidden-design-racks/toggle/",
                  {"design_id": self._design_id, "rack_id": self.rack_a["id"]})
        try:
            self._open_editor()
            self.assertFalse(self._block_visible(self.rack_a["id"]),
                             "rack A should load hidden")
            self._click_eye(self.rack_a["id"])
            self.assertTrue(self._block_visible(self.rack_a["id"]),
                            "clicking the eye must reveal a rack that loaded hidden")
        finally:
            self._show_all()

    def test_show_all_button_reveals_every_rack(self):
        """'All' clears the whole hidden set, in place."""
        self._open_editor()
        a, b = self.rack_a["id"], self.rack_b["id"]
        self._click_eye(a)
        self._click_eye(b)
        self.assertFalse(self._block_visible(a))
        self.assertFalse(self._block_visible(b))
        self.page.click("#nbx-rd-show-all-racks", force=True)
        self.page.wait_for_timeout(700)
        self.assertTrue(self._block_visible(a), "'All' must reveal rack A")
        self.assertTrue(self._block_visible(b), "'All' must reveal rack B")

    def test_a_planned_rack_hides_and_shows_like_a_real_one(self):
        """Planned racks get the same eye: a row of racks not yet in NetBox
        fills the screen as much as real ones (they used to have none)."""
        tag = uuid.uuid4().hex[:8]
        location = self._api("POST", "/api/dcim/locations/", {
            "name": f"e2e-vis-{tag}", "slug": f"e2e-vis-{tag}",
            "site": self.rack_a["site"]["id"], "status": "active"})
        planned = self._api("POST", "/api/plugins/rack-design/planned-racks/", {
            "name": f"E2E-VP-{tag}", "location": location["id"], "u_height": 42})
        self._api("PATCH", f"/api/plugins/rack-design/designs/{self._design_id}/",
                  {"planned_racks": [planned["id"]]})
        dom_id = f"p-{planned['id']}"
        try:
            self._open_editor()
            self.assertTrue(self._block_visible(dom_id), "the planned rack should start visible")
            self._click_eye(dom_id)
            self.assertFalse(self._block_visible(dom_id),
                             "the planned rack must disappear when its eye is clicked")
            self.assertTrue(self._block_visible(self.rack_a["id"]),
                            "hiding a planned rack must not touch a real one")
            self._open_editor()
            self.assertFalse(self._block_visible(dom_id), "it must stay hidden across a reload")
            self._click_eye(dom_id)
            self.assertTrue(self._block_visible(dom_id), "one click must show it again")
        finally:
            self._show_all()
            self._api("PATCH", f"/api/plugins/rack-design/designs/{self._design_id}/",
                      {"planned_racks": []})
            self._api("DELETE", f"/api/plugins/rack-design/planned-racks/{planned['id']}/")
            self._api("DELETE", f"/api/dcim/locations/{location['id']}/")


if __name__ == "__main__":
    unittest.main(verbosity=2)
