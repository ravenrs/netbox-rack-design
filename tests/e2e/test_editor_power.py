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
        cls._suffix = suffix
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
        cls._powered_model = f"E2E-PWR-Srv-{suffix}"
        cls._passive_model = f"E2E-PWR-PP-{suffix}"
        dt_pwr = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": cls._powered_model,
            "slug": f"e2e-pwr-srv-{suffix}", "u_height": 1, "is_full_depth": False})
        cls._powered_dt_id = dt_pwr["id"]
        cls._api("POST", "/api/dcim/power-port-templates/", {
            "device_type": dt_pwr["id"], "name": "psu1", "allocated_draw": 400})
        # Passive type: no power ports at all (patch-panel analogue).
        dt_passive = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": cls._passive_model,
            "slug": f"e2e-pwr-pp-{suffix}", "u_height": 1, "is_full_depth": False})
        rack = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E PWR Rack {suffix}", "site": site["id"],
            "status": "active", "u_height": 20})
        cls._rack_id = rack["id"]
        cls._rack_u_height = 20

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

    def test_removed_tile_shows_no_heat_fill(self):
        # A device flagged for removal is leaving -- on the heatmap it must NOT
        # keep a colored fill (user bug 2026-07-16: a removed/displaced device
        # showed a heat color with no name -> "лейбла нет, хитмап не верный").
        # A removed body is excluded from countingTiles, so fill() never
        # re-touches it and its heat fill goes STALE unless explicitly cleared.
        res = self.page.evaluate("""() => {
            const t = document.querySelector('[data-rd-power-heatmap]');
            t.checked = true; t.dispatchEvent(new Event('change', {bubbles:true}));
            const tile = [...document.querySelectorAll('.grid-stack-item')].find(x => {
                const c = x.querySelector('.grid-stack-item-content');
                return c && parseFloat(c.getAttribute('data-draw-w')) > 0
                    && !x.classList.contains('nbx-rd-opposite')
                    && !x.getAttribute('data-rd-derived-opp')
                    && !x.classList.contains('nbx-rd-state-remove');
            });
            if (!tile) return {err: 'no powered tile'};
            const c = tile.querySelector('.grid-stack-item-content');
            const before = c.style.getPropertyValue('--nbx-rd-heat-pct');
            tile.setAttribute('data-rd-test-mark', '1');
            tile.classList.add('nbx-rd-state-remove');
            return {before};
        }""")
        self.assertNotIn("err", res, res)
        self.assertNotEqual(
            res["before"], "",
            "precondition: the powered tile should have a heat fill before removal")
        self.page.wait_for_timeout(400)  # debounced observer recompute
        after = self.page.evaluate("""() => {
            const tile = document.querySelector('[data-rd-test-mark="1"]');
            const c = tile.querySelector('.grid-stack-item-content');
            return {
                pct: c.style.getPropertyValue('--nbx-rd-heat-pct'),
                unknown: tile.classList.contains('nbx-rd-heat-unknown'),
            };
        }""")
        self.assertEqual(
            after["pct"], "",
            f"a device flagged for removal must not keep a heatmap fill: {after}")
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

    # ---- palette-add-live: draw travels with a catalog add ---------------

    def test_palette_row_stamped_with_projected_draw(self):
        # The catalog palette fetches each type's projected draw (new
        # device-type-power endpoint) so a freshly dropped add can count LIVE.
        # The powered type's row must carry data-draw-w / data-power; the
        # passive type's row must read known-0.
        # Open the device-catalog drawer (the palette + its search live in a
        # drawer that is closed until its section toggle is clicked).
        self.page.click('[data-rd-section-toggle="device"]')
        self.page.wait_for_selector("#nbx-rd-palette-search", state="visible", timeout=8000)
        # Filter the palette to our throwaway types so they are guaranteed to
        # render (the default top-50 may not include them on a busy instance).
        # Search by this run's unique suffix: killed runs leave E2E-PWR-* types
        # behind and the palette only shows the top 50 matches.
        self.page.fill("#nbx-rd-palette-search", self._suffix)
        # Wait for OUR powered row specifically to be rendered AND stamped by the
        # device-type-power fetch (a pre-existing favorites row could otherwise
        # satisfy a generic [data-draw-w] wait before the search results land).
        self.page.wait_for_function(
            """(model) => {
                const li = [...document.querySelectorAll('.nbx-rd-palette-item')]
                    .find(x => (x.getAttribute('data-model') || '') === model);
                return li && li.getAttribute('data-draw-w') !== null;
            }""",
            arg=self._powered_model, timeout=15000)
        res = self.page.evaluate("""(names) => {
            function row(model) {
                return [...document.querySelectorAll('.nbx-rd-palette-item')]
                    .find(li => (li.getAttribute('data-model') || '') === model);
            }
            const pwr = row(names.powered);
            const pas = row(names.passive);
            return {
                pwrDraw: pwr && pwr.getAttribute('data-draw-w'),
                pwrKnown: pwr && pwr.getAttribute('data-draw-known'),
                pwrPower: pwr && pwr.getAttribute('data-power'),
                pasDraw: pas && pas.getAttribute('data-draw-w'),
                pasKnown: pas && pas.getAttribute('data-draw-known'),
            };
        }""", {"powered": self._powered_model, "passive": self._passive_model})
        self.assertEqual(res["pwrDraw"], "400", res)
        self.assertEqual(res["pwrKnown"], "1", res)
        self.assertIn("psu1:400:", res["pwrPower"] or "", res)
        # Passive type: no power ports -> known 0 (not the unknown hatch).
        self.assertEqual(res["pasDraw"], "0", res)
        self.assertEqual(res["pasKnown"], "1", res)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    def test_favorite_quick_row_stamped_with_projected_draw(self):
        # The favorites (quick-access) palette is populated by a SEPARATE render
        # path (renderQuickAccess) from the search results, so it must stamp the
        # projected draw too: a starred powered type's quick row carries the draw.
        # Star the powered type for this user, reload, then read the quick row.
        try:
            self._api("POST",
                      "/api/plugins/rack-design/favorite-device-types/toggle/",
                      {"device_type_id": self._powered_dt_id})
            self.page.reload(wait_until="networkidle")
            self.page.wait_for_selector(".nbx-rd-power-bar", timeout=15000)
            # force: the Django Debug Toolbar handle can overlap this toggle.
            self.page.click('[data-rd-section-toggle="favorites"]', force=True)
            self.page.wait_for_function(
                """(model) => {
                    const li = [...document.querySelectorAll(
                        '#nbx-rd-quick-list .nbx-rd-palette-item')]
                        .find(x => (x.getAttribute('data-model') || '') === model);
                    return li && li.getAttribute('data-draw-w') !== null;
                }""",
                arg=self._powered_model, timeout=15000)
            res = self.page.evaluate("""(model) => {
                const li = [...document.querySelectorAll(
                    '#nbx-rd-quick-list .nbx-rd-palette-item')]
                    .find(x => (x.getAttribute('data-model') || '') === model);
                return {
                    draw: li && li.getAttribute('data-draw-w'),
                    power: li && li.getAttribute('data-power'),
                };
            }""", self._powered_model)
            self.assertEqual(res["draw"], "400", res)
            self.assertIn("psu1:400:", res["power"] or "", res)
            self.assertEqual(self.errors, [], f"console errors: {self.errors}")
        finally:
            # Unstar so the shared test user's favorites stay clean.
            try:
                self._api(
                    "POST",
                    "/api/plugins/rack-design/favorite-device-types/toggle/",
                    {"device_type_id": self._powered_dt_id})
            except Exception:
                pass

    def test_new_counting_tile_raises_bar_live(self):
        # A freshly added tile that carries a draw (exactly what onPaletteDrop
        # stamps onto the content from the palette row) must be picked up by the
        # MutationObserver and raise the rack's projected draw with no reload.
        before = self.page.evaluate(
            "() => parseFloat(document.querySelector('.nbx-rd-power-bar')"
            ".getAttribute('data-rd-power-draw'))")
        ok = self.page.evaluate("""() => {
            const grid = document.querySelector('.nbx-rd-rack-block .grid-stack');
            if (!grid) return false;
            const item = document.createElement('div');
            item.className = 'grid-stack-item nbx-rd-state-add';
            const c = document.createElement('div');
            c.className = 'grid-stack-item-content';
            c.setAttribute('data-draw-w', '250');
            c.setAttribute('data-draw-known', '1');
            const lab = document.createElement('span');
            lab.className = 'nbx-rd-label';
            lab.textContent = 'live-add-probe';
            c.appendChild(lab);
            item.appendChild(c);
            grid.appendChild(item);
            return true;
        }""")
        self.assertTrue(ok, "no grid to append the probe tile")
        self.page.wait_for_timeout(400)  # debounced observer recompute
        after = self.page.evaluate(
            "() => parseFloat(document.querySelector('.nbx-rd-power-bar')"
            ".getAttribute('data-rd-power-draw'))")
        self.assertAlmostEqual(
            after, before + 250, delta=1,
            msg=f"bar did not rise by the new tile's draw: before={before}, after={after}")
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

    # ---- bank ZONE strip / per-leg legend (docs/pdu-distribution-spec.md) -
    #
    # This fixture's rack carries no real PDUs (deliberately -- see the module
    # docstring), so there is no server-rendered Distribution to read back.
    # Follow the same injection technique as
    # tests.e2e.test_editor_distribution: post a Distribution JSON as
    # ``#rd-distribution-<rackId>`` (the element the server would have
    # emitted) AND intercept the live recompute-distribution endpoint to
    # answer with the SAME blob -- otherwise the real (PDU-less) endpoint
    # would answer null and immediately overwrite the injected static blob,
    # since the live cache always wins (power_heatmap.js
    # distributionForRackId).

    def _distribution(self):
        """A Distribution with two feed legs (a/b), one PDU each, two banks
        that tile the whole rack (U1-10, U11-20) with no gap/overlap -- what
        renderBankStrips needs to draw one full-height column per leg."""
        lower = list(range(1, 11))
        upper = list(range(11, 21))
        return {
            "scheme": "e2e", "pdu_location": "bottom",
            "pdus": {
                "site-pwr-r1-a1": {
                    "feed_name": "a1", "feed_letter": "a", "phase": 1,
                    "allocated_draw": 400, "power_bank_count": 2,
                    "banks": {
                        "1": {"max_power": 1840, "allocated_power": 400,
                              "planned_power": 0, "util_pct": 22, "state": "ok",
                              "units": lower,
                              "devices": [{"name": self._powered_name, "ru": 5,
                                           "draw_w": 400, "status": "active"}]},
                        "2": {"max_power": 1840, "allocated_power": 0,
                              "planned_power": 0, "util_pct": 0, "state": "ok",
                              "units": upper, "devices": []},
                    }},
                "site-pwr-r1-b1": {
                    "feed_name": "b1", "feed_letter": "b", "phase": 1,
                    "allocated_draw": 0, "power_bank_count": 2,
                    "banks": {
                        "1": {"max_power": 1840, "allocated_power": 0,
                              "planned_power": 0, "util_pct": 0, "state": "ok",
                              "units": lower, "devices": []},
                        "2": {"max_power": 1840, "allocated_power": 0,
                              "planned_power": 0, "util_pct": 0, "state": "ok",
                              "units": upper, "devices": []},
                    }},
            },
            "rack": {"power_limitation_w": 6000, "total_w": 400, "alarm": False,
                     "warnings": []},
        }

    def _inject_distribution(self):
        """Inject self._distribution() as the static blob AND as the live
        recompute-distribution answer (see class-note above), then ask the
        module to repaint via its public refresh() hook, and wait for the
        bank chip strip to land before returning."""
        self.page.route(
            "**/recompute-distribution/",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({
                    "distributions": {str(self._rack_id): self._distribution()},
                    "distribution_status": {},
                    "power": {},
                }),
            ),
        )
        self.page.evaluate(
            """([dist, rackId]) => {
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
                window.NbxRdPowerHeatmap.refresh();
            }""",
            [self._distribution(), str(self._rack_id)],
        )
        self.page.wait_for_selector(".nbx-rd-dist-chip", timeout=8000)
        self.page.wait_for_selector(".nbx-rd-bank-strip", timeout=8000)

    def test_bank_zone_strip_renders_per_leg(self):
        self._inject_distribution()
        res = self.page.evaluate(
            """(uHeight) => {
                const wraps = [...document.querySelectorAll('.nbx-rd-grid-wrap')]
                    .filter(w => w.querySelector('.grid-stack.nbx-rd-rack[data-face]'));
                return wraps.map(w => {
                    const strip = w.querySelector('.nbx-rd-bank-strip');
                    if (!strip) return {noStrip: true};
                    const cols = [...strip.querySelectorAll('.nbx-rd-bank-col')];
                    return {
                        colCount: cols.length,
                        cols: cols.map(col => {
                            const label = col.querySelector('.nbx-rd-bank-col-label');
                            const segs = [...col.querySelectorAll('.nbx-rd-bank-seg')];
                            return {
                                label: label ? label.textContent : null,
                                tags: [...col.querySelectorAll('.nbx-rd-bank-pdu-tag')]
                                    .map(t => t.textContent),
                                segs: segs.map(s => ({
                                    bottom: s.style.bottom, height: s.style.height,
                                })),
                            };
                        }),
                    };
                });
            }""",
            self._rack_u_height,
        )
        self.assertTrue(res, "no face grid wrappers found")
        uh = self._rack_u_height
        for wrap in res:
            self.assertNotIn("noStrip", wrap, f"a face grid wrap has no bank strip: {wrap}")
            # Two feed letters (a, b) in the fixture -> two columns.
            self.assertEqual(wrap["colCount"], 2, wrap)
            seen_letters = set()
            for col in wrap["cols"]:
                self.assertIn(col["label"], ("A", "B"), col)
                seen_letters.add(col["label"])
                self.assertTrue(col["tags"], f"column has no PDU tag: {col}")
                # site-pwr-r1-a1 -> "a1", site-pwr-r1-b1 -> "b1".
                self.assertTrue(
                    all(t in ("a1", "b1") for t in col["tags"]), col)
                # Derive each segment's U range from its bottom/height % and
                # assert the column tiles 1..u_height with no gap/overlap.
                ranges = []
                for seg in col["segs"]:
                    self.assertTrue(seg["bottom"].endswith("%"), seg)
                    self.assertTrue(seg["height"].endswith("%"), seg)
                    bottom_pct = float(seg["bottom"].rstrip("%"))
                    height_pct = float(seg["height"].rstrip("%"))
                    lo = round(bottom_pct / 100 * uh) + 1
                    span = round(height_pct / 100 * uh)
                    hi = lo + span - 1
                    ranges.append((lo, hi))
                ranges.sort()
                self.assertEqual(ranges[0][0], 1, f"column does not start at U1: {ranges}")
                self.assertEqual(ranges[-1][1], uh,
                                  f"column does not reach U{uh}: {ranges}")
                for i in range(1, len(ranges)):
                    self.assertEqual(
                        ranges[i][0], ranges[i - 1][1] + 1,
                        f"gap or overlap between segments: {ranges}")
            self.assertEqual(seen_letters, {"A", "B"}, wrap)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    def test_bank_zone_strip_toggle(self):
        self._inject_distribution()
        toggle = self.page.locator("input[data-rd-bank-zones]")
        self.assertTrue(toggle.is_checked(), "bank zones default to on")
        toggle.uncheck()
        off = self.page.evaluate("""() => {
            const wraps = [...document.querySelectorAll('.nbx-rd-grid-wrap')]
                .filter(w => w.querySelector('.grid-stack.nbx-rd-rack[data-face]'));
            return wraps.map(w => ({
                stripGone: !w.querySelector('.nbx-rd-bank-strip'),
                marginLeft: w.style.marginLeft,
            }));
        }""")
        for wrap in off:
            self.assertTrue(wrap["stripGone"], f"strip not removed on toggle-off: {wrap}")
            self.assertEqual(wrap["marginLeft"], "", f"wrapper margin-left not cleared: {wrap}")
        toggle.check()
        on = self.page.evaluate("""() => {
            const wraps = [...document.querySelectorAll('.nbx-rd-grid-wrap')]
                .filter(w => w.querySelector('.grid-stack.nbx-rd-rack[data-face]'));
            return wraps.map(w => !!w.querySelector('.nbx-rd-bank-strip'));
        }""")
        self.assertTrue(all(on), f"strip not restored on toggle-on: {on}")
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    def test_dist_legend_one_column_per_leg(self):
        self._inject_distribution()
        res = self.page.evaluate("""() => {
            const legend = document.querySelector('.nbx-rd-dist-legend');
            if (!legend) return null;
            const legs = [...legend.querySelectorAll('.nbx-rd-dist-leg')];
            return legs.map(leg => {
                const pdus = [...leg.querySelectorAll('.nbx-rd-dist-pdu')];
                return pdus.map(pdu => {
                    const head = pdu.querySelector('.nbx-rd-dist-pdu-head');
                    return {
                        headClasses: head ? [...head.classList] : [],
                    };
                });
            });
        }""")
        self.assertIsNotNone(res, "distribution legend not rendered")
        # Two feed letters (a, b) in the fixture -> exactly two legs.
        self.assertEqual(len(res), 2, res)
        seen = set()
        for leg in res:
            self.assertTrue(leg, "a leg column has no PDU block")
            for pdu in leg:
                letters = [c for c in pdu["headClasses"]
                           if c.startswith("nbx-rd-feedhead-")]
                self.assertEqual(len(letters), 1, pdu)
                seen.add(letters[0])
        self.assertEqual(seen, {"nbx-rd-feedhead-a", "nbx-rd-feedhead-b"}, res)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    # ---- live recompute is gated behind a drag (rack.js setDragActive) ----

    def test_live_recompute_waits_for_drag_end(self):
        # While a drag is active, the mutation-driven SERVER round-trip
        # (recompute-distribution) must be withheld entirely -- the local
        # bar/heat re-render is cheap and still runs on every mutation, but
        # the debounced network call must wait for the gate to release
        # (power_heatmap.js LIVE_DIST_DEBOUNCE_MS = 400ms).
        reqs = []
        self.page.on(
            "request",
            lambda r: reqs.append(r.url) if "/recompute-distribution/" in r.url else None)
        marked = self.page.evaluate("""() => {
            const tile = [...document.querySelectorAll('.grid-stack-item')].find(t => {
                const c = t.querySelector('.grid-stack-item-content');
                return c && parseFloat(c.getAttribute('data-draw-w')) > 0
                    && !t.classList.contains('nbx-rd-opposite');
            });
            if (!tile) return false;
            tile.setAttribute('data-rd-test-mark', '1');
            return true;
        }""")
        self.assertTrue(marked, "no powered tile to mutate")
        self.page.evaluate("() => window.NbxRdPowerHeatmap.setDragActive(true)")
        # Mutate the tile twice (toggle a class on then off) the way the
        # existing live tests do -- each toggle is a real attribute mutation
        # the observer sees, but the gate must swallow both while it is up.
        self.page.evaluate("""() => {
            const tile = document.querySelector('[data-rd-test-mark="1"]');
            tile.classList.add('nbx-rd-state-remove');
            tile.classList.remove('nbx-rd-state-remove');
        }""")
        self.page.wait_for_timeout(700)
        self.assertEqual(reqs, [], f"recompute fired while the drag gate was up: {reqs}")
        self.page.evaluate("() => window.NbxRdPowerHeatmap.setDragActive(false)")
        self.page.wait_for_timeout(700)
        self.assertEqual(
            len(reqs), 1,
            f"expected exactly one recompute once the drag gate released: {reqs}")
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
