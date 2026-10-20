#!/usr/bin/env python3
"""Playwright proof that the editor's string-built HTML sinks cannot be turned
into script execution by user-defined names (security 1.2.2).

Feed, site, location and rack names are free text that any NetBox user with
the right to name a DCIM object can set, and the editor renders them through
``innerHTML`` in several places. Each test here plants a hostile name through
the REST API, drives the real UI to the sink, and proves three things:

  * the name arrives as TEXT (textContent / option text equals the payload);
  * no element was injected (no ``<img src=x>``, no ``onfocus`` attribute,
    no request for the broken image);
  * nothing executed (``window.__xss`` stays unset, no alert/confirm fired).

Payloads: ``P1`` is an element injection (``<img src=x onerror=...>``), ``P2``
an attribute breakout (``" autofocus onfocus=...``).

Sinks covered: the per-bank distribution legend (power_heatmap.js), the PDU
"bind to a power feed" dialog and the rack-power dialog (editor/power.js), the
Create-rack dialog's site chip / site select / location select / "copy feeds
from" select (editor_panels.js).

SELF-PROVISIONING and cleaning up after itself: every fixture is created over
the REST API under a unique ``e2e-xss-<uuid>`` tag and deleted in reverse
dependency order (designs, devices, feeds, panel, racks, locations, device
type, manufacturer, sites). Designs are created per test and deleted in
tearDown, and any stale ``e2e-xss-`` design a killed run left behind is swept
at the start of the class, so nothing here can poison another suite's peer
projection. Never clicks Save.
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

P1 = '<img src=x onerror="window.__xss=1">'
P2 = '" autofocus onfocus="window.__xss=1'
PREFIX = "e2e-xss-"


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

_HIDE_DJDT_JS = (
    "() => ['djDebug', 'djDebugRoot'].forEach(function (id) {"
    "  const d = document.getElementById(id);"
    "  if (d) { d.style.display = 'none'; d.style.pointerEvents = 'none'; }"
    "})")


@unittest.skipUnless(_PREREQ_OK, _PREREQ_REASON)
class EditorXssTestCase(unittest.TestCase):
    """Hostile names render as inert text in every string-built editor sink."""

    # -- class fixture ----------------------------------------------------------

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(channel="chrome", headless=True)
        cls._ctx = cls._browser.new_context(viewport={"width": 1900, "height": 1200})
        cls._design_ids = []
        cls._created = {}
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
            cls._sweep_stale_designs()
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
    def _sweep_stale_designs(cls):
        """Remove designs a killed earlier run of THIS suite left behind. Only
        ``e2e-xss-`` titles: nothing else is ever touched."""
        try:
            rows = cls._api(
                "GET", "/api/plugins/rack-design/designs/?limit=1000")["results"]
        except Exception:
            return
        for row in rows:
            if str(row.get("title", "")).startswith(PREFIX):
                try:
                    cls._api("DELETE", f"/api/plugins/rack-design/designs/{row['id']}/")
                except Exception:
                    pass

    @classmethod
    def _make(cls, bucket, path, payload):
        obj = cls._api("POST", path, payload)
        cls._created.setdefault(bucket, []).append(obj["id"])
        return obj

    @classmethod
    def _provision_fixture(cls):
        tag = f"{PREFIX}{uuid.uuid4().hex[:8]}"
        cls._tag = tag
        cf = {"custom_fields": {"warranty_type": ""}}

        # Site A carries payload P1, site B payload P2 (attribute breakout).
        cls._site_a = cls._make("sites", "/api/dcim/sites/", {
            "name": f"{P1} {tag}", "slug": f"{tag}-a", "status": "active"})
        cls._site_b = cls._make("sites", "/api/dcim/sites/", {
            "name": f"{P2} {tag}", "slug": f"{tag}-b", "status": "active"})
        cls._loc_a = cls._make("locations", "/api/dcim/locations/", {
            "name": f"{P1} {tag}", "slug": f"{tag}-loc-a", "site": cls._site_a["id"]})
        cls._loc_b = cls._make("locations", "/api/dcim/locations/", {
            "name": f"{P2} {tag}", "slug": f"{tag}-loc-b", "site": cls._site_b["id"]})
        # Rack A is named with P2 so it shows up as a hostile option in the
        # "copy feeds from" / rack-power selects; rack B is plain.
        cls._rack_a = cls._make("racks", "/api/dcim/racks/", {
            "name": f"{P2} {tag}", "site": cls._site_a["id"],
            "location": cls._loc_a["id"], "status": "active", "u_height": 20})
        cls._rack_b = cls._make("racks", "/api/dcim/racks/", {
            "name": f"{tag}-rack-b", "site": cls._site_b["id"],
            "location": cls._loc_b["id"], "status": "active", "u_height": 20})

        # A real PDU cabled to a real PowerFeed named with P1: the editor paints
        # per-bank chips for it (legend sink) and lists the feed in the bind
        # dialog (feed-list sink).
        mfr = cls._make("manufacturers", "/api/dcim/manufacturers/", {
            "name": f"{tag}-mfr", "slug": f"{tag}-mfr"})
        roles = cls._api("GET", "/api/dcim/device-roles/?slug=pdu")
        if roles["count"]:
            role_id = roles["results"][0]["id"]
        else:
            role_id = cls._make("roles", "/api/dcim/device-roles/", {
                "name": f"{tag}-pdu", "slug": "pdu", "color": "9e9e9e"})["id"]
        dtype = cls._make("device_types", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"{tag}-PDU", "slug": f"{tag}-pdu",
            "u_height": 0, "is_full_depth": False})
        for name in ("1/1", "2/1"):
            cls._api("POST", "/api/dcim/power-outlet-templates/", {
                "device_type": dtype["id"], "name": name})
        cls._api("POST", "/api/dcim/power-port-templates/", {
            "device_type": dtype["id"], "name": "input"})
        panel = cls._make("panels", "/api/dcim/power-panels/", {
            "site": cls._site_a["id"], "name": f"{tag}-panel"})
        cls._feed_name = f"{P1} {tag}"
        feed = cls._make("feeds", "/api/dcim/power-feeds/", {
            "power_panel": panel["id"], "rack": cls._rack_a["id"],
            "name": cls._feed_name, "status": "active",
            "voltage": 230, "amperage": 32, "phase": "single-phase", "supply": "ac"})
        pdu = cls._make("devices", "/api/dcim/devices/", {
            "name": f"{tag}-pdu-r1-a1", "device_type": dtype["id"], "role": role_id,
            "site": cls._site_a["id"], "rack": cls._rack_a["id"],
            "status": "active", **cf})
        port = cls._api("GET", f"/api/dcim/power-ports/?device_id={pdu['id']}")["results"][0]
        cls._api("POST", "/api/dcim/cables/", {
            "a_terminations": [{"object_type": "dcim.powerport", "object_id": port["id"]}],
            "b_terminations": [{"object_type": "dcim.powerfeed", "object_id": feed["id"]}],
            "status": "connected"})

    @classmethod
    def _cleanup_class(cls):
        for design_id in list(getattr(cls, "_design_ids", [])):
            try:
                cls._api("DELETE", f"/api/plugins/rack-design/designs/{design_id}/")
            except Exception:
                pass
        created = getattr(cls, "_created", None) or {}
        for bucket, path in (
            ("devices", "/api/dcim/devices/"),
            ("feeds", "/api/dcim/power-feeds/"),
            ("panels", "/api/dcim/power-panels/"),
            ("racks", "/api/dcim/racks/"),
            ("locations", "/api/dcim/locations/"),
            ("device_types", "/api/dcim/device-types/"),
            ("manufacturers", "/api/dcim/manufacturers/"),
            ("roles", "/api/dcim/device-roles/"),
            ("sites", "/api/dcim/sites/"),
        ):
            for obj_id in reversed(created.get(bucket, [])):
                try:
                    cls._api("DELETE", f"{path}{obj_id}/")
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

    # -- per test ---------------------------------------------------------------

    def setUp(self):
        self._own_designs = []
        self.ctx = self._browser.new_context(
            storage_state=self._ctx.storage_state(),
            viewport={"width": 1900, "height": 1200})
        self.page = self.ctx.new_page()
        # Tripwires: a browser dialog, or a request for the broken <img src=x>,
        # means injected markup ran.
        self.dialogs = []
        self.page.on("dialog", lambda d: (self.dialogs.append(d.message), d.dismiss()))
        self.stray_requests = []
        self.page.on("request", lambda r: self.stray_requests.append(r.url)
                     if r.url.split("?")[0].rstrip("/") == f"{BASE}/x"
                     or r.url.split("?")[0].endswith("/plugins/rack-design/x")
                     else None)

    def tearDown(self):
        try:
            self.ctx.close()
        finally:
            for design_id in self._own_designs:
                try:
                    self._api("DELETE", f"/api/plugins/rack-design/designs/{design_id}/")
                except Exception:
                    pass
                if design_id in self._design_ids:
                    self._design_ids.remove(design_id)
        self.assertEqual(self.dialogs, [], f"a browser dialog fired: {self.dialogs}")

    # -- helpers ----------------------------------------------------------------

    def _design(self, sites, racks, label):
        design = self._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"{self._tag}-{label}", "sites": sites, "racks": racks,
            "status": "draft"})
        self._design_ids.append(design["id"])
        self._own_designs.append(design["id"])
        return design

    def _open_editor(self, design, rack):
        self.page.goto(
            f"{BASE}/plugins/rack-design/designs/{design['id']}/editor/{rack['id']}/",
            wait_until="networkidle")
        self.page.wait_for_selector("#rd-editor", timeout=30000)
        self.page.wait_for_selector(".nbx-rd-rack-block", timeout=30000)
        self.page.evaluate(_HIDE_DJDT_JS)
        self.page.wait_for_timeout(600)

    def _open_create_rack_dialog(self, design, rack):
        self._open_editor(design, rack)
        toggle = self.page.query_selector('[data-rd-section-toggle="racks"]')
        self.assertIsNotNone(toggle, "the Racks section toggle must be rendered")
        if not self.page.is_visible("#nbx-rd-add-rack-card"):
            toggle.click()
        self.page.wait_for_selector("#nbx-rd-add-rack-card", timeout=15000)
        self.page.click("#nbx-rd-create-planned-rack-btn")
        self.page.wait_for_selector(".nbx-rd-create-rack-modal.show", timeout=10000)

    def _assert_nothing_ran(self, where):
        """The three global tripwires: sentinel, injected element, stray request."""
        self.page.wait_for_timeout(700)  # let a broken image's onerror fire
        self.assertIsNone(
            self.page.evaluate("() => window.__xss === undefined ? null : window.__xss"),
            f"{where}: window.__xss was set -- injected markup executed")
        self.assertEqual(
            self.page.evaluate("() => document.querySelectorAll('img[src=\"x\"]').length"),
            0, f"{where}: an <img src=x> was injected into the page")
        tainted = self.page.evaluate(
            """() => [...document.querySelectorAll('*')].filter(
                (el) => !/^(SCRIPT|LINK)$/.test(el.tagName) && [...el.attributes].some(
                    (a) => a.name === 'autofocus' || /^on/.test(a.name)))
                .map((el) => el.outerHTML.slice(0, 200))""")
        self.assertEqual(
            tainted, [], f"{where}: an element carries an injected on*/autofocus attribute")
        self.assertEqual(self.stray_requests, [],
                         f"{where}: the browser requested the injected image")
        self.assertEqual(self.dialogs, [], f"{where}: a browser dialog fired")

    def _texts(self, selector):
        return self.page.evaluate(
            """(sel) => [...document.querySelectorAll(sel)].map(
                (el) => ({text: el.textContent, kids: el.children.length}))""",
            selector)

    # -- sink 1: per-bank distribution legend -----------------------------------

    def test_legend_feed_name_renders_as_text(self):
        design = self._design([self._site_a["id"]], [self._rack_a["id"]], "legend")
        self._open_editor(design, self._rack_a)
        self.page.wait_for_selector(".nbx-rd-dist-pdu-head", timeout=30000)
        heads = self._texts(".nbx-rd-dist-pdu-head")
        self.assertTrue(heads, "the rack must show a per-bank legend head")
        match = [h for h in heads if self._feed_name in h["text"]]
        self.assertTrue(
            match, f"the feed name must be shown verbatim as text; got {heads}")
        for head in match:
            self.assertEqual(
                head["kids"], 0, "the legend head must hold text only, no element children")
        self._assert_nothing_ran("distribution legend")

    # -- sinks 2 + 4b: PDU bind dialog and rack-power dialog --------------------

    def test_bind_dialog_feed_list_renders_name_as_text(self):
        design = self._design([self._site_a["id"]], [self._rack_a["id"]], "bind")
        self._open_editor(design, self._rack_a)
        self.page.evaluate(
            """(rackId) => {
                const widget = {proposed_name: 'xss-bind-pdu', role_slug: 'pdu',
                                label: 'xss-bind-pdu'};
                window.NbxRdEditor.showPduPowerDialog(
                    widget, document.createElement('div'), {rackId: String(rackId)});
            }""",
            self._rack_a["id"])
        self.page.wait_for_selector(
            ".nbx-rd-feed-list label", timeout=15000)
        labels = self._texts(".nbx-rd-feed-list label")
        match = [lab for lab in labels if self._feed_name in lab["text"]]
        self.assertTrue(
            match, f"the hostile feed must be listed, verbatim, as text; got {labels}")
        self.assertEqual(
            self.page.evaluate(
                "() => document.querySelectorAll('.nbx-rd-feed-list img').length"),
            0, "no element may be injected through a feed name")
        self._assert_nothing_ran("bind dialog")

    def _open_rack_power_dialog(self, design, editor_rack, dialog_rack):
        """Real racks have no per-rack Power button (only planned ones do), so
        open the dialog through the editor's own entry point, as the PDU-power
        suite does."""
        self._open_editor(design, editor_rack)
        self.page.evaluate(
            "([rackId, name]) => window.NbxRdEditor.showRackPowerDialog("
            "String(rackId), name)",
            [dialog_rack["id"], dialog_rack["name"]])
        self.page.wait_for_selector(".nbx-rd-rackpower-rack", timeout=10000)

    def test_rack_power_dialog_title_is_text(self):
        """The rack named with P2 is the dialog's title (attribute breakout
        payload) -- it must read as text, not become attributes."""
        design = self._design(
            [self._site_a["id"]], [self._rack_a["id"]], "rackpower-title")
        self._open_rack_power_dialog(design, self._rack_a, self._rack_a)
        titles = [t["text"] for t in self._texts(".modal .modal-title")]
        self.assertTrue(
            any(self._rack_a["name"] in t for t in titles),
            f"the dialog title must show the rack name as text; got {titles}")
        self._assert_nothing_ran("rack power dialog title")

    def test_rack_power_dialog_copy_from_select_is_text(self):
        """'Copy from rack' lists the OTHER racks of the design; the one named
        with P2 must be plain option text."""
        design = self._design(
            [self._site_a["id"], self._site_b["id"]],
            [self._rack_a["id"], self._rack_b["id"]], "rackpower-copy")
        self._open_rack_power_dialog(design, self._rack_b, self._rack_b)
        options = self.page.evaluate(
            """() => [...document.querySelectorAll('.nbx-rd-rackpower-rack option')]
                .map((o) => ({text: o.textContent, value: o.value}))""")
        self.assertIn(self._rack_a["name"], [o["text"] for o in options],
                      f"the hostile rack name must be an option's text; got {options}")
        for opt in options:
            self.assertRegex(opt["value"], r"^\d*$", "option value stays a bare rack id")
        self._assert_nothing_ran("rack power dialog copy-from select")

    # -- sink 3: Create-rack dialog ---------------------------------------------

    def test_create_rack_single_site_chip_is_text_p1(self):
        design = self._design([self._site_a["id"]], [self._rack_a["id"]], "chip-p1")
        self._open_create_rack_dialog(design, self._rack_a)
        chips = self._texts(".nbx-rd-create-rack-site-chip")
        self.assertEqual(len(chips), 1, "a one-site design shows exactly one chip")
        self.assertEqual(
            chips[0]["text"].strip(), self._site_a["name"],
            "the chip's text must be the site name, verbatim")
        self.assertEqual(
            chips[0]["kids"], 1, "the chip holds only its icon -- the name is a text node")
        # Location options (single site: filled at once) stay inert text too.
        loc_options = self.page.evaluate(
            """() => [...document.querySelectorAll('.nbx-rd-create-rack-location option')]
                .map((o) => o.textContent)""")
        self.assertIn(self._loc_a["name"], loc_options)
        # "Copy feeds from" lists the P2-named rack as plain option text.
        copy_options = self.page.evaluate(
            """() => [...document.querySelectorAll('.nbx-rd-create-rack-copy-feeds option')]
                .map((o) => o.textContent)""")
        self.assertIn(self._rack_a["name"], copy_options)
        self.assertEqual(
            self.page.evaluate(
                "() => document.querySelectorAll("
                "'.nbx-rd-create-rack-modal img, .nbx-rd-create-rack-modal [onfocus]').length"),
            0, "nothing may be injected into the Create-rack dialog")
        self._assert_nothing_ran("create-rack dialog, single site P1")

    def test_create_rack_single_site_chip_attribute_breakout_p2(self):
        design = self._design([self._site_b["id"]], [self._rack_b["id"]], "chip-p2")
        self._open_create_rack_dialog(design, self._rack_b)
        chips = self._texts(".nbx-rd-create-rack-site-chip")
        self.assertEqual(len(chips), 1)
        self.assertEqual(chips[0]["text"].strip(), self._site_b["name"])
        modal_attrs = self.page.evaluate(
            """() => [...document.querySelectorAll('.nbx-rd-create-rack-modal *')]
                .filter((el) => el.hasAttribute('onfocus') || el.hasAttribute('autofocus'))
                .length""")
        self.assertEqual(
            modal_attrs, 0, "no onfocus/autofocus attribute may be injected")
        loc_options = self.page.evaluate(
            """() => [...document.querySelectorAll('.nbx-rd-create-rack-location option')]
                .map((o) => o.textContent)""")
        self.assertIn(self._loc_b["name"], loc_options)
        self._assert_nothing_ran("create-rack dialog, single site P2")

    def test_create_rack_multi_site_select_and_locations_are_text(self):
        design = self._design(
            [self._site_a["id"], self._site_b["id"]],
            [self._rack_a["id"], self._rack_b["id"]], "multi")
        self._open_create_rack_dialog(design, self._rack_a)
        site_options = self.page.evaluate(
            """() => [...document.querySelectorAll('.nbx-rd-create-rack-site option')]
                .map((o) => ({text: o.textContent, value: o.value}))""")
        texts = [o["text"] for o in site_options]
        self.assertIn(self._site_a["name"], texts, "P1 site must be option text")
        self.assertIn(self._site_b["name"], texts, "P2 site must be option text")
        for opt in site_options:
            self.assertRegex(
                opt["value"], r"^\d*$", "an option value must stay a bare site id")

        for site, loc in ((self._site_a, self._loc_a), (self._site_b, self._loc_b)):
            self.page.select_option(".nbx-rd-create-rack-site", str(site["id"]))
            self.page.wait_for_function(
                "() => { const el = document.querySelector('.nbx-rd-create-rack-location');"
                " return el && !el.disabled; }", timeout=10000)
            loc_options = self.page.evaluate(
                """() => [...document.querySelectorAll('.nbx-rd-create-rack-location option')]
                    .map((o) => o.textContent)""")
            self.assertIn(loc["name"], loc_options,
                          "the hostile location name must be option text")
        self.assertEqual(
            self.page.evaluate(
                "() => document.querySelectorAll("
                "'.nbx-rd-create-rack-modal img, .nbx-rd-create-rack-modal [onfocus],"
                " .nbx-rd-create-rack-modal [autofocus]').length"),
            0, "nothing may be injected into the Create-rack dialog")
        self._assert_nothing_ran("create-rack dialog, multi site")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
