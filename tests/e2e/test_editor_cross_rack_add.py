#!/usr/bin/env python3
"""Playwright coverage for carrying a PLANNED ADD into another rack (spec §4.6).

Why its own suite, driven by a real mouse: the cross-rack sweep moves tiles
through a JS shim, which calls the adoption hooks directly and therefore never
consults the destination grid's ``acceptWidgets``. That gate is exactly where
this gesture died -- a planned add was refused by every foreign rack, so the
tile snapped back with nothing logged and nothing saved (user 2026-08-27: "I
just cannot move a new device into another rack; within the same rack I can").

SELF-PROVISIONING like the sibling suites: two racks of one site are discovered
via the API, the design is created and deleted around the run, and save-layout
is intercepted, so the only writes are the design create/delete.
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
class EditorCrossRackAddTestCase(unittest.TestCase):
    """A planned add is a tile like any other: it may change racks."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(channel="chrome", headless=True)
        cls._design_id = None
        cls._ctx = cls._browser.new_context(viewport={"width": 1900, "height": 1200})
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
            cls._provision()
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
    def _provision(cls):
        by_site = {}
        for rack in cls._api("GET", "/api/dcim/racks/?limit=200")["results"]:
            by_site.setdefault(rack["site"]["id"], []).append(rack)
        pair = next((v[:2] for v in by_site.values() if len(v) >= 2), None)
        if not pair:
            raise unittest.SkipTest("no site with two racks in this data")
        cls.rack_a, cls.rack_b = pair[0]["id"], pair[1]["id"]
        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"e2e-xrack-add-{uuid.uuid4().hex[:8]}",
            "site": pair[0]["site"]["id"], "status": "draft",
            "racks": [cls.rack_a, cls.rack_b],
        })
        cls._design_id = design["id"]
        cls.url = (
            f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/{cls.rack_a}/")

        # A second design for the chassis half of the same gesture: one rack
        # holding TWO chassis that each still have a free bay. Skipped, never
        # failed, when the deployment has no such hardware.
        cls.chassis_url = None
        devs, by_rack = {}, {}
        for bay in cls._api(
                "GET", "/api/dcim/device-bays/?installed_device_id=null&limit=300"
        )["results"]:
            did = bay["device"]["id"]
            if did not in devs:
                devs[did] = cls._api("GET", f"/api/dcim/devices/{did}/")
            dev = devs[did]
            if dev.get("rack"):
                by_rack.setdefault(dev["rack"]["id"], set()).add(did)
        pick = next((rk for rk, v in by_rack.items() if len(v) >= 2), None)
        if pick is not None:
            site = devs[next(iter(by_rack[pick]))]["site"]["id"]
            chassis_design = cls._api("POST", "/api/plugins/rack-design/designs/", {
                "title": f"e2e-xchassis-add-{uuid.uuid4().hex[:8]}",
                "site": site, "status": "draft", "racks": [pick],
            })
            cls._chassis_design_id = chassis_design["id"]
            cls.chassis_url = (
                f"{BASE}/plugins/rack-design/designs/{chassis_design['id']}/chassis/")

    @classmethod
    def _cleanup_class(cls):
        for attr in ("_design_id", "_chassis_design_id"):
            try:
                pk = getattr(cls, attr, None)
                if pk:
                    cls._api("DELETE", f"/api/plugins/rack-design/designs/{pk}/")
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

    def setUp(self):
        self.ctx = self._browser.new_context(
            storage_state=self._ctx.storage_state(),
            viewport={"width": 1900, "height": 1200})
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on("console", lambda m: self.errors.append(m.text)
                     if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"PAGEERROR: {e}"))
        self._open(self.url)

    def _open(self, url):
        self.page.goto(url, wait_until="networkidle")
        self.page.wait_for_selector(".grid-stack", timeout=30000)
        self.page.wait_for_timeout(1000)
        self.page.evaluate(
            "() => ['djDebug', 'djDebugRoot'].forEach(function (id) {"
            "  const d = document.getElementById(id);"
            "  if (d) { d.style.display = 'none'; d.style.pointerEvents = 'none'; }"
            "})")

    def tearDown(self):
        errs = [e for e in self.errors if "favicon" not in e]
        try:
            self.ctx.close()
        finally:
            self.assertEqual(errs, [], f"console errors: {errs}")

    # -- helpers ------------------------------------------------------------

    def _grid(self, rack_id):
        return f"nbx-rd-grid-front-{rack_id}"

    def _free_row(self, grid_id):
        return self.page.evaluate(f"""() => {{
            const g = document.getElementById('{grid_id}');
            const taken = new Set();
            for (const t of g.querySelectorAll('.grid-stack-item')) {{
                const n = t.gridstackNode; if (!n) continue;
                for (let i = 0; i < n.h; i++) taken.add(n.y + i);
            }}
            const rows = parseInt(g.getAttribute('gs-max-row'), 10) || 84;
            for (let y = 0; y < rows - 2; y += 2) {{
                if (!taken.has(y) && !taken.has(y + 1)) return y;
            }}
            return null;
        }}""")

    def _drop_a_planned_add(self, grid_id):
        """Real-mouse palette drop of the first non-child type into `grid_id`."""
        box = self.page.query_selector("#nbx-rd-palette-search")
        if not (box and box.is_visible()):
            self.page.click('[data-rd-section-toggle="device"]')
        self.page.wait_for_selector("#nbx-rd-palette-search", state="visible", timeout=15000)
        self.page.wait_for_timeout(2500)
        rows = self.page.query_selector_all("#nbx-rd-palette-list .nbx-rd-palette-item")
        self.assertTrue(rows, "palette returned no device types")
        src = next(
            (r for r in rows if r.get_attribute("data-subdevice-role") != "child"), None)
        self.assertIsNotNone(src, "no rack-mountable type offered")
        src.scroll_into_view_if_needed()
        self.page.wait_for_timeout(200)
        row = self._free_row(grid_id)
        self.assertIsNotNone(row, "the source rack has no free 1U row")
        cell = self.page.evaluate(
            f"() => document.getElementById('{grid_id}').gridstack.getCellHeight()")
        s, d = src.bounding_box(), self.page.query_selector("#" + grid_id).bounding_box()
        self.page.mouse.move(s["x"] + s["width"] / 2, s["y"] + s["height"] / 2)
        self.page.mouse.down()
        self.page.mouse.move(d["x"] + d["width"] / 2, d["y"] + row * cell + cell, steps=25)
        self.page.wait_for_timeout(400)
        self.page.mouse.up()
        self.page.wait_for_timeout(1500)

    def _capture_save(self):
        captured = {}

        def handler(route):
            try:
                captured.update(route.request.post_data_json or {})
            except Exception:
                pass
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"status": "ok"}))

        self.page.route("**/save-layout/", handler)
        self.page.click("#rd-editor-save")
        self.page.wait_for_timeout(2000)
        return captured

    # -- the test -----------------------------------------------------------

    def test_a_planned_add_can_be_carried_into_another_rack(self):
        ga, gb = self._grid(self.rack_a), self._grid(self.rack_b)
        self._drop_a_planned_add(ga)
        tile = self.page.query_selector(f"#{ga} .nbx-rd-state-add")
        self.assertIsNotNone(tile, "the palette drop produced no add tile")

        # Real mouse, both endpoints on screen: a drag whose target is outside
        # the viewport silently does nothing and would fake a pass.
        tb = tile.bounding_box()
        dst = self.page.query_selector("#" + gb).bounding_box()
        self.assertLess(dst["x"], 1900, "the destination rack must be on screen")
        self.page.mouse.move(tb["x"] + tb["width"] / 2, tb["y"] + tb["height"] / 2)
        self.page.mouse.down()
        self.page.mouse.move(tb["x"] + tb["width"] / 2 + 40, tb["y"] + tb["height"] / 2,
                             steps=6)
        self.page.mouse.move(dst["x"] + dst["width"] / 2, dst["y"] + 300, steps=30)
        self.page.wait_for_timeout(600)
        self.page.mouse.up()
        self.page.wait_for_timeout(1800)

        self.assertEqual(
            self.page.query_selector_all(f"#{ga} .nbx-rd-state-add"), [],
            "the add must leave the source rack -- no copy stays behind")
        self.assertEqual(
            len(self.page.query_selector_all(f"#{gb} .nbx-rd-state-add")), 1,
            "the add must land in the destination rack")

        payload = self._capture_save()
        adds = [
            (rack["rack_id"], item)
            for rack in payload.get("racks", [])
            for bucket in ("front", "rear", "other")
            for item in rack.get(bucket, [])
            if item.get("kind") == "add"
        ]
        self.assertEqual(
            [rack_id for rack_id, _ in adds], [self.rack_b],
            f"the add must be posted exactly once, under the destination rack: {adds}")
        self.assertIsNotNone(adds[0][1].get("u_position"),
                             "the moved add must carry its new slot")
        self.assertTrue(adds[0][1].get("proposed_name"),
                        "the moved add must keep the name it was given")


    def test_a_planned_blade_can_be_carried_into_another_chassis(self):
        """The same gesture one layer down: a chassis is a Frame with one
        Container (spec §2.6), so a planned blade crosses chassis columns exactly
        as a planned add crosses racks -- and its address must come from the
        DESTINATION column, not the one it started in."""
        if not self.chassis_url:
            self.skipTest("no rack holding two chassis with a free bay in this data")
        self._open(self.chassis_url)
        cols = self.page.evaluate("""() => {
            const out = [];
            for (const b of document.querySelectorAll('.nbx-rd-chassis-block')) {
                const total = parseInt(b.dataset.uHeight, 10);
                const taken = new Set([...b.querySelectorAll('.grid-stack-item')]
                    .map(t => t.gridstackNode ? t.gridstackNode.y : -1));
                for (let i = 0; i < total; i++) {
                    if (!taken.has(i * 2)) {
                        out.push({gid: 'nbx-rd-grid-front-' + b.dataset.rackId,
                                  idx: i, chassis: parseInt(b.dataset.rackId, 10)});
                        break;
                    }
                }
            }
            return out;
        }""")
        if len(cols) < 2:
            self.skipTest("fewer than two chassis columns with a free bay on screen")
        src, dst = cols[0], cols[1]

        box = self.page.query_selector("#nbx-rd-palette-search")
        if not (box and box.is_visible()):
            self.page.click('[data-rd-section-toggle="device"]')
        self.page.wait_for_selector("#nbx-rd-palette-search", state="visible", timeout=15000)
        self.page.wait_for_timeout(2500)
        row = self.page.query_selector("#nbx-rd-palette-list .nbx-rd-palette-item")
        self.assertIsNotNone(row, "the chassis palette offered nothing")
        row.scroll_into_view_if_needed()
        self.page.wait_for_timeout(200)
        s, t = row.bounding_box(), self.page.query_selector("#" + src["gid"]).bounding_box()
        self.page.mouse.move(s["x"] + s["width"] / 2, s["y"] + s["height"] / 2)
        self.page.mouse.down()
        self.page.mouse.move(t["x"] + t["width"] / 2, t["y"] + src["idx"] * 22 + 11, steps=25)
        self.page.wait_for_timeout(350)
        self.page.mouse.up()
        self.page.wait_for_timeout(2500)
        tile = self.page.query_selector(f"#{src['gid']} .nbx-rd-state-add")
        self.assertIsNotNone(tile, "the blade was not planned into the source chassis")

        tb = tile.bounding_box()
        db = self.page.query_selector("#" + dst["gid"]).bounding_box()
        self.assertLess(db["x"], 1900, "the destination column must be on screen")
        self.page.mouse.move(tb["x"] + tb["width"] / 2, tb["y"] + tb["height"] / 2)
        self.page.mouse.down()
        self.page.mouse.move(tb["x"] + tb["width"] / 2 + 30, tb["y"] + tb["height"] / 2, steps=6)
        self.page.mouse.move(db["x"] + db["width"] / 2, db["y"] + dst["idx"] * 22 + 11, steps=30)
        self.page.wait_for_timeout(600)
        self.page.mouse.up()
        self.page.wait_for_timeout(2000)

        self.assertEqual(
            self.page.query_selector_all(f"#{src['gid']} .nbx-rd-state-add"), [],
            "the blade must leave the source chassis")
        self.assertEqual(
            len(self.page.query_selector_all(f"#{dst['gid']} .nbx-rd-state-add")), 1,
            "the blade must land in the destination chassis")

        payload = self._capture_save()
        adds = [item for rack in payload.get("racks", [])
                for item in (rack.get("bays") or [])
                if item.get("kind") == "add"]
        self.assertEqual(len(adds), 1, f"exactly one planned blade must be posted: {adds}")
        self.assertTrue(
            adds[0].get("target_bay_id") or adds[0].get("parent_placement_id"),
            "the moved blade must address a bay")

if __name__ == "__main__":
    unittest.main()
