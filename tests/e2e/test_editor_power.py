#!/usr/bin/env python3
"""Deterministic, self-provisioning Playwright e2e for the power-projection UI
(docs/power-projection-spec.md): the per-rack power bar, the heatmap toggle's
per-device fill bars, the "pull-out" highlight of devices with unconnected power
ports, and the PSU/allocated-power rows on the device hover card.

SELF-PROVISIONING: setUpClass creates its OWN manufacturer / role / site /
device types / rack / devices via the REST API, then a design over the rack.
One device type carries a PowerPortTemplate with a draw (its instantiated ports
stay uncabled -> a connection gap the UI must flag); a second type has none
(passive). tearDownClass removes it all, best-effort. Skips cleanly (does not
fail) when playwright/Chrome or the dev server aren't available.

The dev server's plugin config drives capacity (no PowerFeeds on this throwaway
rack -> the config fallback), so these tests assert structure/behavior, not
exact capacity numbers.

Run via ``dev/e2e.sh tests.e2e.test_editor_power``.
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


@unittest.skipUnless(_PREREQ_OK, f"power e2e prerequisites not met: {_PREREQ_REASON}")
class EditorPowerTestCase(unittest.TestCase):
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
            "name": f"E2E PWR Mfr {suffix}", "slug": f"e2e-pwr-mfr-{suffix}"})
        role = cls._api("POST", "/api/dcim/device-roles/", {
            "name": f"E2E PWR Role {suffix}", "slug": f"e2e-pwr-role-{suffix}",
            "color": "9e9e9e"})
        site = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E PWR Site {suffix}", "slug": f"e2e-pwr-site-{suffix}",
            "status": "active"})
        # Powered type: a PowerPortTemplate with a draw; the device's
        # instantiated port stays uncabled -> a connection gap.
        dt_pwr = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-PWR-Srv-{suffix}",
            "slug": f"e2e-pwr-srv-{suffix}", "u_height": 1, "is_full_depth": False})
        cls._api("POST", "/api/dcim/power-port-templates/", {
            "device_type": dt_pwr["id"], "name": "psu1", "allocated_draw": 400})
        # Passive type: no power ports at all (patch-panel analogue).
        dt_passive = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-PWR-PP-{suffix}",
            "slug": f"e2e-pwr-pp-{suffix}", "u_height": 1, "is_full_depth": False})
        rack = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E PWR Rack {suffix}", "site": site["id"],
            "status": "active", "u_height": 20})

        cls._powered_name = f"e2e-pwr-srv-{suffix}"
        cls._passive_name = f"e2e-pwr-pp-{suffix}"
        cls._powered = cls._api("POST", "/api/dcim/devices/", {
            "name": cls._powered_name, "device_type": dt_pwr["id"], "role": role["id"],
            "site": site["id"], "rack": rack["id"], "position": "5.0",
            "face": "front", "status": "active", **cf})
        cls._api("POST", "/api/dcim/devices/", {
            "name": cls._passive_name, "device_type": dt_passive["id"], "role": role["id"],
            "site": site["id"], "rack": rack["id"], "position": "8.0",
            "face": "front", "status": "active", **cf})

        cls._created = {
            "manufacturer": mfr["id"], "role": role["id"], "site": site["id"],
            "rack": rack["id"], "device_types": [dt_pwr["id"], dt_passive["id"]],
        }
        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"pwr-{suffix}", "site": site["id"], "racks": [rack["id"]]})
        cls._design_id = design["id"]
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
                # Devices go with the rack delete (cascade) -- remove rack first,
                # then the types/role/mfr/site.
                for key, path in (
                    ("rack", "/api/dcim/racks/"),
                ):
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
        self.page.wait_for_selector(".nbx-rd-power-bar", timeout=15000)
        self.page.wait_for_timeout(600)

    def tearDown(self):
        if getattr(self, "ctx", None):
            self.ctx.close()

    # ---- the per-rack power bar ------------------------------------------

    def test_power_bar_renders(self):
        bar = self.page.evaluate("""() => {
            const b = document.querySelector('.nbx-rd-power-bar');
            if (!b) return null;
            return {
                draw: b.getAttribute('data-rd-power-draw'),
                capacity: b.getAttribute('data-rd-power-capacity'),
                util: b.getAttribute('data-rd-power-util'),
                unconnected: b.getAttribute('data-rd-power-unconnected'),
                stateClass: [...b.classList].find(c => c.startsWith('nbx-rd-power-') && c !== 'nbx-rd-power-bar'),
                fillWidth: b.querySelector('.nbx-rd-power-fill')?.style.width,
                label: b.querySelector('.nbx-rd-power-label')?.textContent.replace(/\\s+/g,' ').trim(),
            };
        }""")
        self.assertIsNotNone(bar, "power bar not rendered")
        # Only the powered device (400 W) draws; the passive one does not.
        self.assertEqual(bar["draw"], "400", bar)
        self.assertTrue(int(bar["capacity"]) > 0, bar)
        self.assertTrue(bar["stateClass"], bar)
        self.assertIn("W", bar["label"])
        # The powered device's port is uncabled -> flagged.
        self.assertIn(self._powered_name, (bar["unconnected"] or ""), bar)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    # ---- heatmap toggle ---------------------------------------------------

    def test_heatmap_toggle_fills_and_restores(self):
        res = self.page.evaluate("""() => {
            const t = document.querySelector('[data-rd-power-heatmap]');
            t.checked = true; t.dispatchEvent(new Event('change', {bubbles:true}));
            const powered = [...document.querySelectorAll('.grid-stack-item-content')]
                .find(c => c.getAttribute('data-power'));
            const onPct = powered ? powered.style.getPropertyValue('--nbx-rd-heat-pct') : null;
            const bodyOn = document.body.classList.contains('nbx-rd-heatmap-active');
            t.checked = false; t.dispatchEvent(new Event('change', {bubbles:true}));
            const bodyOff = document.body.classList.contains('nbx-rd-heatmap-active');
            const blockOff = !!document.querySelector('.nbx-rd-rack-block.nbx-rd-heatmap');
            return {onPct, bodyOn, bodyOff, blockOff};
        }""")
        self.assertTrue(res["bodyOn"], "heatmap did not activate")
        # The powered device is the biggest (only) consumer -> full bar.
        self.assertEqual(res["onPct"], "100.0%", res)
        self.assertFalse(res["bodyOff"], "heatmap-active not cleared on toggle off")
        self.assertFalse(res["blockOff"], "rack block kept heatmap class after off")
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    # ---- unconnected pull-out highlight ----------------------------------

    def test_bar_hover_pulls_out_unconnected(self):
        res = self.page.evaluate("""(name) => {
            const bar = document.querySelector('.nbx-rd-power-bar');
            bar.dispatchEvent(new MouseEvent('mouseenter', {bubbles:true}));
            const on = [...document.querySelectorAll('.grid-stack-item.nbx-rd-power-flagged')]
                .map(t => t.querySelector('.nbx-rd-label')?.textContent);
            bar.dispatchEvent(new MouseEvent('mouseleave', {bubbles:true}));
            const off = document.querySelectorAll('.grid-stack-item.nbx-rd-power-flagged').length;
            return {on, off};
        }""", self._powered_name)
        self.assertIn(self._powered_name, res["on"],
                      f"unconnected device not pulled out on hover: {res}")
        self.assertNotIn(self._passive_name, res["on"],
                         "a passive device must not be flagged")
        self.assertEqual(res["off"], 0, "pull-out not cleared on mouse-leave")
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    # ---- live recompute (shuffle hardware) -------------------------------

    def test_power_bar_recomputes_live_on_removal(self):
        # Flagging a powered device for removal must drop the rack's projected
        # draw in the browser, with no save/reload (MutationObserver-driven).
        res = self.page.evaluate("""() => {
            const bar = document.querySelector('.nbx-rd-power-bar');
            const before = parseFloat(bar.getAttribute('data-rd-power-draw'));
            const tile = [...document.querySelectorAll('.grid-stack-item')].find(t => {
                const c = t.querySelector('.grid-stack-item-content');
                return c && parseFloat(c.getAttribute('data-draw-w')) > 0
                    && !t.classList.contains('nbx-rd-opposite');
            });
            if (!tile) return {err: 'no powered tile'};
            const dw = parseFloat(tile.querySelector('.grid-stack-item-content')
                .getAttribute('data-draw-w'));
            tile.classList.add('nbx-rd-state-remove');
            return {before, dw, promise: true};
        }""")
        self.assertNotIn("err", res, res)
        # wait for the debounced observer recompute
        self.page.wait_for_timeout(400)
        after = self.page.evaluate(
            "() => parseFloat(document.querySelector('.nbx-rd-power-bar')"
            ".getAttribute('data-rd-power-draw'))")
        self.assertAlmostEqual(
            after, res["before"] - res["dw"], delta=1,
            msg=f"bar did not drop by the removed device's draw: {res}, after={after}")
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    # ---- PSU rows on the hover card --------------------------------------

    def test_hovercard_shows_psu_and_allocated(self):
        text = self.page.evaluate("""() => {
            const c = [...document.querySelectorAll('.grid-stack-item-content')]
                .find(x => x.getAttribute('data-power'));
            if (!c) return null;
            c.dispatchEvent(new PointerEvent('pointerover', {bubbles:true}));
            const card = document.querySelector('.nbx-rd-hovercard');
            return card ? card.textContent.replace(/\\s+/g,' ').trim() : null;
        }""")
        self.assertIsNotNone(text, "hover card did not render")
        self.assertIn("PS psu1", text, text)
        self.assertIn("400 W", text, text)
        self.assertIn("Allocated", text, text)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
