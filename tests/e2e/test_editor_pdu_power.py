#!/usr/bin/env python3
"""Deterministic Playwright e2e for the planned-PDU / rack power-config dialogs
(docs/pdu-distribution-spec.md — Phase E): the per-rack "Power" button + rack
power-config persistence, the PDU-power dialog, and the copy-from-rack /
power-source endpoints that feed them.

The full drag-add-a-PDU-from-the-palette gesture is flaky to script, so this
tests the pieces DETERMINISTICALLY: the REST endpoints the dialogs call
(rack-power upsert/read, power-source), and the editor's exposed dialog helpers
on ``window.NbxRdEditor`` (looksLikePdu detection + that the dialogs build). The
backend that CONSUMES a saved power_config is covered by tests/test_*.py.

SELF-PROVISIONING: setUpClass creates its own manufacturer / role / site / a PDU
device type (with PowerOutletTemplates + a PowerFeed for the copy-from-rack path)
/ rack / design. Skips cleanly when playwright/Chrome or the dev server aren't
available. Run via ``dev/e2e.sh tests.e2e.test_editor_pdu_power``.
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


@unittest.skipUnless(_PREREQ_OK, f"pdu-power e2e prerequisites not met: {_PREREQ_REASON}")
class EditorPduPowerTestCase(unittest.TestCase):
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
        cf = {"custom_fields": {"warranty_type": ""}}
        mfr = cls._api("POST", "/api/dcim/manufacturers/", {
            "name": f"E2E PP Mfr {suffix}", "slug": f"e2e-pp-mfr-{suffix}"})
        # A role slug of exactly 'pdu' so the server treats it as a PDU.
        role = cls._api("POST", "/api/dcim/device-roles/", {
            "name": f"E2E PP PDU {suffix}", "slug": "pdu", "color": "9e9e9e"}) \
            if not cls._pdu_role_exists() else cls._existing_pdu_role()
        site = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E PP Site {suffix}", "slug": f"e2e-pp-site-{suffix}",
            "status": "active"})
        # PDU device type with two bank outlets (1/1, 2/1) as templates.
        dt = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-PP-PDU-{suffix}",
            "slug": f"e2e-pp-pdu-{suffix}", "u_height": 0, "is_full_depth": False})
        for name in ("1/1", "2/1"):
            cls._api("POST", "/api/dcim/power-outlet-templates/", {
                "device_type": dt["id"], "name": name})
        cls._api("POST", "/api/dcim/power-port-templates/", {
            "device_type": dt["id"], "name": "input"})
        rack = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E PP Rack {suffix}", "site": site["id"],
            "status": "active", "u_height": 20})

        # A real PDU + PowerFeed so copy-from-rack (power-source kind=pdu) works.
        panel = cls._api("POST", "/api/dcim/power-panels/", {
            "site": site["id"], "name": f"E2E PP Panel {suffix}"})
        feed = cls._api("POST", "/api/dcim/power-feeds/", {
            "power_panel": panel["id"], "rack": rack["id"],
            "name": f"E2E PP Feed {suffix}", "status": "active",
            "voltage": 230, "amperage": 32, "phase": "single-phase", "supply": "ac"})
        cls._pdu_name = f"e2e-pp-pdu-r1-a1-{suffix}"
        pdu = cls._api("POST", "/api/dcim/devices/", {
            "name": cls._pdu_name, "device_type": dt["id"], "role": role["id"],
            "site": site["id"], "rack": rack["id"], "status": "active", **cf})
        # The device auto-instantiates an "input" PowerPort from the type
        # template; fetch it rather than creating a duplicate.
        pp = cls._api("GET", f"/api/dcim/power-ports/?device_id={pdu['id']}")["results"][0]
        cls._api("POST", "/api/dcim/cables/", {
            "a_terminations": [{"object_type": "dcim.powerport", "object_id": pp["id"]}],
            "b_terminations": [{"object_type": "dcim.powerfeed", "object_id": feed["id"]}],
            "status": "connected"})

        cls._created = {
            "rack": rack["id"], "device_types": [dt["id"]], "panel": panel["id"],
            "role_slug": "pdu", "manufacturer": mfr["id"], "site": site["id"],
        }
        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"pp-{suffix}", "site": site["id"], "racks": [rack["id"]]})
        cls._design_id = design["id"]
        cls._rack_id = rack["id"]
        cls.editor_url = (
            f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/{rack['id']}/")

    @classmethod
    def _pdu_role_exists(cls):
        r = cls._api("GET", "/api/dcim/device-roles/?slug=pdu")
        return r and r.get("count", 0) > 0

    @classmethod
    def _existing_pdu_role(cls):
        r = cls._api("GET", "/api/dcim/device-roles/?slug=pdu")
        return r["results"][0]

    @classmethod
    def _cleanup_class(cls):
        try:
            if getattr(cls, "_design_id", None) is not None:
                try:
                    cls._api("DELETE", f"/api/plugins/rack-design/designs/{cls._design_id}/")
                except Exception:
                    pass
            created = getattr(cls, "_created", None)
            if created:
                # Rack delete cascades its devices/feeds; panel + type + site after.
                for key, path in (("rack", "/api/dcim/racks/"),
                                  ("panel", "/api/dcim/power-panels/")):
                    if created.get(key) is not None:
                        try:
                            cls._api("DELETE", f"{path}{created[key]}/")
                        except Exception:
                            pass
                for tid in created.get("device_types", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/device-types/{tid}/")
                    except Exception:
                        pass
                for key, path in (("manufacturer", "/api/dcim/manufacturers/"),
                                  ("site", "/api/dcim/sites/")):
                    if created.get(key) is not None:
                        try:
                            cls._api("DELETE", f"{path}{created[key]}/")
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
        cls._created = None
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
                (c["value"] for c in cls._api_ctx.cookies()
                 if c["name"] == "csrftoken"), "")
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
        self.page.on(
            "console",
            lambda m: self.errors.append(f"{m.type}: {m.text}")
            if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"PAGEERROR: {e}"))
        resp = self.page.goto(self.editor_url, wait_until="networkidle")
        self.assertEqual(resp.status, 200, f"editor URL returned {resp.status}")
        self.page.wait_for_selector(".nbx-rd-rack-block", timeout=15000)
        self.page.wait_for_timeout(400)

    def tearDown(self):
        if getattr(self, "ctx", None):
            self.ctx.close()

    # --- per-rack power button + dialogs are present ----------------------

    def test_rack_power_button_and_editor_api_present(self):
        res = self.page.evaluate("""() => {
            return {
                rackPowerButtons: document.querySelectorAll('[data-rd-rack-power-btn]').length,
                rackPowerJson: document.querySelectorAll('[id^="rd-rackpower-"]').length,
                hasApi: !!window.NbxRdEditor,
                fns: window.NbxRdEditor ? Object.keys(window.NbxRdEditor) : [],
            };
        }""")
        # The fixture rack already has a real PowerFeed (for copy-from-rack), so
        # the greenfield per-rack "Power" button is now GATED OFF here (docs/pdu-
        # distribution-spec.md §6.3): it only renders for a rack with NO real
        # feeds. The rd-rackpower-<id> json_script global still always renders
        # (editor.js's showRackPowerDialog reads it regardless of the button).
        self.assertEqual(res["rackPowerButtons"], 0, res)
        self.assertGreaterEqual(res["rackPowerJson"], 1, res)
        self.assertTrue(res["hasApi"], res)
        for fn in ("looksLikePdu", "showPduPowerDialog", "showRackPowerDialog"):
            self.assertIn(fn, res["fns"], res)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    def test_looks_like_pdu_detection(self):
        res = self.page.evaluate("""() => {
            const f = window.NbxRdEditor.looksLikePdu;
            return {
                by_slug: f('pdu', null, null, null),
                by_name: f(null, null, 'sg2-pdu-r0101-a2', null),
                by_type: f(null, null, null, 'Rack PDU 30A'),
                server_no: f('server', 'Server', 'sg2-srv-1', 'Dell R740'),
            };
        }""")
        self.assertTrue(res["by_slug"] and res["by_name"] and res["by_type"], res)
        self.assertFalse(res["server_no"], res)

    def test_pdu_and_rack_dialogs_open(self):
        res = self.page.evaluate("""() => {
            const api = window.NbxRdEditor;
            let pduErr = null, rackErr = null;
            try { api.showPduPowerDialog({proposed_name:'x-pdu-r1-a2', roleSlug:'pdu', power_config:null},
                                        document.createElement('div'), {rackId: String(RID)}); }
            catch(e){ pduErr = String(e); }
            document.querySelectorAll('.modal button').forEach(b=>{ if(/cancel/i.test(b.textContent)) b.click(); });
            try { api.showRackPowerDialog(String(RID), 'RackX'); } catch(e){ rackErr = String(e); }
            const modalPresent = !!document.querySelector('.modal');
            document.querySelectorAll('.modal button').forEach(b=>{ if(/cancel/i.test(b.textContent)) b.click(); });
            return { pduErr, rackErr, modalPresent };
        }""".replace("RID", str(self._rack_id)))
        self.assertIsNone(res["pduErr"], res)
        self.assertIsNone(res["rackErr"], res)
        self.assertTrue(res["modalPresent"], res)

    # --- endpoints the dialogs call ---------------------------------------

    def test_rack_power_post_get_round_trips(self):
        res = self.page.evaluate("""async () => {
            const csrf = window.NbxRdEditor.getCsrfToken();
            const base = '/api/plugins/rack-design/designs/DID/';
            const cfg = {source:'manual', copied_from:null,
                         custom_fields:{power_limitation: 7.5, pdu_location:'top'}};
            const post = await fetch(base+'rack-power/', {method:'POST', credentials:'same-origin',
                headers:{'Content-Type':'application/json','X-CSRFToken':csrf},
                body: JSON.stringify({rack_id: RID, power_config: cfg})});
            const get = await fetch(base+'rack-power/?rack_id='+RID, {credentials:'same-origin'});
            const got = await get.json();
            // cleanup
            await fetch(base+'rack-power/', {method:'POST', credentials:'same-origin',
                headers:{'Content-Type':'application/json','X-CSRFToken':csrf},
                body: JSON.stringify({rack_id: RID, power_config: null})});
            return { postStatus: post.status, stored: got.power_config };
        }""".replace("DID", str(self._design_id)).replace("RID", str(self._rack_id)))
        self.assertEqual(res["postStatus"], 200, res)
        self.assertEqual(res["stored"]["custom_fields"]["power_limitation"], 7.5, res)
        self.assertEqual(res["stored"]["custom_fields"]["pdu_location"], "top", res)

    def test_power_source_pdu_kind_removed(self):
        """kind=pdu was dropped with the feed-binding redesign (a planned PDU
        binds to a feed rather than copying another PDU's electricals) -> 400."""
        res = self.page.evaluate("""async () => {
            const base = '/api/plugins/rack-design/designs/DID/';
            const r = await fetch(base+'power-source/?kind=pdu&rack_id='+RID+'&feed=a1',
                                  {credentials:'same-origin'});
            return { status: r.status };
        }""".replace("DID", str(self._design_id)).replace("RID", str(self._rack_id)))
        self.assertEqual(res["status"], 400, res)

    # --- bind-to-feed dialog (docs/pdu-distribution-spec.md §6.2/§6.3, Phase D) --

    def test_feeds_endpoint_lists_real_feed_first(self):
        """GET .../feeds/ returns the fixture's real PowerFeed, no planned yet."""
        res = self.page.evaluate("""async () => {
            const base = '/api/plugins/rack-design/designs/DID/';
            const r = await fetch(base+'feeds/?rack_id='+RID, {credentials:'same-origin'});
            return { status: r.status, data: await r.json() };
        }""".replace("DID", str(self._design_id)).replace("RID", str(self._rack_id)))
        self.assertEqual(res["status"], 200, res)
        real = res["data"]["real"]
        self.assertGreaterEqual(len(real), 1, res)
        self.assertEqual(real[0]["source"], "real", res)
        self.assertIn(real[0]["phase"], ("single-phase", "three-phase"), res)

    def test_bind_dialog_picks_real_feed(self):
        """Opening the bind-to-feed dialog on a fresh PDU widget, selecting the
        rack's real feed and confirming stashes real_power_feed_id (and clears
        planned_power_feed_id) on the widget -- no more manual V/A/phase entry."""
        res = self.page.evaluate("""async () => {
            const api = window.NbxRdEditor;
            const widget = { proposed_name: 'e2e-pp-pdu-bind-real', role_slug: 'pdu', label: 'e2e-pp-pdu-bind-real' };
            const content = document.createElement('div');
            api.showPduPowerDialog(widget, content, {rackId: String(RID)});
            for (let i = 0; i < 20; i++) {
                await new Promise(r => setTimeout(r, 100));
                if (document.querySelector('.nbx-rd-feed-list input[name=nbx-rd-feed-pick]')) { break; }
            }
            const rows = Array.from(document.querySelectorAll(
                '.nbx-rd-feed-list input[name=nbx-rd-feed-pick]'));
            const realRow = rows.find(r => r.getAttribute('data-source') === 'real');
            if (realRow) { realRow.checked = true; realRow.dispatchEvent(new Event('change')); }
            const confirmBtn = document.querySelector('[data-rd-pdu-confirm]');
            const enabledBeforeClick = confirmBtn ? !confirmBtn.disabled : false;
            if (confirmBtn && enabledBeforeClick) { confirmBtn.click(); }
            document.querySelectorAll('.modal button').forEach(
                b => { if (/cancel/i.test(b.textContent)) b.click(); });
            return {
                rowCount: rows.length, foundReal: !!realRow, enabledBeforeClick,
                realFeedId: widget.real_power_feed_id, plannedFeedId: widget.planned_power_feed_id,
            };
        }""".replace("RID", str(self._rack_id)))
        self.assertTrue(res["foundReal"], res)
        self.assertTrue(res["enabledBeforeClick"], res)
        self.assertIsNotNone(res["realFeedId"], res)
        self.assertIsNone(res["plannedFeedId"], res)
        self.real_feed_id = res["realFeedId"]
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    def test_bind_dialog_defines_and_picks_planned_feed(self):
        """The greenfield 'define planned feed' fallback: fill the inline form,
        Create posts planned-feed/ (upsert), the new feed appears in the list
        and can be selected -- always available, even on a rack with real feeds."""
        suffix = uuid.uuid4().hex[:6]
        feed_name = f"E2E Planned {suffix}"
        res = self.page.evaluate("""async (feedName) => {
            const RID = RACK_ID;
            const api = window.NbxRdEditor;
            const widget = { proposed_name: 'e2e-pp-pdu-bind-planned', role_slug: 'pdu', label: 'e2e-pp-pdu-bind-planned' };
            const content = document.createElement('div');
            api.showPduPowerDialog(widget, content, {rackId: String(RID)});
            await new Promise(r => setTimeout(r, 300));
            document.querySelector('.nbx-rd-feed-new-toggle').click();
            document.querySelector('.nbx-rd-feed-new-name').value = feedName;
            document.querySelector('.nbx-rd-feed-new-voltage').value = '400';
            document.querySelector('.nbx-rd-feed-new-amperage').value = '32';
            document.querySelector('.nbx-rd-feed-new-phase').value = 'three-phase';
            document.querySelector('.nbx-rd-feed-new-supply').value = 'ac';
            document.querySelector('.nbx-rd-feed-new-create').click();
            let plannedRow = null;
            for (let i = 0; i < 20; i++) {
                await new Promise(r => setTimeout(r, 150));
                plannedRow = Array.from(document.querySelectorAll(
                    '.nbx-rd-feed-list input[name=nbx-rd-feed-pick]')).find(
                        r => r.getAttribute('data-source') === 'planned');
                if (plannedRow) { break; }
            }
            if (plannedRow) { plannedRow.checked = true; plannedRow.dispatchEvent(new Event('change')); }
            const confirmBtn = document.querySelector('[data-rd-pdu-confirm]');
            const enabled = confirmBtn ? !confirmBtn.disabled : false;
            if (confirmBtn && enabled) { confirmBtn.click(); }
            document.querySelectorAll('.modal button').forEach(
                b => { if (/cancel/i.test(b.textContent)) b.click(); });
            return {
                foundPlanned: !!plannedRow, enabled,
                realFeedId: widget.real_power_feed_id, plannedFeedId: widget.planned_power_feed_id,
            };
        }""".replace("RACK_ID", str(self._rack_id)), feed_name)
        self.assertTrue(res["foundPlanned"], res)
        self.assertTrue(res["enabled"], res)
        self.assertIsNotNone(res["plannedFeedId"], res)
        self.assertIsNone(res["realFeedId"], res)

        listed = self._api(
            "GET", f"/api/plugins/rack-design/designs/{self._design_id}/planned-feed/"
            f"?rack_id={self._rack_id}")
        self.assertTrue(any(f["id"] == res["plannedFeedId"] for f in listed), listed)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    def _save_pdu_add(self, name, u_position, real_feed_id=None, planned_feed_id=None):
        role = self._api("GET", "/api/dcim/device-roles/?slug=pdu")["results"][0]
        item = {
            "kind": "add", "device_type_id": self._created["device_types"][0],
            "device_role_id": role["id"], "proposed_name": name,
            "u_position": u_position, "face": "front",
        }
        if real_feed_id is not None:
            item["real_power_feed_id"] = real_feed_id
        if planned_feed_id is not None:
            item["planned_power_feed_id"] = planned_feed_id
        return self._api(
            "POST", f"/api/plugins/rack-design/designs/{self._design_id}/save-layout/", {
                "design_id": self._design_id,
                "racks": [{"rack_id": self._rack_id,
                           "front": [item], "rear": [], "other": []}],
            })

    def test_bind_real_feed_save_reload_renders(self):
        """Bind a fresh PDU add to the rack's REAL feed via the dialog, Save via
        the same API shape buildSavePayload sends, reload, and confirm the
        binding round-trips onto the widget JSON + the heatmap renders without
        console errors (the feed-leg accent is driven by feed_letter/feed_source,
        never device-name parsing)."""
        feeds = self._api(
            "GET", f"/api/plugins/rack-design/designs/{self._design_id}/feeds/"
            f"?rack_id={self._rack_id}")
        real_feed_id = feeds["real"][0]["id"]

        self._save_pdu_add("e2e-pp-pdu-real-bound", 6, real_feed_id=real_feed_id)

        self.page.goto(self.editor_url, wait_until="networkidle")
        self.page.wait_for_selector(".nbx-rd-rack-block", timeout=15000)

        widget = self.page.evaluate("""() => {
            const el = document.getElementById('rd-editor-data-' + RID);
            const widgets = el ? JSON.parse(el.textContent) : [];
            return widgets.find(w => w.proposed_name === 'e2e-pp-pdu-real-bound') || null;
        }""".replace("RID", str(self._rack_id)))
        self.assertIsNotNone(widget, "reloaded PDU add widget not found")
        self.assertEqual(widget["real_power_feed_id"], real_feed_id, widget)
        self.assertIsNone(widget["planned_power_feed_id"], widget)

        # Toggle the heatmap on -- must not error, whether or not this dev
        # instance's configured distribution_mode emits a per-bank Distribution.
        self.page.evaluate("""() => {
            const t = document.querySelector('[data-rd-power-heatmap]');
            if (t) { t.checked = true; t.dispatchEvent(new Event('change')); }
        }""")
        self.page.wait_for_timeout(300)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    def test_pdu_reference_source_device_stashed(self):
        """The custom-fields section's 'reference a PDU' mode (docs/pdu-
        distribution-spec.md §6): selecting one of the rack's real PDUs and
        confirming stashes power_source_device_id on the widget. showCfSection
        is gated on planning_fields.pdu OR an already-set power_source_device_id
        (editor.js showPduPowerDialog), so this dev instance -- which may have no
        planning_fields.pdu configured -- is exercised by pre-seeding the widget
        with the fixture's real PDU id (forces the cf section to render); the
        manual-cf mode needs planning_fields.pdu and is covered elsewhere."""
        pdus = self._api(
            "GET", f"/api/dcim/devices/?rack_id={self._rack_id}&role=pdu")
        self.assertGreaterEqual(pdus["count"], 1, pdus)
        pdu_id = pdus["results"][0]["id"]

        res = self.page.evaluate("""async () => {
            const api = window.NbxRdEditor;
            const widget = {
                proposed_name: 'e2e-refcf-pdu', role_slug: 'pdu',
                power_source_device_id: PDUID,
            };
            const content = document.createElement('div');
            api.showPduPowerDialog(widget, content, {rackId: String(RID)});
            let sel = null;
            for (let i = 0; i < 30; i++) {
                await new Promise(r => setTimeout(r, 100));
                sel = document.querySelector('.nbx-rd-pducf-pdu');
                if (sel && sel.options.length > 1) { break; }
            }
            const cfSectionPresent = !!document.querySelector('.nbx-rd-pducf-pdu');
            if (sel) { sel.value = String(PDUID); sel.dispatchEvent(new Event('change')); }
            const confirmBtn = document.querySelector('[data-rd-pdu-confirm]');
            const enabledBeforeClick = confirmBtn ? !confirmBtn.disabled : false;
            if (confirmBtn && enabledBeforeClick) { confirmBtn.click(); }
            document.querySelectorAll('.modal button').forEach(
                b => { if (/cancel/i.test(b.textContent)) b.click(); });
            return {
                cfSectionPresent, enabledBeforeClick,
                selValue: sel ? sel.value : null,
                sourceDev: widget.power_source_device_id,
                manualCf: widget.power_config,
            };
        }""".replace("PDUID", str(pdu_id)).replace("RID", str(self._rack_id)))
        self.assertTrue(res["cfSectionPresent"], res)
        self.assertEqual(res["selValue"], str(pdu_id), res)
        self.assertTrue(res["enabledBeforeClick"], res)
        self.assertEqual(res["sourceDev"], pdu_id, res)
        self.assertIsNone(res["manualCf"], res)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    def test_bind_planned_feed_save_reload_renders(self):
        """Greenfield flow: define a planned feed, bind a fresh PDU add to it,
        Save, reload, and confirm the planned binding round-trips."""
        suffix = uuid.uuid4().hex[:6]
        feed = self._api(
            "POST", f"/api/plugins/rack-design/designs/{self._design_id}/planned-feed/", {
                "rack_id": self._rack_id, "name": f"E2E Greenfield {suffix}",
                "voltage": 230, "amperage": 16, "phase": "single-phase", "supply": "ac",
            })

        self._save_pdu_add("e2e-pp-pdu-planned-bound", 8, planned_feed_id=feed["id"])

        self.page.goto(self.editor_url, wait_until="networkidle")
        self.page.wait_for_selector(".nbx-rd-rack-block", timeout=15000)

        widget = self.page.evaluate("""() => {
            const el = document.getElementById('rd-editor-data-' + RID);
            const widgets = el ? JSON.parse(el.textContent) : [];
            return widgets.find(w => w.proposed_name === 'e2e-pp-pdu-planned-bound') || null;
        }""".replace("RID", str(self._rack_id)))
        self.assertIsNotNone(widget, "reloaded PDU add widget not found")
        self.assertEqual(widget["planned_power_feed_id"], feed["id"], widget)
        self.assertIsNone(widget["real_power_feed_id"], widget)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")
