"""
Tests for T4.1 -- building a Template from something that already exists
(PLAN-templates.md Sec 4/Phase 4, D18/D32).

Two DB-side extraction sources, both converting absolute-U items into
``(anchor, order)`` via ``stamping.compute_anchors`` and writing
``TemplatePlacement`` rows:

  * ``TemplateViewSet.from_design``  -- a design's rack, PROJECTED (D32):
    everything the rack will look like under that design, not merely its
    own ``kind=add`` placements.
  * ``TemplateViewSet.from_rack``    -- a real ``dcim.Rack``'s actual devices.

Written FIRST, against code that does not exist yet -- both endpoints
(``reverse()`` below) are expected to fail (``NoReverseMatch``, since the
actions/urls do not exist) until the view is implemented. That failure is
the "confirm it fails" gate for this task.
"""

from decimal import Decimal

from dcim.choices import SubdeviceRoleChoices
from dcim.models import (
    Device,
    DeviceBay,
    DeviceBayTemplate,
    DeviceRole,
    DeviceType,
    Location,
    Manufacturer,
    Rack,
    Site,
)
from django.urls import reverse
from rest_framework import status
from tenancy.models import Tenant
from utilities.testing import APITestCase

from .. import stamping
from ..choices import DesignPlacementKindChoices
from ..models import Design, PlannedRack, Template


def _from_design_url():
    return reverse("plugins-api:netbox_rack_design-api:template-from-design")


def _from_rack_url():
    return reverse("plugins-api:netbox_rack_design-api:template-from-rack")


