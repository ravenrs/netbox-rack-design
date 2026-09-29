#!/usr/bin/env python3
"""Playwright coverage for the editor's "Create rack" dialog (T1.5,
PLAN-templates.md §1).

Before this task a design's greenfield racks (``PlannedRack`` -- a rack that
does not exist in NetBox yet) had no UI at all: the model, projection,
distribution, Apply and REST endpoint were all built, but the editor only
ever rendered ``design.racks`` (real racks), and there was no way to create
one. This suite drives the dialog end to end: opening it, creating a rack
(which must appear in the workspace with a "Planned" badge so a planner can
never confuse it with a rack that already exists), and the duplicate
``(location, name)`` rejection (D4 -- that pair is the model's whole
identity).

SELF-PROVISIONING: creates its own design and its own Location under a real
rack's site, and deletes the design at the end. Mirrors
test_editor_planned_feeds.py's shape (same login/cleanup scaffolding, same
``p.chromium.launch(channel="chrome")`` -- system Chrome, no download).
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
class EditorCreatePlannedRackTestCase(unittest.TestCase):
    """A planner must be able to draft a rack that does not exist yet."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(channel="chrome", headless=True)
        cls._design_id = None
        cls._location_id = None
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
        cls.site_id = rack["site"]["id"]
        cls.rack_id = rack["id"]
        cls.rack_name = rack["name"]

        # A dedicated Location under the rack's site -- the dialog's Location
        # dropdown is scoped to the design's own site (views._design_editor_
        # context's `site_locations`), so a location must exist there for the
        # test to have anything to pick.
        cls._location_id = cls._api("POST", "/api/dcim/locations/", {
            "site": cls.site_id,
            "name": f"e2e-planned-loc-{uuid.uuid4().hex[:8]}",
            "slug": f"e2e-planned-loc-{uuid.uuid4().hex[:8]}",
        })["id"]

        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"e2e-planned-rack-{uuid.uuid4().hex[:8]}",
            "sites": [cls.site_id], "status": "draft",
        })
        cls._design_id = design["id"]
        cls.editor_url = (
            f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/")

        # Scope this real rack into the design AND give it a planned feed
        # (T?, "copy feeds while creating"): the Create-rack dialog's "Copy
        # feeds from" select only lists racks rendered in the workspace
        # (racksInDom()), and copy-feeds needs something to actually clone.
        cls._api(
            "POST", f"/api/plugins/rack-design/designs/{cls._design_id}/add-rack/",
            {"rack_id": cls.rack_id},
        )
        cls._api("POST", "/api/plugins/rack-design/planned-power-feeds/", {
            "design": cls._design_id, "rack": cls.rack_id,
            "name": f"{cls.rack_name}-A", "voltage": 230, "amperage": 16,
        })

    @classmethod
    def _cleanup_class(cls):
        try:
            if getattr(cls, "_design_id", None):
                cls._api("DELETE", f"/api/plugins/rack-design/designs/{cls._design_id}/")
        except Exception:
            pass
        # Deleting the design only removes its Design.planned_racks JOIN
        # rows -- the PlannedRack objects themselves are shared, first-class
        # rows (D3/D6) and survive. PlannedRack.location is PROTECT (D4), so
        # the location delete below 409s until these are gone too. Clean
        # them up by location -- this suite is the only writer of planned
        # racks at this dedicated e2e location -- BEFORE the location itself.
        try:
            if getattr(cls, "_location_id", None):
                planned = cls._api(
                    "GET",
                    f"/api/plugins/rack-design/planned-racks/?location_id={cls._location_id}",
                )
                for pr in (planned or {}).get("results", []):
                    try:
                        cls._api("DELETE", f"/api/plugins/rack-design/planned-racks/{pr['id']}/")
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            if getattr(cls, "_location_id", None):
                cls._api("DELETE", f"/api/dcim/locations/{cls._location_id}/")
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
        # A test that deliberately provokes a REST 400 (the duplicate
        # (location, name) case below) makes Chrome itself log
        # "Failed to load resource: ... 400 (Bad Request)" to the console --
        # that is the browser reporting the network response, not a bug, and
        # happens even though the dialog's own JS handles the error and shows
        # a friendly inline message. A test that expects one adds the
        # substring here so tearDown's assertion does not choke on it.
        self.allowed_console_errors = []

    def tearDown(self):
        errs = [
            e for e in self.errors
            if "favicon" not in e
            and not any(allowed in e for allowed in self.allowed_console_errors)
        ]
        try:
            self.ctx.close()
        finally:
            self.assertEqual(errs, [], f"console errors: {errs}")

    def _open_editor(self):
        self.page.goto(self.editor_url, wait_until="networkidle")
        self.page.wait_for_selector("#rd-editor", timeout=30000)
        self.page.evaluate(
            "() => ['djDebug', 'djDebugRoot'].forEach(function (id) {"
            "  const d = document.getElementById(id);"
            "  if (d) { d.style.display = 'none'; d.style.pointerEvents = 'none'; }"
            "})")

    def _open_create_rack_dialog(self):
        self._open_editor()
        # The button lives in the (collapsible) Racks drawer section.
        toggle = self.page.query_selector('[data-rd-section-toggle="racks"]')
        if toggle and not self.page.is_visible("#nbx-rd-create-planned-rack-btn"):
            toggle.click()
        self.page.wait_for_selector("#nbx-rd-create-planned-rack-btn", timeout=10000)
        self.page.click("#nbx-rd-create-planned-rack-btn")
        self.page.wait_for_selector(".nbx-rd-create-rack-modal.show", timeout=10000)

    def test_create_rack_button_opens_the_dialog(self):
        self._open_create_rack_dialog()
        self.assertTrue(self.page.is_visible(".nbx-rd-create-rack-name"))
        self.assertTrue(self.page.is_visible(".nbx-rd-create-rack-height"))
        self.assertTrue(self.page.is_visible(".nbx-rd-create-rack-location"))

    def test_submitting_creates_the_rack_with_a_planned_badge(self):
        rack_name = f"e2e-planned-{uuid.uuid4().hex[:6]}"
        self._open_create_rack_dialog()
        self.page.fill(".nbx-rd-create-rack-name", rack_name)
        self.page.fill(".nbx-rd-create-rack-height", "12")
        self.page.select_option(".nbx-rd-create-rack-location", str(self._location_id))
        # Success reloads the page (same contract as Add rack). Once ANOTHER
        # rack already exists in the design (as it will once
        # test_duplicate_location_and_name_shows_a_friendly_error has run --
        # unittest orders methods alphabetically, and "duplicate" <
        # "submitting" -- the CURRENT page already satisfies both
        # `wait_for_load_state("networkidle")` and a bare
        # `wait_for_selector(".grid-stack")`, since a grid-stack from the
        # pre-existing rack is already on screen. That let this assertion
        # race the reload and check the OLD DOM. `expect_navigation` instead
        # waits for an ACTUAL new navigation to finish, so this only ever
        # inspects the post-reload page.
        with self.page.expect_navigation(wait_until="networkidle", timeout=15000):
            self.page.click("[data-rd-create-rack-submit]")
        self.page.wait_for_selector(".grid-stack", timeout=30000)

        block = self.page.query_selector(f'.nbx-rd-rack-block:has-text("{rack_name}")')
        self.assertIsNotNone(block, "the newly created planned rack must appear in the workspace")
        self.assertIn(
            "Planned", block.inner_text(),
            "a planned rack must be visibly badged so it is never mistaken for a real one",
        )

    def test_duplicate_location_and_name_shows_a_friendly_error(self):
        # This test deliberately provokes a REST 400 (below); Chrome logs the
        # failed response to the console on its own regardless of the JS
        # handling it gracefully -- see setUp's comment on
        # `allowed_console_errors`.
        self.allowed_console_errors.append("400 (Bad Request)")

        # Provision one planned rack via the API directly (D4: (location, name)
        # is the whole identity), then try to create the SAME pair through the
        # dialog and expect an inline error, not a silent failure or a 500.
        dup_name = f"e2e-dup-{uuid.uuid4().hex[:6]}"
        self._api(
            "POST",
            f"/api/plugins/rack-design/designs/{self._design_id}/create-planned-rack/",
            {"name": dup_name, "u_height": 10, "location_id": self._location_id},
        )

        self._open_create_rack_dialog()
        self.page.fill(".nbx-rd-create-rack-name", dup_name)
        self.page.fill(".nbx-rd-create-rack-height", "10")
        self.page.select_option(".nbx-rd-create-rack-location", str(self._location_id))
        self.page.click("[data-rd-create-rack-submit]")

        self.page.wait_for_selector(
            ".nbx-rd-create-rack-error:not([style*='display: none'])", timeout=10000)
        error_text = self.page.inner_text(".nbx-rd-create-rack-error")
        self.assertTrue(error_text.strip(), "the duplicate must surface a visible message")
        # The dialog must still be open -- a duplicate is a rejected request,
        # not a silent no-op that looks like success.
        self.assertTrue(self.page.is_visible(".nbx-rd-create-rack-modal"))

    def test_dialog_shows_pattern_hint_and_copy_feeds_select(self):
        """The two new pieces of UI: a hint that Name takes a NetBox range
        pattern, and a "Copy feeds from" select listing this design's racks
        (the real rack _provision scoped in and gave a planned feed)."""
        self._open_create_rack_dialog()
        hint = self.page.inner_text(".nbx-rd-create-rack-name + .form-text")
        self.assertIn("R[1-4]", hint)
        self.assertTrue(
            self.page.is_visible(".nbx-rd-create-rack-copy-feeds"),
            "the Copy feeds from select must be present",
        )
        options = self.page.eval_on_selector_all(
            ".nbx-rd-create-rack-copy-feeds option", "els => els.map(e => e.textContent)")
        self.assertIn(self.rack_name, options)

    def test_pattern_name_creates_one_block_per_expanded_name(self):
        base = f"e2e-pat-{uuid.uuid4().hex[:6]}"
        pattern = f"{base}[1-3]"
        self._open_create_rack_dialog()
        self.page.fill(".nbx-rd-create-rack-name", pattern)
        self.page.fill(".nbx-rd-create-rack-height", "10")
        self.page.select_option(".nbx-rd-create-rack-location", str(self._location_id))
        with self.page.expect_navigation(wait_until="networkidle", timeout=15000):
            self.page.click("[data-rd-create-rack-submit]")
        self.page.wait_for_selector(".grid-stack", timeout=30000)

        for suffix in ("1", "2", "3"):
            block = self.page.query_selector(
                f'.nbx-rd-rack-block:has-text("{base}{suffix}")')
            self.assertIsNotNone(
                block, f"{base}{suffix} must appear in the workspace")
            self.assertIn("Planned", block.inner_text())

    def test_copy_feeds_on_create_gives_the_new_rack_its_own_feed_block(self):
        rack_name = f"e2e-copyfeed-{uuid.uuid4().hex[:6]}"
        self._open_create_rack_dialog()
        self.page.fill(".nbx-rd-create-rack-name", rack_name)
        self.page.fill(".nbx-rd-create-rack-height", "10")
        self.page.select_option(".nbx-rd-create-rack-location", str(self._location_id))
        self.page.select_option(".nbx-rd-create-rack-copy-feeds", str(self.rack_id))
        with self.page.expect_navigation(wait_until="networkidle", timeout=15000):
            self.page.click("[data-rd-create-rack-submit]")
        self.page.wait_for_selector(".grid-stack", timeout=30000)

        feeds = self._api(
            "GET",
            "/api/plugins/rack-design/planned-power-feeds/"
            f"?design_id={self._design_id}",
        )["results"]
        matches = [f for f in feeds if f["name"] == f"{rack_name}-A"]
        self.assertTrue(
            matches,
            f"the new rack must get its own copied feed named for itself; "
            f"feeds were: {[f['name'] for f in feeds]}",
        )


if __name__ == "__main__":
    unittest.main()
