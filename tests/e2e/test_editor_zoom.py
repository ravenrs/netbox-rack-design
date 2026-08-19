#!/usr/bin/env python3
"""Playwright e2e: a drag lands where the CURSOR points at any browser zoom level.

Regression test for the zoom-dependent misplacement (reported 2026-08-19): at any
zoom other than 100% a dragged device landed up to several rows away from the green
landing preview, worst on multi-U devices.

Root cause -- two row computations in different spaces:

  * the plugin measures the host's real rect, so ``rect.height / rowCount`` tracks
    zoom (11px at 100%, 12.0999 at 110%, 16.5 at 150%) and agrees with the pointer
    coordinates, which are in the same space;
  * GridStack uses a FIXED ``cellHeight`` of 11 CSS px, so its row is only right at
    100%.

``enforceCursorPlacement`` used to accept the engine's row whenever the pointer fell
inside the tile as parked -- comparing rows from those two spaces -- so under zoom it
handed placement to the wrong number while the preview showed the right one. The
cursor is authoritative (spec 4.1), so its row is now always enforced.

Also guards the whole-U snap for MOVES: ``grabRows`` and the pointer row are floored
independently, so drifting half a unit vertically used to flip parity and land a
whole-U device on a half-unit boundary.

SELF-PROVISIONING: creates its own manufacturer / role / site / 2U device type / two
racks / one device, then a design over both racks. Skips cleanly when
playwright/Chrome or the dev server aren't available.

STRICTLY READ-ONLY as far as the design is concerned: it drags and inspects in-page
state, never clicks Save. Its own fixture objects are deleted in tearDownClass.

Run via ``dev/e2e.sh tests.e2e.test_editor_zoom``.
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

# Browser zoom factors to exercise. 1.0 must keep working; the others are the
# regression. Values chosen to give both integral (1.25 -> 13.75) and repeating
# (1.1 -> 12.0999...) row heights, since float noise is part of the bug.
ZOOM_LEVELS = (1.0, 1.1, 1.25, 1.5, 0.9, 0.8)

RACK_U = 20          # rack height in units
ROWS_PER_U = 2       # the editor's grid is 0.5U per row
DEVICE_U = 2         # a multi-U device: widest window for the old engine-wins bug


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


@unittest.skipUnless(_PREREQ_OK, f"zoom e2e prerequisites not met: {_PREREQ_REASON}")
class EditorZoomPlacementTestCase(unittest.TestCase):

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
            "name": f"E2E ZOOM Mfr {suffix}", "slug": f"e2e-zoom-mfr-{suffix}"})
        role = cls._api("POST", "/api/dcim/device-roles/", {
            "name": f"E2E ZOOM Role {suffix}", "slug": f"e2e-zoom-role-{suffix}",
            "color": "9e9e9e"})
        site = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E ZOOM Site {suffix}", "slug": f"e2e-zoom-site-{suffix}",
            "status": "active"})
        dt = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-ZOOM-Srv-{suffix}",
            "slug": f"e2e-zoom-srv-{suffix}", "u_height": DEVICE_U,
            "is_full_depth": False})
        src_rack = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E ZOOM Rack A {suffix}", "site": site["id"],
            "status": "active", "u_height": RACK_U})
        dst_rack = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E ZOOM Rack B {suffix}", "site": site["id"],
            "status": "active", "u_height": RACK_U})

        # One 2U device, mounted on a WHOLE unit, near the top of the source rack.
        # The dev instance carries a required custom field on Device; send it empty
        # (same as the other e2e fixtures) so provisioning works on either instance.
        cf = {"custom_fields": {"warranty_type": ""}}
        cls._device_name = f"e2e-zoom-srv-{suffix}"
        cls._api("POST", "/api/dcim/devices/", {
            "name": cls._device_name, "device_type": dt["id"], "role": role["id"],
            "site": site["id"], "rack": src_rack["id"], "position": "15.0",
            "face": "front", "status": "active", **cf})

        cls._created = {
            "racks": [src_rack["id"], dst_rack["id"]], "device_types": [dt["id"]],
            "role": role["id"], "manufacturer": mfr["id"], "site": site["id"],
        }
        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"zoom-{suffix}", "site": site["id"],
            "racks": [src_rack["id"], dst_rack["id"]]})
        cls._design_id = design["id"]
        cls._src_rack = src_rack["id"]
        cls._dst_rack = dst_rack["id"]
        # No rack filter: both racks must be on screen for a cross-rack drag.
        cls.editor_url = f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/"

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
        # Wide enough that both racks stay fully on screen even at 1.5x zoom -- at
        # 1600px the destination rack reaches the viewport edge, where the debug
        # toolbar handle can swallow the drop.
        self.ctx = self._browser.new_context(
            storage_state=self._storage, viewport={"width": 2400, "height": 1600})
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

    # ----------------------------------------------------------------- helpers

    def _set_zoom(self, zoom):
        """Apply a browser-zoom-equivalent scale. CSS zoom on <body> scales layout
        the same way the browser's own zoom does, so the host rect goes fractional
        exactly as it does at 110% in a real window."""
        self.page.evaluate(
            "(z) => { document.body.style.zoom = z === 1 ? '' : String(z); }", zoom)
        self.page.wait_for_timeout(350)

    def _drag_to_row(self, target_top_row, grab_frac=0.5):
        """Drag the fixture device from its rack into the destination rack so that
        its TOP lands on ``target_top_row``, grabbing ``grab_frac`` down the tile.

        Aims using the VISUAL row height (rect height / row count) -- the same space
        the pointer lives in. Using GridStack's fixed cellHeight here would mis-aim
        the drop under zoom and test nothing.
        """
        geo = self.page.evaluate(
            """([name, srcRack, dstRack, targetTop, grabFrac]) => {
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
              const rowPx = dr.height / rows;          // visual row height
              const gsH = tile.gridstackNode ? tile.gridstackNode.h : null;
              const grabRows = Math.floor(grabFrac * (gsH || 1));
              return {
                rowPx, rows, gsH, grabRows,
                srcY: tile.gridstackNode ? tile.gridstackNode.y : null,
                grabX: tr.left + tr.width / 2,
                grabY: tr.top + tr.height * grabFrac,
                dropX: dr.left + dr.width / 2,
                // Pointer must sit grabRows below the intended top row.
                dropY: dr.top + (targetTop + grabRows) * rowPx + rowPx / 2,
              };
            }""",
            [self._device_name, self._src_rack, self._dst_rack, target_top_row,
             grab_frac],
        )
        self.assertNotIn("err", geo, f"drag setup failed: {geo.get('err')}")

        self.page.mouse.move(geo["grabX"], geo["grabY"])
        self.page.mouse.down()
        # A few px first, to trip GridStack's drag threshold, then glide. The small
        # per-step pause lets the editor's pointermove tracker and GridStack's own
        # drag handler run: without it the gesture never becomes a drag at all.
        self.page.mouse.move(geo["grabX"] + 6, geo["grabY"] + 6)
        self.page.wait_for_timeout(60)
        steps = 12
        for i in range(1, steps + 1):
            self.page.mouse.move(
                geo["grabX"] + (geo["dropX"] - geo["grabX"]) * i / steps,
                geo["grabY"] + (geo["dropY"] - geo["grabY"]) * i / steps,
            )
            self.page.wait_for_timeout(25)
        # The green landing preview, sampled while the button is still down.
        preview = self.page.evaluate(
            """() => {
              const el = document.querySelector('.nbx-rd-cursor-allow');
              if (!el || !el.parentElement) return null;
              const host = el.parentElement;
              const rows = host.gridstack ? host.gridstack.getRow() : null;
              const rowPx = rows ? host.getBoundingClientRect().height / rows : null;
              return rowPx ? Math.round(parseFloat(el.style.top) / rowPx) : null;
            }""")
        self.page.mouse.up()
        self.page.wait_for_timeout(700)

        landed = self.page.evaluate(
            """([name, dstRack]) => {
              // A committed cross-rack move leaves a move_out_ghost behind in the
              // SOURCE rack carrying the same device name, and it precedes the real
              // tile in DOM order -- so filter the ghosts out before picking.
              const tile = [...document.querySelectorAll('.grid-stack-item')]
                .filter(el => !el.classList.contains('nbx-rd-state-move_out_ghost'))
                .find(el => (el.innerText || '').includes(name));
              if (!tile) return {err: 'tile vanished'};
              const host = tile.parentElement;
              const block = host ? host.closest('.nbx-rd-rack-block') : null;
              const n = tile.gridstackNode;
              return {
                y: n ? n.y : null, h: n ? n.h : null,
                rack: block ? parseInt(block.getAttribute('data-rack-id'), 10) : null,
                inDestination: !!block
                  && block.getAttribute('data-rack-id') === String(dstRack),
              };
            }""",
            [self._device_name, self._dst_rack])
        return geo, preview, landed

    # ------------------------------------------------------------------- tests

    def test_cross_rack_drop_lands_on_cursor_row_at_every_zoom(self):
        """The tile lands on the row the cursor targets, at every zoom level.

        Before the fix the engine's fixed-cellHeight row won whenever the pointer
        fell inside the parked tile, so anything but 100% landed rows away.
        """
        target_top = 6  # 0.5U rows from the rack top; a whole-U boundary
        for zoom in ZOOM_LEVELS:
            with self.subTest(zoom=zoom):
                self.page.reload(wait_until="networkidle")
                self.page.wait_for_selector(".nbx-rd-rack-block", timeout=15000)
                self.page.wait_for_timeout(300)
                self._set_zoom(zoom)

                geo, preview, landed = self._drag_to_row(target_top)

                self.assertNotIn("err", landed, f"zoom {zoom}: {landed.get('err')}")
                self.assertTrue(
                    landed["inDestination"],
                    f"zoom {zoom}: tile did not reach the destination rack "
                    f"(landed in rack {landed['rack']})")
                self.assertEqual(
                    landed["y"], target_top,
                    f"zoom {zoom}: tile landed on row {landed['y']}, expected "
                    f"{target_top} (visual rowPx={geo['rowPx']:.4f}); "
                    f"green preview showed row {preview}")
                # The preview must agree with the landing: the reported symptom was
                # "the frame says here, the device goes there".
                if preview is not None:
                    self.assertEqual(
                        preview, landed["y"],
                        f"zoom {zoom}: green preview showed row {preview} but the "
                        f"tile landed on {landed['y']}")
                self.assertEqual(self.errors, [], f"zoom {zoom}: console errors")

    def test_whole_u_device_never_lands_on_a_half_unit(self):
        """A whole-U device grabbed anywhere in its body keeps whole-U alignment.

        ``grabRows`` and the pointer row are floored independently, so without the
        move-side snap a mid-body grab could flip parity and land the device on a
        half-unit boundary (an odd row).
        """
        for grab_frac in (0.0, 0.25, 0.5, 0.75, 0.9):
            with self.subTest(grab_frac=grab_frac):
                self.page.reload(wait_until="networkidle")
                self.page.wait_for_selector(".nbx-rd-rack-block", timeout=15000)
                self.page.wait_for_timeout(300)

                _geo, _preview, landed = self._drag_to_row(6, grab_frac=grab_frac)

                self.assertNotIn("err", landed, str(landed.get("err")))
                self.assertTrue(
                    landed["inDestination"],
                    f"grab_frac {grab_frac}: tile never reached the destination rack "
                    f"-- the alignment assertion below would pass vacuously")
                self.assertEqual(
                    landed["y"] % ROWS_PER_U, 0,
                    f"grab_frac {grab_frac}: a {DEVICE_U}U device landed on row "
                    f"{landed['y']}, a half-unit boundary")


if __name__ == "__main__":
    unittest.main(verbosity=2)
