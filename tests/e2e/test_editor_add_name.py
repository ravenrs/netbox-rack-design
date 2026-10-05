#!/usr/bin/env python3
"""Playwright e2e: a SAVED add keeps its name editable after a reload.

A fresh palette drop gets a hidden name input + pencil (rack.js finishAdd), but
an add rehydrated from the server used to get only the planning-attributes
button -- and on a 1U tile that button sits over the pencil's corner anyway. So
the reloaded tile carries the same name input, and the planning-attributes
dialog offers the name too, written back through that input.

SELF-PROVISIONING: setUpClass creates a site / rack / device type / design and
one saved `add` placement over the REST API, and deletes them afterwards.
Run via ``dev/e2e.sh tests.e2e.test_editor_add_name``.
"""
import json
import os
import unittest
import uuid

from tests.e2e.test_editor_pdu_power import _PREREQ_OK, _PREREQ_REASON

BASE = os.environ.get("RD_BASE", "http://127.0.0.1:8000").rstrip("/")
USER = os.environ.get("RD_USER", "rd_shot")
PASS = os.environ.get("RD_PASS", "ShotPass12345!")


@unittest.skipUnless(_PREREQ_OK, f"add-name e2e prerequisites not met: {_PREREQ_REASON}")
class EditorAddNameTestCase(unittest.TestCase):
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
        cls._created = []
        mfr = cls._api("POST", "/api/dcim/manufacturers/", {
            "name": f"E2E AN Mfr {suffix}", "slug": f"e2e-an-mfr-{suffix}"})
        cls._created.append(f"/api/dcim/manufacturers/{mfr['id']}/")
        site = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E AN Site {suffix}", "slug": f"e2e-an-site-{suffix}", "status": "active"})
        cls._created.append(f"/api/dcim/sites/{site['id']}/")
        dt = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-AN-1U-{suffix}",
            "slug": f"e2e-an-1u-{suffix}", "u_height": 1})
        cls._created.append(f"/api/dcim/device-types/{dt['id']}/")
        rack = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E AN Rack {suffix}", "site": site["id"], "status": "active", "u_height": 10})
        cls._created.append(f"/api/dcim/racks/{rack['id']}/")
        role = cls._api("GET", "/api/dcim/device-roles/?limit=1")["results"][0]
        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"an-{suffix}", "sites": [site["id"]], "racks": [rack["id"]]})
        cls._design_id = design["id"]
        cls._api("POST", "/api/plugins/rack-design/placements/", {
            "design": design["id"], "kind": "add", "device_type": dt["id"],
            "device_role": role["id"], "proposed_name": f"an-old-{suffix}",
            "target_rack": rack["id"], "target_position": 3, "target_face": "front"})
        cls._old_name = f"an-old-{suffix}"
        cls.editor_url = f"{BASE}/plugins/rack-design/designs/{design['id']}/editor/{rack['id']}/"

    @classmethod
    def _cleanup_class(cls):
        try:
            if getattr(cls, "_design_id", None) is not None:
                try:
                    cls._api("DELETE", f"/api/plugins/rack-design/designs/{cls._design_id}/")
                except Exception:
                    pass
            for path in reversed(getattr(cls, "_created", None) or []):
                try:
                    cls._api("DELETE", path)
                except Exception:
                    pass
        finally:
            for closer in (lambda: cls._api_ctx.close(),
                           lambda: cls._browser.close(),
                           lambda: cls._pw.stop()):
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
                (c["value"] for c in cls._api_ctx.cookies() if c["name"] == "csrftoken"), "")
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
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(f"PAGEERROR: {e}"))
        resp = self.page.goto(self.editor_url, wait_until="networkidle")
        self.assertEqual(resp.status, 200)
        self.page.wait_for_selector(".nbx-rd-rack-block", timeout=15000)
        self.page.wait_for_timeout(400)
        # The fixture rack holds this one add and nothing else.
        self.tile = self.page.locator(".nbx-rd-state-add > .grid-stack-item-content").first

    def tearDown(self):
        if getattr(self, "ctx", None):
            self.ctx.close()

    def _shown_name(self):
        return self.tile.evaluate(
            "c => (c.querySelector('.nbx-rd-name-display') || c.querySelector('.nbx-rd-label'))"
            ".textContent")

    def test_pencil_renames_a_reloaded_add(self):
        self.tile.hover()
        self.tile.locator(".nbx-rd-name-edit-btn").click()
        name_input = self.tile.locator(".nbx-rd-name-input")
        name_input.fill("an-renamed")
        name_input.press("Enter")
        self.assertEqual(self._shown_name(), "an-renamed")
        self.assertEqual(self.errors, [])

    def test_planning_dialog_renames_the_add(self):
        btn = self.tile.locator(".nbx-rd-placement-btn")
        if not btn.count():
            self.skipTest("this deployment declares no placement_fields")
        btn.click()
        modal = self.page.locator(".nbx-rd-placement-modal.show")
        modal.wait_for()
        name_field = modal.locator(".nbx-rd-placement-name")
        self.assertEqual(name_field.input_value(), self._old_name)
        name_field.fill("an-from-dialog")
        modal.locator("[data-rd-placement-confirm]").click()
        self.page.wait_for_timeout(400)
        self.assertEqual(self._shown_name(), "an-from-dialog")
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
