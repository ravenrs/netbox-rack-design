"""
Namespaced rack keys (PLAN-templates.md D27/D28, task T1.4d).

``dcim.Rack`` and ``PlannedRack`` keep SEPARATE pk sequences, so pk 5 can mean
two different racks. Every place the editor/API keys a rack by pk now uses
``models.rack_key()``'s namespaced form: ``"r:<pk>"`` for a real rack,
``"p:<pk>"`` for a ``PlannedRack``. This is a PURE refactor -- no planned rack
is made visible in the editor here (that is the next task) -- so these tests
only check that the identifier plumbing itself is correct: the editor context
emits the namespaced form, save-layout/recompute-distribution accept EITHER a
legacy bare integer or the namespaced form on the way in and treat them
identically, a malformed key is a clear 400, and "p:<pk>" resolves to a
PlannedRack while a bare int with the same number never does (D28's whole
point).

Written FIRST, against code that (at the time this docstring was written)
does not parse the namespaced form anywhere save preview-template -- the
save-layout/recompute-distribution/editor-context assertions below were
expected to fail until T1.4d's implementation landed. That failure is the
"confirm it fails" gate for this task.
"""

from dcim.models import Location, Rack, Site
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase

from .. import views
from ..api.views import parse_rack_id, parse_real_rack_id, resolve_rack_from_id
from ..models import DesignPlacement, PlannedRack
from .utils import create_dcim_environment, make_design


class ParseRackIdTest(TestCase):
    """Unit tests for the shared parser every rack-identifying action uses."""

    def test_bare_int_is_real(self):
        self.assertEqual(parse_rack_id(5), ("r", 5))

    def test_numeric_string_is_real(self):
        self.assertEqual(parse_rack_id("5"), ("r", 5))

    def test_namespaced_real_form(self):
        self.assertEqual(parse_rack_id("r:5"), ("r", 5))

    def test_namespaced_planned_form(self):
        self.assertEqual(parse_rack_id("p:7"), ("p", 7))

    def test_malformed_values_raise(self):
        for bad in ("x:5", "r:", "abc", "r:abc", "", "5:5:5", None, 1.5, True):
            with self.assertRaises(ValueError, msg=f"{bad!r} should be rejected"):
                parse_rack_id(bad)

    def test_real_only_parser_accepts_real_forms(self):
        self.assertEqual(parse_real_rack_id(5), 5)
        self.assertEqual(parse_real_rack_id("5"), 5)
        self.assertEqual(parse_real_rack_id("r:5"), 5)

    def test_real_only_parser_returns_none_for_planned(self):
        # A well-formed "p:<pk>" is not malformed -- it just names something
        # this real-only caller does not resolve.
        self.assertIsNone(parse_real_rack_id("p:7"))

    def test_real_only_parser_still_raises_for_malformed(self):
        with self.assertRaises(ValueError):
            parse_real_rack_id("bogus")


class ResolveRackFromIdCollisionTest(TestCase):
    """
    "p:<pk> resolves to a PlannedRack and a bare int with the same number does
    NOT" -- D28's whole point, forced the same way test_projection_planned_
    rack_pk.py does (a coincidence this rare is never exercised by accident).
    """

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="RK Site", slug="rk-site")
        cls.real_rack = Rack.objects.create(name="Real Rack", site=cls.site)
        cls.location = Location.objects.create(
            name="RK Loc", slug="rk-loc", site=cls.site,
        )
        cls.planned_rack = PlannedRack(
            name="Planned collider", location=cls.location, u_height=6,
        )
        cls.planned_rack.pk = cls.real_rack.pk
        cls.planned_rack.save(force_insert=True)
        assert cls.planned_rack.pk == cls.real_rack.pk, (
            "setup did not actually force the pk collision this test needs"
        )

    def test_planned_key_resolves_the_planned_rack(self):
        kind, pk, obj = resolve_rack_from_id(f"p:{self.planned_rack.pk}")
        self.assertEqual(kind, "p")
        self.assertEqual(pk, self.planned_rack.pk)
        self.assertEqual(obj, self.planned_rack)

    def test_bare_int_with_same_number_resolves_the_real_rack_not_planned(self):
        kind, pk, obj = resolve_rack_from_id(self.real_rack.pk)
        self.assertEqual(kind, "r")
        self.assertEqual(obj, self.real_rack)
        self.assertNotEqual(obj, self.planned_rack)

    def test_namespaced_real_key_also_resolves_the_real_rack(self):
        kind, pk, obj = resolve_rack_from_id(f"r:{self.real_rack.pk}")
        self.assertEqual(kind, "r")
        self.assertEqual(obj, self.real_rack)


