#!/usr/bin/env python3
"""Playwright coverage for the editor's Templates tab (PLAN-templates.md
Sec 2/3, T3.3): drag a Template onto a rack, or pick several racks (or a
TemplateGroup's member-to-rack mapping) from a dialog.

Both entry points call the READ-ONLY ``preview-template`` action and hand its
response to the SAME materializer (editor/rack.js's ``stampTemplateItems``),
which adds ordinary unsaved ``add`` tiles -- exactly as if the planner had
dragged each device in by hand (D19: nothing is written until the ordinary
design-level Save). This suite drives:

  * the tab actually lists a Template card;
  * dragging one onto a rack adds unsaved tiles at the positions
    ``compute_stamp`` computes (top-anchored, compacted against the top);
  * Save persists them as real ``DesignPlacement`` rows;
  * Cancel (the tile's own x button, like any other unsaved add) discards
    them without a Save;
  * the click-to-open dialog stamps SEVERAL racks in one action;
  * a rack the template does not fit in is reported in that same dialog,
    never silently dropped.

SELF-PROVISIONING, mirroring test_editor_add_sweep.py's shape: a dedicated
manufacturer/role/site/device-type/two-racks/template fixture is created in
setUpClass and torn down in tearDownClass; each test gets its OWN Design (so
one test's Save can never leak into another's occupancy -- peer designs'
placements never block a stamp anyway, D12, but a SAVED tile in a shared
design would still visually collide with a later test's expected positions).
``p.chromium.launch(channel="chrome")`` -- system Chrome, no download.
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

# Big enough that a 2-device, 1U-each, top-anchored template always lands at
# the very top with room to spare; small enough that a 100U template (used
# for the "does not fit" rack below) obviously cannot.
LARGE_RACK_U = 20
TINY_RACK_U = 1


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
class EditorTemplateTabTestCase(unittest.TestCase):
    """A template card in the left rail stamps a rack; nothing writes until Save."""

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
        mfr = cls._api("POST", "/api/dcim/manufacturers/", {
            "name": f"E2E Template Mfr {suffix}", "slug": f"e2e-tmpl-mfr-{suffix}"})
        site = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E Template Site {suffix}", "slug": f"e2e-tmpl-site-{suffix}",
            "status": "active"})
        dt = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-Tmpl-1U-{suffix}",
            "slug": f"e2e-tmpl-1u-{suffix}", "u_height": 1, "is_full_depth": False})
        rack_large = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E Tmpl Large {suffix}", "site": site["id"],
            "status": "active", "u_height": LARGE_RACK_U})
        rack_tiny = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E Tmpl Tiny {suffix}", "site": site["id"],
            "status": "active", "u_height": TINY_RACK_U})
        # A dedicated third rack (T4.2) for the Save-as-template button tests
        # below -- never touched by the drag/drop stamping tests above, so a
        # real dcim.Device this suite plants in it (to give from-design
        # something to capture) can never collide with their expected
        # U20/U19 tile positions.
        rack_extract = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E Tmpl Extract {suffix}", "site": site["id"],
            "status": "active", "u_height": 10})
        role = cls._api("POST", "/api/dcim/device-roles/", {
            "name": f"E2E Tmpl Role {suffix}", "slug": f"e2e-tmpl-role-{suffix}",
            "color": "2196f3"})

        # The worked example's shape (PLAN-templates.md Sec 3): two top-
        # anchored 1U devices, walked in order -> compacted against the top.
        template = cls._api("POST", "/api/plugins/rack-design/templates/", {
            "name": f"E2E Standard Tmpl {suffix}", "u_height": 10})
        placement_ids = []
        for i in range(2):
            tp = cls._api("POST", "/api/plugins/rack-design/template-placements/", {
                "template": template["id"], "device_type": dt["id"],
                "anchor": "top", "order": i + 1, "face": "front",
            })
            placement_ids.append(tp["id"])

        # A chassis + one blade (T4.2, PLAN-templates.md D10): the SAME
        # nested shape from-design/from-rack extract, built here by hand so
        # the stamp side can be driven without going through extraction.
        chassis_type = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-Tmpl-Chassis-{suffix}",
            "slug": f"e2e-tmpl-chassis-{suffix}", "u_height": 1,
            "subdevice_role": "parent", "is_full_depth": False})
        cls._api("POST", "/api/dcim/device-bay-templates/", {
            "device_type": chassis_type["id"], "name": "bay-a"})
        blade_type = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-Tmpl-Blade-{suffix}",
            "slug": f"e2e-tmpl-blade-{suffix}", "u_height": 0,
            "subdevice_role": "child", "is_full_depth": False})
        chassis_template = cls._api("POST", "/api/plugins/rack-design/templates/", {
            "name": f"E2E Chassis Tmpl {suffix}", "u_height": 5})
        chassis_tp = cls._api("POST", "/api/plugins/rack-design/template-placements/", {
            "template": chassis_template["id"], "device_type": chassis_type["id"],
            "anchor": "top", "order": 1, "face": "front",
        })
        cls._api("POST", "/api/plugins/rack-design/template-placements/", {
            "template": chassis_template["id"], "device_type": blade_type["id"],
            "parent_placement": chassis_tp["id"], "target_bay_name": "bay-a",
        })

        # T3.6 gap 2 (PLAN-templates.md §6 "Still open": "Group apply when
        # there are fewer target racks than members"): a TemplateGroup with
        # FOUR members, one more than this fixture's design ever has racks
        # (three -- rack_large/rack_tiny/rack_extract, set in setUp below).
        # Deliberately its OWN four disposable templates, never the shared
        # `template`/`chassis_template` above -- those two must stay
        # GROUP-LESS so the standalone-card tests (test_tab_lists_the_
        # template etc.) keep seeing them as top-level cards (templates.js's
        # render() only cards a group-less template on its own).
        pod_group = cls._api("POST", "/api/plugins/rack-design/template-groups/", {
            "name": f"E2E Too Big Pod {suffix}",
        })
        pod_member_ids = []
        pod_member_names = []
        for i in range(4):
            member = cls._api("POST", "/api/plugins/rack-design/templates/", {
                "name": f"E2E Pod Member {i + 1} {suffix}", "u_height": 1,
                "group": pod_group["id"], "order": i + 1,
            })
            cls._api("POST", "/api/plugins/rack-design/template-placements/", {
                "template": member["id"], "device_type": dt["id"],
                "anchor": "top", "order": 1, "face": "front",
            })
            pod_member_ids.append(member["id"])
            pod_member_names.append(member["name"])

        cls._created = dict(
            manufacturer=mfr["id"], site=site["id"],
            device_types=[dt["id"], chassis_type["id"], blade_type["id"]],
            device_roles=[role["id"]],
            racks=[rack_large["id"], rack_tiny["id"], rack_extract["id"]],
            templates=[template["id"], chassis_template["id"], *pod_member_ids],
            template_groups=[pod_group["id"]],
            placements=placement_ids,
        )
        cls._site_id = site["id"]
        cls._rack_large_id = rack_large["id"]
        cls._rack_tiny_id = rack_tiny["id"]
        cls._rack_extract_id = rack_extract["id"]
        cls._template_id = template["id"]
        cls._template_name = template["name"]
        cls._role_id = role["id"]
        cls._chassis_template_id = chassis_template["id"]
        cls._chassis_template_name = chassis_template["name"]
        cls._pod_group_id = pod_group["id"]
        cls._pod_group_name = pod_group["name"]
        cls._pod_member_names = pod_member_names

    @classmethod
    def _cleanup_class(cls):
        for design_id in getattr(cls, "_design_ids", []):
            try:
                cls._api("DELETE", f"/api/plugins/rack-design/designs/{design_id}/")
            except Exception:
                pass
        created = getattr(cls, "_created", None)
        if created:
            for group_id in created.get("template_groups", []):
                try:
                    cls._api("DELETE", f"/api/plugins/rack-design/template-groups/{group_id}/")
                except Exception:
                    pass
            for template_id in created.get("templates", []):
                try:
                    cls._api("DELETE", f"/api/plugins/rack-design/templates/{template_id}/")
                except Exception:
                    pass
            for rack_id in created.get("racks", []):
                try:
                    cls._api("DELETE", f"/api/dcim/racks/{rack_id}/")
                except Exception:
                    pass
            for dt_id in created.get("device_types", []):
                try:
                    cls._api("DELETE", f"/api/dcim/device-types/{dt_id}/")
                except Exception:
                    pass
            for role_id in created.get("device_roles", []):
                try:
                    cls._api("DELETE", f"/api/dcim/device-roles/{role_id}/")
                except Exception:
                    pass
            try:
                cls._api("DELETE", f"/api/dcim/sites/{created['site']}/")
            except Exception:
                pass
            try:
                cls._api("DELETE", f"/api/dcim/manufacturers/{created['manufacturer']}/")
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

    # -- per-test: a fresh Design over both racks -----------------------------

    def setUp(self):
        suffix = uuid.uuid4().hex[:8]
        design = self._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"e2e-tmpl-tab-{suffix}", "sites": [self._site_id], "status": "draft",
            "racks": [self._rack_large_id, self._rack_tiny_id, self._rack_extract_id],
        })
        self._design_id = design["id"]
        self._design_ids.append(self._design_id)
        self.editor_url = (
            f"{BASE}/plugins/rack-design/designs/{self._design_id}/editor/")

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

    # -- helpers --------------------------------------------------------------

    def _open_editor(self):
        self.page.goto(self.editor_url, wait_until="networkidle")
        self.page.wait_for_selector("#rd-editor", timeout=30000)
        self.page.evaluate(
            "() => ['djDebug', 'djDebugRoot'].forEach(function (id) {"
            "  const d = document.getElementById(id);"
            "  if (d) { d.style.display = 'none'; d.style.pointerEvents = 'none'; }"
            "})")

    def _open_templates_section(self):
        self._open_editor()
        toggle = self.page.query_selector('[data-rd-section-toggle="templates"]')
        self.assertIsNotNone(toggle, "the Templates section toggle must be rendered")
        if not self.page.is_visible("#nbx-rd-templates-list"):
            toggle.click()
        self.page.wait_for_selector(
            f'[data-rd-template-kind="template"][data-rd-template-id="{self._template_id}"]',
            timeout=15000)

    def _rack_block_selector(self, rack_id):
        return f'.nbx-rd-rack-block[data-rack-id="{rack_id}"]'

    def _template_card_selector(self):
        return f'[data-rd-template-kind="template"][data-rd-template-id="{self._template_id}"]'

    def _chassis_template_card_selector(self):
        return (f'[data-rd-template-kind="template"]'
                f'[data-rd-template-id="{self._chassis_template_id}"]')

    def _pod_group_card_selector(self):
        return f'[data-rd-template-kind="group"][data-rd-template-id="{self._pod_group_id}"]'

    def _save_button_disabled(self):
        return self.page.get_attribute("#rd-editor-save", "disabled") is not None

    def _toast_texts(self):
        # createToast (editor/core.js) never clears old toasts synchronously
        # (each schedules its own 6s auto-dismiss), so every `.toast-body`
        # present right after an action is one this action itself raised.
        return [el.inner_text() for el in self.page.query_selector_all(".toast-body")]

    # -- tests ------------------------------------------------------------------

    def test_tab_lists_the_template(self):
        self._open_templates_section()
        card = self.page.query_selector(self._template_card_selector())
        self.assertIsNotNone(card, "the Templates tab must list the provisioned template")
        self.assertIn(self._template_name, card.inner_text())

    def test_drag_onto_a_rack_adds_unsaved_tiles_at_expected_units(self):
        self._open_templates_section()
        self.assertTrue(self._save_button_disabled(), "Save must start disabled")

        self.page.drag_and_drop(
            self._template_card_selector(),
            self._rack_block_selector(self._rack_large_id),
        )
        self.page.wait_for_selector(
            f'{self._rack_block_selector(self._rack_large_id)} .nbx-rd-state-add',
            timeout=15000)

        tiles = self.page.query_selector_all(
            f'{self._rack_block_selector(self._rack_large_id)} .nbx-rd-state-add')
        self.assertEqual(len(tiles), 2, "both template placements must be stamped")

        # Top-anchored, walked in order, compacted against the top of a
        # 20U EMPTY rack (PLAN-templates.md Sec 3's worked example, scaled):
        # the first lands at U20, the second immediately below it at U19.
        # The `title` attribute lives on the inner `.grid-stack-item-content`
        # (stampTemplateItems/finishAdd both put it there, never on the outer
        # `.grid-stack-item` the `.nbx-rd-state-add` class marks -- see e.g.
        # rack.js's finishAdd, which does the identical
        # `content.setAttribute("title", ...)`), not on the tile element
        # itself, so it must be read off that child.
        titles = " ".join(
            (t.query_selector(".grid-stack-item-content") or t).get_attribute("title") or ""
            for t in tiles
        )
        self.assertIn("U20", titles)
        self.assertIn("U19", titles)

        self.assertFalse(
            self._save_button_disabled(),
            "an unsaved stamp must arm Save exactly like a manual add")

    def test_save_persists_the_stamped_tiles(self):
        self._open_templates_section()
        self.page.drag_and_drop(
            self._template_card_selector(),
            self._rack_block_selector(self._rack_large_id),
        )
        self.page.wait_for_selector(
            f'{self._rack_block_selector(self._rack_large_id)} .nbx-rd-state-add',
            timeout=15000)

        with self.page.expect_navigation(wait_until="networkidle", timeout=20000):
            self.page.click("#rd-editor-save")
        self.page.wait_for_selector(".grid-stack", timeout=30000)

        placements = self._api(
            "GET",
            f"/api/plugins/rack-design/placements/?design_id={self._design_id}",
        )["results"]
        adds = [p for p in placements if p.get("kind") == "add"
                and p.get("target_rack", {}).get("id") == self._rack_large_id]
        self.assertEqual(len(adds), 2, "the Save must persist both stamped devices")

    def test_cancel_discards_the_stamped_tiles(self):
        self._open_templates_section()
        self.page.drag_and_drop(
            self._template_card_selector(),
            self._rack_block_selector(self._rack_large_id),
        )
        self.page.wait_for_selector(
            f'{self._rack_block_selector(self._rack_large_id)} .nbx-rd-state-add',
            timeout=15000)

        # Cancel = the tile's own x button, exactly like any other unsaved
        # add (the brief's "Cancel/undo must work like any other unsaved
        # edit"). Click every one.
        for _ in range(2):
            btn = self.page.query_selector(
                f'{self._rack_block_selector(self._rack_large_id)} .nbx-rd-remove-btn')
            self.assertIsNotNone(btn, "a cancel control must exist on each stamped tile")
            btn.click()

        tiles = self.page.query_selector_all(
            f'{self._rack_block_selector(self._rack_large_id)} .nbx-rd-state-add')
        self.assertEqual(len(tiles), 0, "cancelled adds must leave no tile behind")

        # A fully-cancelled stamp leaves save-layout's payload IDENTICAL to
        # the design's already-saved state (still nothing), so the server
        # legitimately answers 304 Not Modified -- editor.js's doSave()
        # deliberately does NOT navigate/reload on that branch (only on a
        # real 200 save), it just toasts "No changes". Waiting on
        # expect_navigation here would hang for the full timeout: there is
        # no bug to route around, this Save genuinely has nothing to do.
        self.page.click("#rd-editor-save")
        self.page.wait_for_selector(".toast", timeout=10000)
        placements = self._api(
            "GET",
            f"/api/plugins/rack-design/placements/?design_id={self._design_id}",
        )["results"]
        self.assertEqual(
            len(placements), 0,
            "a fully-cancelled stamp must persist nothing at all")

    def test_dialog_stamps_multiple_racks_and_reports_the_one_that_does_not_fit(self):
        self._open_templates_section()
        self.page.click(self._template_card_selector())
        self.page.wait_for_selector(".nbx-rd-apply-template-modal.show", timeout=10000)

        rows_text = self.page.inner_text(".nbx-rd-apply-template-list")
        self.assertIn("doesn't fit", rows_text.lower(),
                      "the rack the template cannot fit into must be REPORTED, not omitted")

        large_row = self.page.query_selector(
            f'.nbx-rd-apply-template-list [data-rd-apply-rack="{self._rack_large_id}"]')
        self.assertIsNotNone(large_row)
        self.assertTrue(large_row.is_checked(),
                        "a rack the template fits must be pre-checked")

        tiny_row = self.page.query_selector(
            f'.nbx-rd-apply-template-list [data-rd-apply-rack="{self._rack_tiny_id}"]')
        self.assertIsNotNone(tiny_row)
        self.assertFalse(tiny_row.is_checked(),
                         "a rack the template does NOT fit must not be silently applied")

        self.page.click("[data-rd-apply-template-confirm]")
        self.page.wait_for_selector(
            f'{self._rack_block_selector(self._rack_large_id)} .nbx-rd-state-add',
            timeout=15000)

        large_tiles = self.page.query_selector_all(
            f'{self._rack_block_selector(self._rack_large_id)} .nbx-rd-state-add')
        self.assertEqual(len(large_tiles), 2,
                          "the fitting rack must receive the full stamp from the dialog")

        tiny_tiles = self.page.query_selector_all(
            f'{self._rack_block_selector(self._rack_tiny_id)} .nbx-rd-state-add')
        self.assertEqual(len(tiny_tiles), 0,
                          "the rack that does not fit must receive nothing")

    # -- "Save rack as template" (PLAN-templates.md Sec 4, T4.2) -------------

    def test_save_as_template_button_creates_template_from_rack(self):
        # A real, already-standing device -- from-design captures the
        # rack's PROJECTED state (D32), not just this design's own adds, so
        # planting it directly via the API (never touching this design at
        # all) is enough to give the button something to extract.
        device = self._api("POST", "/api/dcim/devices/", {
                    # This deployment has a TEXT custom field "warranty_type" whose
                    # default is the boolean False, so NetBox rejects any device
                    # created through the REST API unless it is set explicitly
                    # ("Value must be a string"). Local DCIM data, not a plugin
                    # concern -- but the fixture has to survive it.
                    "custom_fields": {"warranty_type": ""},
            "name": f"E2E SaveAsTmpl Dev {uuid.uuid4().hex[:6]}",
            "site": self._site_id, "device_type": self._created["device_types"][0],
            "role": self._role_id, "rack": self._rack_extract_id,
            # U1, NOT a middle unit. A lone device in the middle of a rack is an
            # ISLAND by definition (dead air on both sides, touching neither
            # physical end), and D17/D32 say a template cannot represent one --
            # extraction correctly reports it as a warning and stores nothing.
            # Anchoring it at the bottom is what makes this a test of the happy
            # path; the island case has its own test below.
            "position": 1, "face": "front", "status": "active",
        })
        new_template_id = None
        try:
            self._open_editor()
            block = self._rack_block_selector(self._rack_extract_id)
            btn = self.page.query_selector(f'{block} [data-rd-save-as-template-btn]')
            self.assertIsNotNone(btn, "every rack block must carry a Save-as-template button")
            btn.click()
            self.page.wait_for_selector(".nbx-rd-apply-template-modal.show", timeout=10000)

            name = f"E2E SaveAsTmpl Result {uuid.uuid4().hex[:6]}"
            self.page.fill("[data-rd-sat-name]", name)
            self.page.click("[data-rd-apply-template-confirm]")
            self.page.wait_for_selector(".toast", timeout=15000)

            texts = " ".join(self._toast_texts())
            self.assertIn(name, texts, "the success toast must name the template just saved")
            self.assertIn("1 device", texts, "placement_count must be reported, not swallowed")

            templates = self._api(
                "GET", f"/api/plugins/rack-design/templates/?name={name}")["results"]
            self.assertEqual(len(templates), 1, "the template must actually exist server-side")
            new_template_id = templates[0]["id"]
            placements = self._api(
                "GET",
                f"/api/plugins/rack-design/template-placements/?template_id={new_template_id}",
            )["results"]
            self.assertEqual(len(placements), 1)
        finally:
            if new_template_id is not None:
                self._api("DELETE", f"/api/plugins/rack-design/templates/{new_template_id}/")
            self._api("DELETE", f"/api/dcim/devices/{device['id']}/")

    def test_save_as_template_surfaces_island_warning(self):
        # Exactly test_template_extraction.py's island fixture (top edge /
        # island / bottom edge), replayed as real devices so the WARNING
        # this button must show, not swallow (D32), has something to fire
        # on: "Island" touches neither physical end of the rack, so it has
        # no (anchor, order) and is left OUT of the template entirely.
        dt = self._created["device_types"][0]
        devices = []
        try:
            for name, pos in (("TopEdge", 10), ("Island", 5), ("BottomEdge", 1)):
                devices.append(self._api("POST", "/api/dcim/devices/", {
                    # This deployment has a TEXT custom field "warranty_type" whose
                    # default is the boolean False, so NetBox rejects any device
                    # created through the REST API unless it is set explicitly
                    # ("Value must be a string"). Local DCIM data, not a plugin
                    # concern -- but the fixture has to survive it.
                    "custom_fields": {"warranty_type": ""},
                    "name": f"E2E {name} {uuid.uuid4().hex[:6]}",
                    "site": self._site_id, "device_type": dt, "role": self._role_id,
                    "rack": self._rack_extract_id, "position": pos, "face": "front",
                    "status": "active",
                }))

            self._open_editor()
            block = self._rack_block_selector(self._rack_extract_id)
            self.page.click(f'{block} [data-rd-save-as-template-btn]')
            self.page.wait_for_selector(".nbx-rd-apply-template-modal.show", timeout=10000)

            name = f"E2E SaveAsTmpl Island {uuid.uuid4().hex[:6]}"
            self.page.fill("[data-rd-sat-name]", name)
            self.page.click("[data-rd-apply-template-confirm]")
            self.page.wait_for_selector(".toast", timeout=15000)

            texts = self._toast_texts()
            self.assertTrue(
                any("island" in t.lower() for t in texts),
                f"an island warning must be surfaced, not swallowed: {texts}")

            templates = self._api(
                "GET", f"/api/plugins/rack-design/templates/?name={name}")["results"]
            self.assertEqual(len(templates), 1)
            self._api("DELETE", f"/api/plugins/rack-design/templates/{templates[0]['id']}/")
        finally:
            for device in devices:
                self._api("DELETE", f"/api/dcim/devices/{device['id']}/")

    # -- Blades on stamp (PLAN-templates.md D10, T4.2) -----------------------

    def test_stamped_chassis_blades_land_in_their_bays(self):
        self._open_templates_section()
        self.page.drag_and_drop(
            self._chassis_template_card_selector(),
            self._rack_block_selector(self._rack_large_id),
        )
        self.page.wait_for_selector(
            f'{self._rack_block_selector(self._rack_large_id)} .nbx-rd-state-add',
            timeout=15000)

        # The blade never took a GridStack slot of its own (D10) -- it must
        # show up as a row inside the CHASSIS tile, not a tile of its own.
        blade_rows = self.page.query_selector_all(
            f'{self._rack_block_selector(self._rack_large_id)} .nbx-rd-stamped-blade')
        self.assertEqual(len(blade_rows), 1, "the chassis's one blade must be placed, not dropped")
        self.assertIn("bay-a", blade_rows[0].inner_text())

        chassis_tiles = self.page.query_selector_all(
            f'{self._rack_block_selector(self._rack_large_id)} .nbx-rd-state-add')
        self.assertEqual(len(chassis_tiles), 1, "only the chassis takes a unit; the blade does not")

        with self.page.expect_navigation(wait_until="networkidle", timeout=20000):
            self.page.click("#rd-editor-save")
        self.page.wait_for_selector(".grid-stack", timeout=30000)

        placements = self._api(
            "GET",
            f"/api/plugins/rack-design/placements/?design_id={self._design_id}",
        )["results"]
        chassis_placements = [
            p for p in placements
            if p.get("kind") == "add"
            and (p.get("device_type") or {}).get("id") == self._created["device_types"][1]
        ]
        self.assertEqual(len(chassis_placements), 1, "the chassis itself must be persisted")
        blade_placements = [
            p for p in placements
            if p.get("kind") == "add"
            and (p.get("device_type") or {}).get("id") == self._created["device_types"][2]
        ]
        self.assertEqual(len(blade_placements), 1, "the blade must be persisted alongside it")
        blade = blade_placements[0]
        self.assertEqual(blade.get("target_bay_name"), "bay-a")
        self.assertEqual(
            (blade.get("parent_placement") or {}).get("id"),
            chassis_placements[0]["id"],
            "the blade must point at the SAME chassis this save just created",
        )

    # -- T3.6 gap 1: warn before re-stamping the same template --------------

    def test_drag_a_second_time_warns_of_the_existing_stamp(self):
        self._open_templates_section()
        self.page.drag_and_drop(
            self._template_card_selector(),
            self._rack_block_selector(self._rack_large_id),
        )
        self.page.wait_for_selector(
            f'{self._rack_block_selector(self._rack_large_id)} .nbx-rd-state-add',
            timeout=15000)

        with self.page.expect_navigation(wait_until="networkidle", timeout=20000):
            self.page.click("#rd-editor-save")
        self.page.wait_for_selector(".grid-stack", timeout=30000)

        # Pin down WHERE a failure is before asserting on the UI: the warning
        # below can only fire if the first stamp actually persisted its D20
        # provenance. Asserting it here separates "save dropped from_template"
        # from "the UI did not render the note" -- two very different bugs,
        # indistinguishable from the toast assertion alone.
        persisted = self._api(
            "GET",
            f"/api/plugins/rack-design/placements/?design_id={self._design_id}"
            f"&from_template_id={self._template_id}",
        )
        all_placements = self._api(
            "GET",
            f"/api/plugins/rack-design/placements/?design_id={self._design_id}",
        )
        self.assertGreater(
            persisted["count"], 0,
            "the first stamp must have persisted from_template on its placements "
            "-- without that the re-stamp warning can never fire. "
            f"(design has {all_placements['count']} placements in total; "
            f"{persisted['count']} carry from_template={self._template_id})",
        )

        # Re-open the Templates tab and drag the SAME template onto the SAME
        # rack a second time. Not blocked -- both copies must land -- but a
        # warning toast must name the prior stamp BEFORE the planner walks
        # away thinking this is the design's only copy.
        self._open_templates_section()
        # Wait for the preview-template RESPONSE, not just for a toast to
        # appear. The toast is rendered from that response, so racing it makes
        # the test fail on a slower machine or a slower NetBox version for no
        # product reason -- observed as a 4.5-only failure whose server
        # response was byte-identical to 4.4's.
        with self.page.expect_response(
            lambda r: "preview-template" in r.url, timeout=30000
        ):
            self.page.drag_and_drop(
                self._template_card_selector(),
                self._rack_block_selector(self._rack_large_id),
            )
        self.page.wait_for_selector(".toast", timeout=30000)
        texts = " ".join(self._toast_texts()).lower()
        self.assertIn("already has", texts,
                      "re-stamping the same template must warn, not silently duplicate")
        self.assertIn("2 device", texts, "the warning must NAME how many placements exist")
        # The warning must NAME the version recorded on the existing placements.
        # Do NOT hardcode 1: Template.version is bumped by every
        # TemplatePlacement.save(), so a fixture template with N items is
        # already at version N+1 before anything is ever stamped. Read the
        # live value and assert the warning quotes THAT.
        stamped_version = self._api(
            "GET", f"/api/plugins/rack-design/templates/{self._template_id}/"
        )["version"]
        self.assertIn(
            f"version {stamped_version}", texts,
            "the warning must NAME the recorded from_template_version")

        # BOTH copies must be present: 2 from the saved first stamp plus 2 from
        # this one. The warning is informational (D19/T3.6) -- two identical ToR
        # blocks in one rack can be a legitimate ask, so nothing is blocked and
        # nothing is de-duplicated.
        tiles = self.page.query_selector_all(
            f'{self._rack_block_selector(self._rack_large_id)} .nbx-rd-state-add')
        self.assertEqual(
            len(tiles), 4,
            "the second stamp is NOT blocked -- two identical ToR blocks can be a legitimate ask")

    def test_single_template_dialog_shows_the_existing_stamp_note(self):
        self._open_templates_section()
        self.page.drag_and_drop(
            self._template_card_selector(),
            self._rack_block_selector(self._rack_large_id),
        )
        self.page.wait_for_selector(
            f'{self._rack_block_selector(self._rack_large_id)} .nbx-rd-state-add',
            timeout=15000)
        with self.page.expect_navigation(wait_until="networkidle", timeout=20000):
            self.page.click("#rd-editor-save")
        self.page.wait_for_selector(".grid-stack", timeout=30000)

        self._open_templates_section()
        # Same reasoning as the drag test above: the dialog's rows are rendered
        # from the preview-template response, so wait for the response rather
        # than for the modal element, which appears before the rows are filled.
        with self.page.expect_response(
            lambda r: "preview-template" in r.url, timeout=30000
        ):
            self.page.click(self._template_card_selector())
        self.page.wait_for_selector(".nbx-rd-apply-template-modal.show", timeout=30000)
        self.page.wait_for_selector(
            ".nbx-rd-apply-template-list .list-group-item", timeout=30000)

        rows_text = self.page.inner_text(".nbx-rd-apply-template-list").lower()
        self.assertIn(
            "already has", rows_text,
            "the checklist dialog must show the existing-stamp note BEFORE Apply is clicked")

    # -- T3.6 gap 2: group apply with fewer target racks than members -------

    def test_group_dialog_refuses_when_fewer_racks_than_members(self):
        # This fixture's design (setUp) always has exactly THREE racks
        # (large/tiny/extract); `_pod_group_id` was provisioned with FOUR
        # members specifically so this never needs a design-specific rack
        # count tweak. D14: an ordered correspondence with more members than
        # racks cannot be built without reusing a rack for two members, so
        # the group dialog must refuse to even open rather than show a
        # (silently doubled-up) mapping table.
        self._open_templates_section()
        self.page.click(self._pod_group_card_selector())
        self.page.wait_for_timeout(1000)

        self.assertIsNone(
            self.page.query_selector(".nbx-rd-apply-template-modal.show"),
            "the group dialog must NOT open when there are fewer racks than members")

        texts = " ".join(self._toast_texts())
        for member_name in self._pod_member_names[3:]:
            self.assertIn(
                member_name, texts,
                "the refusal must NAME every member left without a target rack")

    def test_group_dialog_shows_the_existing_stamp_note_per_row(self):
        # Give the design a FOURTH rack so the four-member pod group can
        # actually map (this test's own concern is the per-row note, not
        # gap 2's refusal, which the test above already covers).
        extra_rack = self._api("POST", "/api/dcim/racks/", {
            "name": f"E2E Tmpl Pod Extra {uuid.uuid4().hex[:8]}",
            "site": self._site_id, "status": "active", "u_height": LARGE_RACK_U,
        })
        self._api("PATCH", f"/api/plugins/rack-design/designs/{self._design_id}/", {
            "racks": [
                self._rack_large_id, self._rack_tiny_id, self._rack_extract_id,
                extra_rack["id"],
            ],
        })
        try:
            # Pre-stamp the group's first member onto rack_large directly via
            # the read-only preview + an ordinary save-layout write, so the
            # dialog opened below has an existing stamp to report.
            preview = self._api("POST", (
                f"/api/plugins/rack-design/designs/{self._design_id}/preview-template/"
            ), {"group": self._pod_group_id, "racks": [
                f"r:{self._rack_large_id}", f"r:{self._rack_tiny_id}",
                f"r:{self._rack_extract_id}", f"r:{extra_rack['id']}",
            ]})
            first_member_key = f"r:{self._rack_large_id}"
            entry = preview[first_member_key][0]
            self._api("POST", (
                f"/api/plugins/rack-design/designs/{self._design_id}/save-layout/"
            ), {
                "design_id": self._design_id,
                "racks": [{
                    "rack_id": self._rack_large_id,
                    "front": [{
                        "kind": "add", "device_type_id": entry["device_type"],
                        "u_position": entry["position"], "face": entry["face"],
                        "from_template_id": entry["from_template"],
                        "from_template_version": entry["from_template_version"],
                    }],
                }],
            })

            self._open_templates_section()
            self.page.click(self._pod_group_card_selector())
            self.page.wait_for_selector(".nbx-rd-apply-group-table", timeout=10000)

            # The dialog's default row->rack mapping is positional (row i ->
            # the i-th rack in DOM order), which this test does not control.
            # Rather than rely on that default landing on rack_large, drive
            # the FIRST member's row select to rack_large explicitly -- the
            # same action a planner takes when confirming/adjusting the
            # mapping -- so this test asserts the note appears once the row
            # actually targets the rack the pre-stamp above used.
            first_row = self.page.query_selector_all(
                ".nbx-rd-apply-group-table tbody tr")[0]
            self.assertIn(self._pod_member_names[0], first_row.inner_text())
            first_row.query_selector("select").select_option(str(self._rack_large_id))
            self.page.wait_for_timeout(1500)  # let that row's own preview land

            row_text = first_row.inner_text().lower()
            self.assertIn(
                "already has", row_text,
                "a group row targeting a rack that already carries this member "
                "must show the existing-stamp note before Apply")
        finally:
            self._api("DELETE", f"/api/dcim/racks/{extra_rack['id']}/")


if __name__ == "__main__":
    unittest.main()
