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
from ..models import PlannedRack, Template
from .utils import make_design


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
        design = make_design(title="TE Design", site=self.site)
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

    def test_a_tray_pdu_goes_into_the_template(self):
        """A 0U device in the non-racked tray (a PDU) is saved with the
        template: stamping puts every 0U item back into the target's tray."""
        self._add_perms()
        rack = Rack.objects.create(name="TE Rack Tray", site=self.site, u_height=10)
        zero_u = DeviceType.objects.create(
            manufacturer=self.mfr, model="TE PDU", slug="te-pdu", u_height=0)
        design = make_design(title="TE Tray Design", site=self.site)
        design.racks.add(rack)
        from ..models import DesignPlacement
        DesignPlacement.objects.create(
            design=design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=zero_u, device_role=self.role_a, target_rack=rack,
            proposed_name="te-pdu-a1",
        )
        DesignPlacement.objects.create(
            design=design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, device_role=self.role_b,
            target_rack=rack, target_position=1, target_face="front",
            proposed_name="te-srv-1",
        )

        resp = self.client.post(
            _from_design_url(),
            {"design": design.pk, "rack": f"r:{rack.pk}", "name": "Tray Template"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["placement_count"], 2)
        self.assertEqual(resp.data["warnings"], [])
        template = Template.objects.get(pk=resp.data["template_id"])
        pdu = template.placements.get(device_type=zero_u)
        self.assertEqual((pdu.face, pdu.device_role_id), ("", self.role_a.pk))

    def test_a_real_racks_tray_pdu_goes_into_the_template(self):
        """Same for a real rack's own 0U devices (from-rack)."""
        self._add_perms()
        rack = Rack.objects.create(name="TE Rack Real Tray", site=self.site, u_height=10)
        zero_u = DeviceType.objects.create(
            manufacturer=self.mfr, model="TE PDU 2", slug="te-pdu-2", u_height=0)
        Device.objects.create(
            name="real-pdu-a1", site=self.site, rack=rack, device_type=zero_u,
            role=self.role_a)
        Device.objects.create(
            name="Bot", site=self.site, rack=rack, device_type=self.device_type,
            role=self.role_b, position=1, face="front")

        resp = self.client.post(
            _from_rack_url(), {"rack_id": rack.pk, "name": "Real Tray Template"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["placement_count"], 2, resp.data["warnings"])
        template = Template.objects.get(pk=resp.data["template_id"])
        self.assertTrue(template.placements.filter(device_type=zero_u).exists())

    def test_a_unit_high_device_parked_in_the_tray_goes_back_to_the_tray(self):
        """Where a device stood decides, not its height: a 1U server parked in
        the tray is saved anchored to the tray, and stamps back into it."""
        self._add_perms()
        rack = Rack.objects.create(name="TE Rack Parked", site=self.site, u_height=10)
        Device.objects.create(
            name="parked-srv", site=self.site, rack=rack, device_type=self.device_type,
            role=self.role_a)

        resp = self.client.post(
            _from_rack_url(), {"rack_id": rack.pk, "name": "Parked Template"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["placement_count"], 1)
        placement = Template.objects.get(pk=resp.data["template_id"]).placements.get()
        self.assertEqual(placement.anchor, "tray")
        (_item, position, face), = stamping.compute_stamp(
            [_StampItem(placement)], {"front": [], "rear": []}, 10)[0]
        self.assertEqual((position, face), (None, ""))

    def test_a_planned_rack_is_saved_from_what_the_design_plans_in_it(self):
        """A planned rack holds the design's planned devices, so it saves like
        a real one (it used to be refused as "no devices yet")."""
        self._add_perms()
        location = Location.objects.create(name="TE Loc", slug="te-loc", site=self.site)
        planned = PlannedRack.objects.create(name="TE Planned", location=location, u_height=10)
        design = make_design(title="TE Planned Design", site=self.site)
        design.planned_racks.add(planned)
        from ..models import DesignPlacement
        DesignPlacement.objects.create(
            design=design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, device_role=self.role_a,
            target_planned_rack=planned, target_position=1, target_face="front",
            proposed_name="te-planned-1",
        )

        resp = self.client.post(
            _from_design_url(),
            {"design": design.pk, "rack": f"p:{planned.pk}", "name": "Planned Template"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["placement_count"], 1)

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

    def test_a_device_with_gaps_on_both_sides_keeps_its_distance(self):
        """A device floating mid-rack is anchored to the end of the half it
        sits in, at its real distance from it -- so the gap comes back, in a
        rack of the same height and in a taller one (it used to be left out
        as an "island")."""
        self._add_perms()
        rack = Rack.objects.create(name="TE Rack Island", site=self.site, u_height=10)
        for name, position in (("TopEdge", 10), ("Island", 5), ("BottomEdge", 1)):
            Device.objects.create(
                name=name, site=self.site, rack=rack, device_type=self.device_type,
                role=self.role_a, position=position, face="front",
            )

        resp = self.client.post(
            _from_rack_url(), {"rack_id": rack.pk, "name": "Island Template"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_201_CREATED)
        self.assertEqual(resp.data.get("warnings", []), [])
        template = Template.objects.get(pk=resp.data["template_id"])
        island = template.placements.get(label="Island")
        self.assertEqual((island.anchor, island.offset), ("bottom", Decimal(4)))

        items = [_StampItem(tp) for tp in template.placements.all()]
        for height, top in ((10, 10), (12, 12), (47, 47)):
            placements, unplaced = stamping.compute_stamp(
                items, {"front": [], "rear": []}, height, starting_unit=1)
            self.assertEqual(unplaced, [])
            by_label = {item.tp.label: position for item, position, _face in placements}
            self.assertEqual(by_label, {"TopEdge": Decimal(top), "Island": Decimal(5),
                                        "BottomEdge": Decimal(1)}, height)

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
        design = make_design(title="TE 400 Design", site=self.site)
        resp = self.client.post(
            _from_design_url(),
            {"design": design.pk, "rack": "r:999999", "name": "Nope"},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)

    def test_from_design_malformed_rack_key_is_400(self):
        self._add_perms()
        design = make_design(title="TE 400b Design", site=self.site)
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
        self.offset = tp.offset
