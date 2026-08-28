#!/usr/bin/env python3
"""Playwright coverage for MANAGING a rack's planned power feeds.

Planned feeds (``DesignPowerFeed``) size a greenfield rack's capacity bar, and
before 0.21.0 they were reachable through no UI at all: the rack power dialog
wrote them and nothing ever showed or removed them, so a feed copied by mistake
inflated the bar with nothing to point at (user 2026-08-28). This suite drives
the two ways out of that: the dialog's own list with a × per feed, and the
design page's panel.

SELF-PROVISIONING and cleaning up after itself: it creates its own design and
its own planned feeds through the API, and deletes the design at the end. It
does write -- planned feeds are design data the dialog persists immediately,
which is the very behaviour under test -- but only inside that throwaway design.
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
class EditorPlannedFeedsTestCase(unittest.TestCase):
    """A plan must be able to show, and undo, its own supply."""

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
        racks = cls._api("GET", "/api/dcim/racks/?limit=50")["results"]
        if not racks:
            raise unittest.SkipTest("no racks in this data")
        rack = racks[0]
        cls.rack_id = rack["id"]
        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"e2e-feeds-{uuid.uuid4().hex[:8]}",
            "site": rack["site"]["id"], "status": "draft", "racks": [cls.rack_id],
        })
        cls._design_id = design["id"]
        cls.editor_url = (
            f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/{cls.rack_id}/")
        cls.detail_url = f"{BASE}/plugins/rack-design/designs/{cls._design_id}/"

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
        # A known supply for every test, replacing whatever a previous test left.
        for feed in self._planned_feeds():
            self._api("DELETE",
                      f"/api/plugins/rack-design/designs/{self._design_id}/planned-feed/",
                      {"feed_id": feed["id"]})
        for name, amps in (("Feed B", 16), ("Utility A", 63)):
            self._api("POST",
                      f"/api/plugins/rack-design/designs/{self._design_id}/planned-feed/",
                      {"rack_id": self.rack_id, "name": name,
                       "voltage": 230, "amperage": amps})
        self.ctx = self._browser.new_context(
            storage_state=self._ctx.storage_state(),
            viewport={"width": 1800, "height": 1100})
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on("console", lambda m: self.errors.append(m.text)
                     if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"PAGEERROR: {e}"))

    def tearDown(self):
        errs = [e for e in self.errors if "favicon" not in e]
        try:
            self.ctx.close()
        finally:
            self.assertEqual(errs, [], f"console errors: {errs}")

    def _planned_feeds(self):
        return self._api(
            "GET",
            f"/api/plugins/rack-design/designs/{self._design_id}"
            f"/planned-feed/?rack_id={self.rack_id}")

    def _open_power_dialog(self):
        self.page.goto(self.editor_url, wait_until="networkidle")
        self.page.wait_for_selector(".grid-stack", timeout=30000)
        self.page.wait_for_timeout(900)
        self.page.evaluate(
            "() => ['djDebug', 'djDebugRoot'].forEach(function (id) {"
            "  const d = document.getElementById(id);"
            "  if (d) { d.style.display = 'none'; d.style.pointerEvents = 'none'; }"
            "})")
        self.page.click("[data-rd-rack-power-btn]")
        self.page.wait_for_selector(".nbx-rd-planned-list", timeout=10000)
        self.page.wait_for_timeout(1200)

    # -- tests --------------------------------------------------------------

    def test_the_dialog_lists_the_racks_planned_feeds(self):
        self._open_power_dialog()
        listed = self.page.inner_text(".nbx-rd-planned-list")
        self.assertIn("Feed B", listed)
        self.assertIn("Utility A", listed)
        self.assertIn("230V", listed, "a feed must show the electricals it contributes")

    def test_a_planned_feed_can_be_removed_from_the_dialog(self):
        """The whole point: a feed added by mistake had no way out short of
        deleting the design."""
        self._open_power_dialog()
        buttons = self.page.query_selector_all(".nbx-rd-planned-del")
        self.assertEqual(len(buttons), 2, "one remove button per planned feed")
        buttons[1].click()          # rows are ordered by name: Feed B, Utility A
        self.page.wait_for_timeout(1800)
        self.assertEqual(
            [f["name"] for f in self._planned_feeds()], ["Feed B"],
            "the removed feed must be gone server-side, not just from the DOM")
        self.assertNotIn("Utility A", self.page.inner_text(".nbx-rd-planned-list"))

    def test_the_design_page_shows_the_planned_feeds(self):
        """The read-only half of the same question: what supply does this plan
        assume, and which PDUs hang off it."""
        self.page.goto(self.detail_url, wait_until="networkidle")
        body = self.page.inner_text("body")
        self.assertIn("Planned power feeds", body)
        self.assertIn("Utility A", body)
        self.assertIn("Feed B", body)


if __name__ == "__main__":
    unittest.main()
