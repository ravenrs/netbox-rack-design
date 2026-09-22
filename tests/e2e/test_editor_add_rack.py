#!/usr/bin/env python3
"""Playwright coverage for the editor's Add-rack panel and Create-rack dialog
becoming Site -> Location -> Rack (PLAN-multi-site.md M8/P3).

Design.sites is an M2M (>= 1, P1). forms.DesignEditorAddRackForm now leads
with an ``add_site`` DynamicModelChoiceField scoped to the design's own
sites; Location and Rack chain off it via query_params (``$add_site``) and
start out HTML-disabled until a site is picked. Exactly one site is the
common case: the panel renders it as a read-only chip and Location/Rack come
back already enabled/pre-filtered. This suite drives:

  * one-site design -> the Site chip is shown, Location and Rack start
    enabled (nothing to disable-until);
  * two-site design -> Location and Rack start disabled; choosing a site
    enables them and scopes the Rack API query to that site only; switching
    site clears the Rack selection (NetBox's own DynamicTomSelect dependency
    wiring, `updateQueryParams`/`clear()` in project-static's
    classes/dynamicTomSelect.ts, already does this once the two fields
    declare ``query_params={"site_id": "$add_site", ...}``);
  * the Create-rack dialog (views.py's ``site_locations``, grouped per site)
    shows the same one-site chip / multi-site Site-then-Location behaviour.

SELF-PROVISIONING, mirroring test_editor_template_tab.py's shape: a shared
manufacturer/two-sites/two-locations/two-racks fixture in setUpClass/
tearDownClass; each test gets its OWN Design in setUp (created via the REST
API, sites=[...] -- Design.site (FK) no longer exists, P1). Never clicks
Save. ``p.chromium.launch(channel="chrome")`` -- system Chrome, no download.
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
class EditorAddRackPanelTestCase(unittest.TestCase):
    """The Add-rack panel and Create-rack dialog gate Location/Rack on Site."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(channel="chrome", headless=True)
        cls._ctx = cls._browser.new_context(viewport={"width": 1900, "height": 1200})
        cls._design_ids = []
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
            cls._provision_fixture()
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
    def _provision_fixture(cls):
        suffix = uuid.uuid4().hex[:8]
        site_a = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E Addrack Site A {suffix}", "slug": f"e2e-addrack-a-{suffix}",
            "status": "active"})
        site_b = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E Addrack Site B {suffix}", "slug": f"e2e-addrack-b-{suffix}",
            "status": "active"})
        location_a = cls._api("POST", "/api/dcim/locations/", {
            "name": f"E2E Addrack Loc A {suffix}", "slug": f"e2e-addrack-loc-a-{suffix}",
            "site": site_a["id"]})
        location_b = cls._api("POST", "/api/dcim/locations/", {
            "name": f"E2E Addrack Loc B {suffix}", "slug": f"e2e-addrack-loc-b-{suffix}",
            "site": site_b["id"]})
        rack_a = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E Addrack Rack A {suffix}", "site": site_a["id"],
            "location": location_a["id"], "status": "active", "u_height": 20})
        rack_b = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E Addrack Rack B {suffix}", "site": site_b["id"],
            "location": location_b["id"], "status": "active", "u_height": 20})
        # A second rack in site A -- what the unsaved-changes tests add, so
        # the gesture under test is "add a rack", not "change site".
        rack_a2 = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E Addrack Rack A2 {suffix}", "site": site_a["id"],
            "location": location_a["id"], "status": "active", "u_height": 20})

        # One real device in rack A: the unsaved-changes tests need a tile
        # they can flag for removal, which is the cheapest REAL gesture that
        # arms the editor's dirty flag (no drag shim involved).
        dtype = cls._api("GET", "/api/dcim/device-types/?limit=1")["results"][0]
        role = cls._api("GET", "/api/dcim/device-roles/?limit=1")["results"][0]
        # `custom_fields` explicitly: this dev instance carries a text custom
        # field whose stored default is not a string, which 400s any device
        # create that leaves it out (same workaround as
        # test_editor_distribution.py).
        device = cls._api("POST", "/api/dcim/devices/", {
            "name": f"e2e-addrack-dev-{suffix}", "device_type": dtype["id"],
            "role": role["id"], "site": site_a["id"], "rack": rack_a["id"],
            "position": 1, "face": "front", "status": "active",
            "custom_fields": {"warranty_type": ""}})

        cls._created = {
            "sites": [site_a["id"], site_b["id"]],
            "locations": [location_a["id"], location_b["id"]],
            "racks": [rack_a["id"], rack_b["id"], rack_a2["id"]],
            "devices": [device["id"]],
        }
        cls._device = device
        cls._site_a = site_a
        cls._site_b = site_b
        cls._location_a = location_a
        cls._location_b = location_b
        cls._rack_a = rack_a
        cls._rack_b = rack_b
        cls._rack_a2 = rack_a2

    @classmethod
    def _cleanup_class(cls):
        for design_id in getattr(cls, "_design_ids", []):
            try:
                cls._api("DELETE", f"/api/plugins/rack-design/designs/{design_id}/")
            except Exception:
                pass
        created = getattr(cls, "_created", None)
        if created:
            for device_id in created.get("devices", []):
                try:
                    cls._api("DELETE", f"/api/dcim/devices/{device_id}/")
                except Exception:
                    pass
            for rack_id in created.get("racks", []):
                try:
                    cls._api("DELETE", f"/api/dcim/racks/{rack_id}/")
                except Exception:
                    pass
            for location_id in created.get("locations", []):
                try:
                    cls._api("DELETE", f"/api/dcim/locations/{location_id}/")
                except Exception:
                    pass
            for site_id in created.get("sites", []):
                try:
                    cls._api("DELETE", f"/api/dcim/sites/{site_id}/")
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

    # -- per-test: fresh one-site and two-site designs -------------------------

    def setUp(self):
        suffix = uuid.uuid4().hex[:8]
        self._design_one = self._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"e2e-addrack-one-{suffix}", "sites": [self._site_a["id"]],
            "status": "draft", "racks": [self._rack_a["id"]],
        })
        self._design_ids.append(self._design_one["id"])
        self._design_two = self._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"e2e-addrack-two-{suffix}", "status": "draft",
            "sites": [self._site_a["id"], self._site_b["id"]],
            "racks": [self._rack_a["id"], self._rack_b["id"]],
        })
        self._design_ids.append(self._design_two["id"])

        self.ctx = self._browser.new_context(
            storage_state=self._ctx.storage_state(),
            viewport={"width": 1900, "height": 1200})
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

    # -- helpers ----------------------------------------------------------------

    def _editor_url(self, design, rack_id):
        return f"{BASE}/plugins/rack-design/designs/{design['id']}/editor/{rack_id}/"

    def _open_editor(self, design, rack_id):
        self.page.goto(self._editor_url(design, rack_id), wait_until="networkidle")
        self.page.wait_for_selector("#rd-editor", timeout=30000)
        self.page.evaluate(
            "() => ['djDebug', 'djDebugRoot'].forEach(function (id) {"
            "  const d = document.getElementById(id);"
            "  if (d) { d.style.display = 'none'; d.style.pointerEvents = 'none'; }"
            "})")

    def _open_racks_section(self, design, rack_id):
        self._open_editor(design, rack_id)
        toggle = self.page.query_selector('[data-rd-section-toggle="racks"]')
        self.assertIsNotNone(toggle, "the Racks section toggle must be rendered")
        if not self.page.is_visible("#nbx-rd-add-rack-card"):
            toggle.click()
        self.page.wait_for_selector("#nbx-rd-add-rack-card", timeout=15000)

    def _set_tomselect(self, elem_selector, value, query=""):
        # Drive the TomSelect-backed field programmatically. `preload: 'focus'`
        # (project-static's select/dynamic.ts) means the option list is empty
        # until something triggers a load -- a bare setValue() on a value with
        # no matching (not-yet-fetched) option is silently a no-op, so this
        # first calls load() and waits for it to settle before setting the
        # value. Once set, TomSelect dispatches the same native `change` event
        # NetBox's own dependency wiring listens for (dynamicTomSelect.ts), so
        # this exercises the exact chain a real click-and-pick would.
        # `elem_selector` is a "#id_..." CSS selector like every other
        # selector in this suite. `query` is the search term to load with:
        # the Site picker is unscoped (it must be able to reach ANY site --
        # that is how a design becomes multi-site), so on an instance with
        # hundreds of sites the wanted one is not in the first unfiltered
        # page. Passing its name loads exactly what a typing user would.
        self.page.evaluate(
            """([id, value, query]) => new Promise((resolve) => {
                const el = document.getElementById(id);
                const ts = el && el.tomselect;
                if (!ts) { resolve(false); return; }
                ts.load(query || '');
                const check = () => {
                    if (ts.loading === 0) {
                        ts.setValue(value === null ? '' : String(value));
                        resolve(true);
                    } else {
                        setTimeout(check, 50);
                    }
                };
                setTimeout(check, 50);
            })""",
            [elem_selector.lstrip("#"), value, query],
        )

    def _tomselect_value(self, elem_selector):
        return self.page.evaluate(
            "(id) => { const el = document.getElementById(id);"
            " return el && el.tomselect ? el.tomselect.getValue() : null; }",
            elem_selector.lstrip("#"),
        )

    def _is_disabled(self, selector):
        return self.page.get_attribute(selector, "disabled") is not None

    # -- tests: Add-rack panel ---------------------------------------------------

    def test_one_site_design_prefills_site_and_enables_fields(self):
        """A one-site design starts pre-filled -- and still lets you re-aim.

        The Site field is the filter for Location/Rack, never a fixed chip
        (user ruling 2026-09-22): a planner must be able to point it at
        another site and pull a rack from there, which is how a design
        becomes multi-site.
        """
        self._open_racks_section(self._design_one, self._rack_a["id"])

        self.assertIsNone(
            self.page.query_selector(".nbx-rd-add-site-chip"),
            "Site must be a live picker, not a read-only chip")
        self.assertIsNotNone(self.page.query_selector("#id_add_site"))
        self.assertFalse(
            self._is_disabled("#id_add_site"),
            "the Site filter must stay usable on a one-site design")

        self.assertFalse(
            self._is_disabled("#id_add_location"),
            "Location must start enabled when the site is already known")
        self.assertFalse(
            self._is_disabled("#id_add_rack"),
            "Rack must start enabled when the site is already known")
        self.assertEqual(
            self._tomselect_value("#id_add_site"), str(self._site_a["id"]),
            "the single site must be pre-selected on the hidden add_site field")

    def test_two_site_design_disables_until_a_site_is_chosen(self):
        self._open_racks_section(self._design_two, self._rack_a["id"])

        self.assertIsNotNone(self.page.query_selector("#id_add_site"))
        self.assertTrue(
            self._is_disabled("#id_add_location"),
            "Location must start disabled until a site is chosen")
        self.assertTrue(
            self._is_disabled("#id_add_rack"),
            "Rack must start disabled until a site is chosen")

        # Choosing a site enables both dependents.
        self._set_tomselect("#id_add_site", self._site_a["id"],
                            query=self._site_a["name"])
        self.page.wait_for_function(
            "() => { const el = document.getElementById('id_add_location');"
            " return el && !el.disabled; }",
            timeout=10000)
        self.assertFalse(self._is_disabled("#id_add_location"))
        self.assertFalse(self._is_disabled("#id_add_rack"))

        # The rack chooser's live query is scoped to the chosen site only:
        # site A's own rack query (same endpoint, same site_id) must return
        # rack A and must NOT return rack B.
        with self.page.expect_request(
            lambda req: "/api/dcim/racks/" in req.url and "site_id" in req.url,
            timeout=10000,
        ) as req_info:
            self.page.evaluate(
                "() => { const el = document.getElementById('id_add_rack');"
                " if (el && el.tomselect) { el.tomselect.focus(); } }")
        request_url = req_info.value.url
        self.assertIn(f"site_id={self._site_a['id']}", request_url)

        rack_names = self._api("GET", request_url.split(BASE, 1)[-1])["results"]
        rack_ids = {r["id"] for r in rack_names}
        self.assertIn(self._rack_a["id"], rack_ids)
        self.assertNotIn(self._rack_b["id"], rack_ids)

        # Pick rack A, then switch the site: the dependent selection clears.
        self._set_tomselect("#id_add_rack", self._rack_a["id"])
        self.assertEqual(self._tomselect_value("#id_add_rack"), str(self._rack_a["id"]))

        self._set_tomselect("#id_add_site", self._site_b["id"])
        self.page.wait_for_function(
            "() => { const el = document.getElementById('id_add_rack');"
            " return el && el.tomselect && el.tomselect.getValue() === ''; }",
            timeout=10000)
        self.assertEqual(
            self._tomselect_value("#id_add_rack"), "",
            "switching the site must clear the previously chosen rack")

    # -- tests: Create-rack dialog -----------------------------------------------

    def _open_create_rack_dialog(self, design, rack_id):
        self._open_racks_section(design, rack_id)
        self.page.click("#nbx-rd-create-planned-rack-btn")
        self.page.wait_for_selector(".nbx-rd-create-rack-modal.show", timeout=10000)

    def test_create_rack_dialog_one_site_shows_chip_and_filtered_locations(self):
        self._open_create_rack_dialog(self._design_one, self._rack_a["id"])

        chip = self.page.query_selector(".nbx-rd-create-rack-site-chip")
        self.assertIsNotNone(chip, "a one-site design's dialog must show a Site chip")
        self.assertIn(self._site_a["name"], chip.inner_text())

        location_select = self.page.query_selector(".nbx-rd-create-rack-location")
        self.assertIsNotNone(location_select)
        self.assertIsNone(
            location_select.get_attribute("disabled"),
            "Location must start enabled -- the one site is already known")
        options_text = location_select.inner_text()
        self.assertIn(self._location_a["name"], options_text)
        self.assertNotIn(self._location_b["name"], options_text)

    def test_create_rack_dialog_two_sites_filters_location_by_site(self):
        self._open_create_rack_dialog(self._design_two, self._rack_a["id"])

        site_select = self.page.query_selector(".nbx-rd-create-rack-site")
        self.assertIsNotNone(site_select, "a two-site design's dialog must show a Site select")
        location_select = self.page.query_selector(".nbx-rd-create-rack-location")
        self.assertIsNotNone(location_select.get_attribute("disabled"))

        self.page.select_option(".nbx-rd-create-rack-site", str(self._site_a["id"]))
        self.page.wait_for_function(
            "() => { const el = document.querySelector('.nbx-rd-create-rack-location');"
            " return el && !el.disabled; }",
            timeout=10000)
        options_text = self.page.inner_text(".nbx-rd-create-rack-location")
        self.assertIn(self._location_a["name"], options_text)
        self.assertNotIn(self._location_b["name"], options_text)

        self.page.select_option(".nbx-rd-create-rack-site", str(self._site_b["id"]))
        self.page.wait_for_function(
            "() => { const el = document.querySelector('.nbx-rd-create-rack-location');"
            " return el && el.value === ''; }",
            timeout=10000)
        options_text = self.page.inner_text(".nbx-rd-create-rack-location")
        self.assertIn(self._location_b["name"], options_text)
        self.assertNotIn(self._location_a["name"], options_text)


    # -- tests: reloading without losing unsaved work ---------------------------

    def _flag_first_device_for_removal(self):
        """Arm the dirty flag through a real gesture: the tile's red x."""
        self.page.evaluate(
            """() => {
                const tile = document.querySelector('.nbx-rd-rack-block .grid-stack-item');
                const btn = tile && tile.querySelector('.nbx-rd-remove-btn');
                if (btn) { btn.click(); }
            }""")
        self.page.wait_for_timeout(400)
        # Read the dirty state the way the UI shows it -- Save arms when the
        # editor has unsaved work -- so this helper works on a build with or
        # without the NbxRdEditor hooks the fix adds.
        armed = self.page.evaluate(
            "() => { const b = document.getElementById('rd-editor-save');"
            " return !!b && !b.hasAttribute('disabled'); }")
        self.assertTrue(armed, "the test needs the editor to be dirty")

    def test_add_rack_with_unsaved_edits_asks_in_the_app_not_the_browser(self):
        """A reload must never hand the planner the browser's own prompt.

        Adding a rack reloads (the server renders the new block). With
        unsaved edits pending that used to trip beforeunload, so Chrome
        asked "Reload site? Changes you made may not be saved." -- whose
        Reload button throws the work away. The editor asks its own
        three-choice question first (user report 2026-09-22).
        """
        self._open_racks_section(self._design_one, self._rack_a["id"])
        self._flag_first_device_for_removal()

        browser_prompts = []
        self.page.on("dialog", lambda d: (browser_prompts.append(d.message), d.dismiss()))

        self._set_tomselect("#id_add_rack", self._rack_a2["id"],
                            query=self._rack_a2["name"])
        self.page.click("#nbx-rd-add-rack-btn", force=True)
        self.page.wait_for_selector(".nbx-rd-switch-modal.show", timeout=5000)

        modal = self.page.query_selector(".nbx-rd-switch-modal")
        self.assertIsNotNone(modal, "the editor's own dialog must appear")
        self.assertIn("Save your changes", modal.inner_text())
        self.assertIn("Save and add", modal.inner_text())
        self.assertEqual(browser_prompts, [],
                         "the browser's beforeunload prompt must not be reached")

        # Cancel leaves everything alone -- and adds nothing.
        self.page.click(".nbx-rd-switch-modal [data-bs-dismiss='modal']", force=True)
        self.page.wait_for_timeout(500)
        racks = self._api("GET",
                          f"/api/plugins/rack-design/designs/{self._design_one['id']}/")["racks"]
        self.assertEqual(len(racks), 1,
                         "Cancel must not add the rack behind the planner's back")

    def test_add_rack_saves_then_reloads_when_asked(self):
        """'Save and add' keeps the work AND lands the rack."""
        self._open_racks_section(self._design_one, self._rack_a["id"])
        self._flag_first_device_for_removal()

        self._set_tomselect("#id_add_rack", self._rack_a2["id"],
                            query=self._rack_a2["name"])
        self.page.click("#nbx-rd-add-rack-btn", force=True)
        self.page.wait_for_selector(".nbx-rd-switch-modal.show", timeout=5000)
        self.page.click(".nbx-rd-switch-modal [data-rd-switch-save]", force=True)
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(1200)

        design = self._api("GET", f"/api/plugins/rack-design/designs/{self._design_one['id']}/")
        self.assertEqual(len(design["racks"]), 2, "the rack must be added")
        placements = self._api(
            "GET", f"/api/plugins/rack-design/placements/?design_id={self._design_one['id']}")
        self.assertGreaterEqual(placements["count"], 1,
                                "the unsaved removal must have been saved, not dropped")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
