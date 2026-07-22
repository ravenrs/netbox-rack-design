#!/usr/bin/env python3
"""Deterministic Playwright e2e for the per-bank power-distribution heatmap
(docs/pdu-distribution-spec.md, script mode): when the server emits a
Distribution for a rack, toggling "Power heatmap" colors the PDU banks (a legend
of used/breaker chips + overload/alarm), tints each consumer tile by the bank
load it lands on, and accents its A/B feed leg -- and toggling off restores the
plain rendering exactly.

The distribution normally comes from a site ``distribution_script``; provisioning
a full PDU/outlet topology on a throwaway rack (and flipping the dev server into
script mode) is out of scope for a UI test. So this test **injects** a
Distribution JSON (the exact shape ``distribution.py`` delivers) into the loaded
page as ``#rd-distribution-<rackId>`` -- the same element the server template
emits -- and asserts the frontend renders it. The backend that PRODUCES the
Distribution is covered by ``tests/test_distribution*.py``.

SELF-PROVISIONING: setUpClass creates its own manufacturer / role / site / a
powered device type / rack / two devices, then a design over the rack. Skips
cleanly when playwright/Chrome or the dev server aren't available.

Run via ``dev/e2e.sh tests.e2e.test_editor_distribution``.
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


@unittest.skipUnless(_PREREQ_OK, f"distribution e2e prerequisites not met: {_PREREQ_REASON}")
class EditorDistributionTestCase(unittest.TestCase):
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
            "name": f"E2E DIST Mfr {suffix}", "slug": f"e2e-dist-mfr-{suffix}"})
        role = cls._api("POST", "/api/dcim/device-roles/", {
            "name": f"E2E DIST Role {suffix}", "slug": f"e2e-dist-role-{suffix}",
            "color": "9e9e9e"})
        site = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E DIST Site {suffix}", "slug": f"e2e-dist-site-{suffix}",
            "status": "active"})
        dt = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-DIST-Srv-{suffix}",
            "slug": f"e2e-dist-srv-{suffix}", "u_height": 1, "is_full_depth": False})
        cls._api("POST", "/api/dcim/power-port-templates/", {
            "device_type": dt["id"], "name": "psu1", "allocated_draw": 400})
        rack = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E DIST Rack {suffix}", "site": site["id"],
            "status": "active", "u_height": 20})

        # Two named consumers we will attribute to banks in the injected JSON.
        cls._hot_name = f"e2e-dist-hot-{suffix}"    # -> overloaded bank (a1 B1)
        cls._cool_name = f"e2e-dist-cool-{suffix}"  # -> ok bank (b1 B2)
        cls._api("POST", "/api/dcim/devices/", {
            "name": cls._hot_name, "device_type": dt["id"], "role": role["id"],
            "site": site["id"], "rack": rack["id"], "position": "5.0",
            "face": "front", "status": "active", **cf})
        cls._api("POST", "/api/dcim/devices/", {
            "name": cls._cool_name, "device_type": dt["id"], "role": role["id"],
            "site": site["id"], "rack": rack["id"], "position": "8.0",
            "face": "front", "status": "active", **cf})

        cls._created = {
            "rack": rack["id"], "device_types": [dt["id"]],
            "role": role["id"], "manufacturer": mfr["id"], "site": site["id"],
        }
        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"dist-{suffix}", "site": site["id"], "racks": [rack["id"]]})
        cls._design_id = design["id"]
        cls._rack_id = rack["id"]
        cls.editor_url = (
            f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/{rack['id']}/")

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
                if created.get("rack") is not None:
                    try:
                        cls._api("DELETE", f"/api/dcim/racks/{created['rack']}/")
                    except Exception:
                        pass
                for tid in created.get("device_types", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/device-types/{tid}/")
                    except Exception:
                        pass
                for key, path in (
                    ("role", "/api/dcim/device-roles/"),
                    ("manufacturer", "/api/dcim/manufacturers/"),
                    ("site", "/api/dcim/sites/"),
                ):
                    if created.get(key) is not None:
                        try:
                            cls._api("DELETE", f"{path}{created[key]}/")
                        except Exception:
                            pass
        finally:
            for closer in (
                lambda: cls._api_ctx.close(),
                lambda: cls._browser.close(),
                lambda: cls._pw.stop(),
            ):
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

    def _distribution(self):
        """A Distribution (docs/pdu-distribution-spec.md §3) attributing the two
        provisioned devices to banks: the 'hot' device to an OVERLOADED a1 bank 1,
        the 'cool' device to an ok b1 bank 2."""
        return {
            "scheme": "e2e", "pdu_location": "bottom",
            "pdus": {
                "site-pdu-r1-a1": {
                    "feed_name": "a1", "feed_letter": "a", "phase": 1,
                    "allocated_draw": 3680, "power_bank_count": 2,
                    "banks": {
                        "1": {"max_power": 1840, "allocated_power": 2100,
                              "planned_power": 0, "util_pct": 114, "state": "overload",
                              "units": [1, 2, 3, 4, 5],
                              "devices": [{"name": self._hot_name, "ru": 5,
                                           "draw_w": 2100, "status": "active"}]},
                        "2": {"max_power": 1840, "allocated_power": 300,
                              "planned_power": 0, "util_pct": 16, "state": "ok",
                              "units": [6, 7, 8, 9, 10], "devices": []},
                    }},
                "site-pdu-r1-b1": {
                    "feed_name": "b1", "feed_letter": "b", "phase": 1,
                    "allocated_draw": 3680, "power_bank_count": 2,
                    "banks": {
                        # The hot device is redundant -> also on this B leg, so
                        # its tile must show BOTH feed accents (blue A + orange B).
                        "1": {"max_power": 1840, "allocated_power": 900,
                              "planned_power": 0, "util_pct": 49, "state": "ok",
                              "units": [1, 2, 3, 4, 5],
                              "devices": [{"name": self._hot_name, "ru": 5,
                                           "draw_w": 900, "status": "active"}]},
                        "2": {"max_power": 1840, "allocated_power": 400,
                              "planned_power": 0, "util_pct": 22, "state": "ok",
                              "units": [6, 7, 8, 9, 10],
                              "devices": [{"name": self._cool_name, "ru": 8,
                                           "draw_w": 400, "status": "active"}]},
                    }},
            },
            "rack": {"power_limitation_w": 6000, "total_w": 2500, "alarm": True,
                     "warnings": ["PDU site-pdu-r1-a1 bank 1: 2100W exceeds breaker 1840W"]},
        }

    def _inject_and_toggle(self, on):
        """Inject the Distribution as #rd-distribution-<rackId> and set the
        heatmap toggle. Returns the rendered state read back from the DOM."""
        return self.page.evaluate(
            """([dist, rackId, on, hot, cool]) => {
                const block = document.querySelector(
                    '.nbx-rd-rack-block[data-rack-id="'+rackId+'"]')
                    || document.querySelector('.nbx-rd-rack-block');
                let el = document.getElementById('rd-distribution-'+rackId);
                if (!el) {
                    el = document.createElement('script');
                    el.type = 'application/json';
                    el.id = 'rd-distribution-'+rackId;
                    block.appendChild(el);
                }
                el.textContent = JSON.stringify(dist);
                const t = document.querySelector('[data-rd-power-heatmap]');
                t.checked = on; t.dispatchEvent(new Event('change', {bubbles:true}));

                function tileFor(name) {
                    return [...block.querySelectorAll('.grid-stack-item')].find(function (ti) {
                        const c = ti.querySelector('.grid-stack-item-content');
                        if (!c) return false;
                        const d = c.querySelector('.nbx-rd-name-display');
                        const l = c.querySelector('.nbx-rd-label');
                        const nm = (d && d.textContent.trim()) || (l && l.textContent.trim()) || '';
                        return nm === name;
                    });
                }
                const legend = block.querySelector('.nbx-rd-dist-legend');
                const chips = legend ? [...legend.querySelectorAll('.nbx-rd-dist-chip')]
                    .map(function (c) { return {txt: c.textContent, cls: c.className}; }) : null;
                const hotTile = tileFor(hot);
                const coolTile = tileFor(cool);
                const hotContent = hotTile && hotTile.querySelector('.grid-stack-item-content');
                return {
                    bodyActive: document.body.classList.contains('nbx-rd-heatmap-active'),
                    legendPresent: !!legend,
                    chipCount: chips ? chips.length : 0,
                    overloadChip: chips ? chips.some(function (c) {
                        return c.cls.indexOf('nbx-rd-dist-overload') >= 0; }) : false,
                    alarmPresent: legend ? !!legend.querySelector('.nbx-rd-dist-alarm') : false,
                    hotFeedA: hotTile ? hotTile.classList.contains('nbx-rd-feed-a') : null,
                    hotFeedB: hotTile ? hotTile.classList.contains('nbx-rd-feed-b') : null,
                    coolFeed: coolTile ? (coolTile.className.match(/nbx-rd-feed-[ab]/) || [])[0] : null,
                    hotHeatCol: hotContent ? hotContent.style.getPropertyValue('--nbx-rd-heat-col') : null,
                    feedTiles: block.querySelectorAll('.nbx-rd-feed-a, .nbx-rd-feed-b').length,
                };
            }""",
            [self._distribution(), str(self._rack_id), on, self._hot_name, self._cool_name],
        )

    def test_distribution_heatmap_banks_legend_and_feed_tint(self):
        res = self._inject_and_toggle(True)
        self.assertTrue(res["bodyActive"], "heatmap did not activate")
        self.assertTrue(res["legendPresent"], "per-bank legend not rendered")
        self.assertEqual(res["chipCount"], 4, res)          # 2 PDUs x 2 banks
        self.assertTrue(res["overloadChip"], "overloaded bank chip missing")
        self.assertTrue(res["alarmPresent"], "rack alarm marker missing")
        # The redundant device shows BOTH feed accents (A blue + B orange); the
        # single-leg device shows only its feed.
        self.assertTrue(res["hotFeedA"], res)
        self.assertTrue(res["hotFeedB"], res)
        self.assertEqual(res["coolFeed"], "nbx-rd-feed-b", res)
        # The overloaded device paints the hard-red overload color, not gradient.
        self.assertEqual(res["hotHeatCol"], "#b01919", res)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    def test_distribution_heatmap_off_restores(self):
        self._inject_and_toggle(True)
        off = self._inject_and_toggle(False)
        self.assertFalse(off["bodyActive"], "heatmap-active not cleared on off")
        self.assertFalse(off["legendPresent"], "legend not removed on off")
        self.assertEqual(off["feedTiles"], 0, "feed accent classes not cleared on off")
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")
