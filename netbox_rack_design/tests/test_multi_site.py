"""
Tests for multi-site designs (PLAN-multi-site.md P1).

``Design.site`` (a single FK) becomes ``Design.sites`` (M2M to ``dcim.Site``,
at least one required, M1). Every "must be in the design's site" check
becomes "must be in one of the design's sites" (M3); name uniqueness and
naming counters scope by the PLACEMENT's (target rack's) site, never the
design's (M4); a chain requires only that child and parent share at least one
site, not the same one (M5); ``sequence`` is a single global gapped counter,
not per site (M6); Apply's permission checks/messages and device creation use
the device's own site (M7).
"""

from core.models import ObjectType
from dcim.models import Device, Location, Rack, Site
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from users.models import ObjectPermission, User

from .. import apply, versioning
from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignPlacement, DesignPowerFeed, DesignRackPower, PlannedRack
from ..naming import generate_name, name_exists_in_site
from .utils import create_dcim_environment, make_design


class MultiSiteTestCase(TestCase):
    """Shared three-site fixture: site_a (from create_dcim_environment, with
    two real racks and two real devices), site_b (one real rack), site_c
    (one real rack, deliberately never in scope -- "the third site")."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site_a = env["site"]
        cls.device_type = env["device_type"]
        cls.device_role = env["device_role"]
        cls.tenant = env["tenant"]
        cls.racks_a = env["racks"]  # Rack 1, Rack 2 @ site_a
        cls.devices = env["devices"]  # Device 1 @ Rack 1/U1, Device 2 @ Rack 1/U2

        cls.site_b = Site.objects.create(name="Site B", slug="site-b")
        cls.rack_b = Rack.objects.create(name="Rack B1", site=cls.site_b)

        cls.site_c = Site.objects.create(name="Site C", slug="site-c")
        cls.rack_c = Rack.objects.create(name="Rack C1", site=cls.site_c)


# --- M1: at least one site required, M2: the `site` back-compat property ----

class DesignSitesTestCase(MultiSiteTestCase):

    def test_at_least_one_site_required(self):
        design = Design.objects.create(title="No sites")
        with self.assertRaises(ValidationError):
            design.full_clean()

    def test_two_sites_accepts_racks_from_both(self):
        design = make_design("Two-site", sites=(self.site_a, self.site_b))
        design.racks.set([self.racks_a[0], self.rack_b])
        design.full_clean()  # must not raise

    def test_rack_from_third_site_refused_same_message_shape(self):
        design = make_design("Two-site", sites=(self.site_a, self.site_b))
        design.racks.set([self.racks_a[0], self.rack_c])
        with self.assertRaises(ValidationError) as ctx:
            design.full_clean()
        self.assertIn("racks", ctx.exception.message_dict)
        self.assertIn(str(self.rack_c), ctx.exception.message_dict["racks"][0])

    def test_site_property_one_site(self):
        design = make_design("One", site=self.site_a)
        self.assertEqual(design.site, self.site_a)

    def test_site_property_multi_site_is_none(self):
        design = make_design("Two", sites=(self.site_a, self.site_b))
        self.assertIsNone(design.site)


# --- M6: sequence is a single global gapped counter --------------------------

class SequenceTestCase(MultiSiteTestCase):

    def test_sequence_is_global_not_per_site(self):
        d1 = make_design("A", site=self.site_a)
        d2 = make_design("B", site=self.site_b)
        d3 = make_design("C", site=self.site_a)
        self.assertEqual(d2.sequence, d1.sequence + 10)
        self.assertEqual(d3.sequence, d2.sequence + 10)


# --- M3: placement / feed / rack-power / planned-rack validate across sites --

class PlacementMultiSiteTestCase(MultiSiteTestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.design = make_design("Plan", sites=(cls.site_a, cls.site_b))
        cls.design.racks.set([cls.racks_a[0], cls.rack_b])

    def test_placement_in_first_site_valid(self):
        placement = DesignPlacement(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_rack=self.racks_a[0], target_position=10,
        )
        placement.full_clean()  # must not raise

    def test_placement_in_second_site_valid(self):
        placement = DesignPlacement(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_rack=self.rack_b, target_position=10,
        )
        placement.full_clean()  # must not raise

    # The per-placement site check (``_validate_tray_target``) only runs for
    # a tray target (``target_position=None``, spec §9.5) -- a POSITIONED
    # target's site membership is enforced at the DESIGN level instead
    # (``Design.clean()``'s ``racks`` scope check, covered in
    # ``DesignSitesTestCase`` above), unchanged from before this phase.
    def test_tray_placement_in_third_site_refused(self):
        placement = DesignPlacement(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_rack=self.rack_c,
        )
        with self.assertRaises(ValidationError):
            placement.full_clean()

    def test_tray_placement_in_scoped_site_valid(self):
        placement = DesignPlacement(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_rack=self.rack_b,
        )
        placement.full_clean()  # must not raise


class PlannedRackMultiSiteTestCase(MultiSiteTestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.design = make_design("Plan", sites=(cls.site_a, cls.site_b))
        cls.location_a = Location.objects.create(name="Loc A", slug="loc-a-ms", site=cls.site_a)
        cls.location_c = Location.objects.create(name="Loc C", slug="loc-c-ms", site=cls.site_c)

    def test_planned_rack_in_scoped_site_valid(self):
        planned = PlannedRack.objects.create(name="Greenfield", location=self.location_a, u_height=20)
        self.design.planned_racks.set([planned])
        self.design.full_clean()  # must not raise

    def test_planned_rack_outside_scope_refused(self):
        planned = PlannedRack.objects.create(name="Greenfield-C", location=self.location_c, u_height=20)
        self.design.planned_racks.set([planned])
        with self.assertRaises(ValidationError):
            self.design.full_clean()

    def test_placement_target_planned_rack_outside_scope_refused(self):
        planned = PlannedRack.objects.create(name="GF-blocked", location=self.location_c, u_height=20)
        placement = DesignPlacement(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_planned_rack=planned, target_position=5,
        )
        with self.assertRaises(ValidationError):
            placement.full_clean()


class FeedRackPowerMultiSiteTestCase(MultiSiteTestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.design = make_design("Plan", sites=(cls.site_a, cls.site_b))
        cls.location_a = Location.objects.create(name="Loc A2", slug="loc-a-fp", site=cls.site_a)
        cls.location_c = Location.objects.create(name="Loc C2", slug="loc-c-fp", site=cls.site_c)

    def test_feed_planned_rack_in_scope_valid(self):
        planned = PlannedRack.objects.create(name="GF-feed-ok", location=self.location_a, u_height=20)
        feed = DesignPowerFeed(design=self.design, planned_rack=planned, name="Feed A")
        feed.full_clean()  # must not raise

    def test_feed_planned_rack_outside_scope_refused(self):
        planned = PlannedRack.objects.create(name="GF-feed-bad", location=self.location_c, u_height=20)
        feed = DesignPowerFeed(design=self.design, planned_rack=planned, name="Feed A")
        with self.assertRaises(ValidationError):
            feed.full_clean()

    def test_rack_power_planned_rack_outside_scope_refused(self):
        planned = PlannedRack.objects.create(name="GF-power-bad", location=self.location_c, u_height=20)
        rack_power = DesignRackPower(design=self.design, planned_rack=planned)
        with self.assertRaises(ValidationError):
            rack_power.full_clean()


# --- M4: name uniqueness/naming counters scope by the placement's site ------

class NamingMultiSiteTestCase(MultiSiteTestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.design = make_design("Multi-Build", sites=(cls.site_a, cls.site_b))
        cls.p_a = DesignPlacement.objects.create(
            design=cls.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type, target_rack=cls.racks_a[1], target_position=10,
            target_face="front", proposed_name="claimed-a",
        )
        cls.p_b = DesignPlacement.objects.create(
            design=cls.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type, target_rack=cls.rack_b, target_position=10,
            target_face="front", proposed_name="claimed-b",
        )

    def test_name_exists_in_site_scopes_by_placement_site_not_design(self):
        # "claimed-a" belongs to a placement targeting site_a: visible there...
        self.assertTrue(name_exists_in_site("claimed-a", self.site_a))
        # ...but NOT in site_b, even though both sites belong to the SAME
        # (two-site) design.
        self.assertFalse(name_exists_in_site("claimed-a", self.site_b))
        self.assertTrue(name_exists_in_site("claimed-b", self.site_b))
        self.assertFalse(name_exists_in_site("claimed-b", self.site_a))

    def test_design_placement_site_property(self):
        self.assertEqual(self.p_a.site, self.site_a)
        self.assertEqual(self.p_b.site, self.site_b)

    @override_settings(PLUGINS_CONFIG={"netbox_rack_design": {
        "naming_mode": "template",
        "naming_template": "{design.site.name}",
        "naming_script": "",
    }})
    def test_design_site_token_one_site_design_unchanged(self):
        design = make_design("Single", site=self.site_a)
        placement = DesignPlacement.objects.create(
            design=design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_rack=self.racks_a[0], target_position=20,
            target_face="front",
        )
        self.assertEqual(generate_name(placement), self.site_a.name)

    @override_settings(PLUGINS_CONFIG={"netbox_rack_design": {
        "naming_mode": "template",
        "naming_template": "{design.site.name}",
        "naming_script": "",
    }})
    def test_design_site_token_two_site_design_equals_target_rack_site(self):
        self.assertEqual(generate_name(self.p_a), self.site_a.name)
        self.assertEqual(generate_name(self.p_b), self.site_b.name)


# --- M5: chain requires only a shared site, not the same one ----------------

class ChainMultiSiteTestCase(MultiSiteTestCase):

    def test_derive_refused_when_no_shared_site(self):
        parent = make_design("Parent", sites=(self.site_a,))
        parent.status = DesignStatusChoices.STATUS_APPROVED
        parent.save()
        child = Design.objects.create(title="Child", based_on=parent)
        child.sites.set([self.site_c])
        with self.assertRaises(ValidationError) as ctx:
            child.full_clean()
        self.assertIn("based_on", ctx.exception.message_dict)

    def test_derive_allowed_on_overlap(self):
        parent = make_design("Parent", sites=(self.site_a, self.site_b))
        parent.status = DesignStatusChoices.STATUS_APPROVED
        parent.save()
        child = Design.objects.create(title="Child", based_on=parent)
        child.sites.set([self.site_b, self.site_c])
        child.full_clean()  # must not raise -- overlap on site_b

    def test_new_version_copies_sites(self):
        design = make_design("Versioned", sites=(self.site_a, self.site_b))
        clone = versioning.new_version(design)
        self.assertEqual(set(clone.sites.all()), {self.site_a, self.site_b})


# --- M7: apply uses the device's own site, not the design's -----------------

class ApplyMultiSiteTestCase(MultiSiteTestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.superuser = User.objects.create_superuser(username="ms-apply-super")

    def _design(self):
        design = make_design("Multi-site apply", sites=(self.site_a, self.site_b))
        design.racks.set([self.racks_a[0], self.rack_b])
        return design

    def test_apply_creates_device_in_the_racks_own_site(self):
        design = self._design()
        DesignPlacement.objects.create(
            design=design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, device_role=self.device_role,
            target_rack=self.rack_b, target_position=5,
            target_face="front", proposed_name="ms-srv-b",
        )
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()

        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)
        device = Device.objects.get(name="ms-srv-b")
        self.assertEqual(device.site, self.site_b)

    def test_missing_add_permission_names_the_racks_site(self):
        design = self._design()
        DesignPlacement.objects.create(
            design=design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, device_role=self.device_role,
            target_rack=self.rack_b, target_position=5,
            target_face="front", proposed_name="ms-srv-noperm",
        )
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()
        user = User.objects.create_user(username="ms-no-perm-user")

        result = apply.plan(design, user)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("permission to create devices" in p and str(self.site_b) in p
                for p in result.problems),
            result.problems,
        )

    def test_change_permission_error_names_the_devices_own_site(self):
        design = self._design()
        DesignPlacement.objects.create(
            design=design, kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.devices[1],
        )
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()

        other_site = Site.objects.create(name="Other site MS", slug="other-site-ms")
        user = User.objects.create_user(username="ms-wrong-site-user")
        permission = ObjectPermission(
            name="ms-constrained-change", actions=["change"],
            constraints={"site_id": other_site.pk},
        )
        permission.save()
        permission.users.add(user)
        permission.object_types.add(ObjectType.objects.get_for_model(Device))

        result = apply.plan(design, user)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("permission to modify device" in p and self.devices[1].name in p
                for p in result.problems),
            result.problems,
        )
