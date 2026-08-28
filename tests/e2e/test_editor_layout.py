#!/usr/bin/env python3
"""Playwright e2e: workspace layout — rack floors, an empty workspace, × labels.

Three things the Python suite cannot see, all reported 2026-08-28:

1. **Racks of different heights hung from a common ceiling.** A 20U and a 40U
   rack started at the same y, so the taller one just ran further down instead
   of standing taller, and U1 of one sat level with U21 of the other. They now
   share a FLOOR: rack_layout.js pads the shorter block's face rows by the
   height difference (``--nbx-rd-floor-pad``).

2. **An empty workspace collapsed to a thin strip.** With no rack rendered (a
   design with zero racks, or every rack hidden) the racks region fell to the
   height of whatever was left, and because the drawer columns stretch to the
   shell, the Add-rack panel collapsed with it — making the first rack painful
   to add. The region now keeps a workspace-sized floor.

3. **The red × said "Flag for removal" on every tile.** On a planned move it
   cancels the move; on a planned add it cancels the add. Nothing is flagged
   for removal there, and the label now says what the click will do.

SELF-PROVISIONING: creates its own manufacturer / role / site / device type /
two racks of DIFFERENT heights / one device, then a design over both racks.
Skips cleanly when playwright/Chrome or the dev server aren't available.

STRICTLY READ-ONLY as far as the design is concerned: it drags and inspects
in-page state, never clicks Save. Its fixtures are deleted in tearDownClass.

Run via ``dev/e2e.sh tests.e2e.test_editor_layout``.
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

SHORT_U = 20         # the short rack
TALL_U = 40          # the tall one: a full 20U taller, unmissable when misaligned
DEVICE_U = 2


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


@unittest.skipUnless(_PREREQ_OK, f"layout e2e prerequisites not met: {_PREREQ_REASON}")
class EditorWorkspaceLayoutTestCase(unittest.TestCase):

    # ---------------------------------------------------------------- fixture

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
        mfr = cls._api("POST", "/api/dcim/manufacturers/", {
            "name": f"E2E LAY Mfr {suffix}", "slug": f"e2e-lay-mfr-{suffix}"})
        role = cls._api("POST", "/api/dcim/device-roles/", {
            "name": f"E2E LAY Role {suffix}", "slug": f"e2e-lay-role-{suffix}",
            "color": "9e9e9e"})
        site = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E LAY Site {suffix}", "slug": f"e2e-lay-site-{suffix}",
            "status": "active"})
        dt = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-LAY-Srv-{suffix}",
            "slug": f"e2e-lay-srv-{suffix}", "u_height": DEVICE_U,
            "is_full_depth": False})
        short_rack = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E LAY Rack Short {suffix}", "site": site["id"],
            "status": "active", "u_height": SHORT_U})
        tall_rack = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E LAY Rack Tall {suffix}", "site": site["id"],
            "status": "active", "u_height": TALL_U})

        cf = {"custom_fields": {"warranty_type": ""}}
        cls._device_name = f"e2e-lay-srv-{suffix}"
        cls._api("POST", "/api/dcim/devices/", {
            "name": cls._device_name, "device_type": dt["id"], "role": role["id"],
            "site": site["id"], "rack": short_rack["id"], "position": "15.0",
            "face": "front", "status": "active", **cf})

        cls._created = {
            "racks": [short_rack["id"], tall_rack["id"]], "device_types": [dt["id"]],
            "role": role["id"], "manufacturer": mfr["id"], "site": site["id"],
        }
        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"layout-{suffix}", "site": site["id"],
            "racks": [short_rack["id"], tall_rack["id"]]})
        cls._design_id = design["id"]
        cls._short_rack = short_rack["id"]
        cls._tall_rack = tall_rack["id"]
        cls.editor_url = f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/"
        cls.elevation_url = f"{BASE}/plugins/rack-design/designs/{cls._design_id}/elevation/"

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
                for rid in created.get("racks", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/racks/{rid}/")
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
            storage_state=self._storage, viewport={"width": 2400, "height": 1600})
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on(
            "console",
            lambda m: self.errors.append(f"{m.type}: {m.text}")
            if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"PAGEERROR: {e}"))

    def tearDown(self):
        if getattr(self, "ctx", None):
            self.ctx.close()

    # ----------------------------------------------------------------- helpers

    def _open(self, url):
        resp = self.page.goto(url, wait_until="networkidle")
        self.assertEqual(resp.status, 200, f"{url} returned {resp.status}")
        self.page.wait_for_selector(".nbx-rd-rack-block", timeout=15000)
        self.page.wait_for_timeout(600)

    def _floors(self):
        """Per rack block: the rendered front grid's top/bottom in page space."""
        return self.page.evaluate(
            """() => {
              const out = {};
              document.querySelectorAll('.nbx-rd-rack-block').forEach(block => {
                const face = block.querySelector('.nbx-rd-face[data-rd-face="front"]');
                const wrap = face ? face.querySelector('.nbx-rd-grid-wrap') : null;
                if (!wrap) return;
                const r = wrap.getBoundingClientRect();
                out[block.getAttribute('data-rack-id')] = {
                  top: r.top, bottom: r.bottom, height: r.height,
                  pad: block.style.getPropertyValue('--nbx-rd-floor-pad') || '',
                };
              });
              return out;
            }""")

    # ------------------------------------------------------------------- tests

    def test_racks_of_different_heights_share_a_floor(self):
        """The 20U and the 40U rack end at the same y; the taller one starts higher."""
        self._open(self.editor_url)
        floors = self._floors()
        short = floors[str(self._short_rack)]
        tall = floors[str(self._tall_rack)]

        self.assertGreater(
            tall["height"], short["height"] + 50,
            "fixture is wrong: the two racks render at the same height")
        self.assertAlmostEqual(
            short["bottom"], tall["bottom"], delta=2.0,
            msg=f"rack floors are {abs(short['bottom'] - tall['bottom']):.1f}px "
                f"apart — the blocks are still ceiling-aligned")
        self.assertGreater(
            short["top"], tall["top"] + 50,
            "the short rack must start lower down, so the tall one reads as taller")
        self.assertTrue(
            short["pad"], "the short rack carries no --nbx-rd-floor-pad")
        self.assertEqual(self.errors, [], "console errors")

    def test_the_read_only_elevation_shares_the_same_floor(self):
        """Same alignment on the projected elevation — it renders the same blocks."""
        self._open(self.elevation_url)
        floors = self._floors()
        short = floors[str(self._short_rack)]
        tall = floors[str(self._tall_rack)]
        self.assertAlmostEqual(
            short["bottom"], tall["bottom"], delta=2.0,
            msg="elevation rack floors are not aligned")
        self.assertEqual(self.errors, [], "console errors")

    def test_hiding_every_rack_keeps_a_usable_workspace(self):
        """With nothing rendered the workspace keeps a floor instead of collapsing."""
        self._open(self.editor_url)
        height = self.page.evaluate(
            """() => {
              document.querySelectorAll('.nbx-rd-rack-block')
                .forEach(b => b.classList.add('hidden'));
              const scroll = document.getElementById('nbx-rd-racks-scroll');
              return scroll.getBoundingClientRect().height;
            }""")
        self.assertGreater(
            height, 300,
            f"the racks workspace collapsed to {height:.0f}px with every rack "
            f"hidden — the drawer stretches to it, so Add-rack collapses too")
        self.assertEqual(self.errors, [], "console errors")

    def test_the_cancel_button_names_what_it_cancels(self):
        """× on a planned move says 'Cancel this planned move', not 'Flag for removal'."""
        self._open(self.editor_url)

        # Untouched real device: the × really does flag it for removal.
        before = self._remove_btn_title(self._device_name)
        self.assertEqual(before, "Flag for removal")

        self._drag_device_to_tall_rack()

        after = self._remove_btn_title(self._device_name)
        self.assertEqual(
            after, "Cancel this planned move",
            f"the × on a planned move still says {after!r}")
        self.assertEqual(self.errors, [], "console errors")

    def _remove_btn_title(self, name):
        return self.page.evaluate(
            """(name) => {
              const tile = [...document.querySelectorAll('.grid-stack-item')]
                .filter(el => !el.classList.contains('nbx-rd-state-move_out_ghost'))
                .find(el => (el.innerText || '').includes(name));
              if (!tile) return null;
              const btn = tile.querySelector('.nbx-rd-remove-btn');
              return btn ? btn.getAttribute('title') : null;
            }""", name)

    def _drag_device_to_tall_rack(self):
        geo = self.page.evaluate(
            """([name, srcRack, dstRack]) => {
              const hostOf = (rackId) => {
                const block = document.querySelector(
                  `.nbx-rd-rack-block[data-rack-id="${rackId}"]`);
                if (!block) return null;
                return [...block.querySelectorAll('.grid-stack')]
                  .find(h => h.getAttribute('data-face') === 'front') || null;
              };
              const src = hostOf(srcRack), dst = hostOf(dstRack);
              if (!src || !dst) return {err: 'face host not found'};
              const tile = [...src.querySelectorAll('.grid-stack-item')]
                .find(el => (el.innerText || '').includes(name));
              if (!tile) return {err: 'tile not found: ' + name};
              const tr = tile.getBoundingClientRect();
              const dr = dst.getBoundingClientRect();
              const rows = dst.gridstack ? dst.gridstack.getRow() : null;
              if (!rows) return {err: 'destination row count unknown'};
              const rowPx = dr.height / rows;
              return {
                grabX: tr.left + tr.width / 2,
                grabY: tr.top + tr.height / 2,
                dropX: dr.left + dr.width / 2,
                dropY: dr.top + 7 * rowPx + rowPx / 2,
              };
            }""",
            [self._device_name, self._short_rack, self._tall_rack])
        self.assertNotIn("err", geo, f"drag setup failed: {geo.get('err')}")

        self.page.mouse.move(geo["grabX"], geo["grabY"])
        self.page.mouse.down()
        self.page.mouse.move(geo["grabX"] + 6, geo["grabY"] + 6)
        self.page.wait_for_timeout(60)
        steps = 12
        for i in range(1, steps + 1):
            self.page.mouse.move(
                geo["grabX"] + (geo["dropX"] - geo["grabX"]) * i / steps,
                geo["grabY"] + (geo["dropY"] - geo["grabY"]) * i / steps,
            )
            self.page.wait_for_timeout(25)
        self.page.mouse.up()
        self.page.wait_for_timeout(900)
        # The cross-rack dialog names the move; accept whatever it proposes.
        self._accept_any_dialog()
        self.page.wait_for_timeout(600)

    def _accept_any_dialog(self):
        self.page.evaluate(
            """() => {
              const btn = [...document.querySelectorAll('.modal.show button, dialog button')]
                .find(b => /ok|confirm|apply|move|save/i.test(b.textContent || ''));
              if (btn) btn.click();
            }""")


if __name__ == "__main__":
    unittest.main(verbosity=2)
