#!/usr/bin/env python3
"""Playwright e2e for the Design "Execution plan" tab (PLAN-execution-steps.md
Sec. 6): a rack near its power cap goes red when two servers are racked before
the old one leaves, and green again once the remove is dragged into step 1.

SELF-PROVISIONING: setUpClass builds its own site / rack / device type / three
devices / design with five placements over the REST API, plus a two-step plan
(adds first, remove second). Everything is deleted in tearDownClass. The rack's
capacity is the plugin default (1000 W): 2 devices at 300 W + 2 adds = 1200 W.

Skips cleanly when playwright/Chrome or the dev server is unavailable.
Run via ``dev/e2e.sh tests.e2e.test_execution_plan``.
"""
import json
import os
import unittest
import urllib.error
import urllib.request
import uuid

from tests.e2e.helpers import pick_role

BASE = os.environ.get("RD_BASE", "http://127.0.0.1:8000").rstrip("/")
USER = os.environ.get("RD_USER", "rd_shot")
PASS = os.environ.get("RD_PASS", "ShotPass12345!")


def _check_prereqs():
    try:
        import playwright.sync_api  # noqa: F401
    except Exception as exc:  # pragma: no cover
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
            p.chromium.launch(channel="chrome", headless=True).close()
    except Exception as exc:  # pragma: no cover
        return False, f"headless Chrome unavailable ({exc})"
    return True, ""


_PREREQ_OK, _PREREQ_REASON = _check_prereqs()