class EditorContextRackKeyTest(TestCase):
    """The editor context's rack_meta["id"] emits the namespaced "r:<pk>" form."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.rack = env["racks"][0]
        cls.design = make_design(title="RK editor design", site=cls.site)
        cls.design.racks.add(cls.rack)

    def test_project_rack_bundle_emits_namespaced_id(self):
        bundle = views._project_rack_bundle(self.design, self.rack)
        self.assertEqual(bundle["rack_meta"]["id"], f"r:{self.rack.pk}")


class SaveLayoutDualFormTest(APITestCase):
    """save-layout accepts a bare int or "r:<pk>" for rack_id, identically."""

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]
        cls.device_role = env["device_role"]
        cls.design = make_design(title="RK save-layout design", site=cls.site)

    def _url(self, design):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-save-layout",
            kwargs={"pk": design.pk},
        )

    def _grant_all(self):
        self.add_permissions(
            "netbox_rack_design.change_design",
            "netbox_rack_design.add_designplacement",
            "netbox_rack_design.change_designplacement",
            "netbox_rack_design.delete_designplacement",
        )

    def _add_item(self, position):
        return {
            "kind": "add", "device_type_id": self.device_type.pk,
            "device_role_id": self.device_role.pk,
            "u_position": position, "face": "front",
            "proposed_name": f"rk-add-{position}",
        }

    def test_bare_int_and_namespaced_key_persist_identically(self):
        self._grant_all()
        rack = self.racks[0]

        resp_bare = self.client.post(
            self._url(self.design),
            {"design_id": self.design.pk, "racks": [
                {"rack_id": rack.pk, "front": [self._add_item(10)]},
            ]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp_bare, status.HTTP_200_OK)
        placement = DesignPlacement.objects.get(design=self.design, target_position=10)
        self.assertEqual(placement.target_rack_id, rack.pk)
        placement.delete()

        resp_namespaced = self.client.post(
            self._url(self.design),
            {"design_id": self.design.pk, "racks": [
                {"rack_id": f"r:{rack.pk}", "front": [self._add_item(10)]},
            ]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp_namespaced, status.HTTP_200_OK)
        placement = DesignPlacement.objects.get(design=self.design, target_position=10)
        self.assertEqual(placement.target_rack_id, rack.pk)

    def test_malformed_rack_key_is_a_400(self):
        self._grant_all()
        resp = self.client.post(
            self._url(self.design),
            {"design_id": self.design.pk, "racks": [
                {"rack_id": "bogus", "front": [self._add_item(11)]},
            ]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)
        self.assertIn("racks", resp.data)
        self.assertFalse(DesignPlacement.objects.filter(design=self.design).exists())


class RecomputeDistributionDualFormTest(APITestCase):
    """
    recompute-distribution accepts a bare int or "r:<pk>" identically, a
    malformed key is a 400, and a round trip returns results under the
    namespaced key (D27's own requirement for this response).
    """

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.rack = env["racks"][0]
        cls.design = make_design(title="RK recompute design", site=cls.site)

    def _url(self):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-recompute-distribution",
            kwargs={"pk": self.design.pk},
        )

    def test_bare_int_and_namespaced_key_agree(self):
        self.add_permissions("netbox_rack_design.view_design")

        resp_bare = self.client.post(
            self._url(),
            {"design_id": self.design.pk, "racks": [{"rack_id": self.rack.pk}]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp_bare, status.HTTP_200_OK)

        resp_namespaced = self.client.post(
            self._url(),
            {"design_id": self.design.pk, "racks": [
                {"rack_id": f"r:{self.rack.pk}"},
            ]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp_namespaced, status.HTTP_200_OK)

        key = f"r:{self.rack.pk}"
        self.assertIn(key, resp_bare.data["distributions"])
        self.assertIn(key, resp_namespaced.data["distributions"])
        self.assertEqual(
            resp_bare.data["distributions"][key],
            resp_namespaced.data["distributions"][key],
        )
        # The response is ALSO still keyed by the bare pk (as a string) --
        # power_heatmap.js (outside this task's touch scope) indexes this
        # exact response by that legacy key, so it must keep working.
        self.assertIn(str(self.rack.pk), resp_bare.data["distributions"])

    def test_malformed_rack_key_is_a_400(self):
        self.add_permissions("netbox_rack_design.view_design")
        resp = self.client.post(
            self._url(),
            {"design_id": self.design.pk, "racks": [{"rack_id": "bogus"}]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)