class TemplateExtractionTest(APITestCase):
    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="TE Site", slug="te-site")
        cls.mfr = Manufacturer.objects.create(name="TE Mfr", slug="te-mfr")
        cls.device_type = DeviceType.objects.create(
            manufacturer=cls.mfr, model="TE Device", slug="te-device",
            u_height=1, is_full_depth=False,
        )
        cls.role_a = DeviceRole.objects.create(name="TE Role A", slug="te-role-a")
        cls.role_b = DeviceRole.objects.create(name="TE Role B", slug="te-role-b")
        cls.tenant = Tenant.objects.create(name="TE Tenant", slug="te-tenant")

    def _add_perms(self):
        self.add_permissions(
            "netbox_rack_design.add_template",
            "netbox_rack_design.view_design",
        )

    # ------------------------------------------------------------------
    # Source 2: a real dcim.Rack -- the round trip
    # ------------------------------------------------------------------

    def test_round_trip_from_real_rack(self):
        """Extract a template from a populated rack, stamp it back into an
        EMPTY rack of the same height, and assert the positions match the
        original exactly (PLAN-templates.md D17's round-trip property)."""
        self._add_perms()
        rack = Rack.objects.create(name="TE Rack RT", site=self.site, u_height=10)
        # Top-anchored contiguous run: U10, U9.
        Device.objects.create(
            name="Top1", site=self.site, rack=rack, device_type=self.device_type,
            role=self.role_a, tenant=self.tenant, position=10, face="front",
        )
        Device.objects.create(
            name="Top2", site=self.site, rack=rack, device_type=self.device_type,
            role=self.role_a, position=9, face="front",
        )
        # Bottom-anchored contiguous run: U1, U2.
        Device.objects.create(
            name="Bot1", site=self.site, rack=rack, device_type=self.device_type,
            role=self.role_b, position=1, face="front",
        )
        Device.objects.create(
            name="Bot2", site=self.site, rack=rack, device_type=self.device_type,
            role=self.role_b, position=2, face="front",
        )

        resp = self.client.post(
            _from_rack_url(), {"rack_id": rack.pk, "name": "RT Template"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_201_CREATED)
        template = Template.objects.get(pk=resp.data["template_id"])
        self.assertEqual(resp.data.get("warnings", []), [])

        top_level = list(
            template.placements.filter(parent_placement__isnull=True).order_by("anchor", "order")
        )
        self.assertEqual(len(top_level), 4)

        # Stamp back into an EMPTY rack of the same height/starting_unit.
        items = [_StampItem(tp) for tp in template.placements.filter(parent_placement__isnull=True)]
        placements, unplaced = stamping.compute_stamp(
            items, {"front": [], "rear": []}, rack.u_height, starting_unit=1,
        )
        self.assertEqual(unplaced, [])
        positions_by_device_name = {}
        for stamp_item, position, _face in placements:
            positions_by_device_name[stamp_item.tp.label] = position

        self.assertEqual(positions_by_device_name["Top1"], Decimal(10))
        self.assertEqual(positions_by_device_name["Top2"], Decimal(9))
        self.assertEqual(positions_by_device_name["Bot1"], Decimal(1))
        self.assertEqual(positions_by_device_name["Bot2"], Decimal(2))

    # ------------------------------------------------------------------
    # Source 1: a design's rack -- projected state, not just kind=add (D32)
    # ------------------------------------------------------------------

    def test_extraction_from_design_includes_existing_devices(self):
        """D32 corrects D18's literal wording: 'our standard rack' includes
        the devices already standing in it, not only this design's own new
        adds."""
        self._add_perms()
        rack = Rack.objects.create(name="TE Rack Design", site=self.site, u_height=10)
        Device.objects.create(
            name="AlreadyThere", site=self.site, rack=rack, device_type=self.device_type,
            role=self.role_a, tenant=self.tenant, position=10, face="front",
        )
        design = Design.objects.create(title="TE Design", site=self.site)
        design.racks.add(rack)
        from ..models import DesignPlacement
        DesignPlacement.objects.create(
            design=design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, device_role=self.role_b,
            target_rack=rack, target_position=9, target_face="front",
            proposed_name="NewAdd",
        )

        resp = self.client.post(
            _from_design_url(),
            {"design": design.pk, "rack": f"r:{rack.pk}", "name": "Design Template"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_201_CREATED)
        template = Template.objects.get(pk=resp.data["template_id"])
        top_level = template.placements.filter(parent_placement__isnull=True)
        self.assertEqual(top_level.count(), 2)
        device_types = {tp.device_type_id for tp in top_level}
        self.assertEqual(device_types, {self.device_type.pk})
        roles = {tp.device_role_id for tp in top_level}
        self.assertEqual(roles, {self.role_a.pk, self.role_b.pk})

        # The plain-existing device's role/tenant come from the real device;
        # the new add's from the placement.
        existing_tp = top_level.get(device_role=self.role_a)
        self.assertEqual(existing_tp.tenant_id, self.tenant.pk)

    def test_saving_planned_rack_as_template_is_refused(self):
        """D32: a PlannedRack has no devices by definition, so it can only
        ever produce an empty template -- refused with a message, not a 500."""
        self._add_perms()
        location = Location.objects.create(name="TE Loc", slug="te-loc", site=self.site)
        planned = PlannedRack.objects.create(name="TE Planned", location=location, u_height=10)
        design = Design.objects.create(title="TE Planned Design", site=self.site)
        design.planned_racks.add(planned)

        resp = self.client.post(
            _from_design_url(),
            {"design": design.pk, "rack": f"p:{planned.pk}", "name": "Planned Template"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Template.objects.filter(name="Planned Template").exists())

    # ------------------------------------------------------------------
    # Role / tenant
    # ------------------------------------------------------------------

    def test_role_and_tenant_carry_across_from_rack(self):
        self._add_perms()
        rack = Rack.objects.create(name="TE Rack Role", site=self.site, u_height=5)
        Device.objects.create(
            name="RoleDev", site=self.site, rack=rack, device_type=self.device_type,
            role=self.role_a, tenant=self.tenant, position=5, face="front",
        )
        resp = self.client.post(
            _from_rack_url(), {"rack_id": rack.pk, "name": "Role Template"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_201_CREATED)
        template = Template.objects.get(pk=resp.data["template_id"])
        tp = template.placements.get()
        self.assertEqual(tp.device_role_id, self.role_a.pk)
        self.assertEqual(tp.tenant_id, self.tenant.pk)

    # ------------------------------------------------------------------
    # Chassis + blades (D10)
    # ------------------------------------------------------------------

    def test_chassis_with_blades_extracts_as_nested_placements(self):
        self._add_perms()
        rack = Rack.objects.create(name="TE Rack Chassis", site=self.site, u_height=10)
        chassis_type = DeviceType.objects.create(
            manufacturer=self.mfr, model="TE Chassis", slug="te-chassis",
            u_height=4, subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        )
        DeviceBayTemplate.objects.create(device_type=chassis_type, name="bay-a")
        blade_type = DeviceType.objects.create(
            manufacturer=self.mfr, model="TE Blade", slug="te-blade",
            u_height=0, subdevice_role=SubdeviceRoleChoices.ROLE_CHILD,
        )
        chassis = Device.objects.create(
            name="Chassis1", site=self.site, rack=rack, device_type=chassis_type,
            role=self.role_a, position=1, face="front",
        )
        bay = DeviceBay.objects.get(device=chassis, name="bay-a")
        blade = Device.objects.create(
            name="Blade1", site=self.site, device_type=blade_type,
            role=self.role_b, tenant=self.tenant,
        )
        bay.installed_device = blade
        bay.save()

        resp = self.client.post(
            _from_rack_url(), {"rack_id": rack.pk, "name": "Chassis Template"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_201_CREATED)
        template = Template.objects.get(pk=resp.data["template_id"])
        chassis_tp = template.placements.get(parent_placement__isnull=True)
        self.assertEqual(chassis_tp.device_type_id, chassis_type.pk)
        blade_tp = template.placements.get(parent_placement__isnull=False)
        self.assertEqual(blade_tp.parent_placement_id, chassis_tp.pk)
        self.assertEqual(blade_tp.target_bay_name, "bay-a")
        self.assertEqual(blade_tp.device_type_id, blade_type.pk)
        self.assertEqual(blade_tp.device_role_id, self.role_b.pk)
        self.assertEqual(blade_tp.tenant_id, self.tenant.pk)

    # ------------------------------------------------------------------
    # desc_units (the physical-top/bottom trap)
    # ------------------------------------------------------------------

    def test_desc_units_rack_extracts_correctly_not_mirrored(self):
        """A desc_units=True rack numbers U1 at the physical TOP. A naive
        'position < u_height/2' guess would mirror the whole layout
        top-for-bottom. Extraction must go through rackinfo.rack_desc_units
        and round-trip to the SAME numeric positions."""
        self._add_perms()
        rack = Rack.objects.create(
            name="TE Rack Desc", site=self.site, u_height=10, desc_units=True,
        )
        # Physically topmost (numerically LOWEST position under desc_units).
        Device.objects.create(
            name="DescTop", site=self.site, rack=rack, device_type=self.device_type,
            role=self.role_a, position=1, face="front",
        )
        # Physically bottommost (numerically HIGHEST position under desc_units).
        Device.objects.create(
            name="DescBottom", site=self.site, rack=rack, device_type=self.device_type,
            role=self.role_a, position=10, face="front",
        )

        resp = self.client.post(
            _from_rack_url(), {"rack_id": rack.pk, "name": "Desc Template"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_201_CREATED)
        template = Template.objects.get(pk=resp.data["template_id"])
        top_level = list(template.placements.all())
        self.assertEqual(len(top_level), 2)

        items = [_StampItem(tp) for tp in top_level]
        placements, unplaced = stamping.compute_stamp(
            items, {"front": [], "rear": []}, rack.u_height, starting_unit=1,
        )
        self.assertEqual(unplaced, [])
        by_label = {stamp_item.tp.label: position for stamp_item, position, _face in placements}
        self.assertEqual(by_label["DescTop"], Decimal(1))
        self.assertEqual(by_label["DescBottom"], Decimal(10))

    # ------------------------------------------------------------------
    # Islands (D17/D32)
    # ------------------------------------------------------------------

    def test_island_is_reported_as_warning_and_rest_round_trips(self):
        self._add_perms()
        rack = Rack.objects.create(name="TE Rack Island", site=self.site, u_height=10)
        Device.objects.create(
            name="TopEdge", site=self.site, rack=rack, device_type=self.device_type,
            role=self.role_a, position=10, face="front",
        )
        Device.objects.create(
            name="Island", site=self.site, rack=rack, device_type=self.device_type,
            role=self.role_a, position=5, face="front",
        )
        Device.objects.create(
            name="BottomEdge", site=self.site, rack=rack, device_type=self.device_type,
            role=self.role_a, position=1, face="front",
        )

        resp = self.client.post(
            _from_rack_url(), {"rack_id": rack.pk, "name": "Island Template"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_201_CREATED)
        warnings = resp.data.get("warnings", [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("Island", warnings[0])

        template = Template.objects.get(pk=resp.data["template_id"])
        top_level = list(template.placements.all())
        # The island is left OUT -- never silently dropped without a trace
        # (the warning above) and never silently reattached to an end.
        self.assertEqual(len(top_level), 2)
        labels = {tp.label for tp in top_level}
        self.assertEqual(labels, {"TopEdge", "BottomEdge"})
        self.assertFalse(any("Island" == tp.label for tp in top_level))

        items = [_StampItem(tp) for tp in top_level]
        placements, unplaced = stamping.compute_stamp(
            items, {"front": [], "rear": []}, rack.u_height, starting_unit=1,
        )
        self.assertEqual(unplaced, [])
        by_label = {stamp_item.tp.label: position for stamp_item, position, _face in placements}
        self.assertEqual(by_label["TopEdge"], Decimal(10))
        self.assertEqual(by_label["BottomEdge"], Decimal(1))

    # ------------------------------------------------------------------
    # Empty rack
    # ------------------------------------------------------------------

    def test_empty_rack_produces_empty_template(self):
        self._add_perms()
        rack = Rack.objects.create(name="TE Rack Empty", site=self.site, u_height=42)
        resp = self.client.post(
            _from_rack_url(), {"rack_id": rack.pk, "name": "Empty Template"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_201_CREATED)
        template = Template.objects.get(pk=resp.data["template_id"])
        self.assertEqual(template.placements.count(), 0)
        self.assertEqual(resp.data.get("warnings", []), [])

    # ------------------------------------------------------------------
    # 400s
    # ------------------------------------------------------------------

    def test_from_rack_unknown_rack_is_400(self):
        self._add_perms()
        resp = self.client.post(
            _from_rack_url(), {"rack_id": 999999, "name": "Nope"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)

    def test_from_design_unknown_design_is_400(self):
        self._add_perms()
        rack = Rack.objects.create(name="TE Rack 400a", site=self.site, u_height=5)
        resp = self.client.post(
            _from_design_url(),
            {"design": 999999, "rack": f"r:{rack.pk}", "name": "Nope"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)

    def test_from_design_unknown_rack_is_400(self):
        self._add_perms()
        design = Design.objects.create(title="TE 400 Design", site=self.site)
        resp = self.client.post(
            _from_design_url(),
            {"design": design.pk, "rack": "r:999999", "name": "Nope"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)

    def test_from_design_malformed_rack_key_is_400(self):
        self._add_perms()
        design = Design.objects.create(title="TE 400b Design", site=self.site)
        resp = self.client.post(
            _from_design_url(),
            {"design": design.pk, "rack": "not-a-key", "name": "Nope"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)


class _StampItem:
    """Local duck-typed wrapper mirroring api/views.py's own ``_StampItem``,
    so this test module can round-trip a real ``TemplatePlacement`` through
    ``stamping.compute_stamp`` without importing a private view helper."""

    def __init__(self, tp):
        self.tp = tp
        self.u_height = tp.device_type.u_height
        self.is_full_depth = tp.device_type.is_full_depth
        self.face = tp.face if tp.face in ("front", "rear") else "front"
        self.anchor = tp.anchor