@unittest.skipUnless(_PREREQ_OK, f"execution-plan e2e prerequisites not met: {_PREREQ_REASON}")
class ExecutionPlanTestCase(unittest.TestCase):
    @classmethod
    def _api(cls, method, path, payload=None):
        kwargs = {"method": method,
                  "headers": {"X-CSRFToken": cls._csrf, "Accept": "application/json"}}
        if payload is not None:
            kwargs["data"] = payload
        resp = cls._api_ctx.request.fetch(f"{BASE}{path}", **kwargs)
        body = resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"{method} {path} -> HTTP {resp.status}: {body[:500]}")
        return json.loads(body) if body.strip() else None

    @classmethod
    def _provision(cls):
        sfx = uuid.uuid4().hex[:8]
        cls._created = {}
        mfr = cls._api("POST", "/api/dcim/manufacturers/",
                       {"name": f"E2E EP Mfr {sfx}", "slug": f"e2e-ep-mfr-{sfx}"})
        cls._created["manufacturer"] = mfr["id"]
        role = cls._api("POST", "/api/dcim/device-roles/", {
            "name": f"E2E EP Role {sfx}", "slug": f"e2e-ep-role-{sfx}", "color": "9e9e9e"})
        cls._created["role"] = role["id"]
        site = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E EP Site {sfx}", "slug": f"e2e-ep-site-{sfx}", "status": "active"})
        cls._created["site"] = site["id"]
        dt = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-EP-Srv-{sfx}", "slug": f"e2e-ep-srv-{sfx}",
            "u_height": 1, "is_full_depth": False})
        cls._created["device_type"] = dt["id"]
        cls._api("POST", "/api/dcim/power-port-templates/", {
            "device_type": dt["id"], "name": "psu1", "allocated_draw": 300, "maximum_draw": 300})
        rack = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E EP Rack {sfx}", "site": site["id"], "status": "active", "u_height": 20})
        cls._created["rack"] = rack["id"]
        rack2 = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E EP Rack B {sfx}", "site": site["id"], "status": "active", "u_height": 20})
        cls._created["rack2"] = rack2["id"]
        cf = {"custom_fields": {"warranty_type": ""}}
        devices = []
        for name, pos in ((f"ep-base-{sfx}", 1), (f"ep-old-{sfx}", 3)):
            dev = cls._api("POST", "/api/dcim/devices/", {
                "name": name, "device_type": dt["id"], "role": role["id"], "site": site["id"],
                "rack": rack["id"], "position": f"{pos}.0", "face": "front",
                "status": "active", **cf})
            devices.append(dev["id"])
        cls._created["devices"] = devices
        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"ep-{sfx}", "sites": [site["id"]], "racks": [rack["id"], rack2["id"]]})
        cls._design_id = design["id"]

        def placement(**kw):
            return cls._api("POST", "/api/plugins/rack-design/placements/",
                            {"design": design["id"], **kw})["id"]

        def add(name, pos):
            return placement(kind="add", device_type=dt["id"], device_role=role["id"],
                             target_rack=rack["id"], target_position=pos,
                             target_face="front", proposed_name=name)

        cls.add1 = add(f"ep-new-1-{sfx}", 10)
        cls.add2 = add(f"ep-new-2-{sfx}", 12)
        cls.add3 = placement(kind="add", device_type=dt["id"], device_role=role["id"],
                             target_rack=rack2["id"], target_position=5,
                             target_face="front", proposed_name=f"ep-new-3-{sfx}")
        cls.remove = placement(kind="remove", device=devices[1])
        cls._api("POST", f"/api/plugins/rack-design/designs/{design['id']}/save-steps/", {
            "steps": [
                {"id": None, "title": "Racking", "placements": [cls.add1, cls.add2, cls.add3]},
                {"id": None, "title": "Decommission", "placements": [cls.remove]},
            ]})
        cls.url = f"{BASE}/plugins/rack-design/designs/{design['id']}/execution-plan/"

    @classmethod
    def _cleanup(cls):
        c = getattr(cls, "_created", None) or {}
        steps = []
        if getattr(cls, "_design_id", None) is not None:
            steps.append(f"/api/plugins/rack-design/designs/{cls._design_id}/")
        steps += [f"/api/dcim/devices/{d}/" for d in c.get("devices", [])]
        for key, path in (("rack", "racks"), ("rack2", "racks"), ("device_type", "device-types"),
                          ("role", "device-roles"), ("manufacturer", "manufacturers"),
                          ("site", "sites")):
            if c.get(key) is not None:
                steps.append(f"/api/dcim/{path}/{c[key]}/")
        for path in steps:
            try:
                cls._api("DELETE", path)
            except Exception:
                pass
        for closer in (lambda: cls._api_ctx.close(), lambda: cls._browser.close(),
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
        cls._created = {}
        cls._api_ctx = cls._browser.new_context(viewport={"width": 1600, "height": 1200})
        try:
            pg = cls._api_ctx.new_page()
            pg.goto(f"{BASE}/login/", wait_until="networkidle")
            pg.fill("#id_username", USER)
            pg.fill("#id_password", PASS)
            pg.click("button[type=submit]")
            pg.wait_for_load_state("networkidle")
            pg.close()
            cls._csrf = next((c["value"] for c in cls._api_ctx.cookies()
                              if c["name"] == "csrftoken"), "")
            cls._provision()
        except BaseException:
            cls._cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()

    def _idle(self, page):
        page.wait_for_function(
            "document.querySelector('[data-rd-plan-root]').dataset.rdPlanBusy === '0'",
            timeout=20000)

    def _red(self, page, step):
        return page.locator(
            f'[data-rd-step][data-step-index="{step}"] .rd-plan-badge-error').count()

    def test_drag_remove_into_step_one_clears_the_red_badge(self):
        page = self._api_ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(self.url, wait_until="networkidle")
            page.wait_for_selector("[data-rd-step]", timeout=15000)
            self._idle(page)
            page.wait_for_selector('[data-step-index="1"] [data-rd-rack] .nbx-rd-power-bar')
            # Adds first: the rack peaks at 1200 of 1000 W in step 1.
            self.assertGreaterEqual(self._red(page, 1), 1)
            page.locator('[data-step-index="1"] [data-code="rack_over"]').first.wait_for()
            # The detail text is inline and visible without hovering the badge.
            detail = page.locator('[data-step-index="1"] [data-rd-detail]').first
            self.assertTrue(detail.is_visible())
            self.assertTrue("W of" in detail.inner_text() or "U" in detail.inner_text(),
                            detail.inner_text())

            row = page.locator(f'[data-rd-action="{self.remove}"]')
            row.drag_to(page.locator('[data-step-index="1"] [data-rd-drop]'))
            # Grey right away ...
            page.wait_for_selector(".rd-plan-outdated", timeout=2000)
            self._idle(page)
            # ... then repainted: remove first, 900 W, no red.
            self.assertEqual(self._red(page, 1), 0)
            self.assertEqual(self._red(page, 2), 0)
            self.assertEqual(page.locator(".rd-plan-outdated").count(), 0)

            # Persisted: a reload shows the remove in step 1.
            page.reload(wait_until="networkidle")
            page.wait_for_selector("[data-rd-step]")
            self._idle(page)
            self.assertEqual(page.locator(
                f'[data-step-index="1"] [data-rd-action="{self.remove}"]').count(), 1)

            # Back after step 1 (its old step 2 vanished when emptied): a new
            # step 2 appears and the red returns in step 1.
            page.locator(f'[data-rd-action="{self.remove}"]').drag_to(
                page.locator('[data-rd-gap="1"]'))
            self._idle(page)
            self.assertGreaterEqual(self._red(page, 1), 1)
            self.assertEqual(errors, [])
        finally:
            page.close()

    # ---- steps as units: auto-order, step drag, gap drop ------------------

    def _mini_design(self, layout):
        """New design over the shared rack with three placements: two adds and
        a remove. ``layout(a1, a2, rm)`` returns the save-steps payload."""
        c = self._created
        sfx = uuid.uuid4().hex[:6]
        did = self._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"ep-mini-{sfx}", "racks": [c["rack"]], "sites": [c["site"]]})["id"]

        def pl(**kw):
            return self._api("POST", "/api/plugins/rack-design/placements/",
                             {"design": did, **kw})["id"]

        def add(name, pos):
            return pl(kind="add", device_type=c["device_type"], device_role=c["role"],
                      target_rack=c["rack"], target_position=pos, target_face="front",
                      proposed_name=name)

        a1, a2 = add(f"mini-1-{sfx}", 14), add(f"mini-2-{sfx}", 16)
        rm = pl(kind="remove", device=c["devices"][1])
        self._api("POST", f"/api/plugins/rack-design/designs/{did}/save-steps/",
                  {"steps": layout(a1, a2, rm)})
        return did, a1, a2, rm

    def _open_mini(self, did, errors):
        page = self._api_ctx.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{BASE}/plugins/rack-design/designs/{did}/execution-plan/",
                  wait_until="networkidle")
        page.wait_for_selector("[data-rd-step]", timeout=15000)
        self._idle(page)
        self._hide_djdt(page)
        return page

    @staticmethod
    def _hide_djdt(page):
        page.evaluate(
            "() => ['djDebug', 'djDebugRoot'].forEach(function (id) {"
            "  const d = document.getElementById(id);"
            "  if (d) { d.style.display = 'none'; d.style.pointerEvents = 'none'; }"
            "})")

    def _step_actions(self, page):
        return page.eval_on_selector_all(
            "[data-rd-step]",
            "els => els.map(s => [...s.querySelectorAll('[data-rd-action]')]"
            ".map(a => Number(a.dataset.rdAction)))")

    def _titles(self, page):
        return page.eval_on_selector_all("[data-rd-title]", "els => els.map(e => e.value)")

    def _drop_design(self, did):
        try:
            self._api("DELETE", f"/api/plugins/rack-design/designs/{did}/")
        except Exception:
            pass

    def test_auto_order_reorders_whole_steps_and_never_merges(self):
        did, a1, a2, rm = self._mini_design(lambda a1, a2, rm: [
            {"id": None, "title": "Rack one", "placements": [a1]},
            {"id": None, "title": "Rack two", "placements": [a2]},
            {"id": None, "title": "Pull old", "placements": [rm]}])
        errors = []
        page = self._open_mini(did, errors)
        try:
            self.assertEqual(self._step_actions(page), [[a1], [a2], [rm]])
            page.click("[data-rd-plan-auto]")
            page.wait_for_function(
                "document.querySelectorAll('[data-rd-step]')[0]"
                ".querySelector('[data-kind=remove]') !== null", timeout=10000)
            self._idle(page)
            self.assertEqual(self._step_actions(page), [[rm], [a1], [a2]])
            self.assertEqual(self._titles(page), ["Pull old", "Rack one", "Rack two"])
            page.reload(wait_until="networkidle")
            page.wait_for_selector("[data-rd-step]")
            self._idle(page)
            self.assertEqual(self._step_actions(page), [[rm], [a1], [a2]])
            self.assertEqual(errors, [])
        finally:
            page.close()
            self._drop_design(did)

    def test_auto_order_fixes_a_pdu_swap_by_simulating(self):
        """The PDU is removed first while dual-PSU servers stay cabled to it
        (redundancy_lost warning). Auto-order, asked of the server, puts the PDU
        removal last and every step reads OK."""
        sfx = uuid.uuid4().hex[:6]
        made = []  # (path, id) in creation order; deleted in reverse

        def mk(path, payload, track=True):
            obj = self._api("POST", path, payload)
            if track:
                made.append((path, obj["id"]))
            return obj

        did = None
        errors = []
        try:
            c = self._created
            mfr = mk("/api/dcim/manufacturers/", {"name": f"E2E SW Mfr {sfx}", "slug": f"e2e-sw-mfr-{sfx}"})
            found = self._api("GET", "/api/dcim/device-roles/?slug=pdu")
            pdu_role = found["results"][0] if found["count"] else mk(
                "/api/dcim/device-roles/", {"name": f"E2E SW PDU {sfx}", "slug": "pdu", "color": "9e9e9e"})
            srv_type = mk("/api/dcim/device-types/", {
                "manufacturer": mfr["id"], "model": f"E2E-SW-Srv-{sfx}", "slug": f"e2e-sw-srv-{sfx}",
                "u_height": 1, "is_full_depth": False})
            for name in ("psu1", "psu2"):
                self._api("POST", "/api/dcim/power-port-templates/", {
                    "device_type": srv_type["id"], "name": name,
                    "allocated_draw": 100, "maximum_draw": 100})
            pdu_type = mk("/api/dcim/device-types/", {
                "manufacturer": mfr["id"], "model": f"E2E-SW-PDU-{sfx}", "slug": f"e2e-sw-pdu-{sfx}",
                "u_height": 0})
            self._api("POST", "/api/dcim/power-port-templates/", {
                "device_type": pdu_type["id"], "name": "Input"})
            for name in ("1", "2"):
                self._api("POST", "/api/dcim/power-outlet-templates/", {
                    "device_type": pdu_type["id"], "name": name})
            panel = mk("/api/dcim/power-panels/", {"site": c["site"], "name": f"E2E SW Panel {sfx}"})
            rack = mk("/api/dcim/racks/", {
                "name": f"E2E SW Rack {sfx}", "site": c["site"], "status": "active", "u_height": 20})

            def cable(a_type, a_id, b_type, b_id):
                mk("/api/dcim/cables/", {
                    "a_terminations": [{"object_type": a_type, "object_id": a_id}],
                    "b_terminations": [{"object_type": b_type, "object_id": b_id}]}, track=False)

            def pdu(label):
                feed = mk("/api/dcim/power-feeds/", {
                    "power_panel": panel["id"], "name": f"E2E SW Feed {label} {sfx}",
                    "voltage": 230, "amperage": 32, "phase": "single-phase"})
                dev = mk("/api/dcim/devices/", {
                    "name": f"sw-pdu-{label}-{sfx}", "device_type": pdu_type["id"],
                    "role": pdu_role["id"], "site": c["site"], "rack": rack["id"],
                    "status": "active"})
                port = self._api("GET", f"/api/dcim/power-ports/?device_id={dev['id']}")["results"][0]
                cable("dcim.powerport", port["id"], "dcim.powerfeed", feed["id"])
                outs = {o["name"]: o["id"] for o in self._api(
                    "GET", f"/api/dcim/power-outlets/?device_id={dev['id']}")["results"]}
                return dev, outs

            pdu_l, out_l = pdu("l")
            pdu_r, out_r = pdu("r")

            def server(label, pos, outlet):
                dev = mk("/api/dcim/devices/", {
                    "name": f"sw-srv-{label}-{sfx}", "device_type": srv_type["id"],
                    "role": c["role"], "site": c["site"], "rack": rack["id"],
                    "position": f"{pos}.0", "face": "front", "status": "active",
                    "custom_fields": {"warranty_type": ""}})
                ports = {q["name"]: q["id"] for q in self._api(
                    "GET", f"/api/dcim/power-ports/?device_id={dev['id']}")["results"]}
                cable("dcim.powerport", ports["psu1"], "dcim.poweroutlet", out_l[outlet])
                cable("dcim.powerport", ports["psu2"], "dcim.poweroutlet", out_r[outlet])
                return dev

            s1, s2 = server("1", 2, "1"), server("2", 4, "2")
            did = self._api("POST", "/api/plugins/rack-design/designs/", {
                "title": f"ep-swap-{sfx}", "racks": [rack["id"]], "sites": [c["site"]]})["id"]
            ids = [self._api("POST", "/api/plugins/rack-design/placements/",
                             {"design": did, "kind": "remove", "device": d["id"]})["id"]
                   for d in (pdu_l, s1, s2)]
            self._api("POST", f"/api/plugins/rack-design/designs/{did}/save-steps/", {
                "steps": [{"id": None, "title": t, "placements": [i]}
                          for t, i in zip(("Pull PDU", "Pull srv 1", "Pull srv 2"), ids)]})

            page = self._open_mini(did, errors)
            try:
                self.assertEqual(self._step_actions(page), [[ids[0]], [ids[1]], [ids[2]]])
                self.assertEqual(page.locator('[data-rd-step-status="ok"]').count() < 3, True)
                page.click("[data-rd-plan-auto]")
                page.wait_for_function(
                    "document.querySelectorAll('[data-rd-step]')[2]"
                    ".querySelector('[data-rd-action=\"%d\"]') !== null" % ids[0], timeout=15000)
                self._idle(page)
                self.assertEqual(self._step_actions(page), [[ids[1]], [ids[2]], [ids[0]]])
                self.assertEqual(self._titles(page), ["Pull srv 1", "Pull srv 2", "Pull PDU"])
                self.assertEqual(page.locator('[data-rd-step-status="ok"]').count(), 3)
                page.reload(wait_until="networkidle")
                page.wait_for_selector("[data-rd-step]")
                self._idle(page)
                self.assertEqual(self._step_actions(page), [[ids[1]], [ids[2]], [ids[0]]])
                self.assertEqual(errors, [])
            finally:
                page.close()
        finally:
            if did is not None:
                self._drop_design(did)
            for path, oid in reversed(made):
                try:
                    self._api("DELETE", f"{path}{oid}/")
                except Exception:
                    pass

    def test_drag_step_by_its_handle_reorders_and_persists(self):
        did, a1, a2, rm = self._mini_design(lambda a1, a2, rm: [
            {"id": None, "title": "S1", "placements": [a1]},
            {"id": None, "title": "S2", "placements": [a2]},
            {"id": None, "title": "S3", "placements": [rm]}])
        errors = []
        page = self._open_mini(did, errors)
        try:
            page.locator('[data-step-index="3"] [data-rd-step-handle]').drag_to(
                page.locator('[data-rd-gap="0"]'))
            page.wait_for_function(
                "document.querySelector('[data-step-index=\"1\"] [data-rd-title]').value === 'S3'",
                timeout=5000)
            self._idle(page)
            self.assertEqual(self._titles(page), ["S3", "S1", "S2"])
            self.assertEqual(self._step_actions(page), [[rm], [a1], [a2]])
            page.reload(wait_until="networkidle")
            page.wait_for_selector("[data-rd-step]")
            self._idle(page)
            self.assertEqual(self._titles(page), ["S3", "S1", "S2"])
            # Keyboard: Arrow down on the first handle moves that step down.
            page.locator('[data-step-index="1"] [data-rd-step-handle]').focus()
            page.keyboard.press("ArrowDown")
            self._idle(page)
            self.assertEqual(self._titles(page), ["S1", "S3", "S2"])
            self.assertEqual(errors, [])
        finally:
            page.close()
            self._drop_design(did)

    def test_drop_action_between_steps_creates_a_step_and_drops_emptied_ones(self):
        did, a1, a2, rm = self._mini_design(lambda a1, a2, rm: [
            {"id": None, "title": "Racking", "placements": [a1, a2]},
            {"id": None, "title": "Pull old", "placements": [rm]}])
        errors = []
        page = self._open_mini(did, errors)
        try:
            page.locator(f'[data-rd-action="{a2}"]').drag_to(page.locator('[data-rd-gap="1"]'))
            page.wait_for_function(
                "document.querySelectorAll('[data-rd-step]').length === 3", timeout=5000)
            self._idle(page)
            self.assertEqual(self._step_actions(page), [[a1], [a2], [rm]])
            # a1 sits alone in step 1: dropping it between steps 2 and 3 empties
            # step 1, which disappears.
            page.locator(f'[data-rd-action="{a1}"]').drag_to(page.locator('[data-rd-gap="2"]'))
            page.wait_for_function(
                "document.querySelector('[data-step-index=\"1\"] [data-rd-action=\"%d\"]') !== null"
                % a2, timeout=5000)
            self._idle(page)
            self.assertEqual(self._step_actions(page), [[a2], [a1], [rm]])
            page.reload(wait_until="networkidle")
            page.wait_for_selector("[data-rd-step]")
            self._idle(page)
            self.assertEqual(self._step_actions(page), [[a2], [a1], [rm]])
            self.assertEqual(errors, [])
        finally:
            page.close()
            self._drop_design(did)

    # ---- reset plan + save indicator ---------------------------------------

    def _status(self, page):
        return page.locator("[data-rd-plan-save-status]")

    def test_reset_plan_returns_to_creation_order_and_cancel_keeps_it(self):
        did, a1, a2, rm = self._mini_design(lambda a1, a2, rm: [
            {"id": None, "title": "Pull old", "placements": [rm]},
            {"id": None, "title": "Custom", "placements": [a2]},
            {"id": None, "title": "Rack one", "placements": [a1]}])
        errors = []
        page = self._open_mini(did, errors)
        try:
            self.assertEqual(self._step_actions(page), [[rm], [a2], [a1]])
            # Cancel leaves the plan alone.
            page.once("dialog", lambda d: d.dismiss())
            page.click("[data-rd-plan-reset]")
            self._idle(page)
            self.assertEqual(self._step_actions(page), [[rm], [a2], [a1]])
            self.assertEqual(self._titles(page), ["Pull old", "Custom", "Rack one"])
            # Confirm: creation order (a1, a2, rm) with default titles.
            msgs = []

            def accept(d):
                msgs.append(d.message)
                d.accept()

            page.once("dialog", accept)
            page.click("[data-rd-plan-reset]")
            page.wait_for_function(
                "document.querySelector('[data-step-index=\"1\"] [data-rd-action]')"
                ".dataset.rdAction === '%d'" % a1, timeout=10000)
            self._idle(page)
            self.assertIn("Reset the plan", msgs[0])
            self.assertEqual(self._step_actions(page), [[a1], [a2], [rm]])
            titles = self._titles(page)
            self.assertTrue(titles[0].startswith("Add mini-1-"), titles)
            self.assertTrue(titles[1].startswith("Add mini-2-"), titles)
            self.assertTrue(titles[2].startswith("Remove "), titles)
            page.reload(wait_until="networkidle")
            page.wait_for_selector("[data-rd-step]")
            self._idle(page)
            self.assertEqual(self._step_actions(page), [[a1], [a2], [rm]])
            self.assertEqual(self._titles(page), titles)
            self.assertEqual(errors, [])
        finally:
            page.close()
            self._drop_design(did)

    def test_save_indicator_saving_then_saved(self):
        did, a1, a2, rm = self._mini_design(lambda a1, a2, rm: [
            {"id": None, "title": "S1", "placements": [a1]},
            {"id": None, "title": "S2", "placements": [a2]},
            {"id": None, "title": "S3", "placements": [rm]}])
        errors = []
        page = self._open_mini(did, errors)
        try:
            # Hold the save so "Saving…" is observable.
            def slow(route):
                import time
                time.sleep(1.0)
                route.continue_()

            page.route("**/save-steps/", slow)
            page.locator('[data-step-index="3"] [data-rd-step-handle]').drag_to(
                page.locator('[data-rd-gap="0"]'))
            page.wait_for_function(
                "/Saving/.test(document.querySelector('[data-rd-plan-save-status]').textContent)",
                timeout=5000)
            page.wait_for_function(
                "/Saved \\d\\d:\\d\\d/.test(document.querySelector('[data-rd-plan-save-status]')"
                ".textContent)", timeout=10000)
            self._idle(page)
            self.assertIn("saved automatically", self._status(page).get_attribute("title"))
            page.unroute("**/save-steps/")
            # A title edit saves on change too.
            page.fill('[data-step-index="1"] [data-rd-title]', "Renamed")
            page.keyboard.press("Enter")
            page.locator('[data-step-index="2"] [data-rd-title]').focus()
            page.locator('[data-step-index="1"] [data-rd-title]').blur()
            self._idle(page)
            page.wait_for_function(
                "/Saved \\d\\d:\\d\\d/.test(document.querySelector('[data-rd-plan-save-status]')"
                ".textContent)", timeout=10000)
            page.reload(wait_until="networkidle")
            page.wait_for_selector("[data-rd-step]")
            self._idle(page)
            self.assertEqual(self._titles(page)[0], "Renamed")
            self.assertRegex(self._status(page).inner_text(), r"Saved \d\d:\d\d")
            self.assertEqual(errors, [])
        finally:
            page.close()
            self._drop_design(did)

    def test_save_failure_shows_not_saved_and_retry_succeeds(self):
        did, a1, a2, rm = self._mini_design(lambda a1, a2, rm: [
            {"id": None, "title": "S1", "placements": [a1]},
            {"id": None, "title": "S2", "placements": [a2]},
            {"id": None, "title": "S3", "placements": [rm]}])
        errors = []
        page = self._open_mini(did, errors)
        try:
            page.route("**/save-steps/", lambda route: route.fulfill(
                status=500, content_type="application/json", body='{"detail": "boom"}'))
            page.locator('[data-step-index="3"] [data-rd-step-handle]').drag_to(
                page.locator('[data-rd-gap="0"]'))
            page.wait_for_function(
                "/Not saved/.test(document.querySelector('[data-rd-plan-save-status]').textContent)",
                timeout=10000)
            self._idle(page)
            retry = page.locator("[data-rd-plan-save-retry]")
            self.assertTrue(retry.is_visible())
            page.unroute("**/save-steps/")
            retry.click()
            page.wait_for_function(
                "/Saved \\d\\d:\\d\\d/.test(document.querySelector('[data-rd-plan-save-status]')"
                ".textContent)", timeout=10000)
            self.assertEqual(page.locator("[data-rd-plan-save-retry]").count(), 0)
            page.reload(wait_until="networkidle")
            page.wait_for_selector("[data-rd-step]")
            self._idle(page)
            self.assertEqual(self._titles(page), ["S3", "S1", "S2"])
            # The 500 is the only expected console error.
            self.assertTrue(all("500" in e or "Failed to load resource" in e for e in errors),
                            errors)
        finally:
            page.close()
            self._drop_design(did)

    def test_rack_blocks_use_the_card_width(self):
        """Wide: actions left, rack blocks side by side filling the right
        column. Narrow: stacked, no horizontal scroll. RD_SHOT_DIR saves
        wide-{dark,light,narrow}.png."""
        shot_dir = os.environ.get("RD_SHOT_DIR")
        page = self._api_ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.set_viewport_size({"width": 2560, "height": 1440})
            page.goto(self.url, wait_until="networkidle")
            page.wait_for_selector('[data-step-index="1"] [data-rd-rack] .nbx-rd-power-bar')
            self._idle(page)
            racks = page.locator('[data-step-index="1"] [data-rd-rack]')
            self.assertEqual(racks.count(), 2)
            a, b = racks.nth(0).bounding_box(), racks.nth(1).bounding_box()
            self.assertAlmostEqual(a["y"], b["y"], delta=2)  # side by side
            self.assertGreater(a["width"], 400)
            bar = racks.nth(0).locator(".nbx-rd-power-bar").bounding_box()
            self.assertGreater(bar["width"], a["width"] - 4)  # bar stretches
            acts = page.locator('[data-step-index="1"] [data-rd-drop]').bounding_box()
            self.assertLess(acts["x"] + acts["width"], a["x"])  # actions left
            if shot_dir:
                for theme in ("dark", "light"):
                    page.evaluate(
                        "t => { const h = document.body; h.setAttribute('data-bs-theme', t);"
                        " const d = document.getElementById('djDebug'); if (d) d.style.display = 'none'; }",
                        theme)
                    page.screenshot(path=os.path.join(shot_dir, f"wide-{theme}.png"))
            page.set_viewport_size({"width": 900, "height": 1200})
            page.wait_for_timeout(300)
            self.assertLessEqual(page.evaluate(
                "document.documentElement.scrollWidth - window.innerWidth"), 0)
            if shot_dir:
                page.screenshot(path=os.path.join(shot_dir, "wide-narrow.png"))
            self.assertEqual(errors, [])
        finally:
            page.close()

    def test_create_plan_uses_creation_order_and_conflict_pill(self):
        """E6: Create plan = one step per action in placement creation order;
        a slot clash reads "Conflict" in the step pill."""
        c = self._created
        design = self._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"ep-order-{uuid.uuid4().hex[:6]}", "racks": [c["rack"]],
            "sites": [c["site"]]})
        did = design["id"]
        page = self._api_ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            def pl(**kw):
                return self._api("POST", "/api/plugins/rack-design/placements/",
                                 {"design": did, **kw})["id"]

            def add(name, pos):
                return pl(kind="add", device_type=c["device_type"], device_role=c["role"],
                          target_rack=c["rack"], target_position=pos, target_face="front",
                          proposed_name=name)

            a_new = add("ord-add-1", 14)
            removed = pl(kind="remove", device=c["devices"][1])
            a_slot = add("ord-add-2", 3)  # takes the slot the remove frees
            page.goto(f"{BASE}/plugins/rack-design/designs/{did}/execution-plan/",
                      wait_until="networkidle")
            page.click("[data-rd-create]")
            page.wait_for_selector("[data-rd-step]", timeout=15000)
            self._idle(page)
            order = page.eval_on_selector_all(
                "[data-rd-step] [data-rd-action]", "els => els.map(e => Number(e.dataset.rdAction))")
            self.assertEqual(order, [a_new, removed, a_slot])
            titles = page.eval_on_selector_all(
                "[data-rd-title]", "els => els.map(e => e.value)")
            self.assertEqual(titles, ["Add ord-add-1", "Remove " + titles[1].split(" ", 1)[1],
                                      "Add ord-add-2"])
            # slot clash: drag the add into the step that still has the old device
            page.locator(f'[data-rd-action="{a_slot}"]').drag_to(
                page.locator('[data-step-index="1"] [data-rd-drop]'))
            self._idle(page)
            self.assertEqual(page.locator(
                '[data-step-index="1"] [data-rd-step-status="error"]').inner_text(), "Conflict")
            self.assertEqual(errors, [])
        finally:
            page.close()
            try:
                self._api("DELETE", f"/api/plugins/rack-design/designs/{did}/")
            except Exception:
                pass

    def test_editor_actions_become_steps_in_click_order(self):
        """E6 end to end: move a device to another rack, add one, remove one
        (in that order) in the editor, Save, Create plan -> the steps are
        exactly move, add, remove -- not the payload's rack/face order."""
        c = self._created
        sfx = uuid.uuid4().hex[:6]
        rack_b = self._api("POST", "/api/dcim/racks/", {
            "name": f"E2E EP Rack B {sfx}", "site": c["site"], "status": "active",
            "u_height": 20})["id"]
        design = self._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"ep-click-{sfx}", "racks": [c["rack"], rack_b],
            "sites": [c["site"]]})
        did = design["id"]
        base = self._api("GET", f"/api/dcim/devices/{c['devices'][0]}/")["name"]
        old = self._api("GET", f"/api/dcim/devices/{c['devices'][1]}/")["name"]
        ctx = self._browser.new_context(
            storage_state=self._api_ctx.storage_state(),
            viewport={"width": 1900, "height": 1200})
        page = ctx.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(f"{BASE}/plugins/rack-design/designs/{did}/editor/{c['rack']}/",
                      wait_until="networkidle")
            page.wait_for_selector(".grid-stack", timeout=30000)
            page.wait_for_timeout(1000)
            page.evaluate(
                "() => ['djDebug', 'djDebugRoot'].forEach(function (id) {"
                "  const d = document.getElementById(id);"
                "  if (d) { d.style.display = 'none'; d.style.pointerEvents = 'none'; }"
                "})")
            ga, gb = f"nbx-rd-grid-front-{c['rack']}", f"nbx-rd-grid-front-{rack_b}"

            # 1. move ep-base to the other rack (real mouse, cross-grid)
            tile = page.locator(f'#{ga} .grid-stack-item:has([data-name="{base}"])').first
            tb = tile.bounding_box()
            dst = page.query_selector("#" + gb).bounding_box()
            page.mouse.move(tb["x"] + tb["width"] / 2, tb["y"] + tb["height"] / 2)
            page.mouse.down()
            page.mouse.move(tb["x"] + tb["width"] / 2 + 40, tb["y"] + tb["height"] / 2, steps=6)
            page.mouse.move(dst["x"] + dst["width"] / 2, dst["y"] + 300, steps=30)
            page.wait_for_timeout(600)
            page.mouse.up()
            page.wait_for_selector(".nbx-rd-move-modal", state="visible", timeout=8000)
            page.click("[data-rd-move-apply]")
            page.wait_for_timeout(1200)
            self.assertEqual(page.locator(f"#{gb} .nbx-rd-state-move_in").count(), 1)

            # 2. add a device from the palette into the first rack
            pick_role(page)
            if not (page.query_selector("#nbx-rd-palette-search")
                    and page.query_selector("#nbx-rd-palette-search").is_visible()):
                page.click('[data-rd-section-toggle="device"]')
            page.wait_for_selector("#nbx-rd-palette-search", state="visible", timeout=15000)
            page.wait_for_timeout(2500)
            src = next(r for r in page.query_selector_all(
                "#nbx-rd-palette-list .nbx-rd-palette-item")
                if r.get_attribute("data-subdevice-role") != "child")
            src.scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            row = page.evaluate(f"""() => {{
                const g = document.getElementById('{ga}');
                const taken = new Set();
                for (const t of g.querySelectorAll('.grid-stack-item')) {{
                    const n = t.gridstackNode; if (!n) continue;
                    for (let i = 0; i < n.h; i++) taken.add(n.y + i);
                }}
                for (let y = 20; y < 30; y += 2) {{
                    if (!taken.has(y) && !taken.has(y + 1)) return y;
                }}
                return null;
            }}""")
            self.assertIsNotNone(row)
            cell = page.evaluate(f"() => document.getElementById('{ga}').gridstack.getCellHeight()")
            s_box, d_box = src.bounding_box(), page.query_selector("#" + ga).bounding_box()
            page.mouse.move(s_box["x"] + s_box["width"] / 2, s_box["y"] + s_box["height"] / 2)
            page.mouse.down()
            page.mouse.move(d_box["x"] + d_box["width"] / 2, d_box["y"] + row * cell + cell,
                            steps=25)
            page.wait_for_timeout(400)
            page.mouse.up()
            page.wait_for_timeout(1500)
            self.assertEqual(page.locator(f"#{ga} .nbx-rd-state-add").count(), 1)

            # 3. flag ep-old for removal
            old_tile = page.locator(f'#{ga} .grid-stack-item:has([data-name="{old}"])').first
            old_tile.hover()
            old_tile.locator(".nbx-rd-remove-btn").first.click(force=True)
            page.wait_for_timeout(500)
            self.assertEqual(page.locator(f"#{ga} .nbx-rd-state-remove").count(), 1)

            # Save for real
            with page.expect_response(lambda r: "save-layout" in r.url, timeout=20000) as info:
                page.click("#rd-editor-save")
            self.assertEqual(info.value.status, 200)

            page.wait_for_timeout(2500)  # the editor reloads itself after a save
            page.wait_for_load_state("networkidle")

            page.goto(f"{BASE}/plugins/rack-design/designs/{did}/execution-plan/",
                      wait_until="networkidle")
            page.click("[data-rd-create]")
            page.wait_for_selector("[data-rd-step]", timeout=15000)
            self._idle(page)
            ids = page.eval_on_selector_all(
                "[data-rd-step] [data-rd-action]",
                "els => els.map(e => Number(e.dataset.rdAction))")
            kinds = [self._api(
                "GET", f"/api/plugins/rack-design/placements/{i}/")["kind"] for i in ids]
            self.assertEqual(kinds, ["move", "add", "remove"])
            self.assertEqual(errors, [])
        finally:
            ctx.close()
            for path in (f"/api/plugins/rack-design/designs/{did}/",
                         f"/api/dcim/racks/{rack_b}/"):
                try:
                    self._api("DELETE", path)
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
