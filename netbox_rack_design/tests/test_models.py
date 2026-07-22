"""
Model-level tests for NetBox Rack Design.

These cover behaviour that the generic suites do NOT exercise: the custom
``clean()`` validation rules, sequence auto-assignment, and string/URL helpers.
The CRUD/permissions/changelog matrix lives in test_api.py and test_views.py.
"""

from dcim.models import PowerFeed, PowerPanel, Rack, Site
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import (
    Design,
    DesignGroup,
    DesignPlacement,
    DesignPowerFeed,
    DesignRackPower,
)
from .utils import create_dcim_environment


class DesignGroupTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.parent = DesignGroup.objects.create(name="Parent")
        cls.child = DesignGroup.objects.create(name="Child", parent=cls.parent)

    def test_str(self):
        self.assertEqual(str(self.parent), "Parent")

    def test_unique_name(self):
        with self.assertRaises(ValidationError):
            DesignGroup(name="Parent").full_clean()

    def test_cyclic_parent_rejected(self):
        # A group cannot be its own ancestor.
        self.parent.parent = self.child
        with self.assertRaises(ValidationError):
            self.parent.full_clean()


class DesignTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]

    def test_str_includes_version(self):
        design = Design.objects.create(title="Plan", site=self.site, version=2)
        self.assertEqual(str(design), "Plan (v2)")

    def test_sequence_auto_assigned(self):
        d1 = Design.objects.create(title="A", site=self.site)
        d2 = Design.objects.create(title="B", site=self.site)
        self.assertEqual(d1.sequence, 10)
        self.assertEqual(d2.sequence, 20)

    def test_cannot_be_based_on_self(self):
        design = Design.objects.create(title="A", site=self.site)
        design.based_on = design
        with self.assertRaises(ValidationError):
            design.full_clean()

    def test_single_approved_version_per_plan(self):
        root = Design.objects.create(
            title="Root", site=self.site, status=DesignStatusChoices.STATUS_APPROVED
        )
        sibling = Design(
            title="V2",
            site=self.site,
            version=2,
            root=root,
            status=DesignStatusChoices.STATUS_APPROVED,
        )
        with self.assertRaises(ValidationError):
            sibling.full_clean()

    def test_first_approved_design_validates(self):
        # A brand-new, unsaved standalone design created directly as Approved must
        # validate cleanly -- it has no persisted version group to conflict with.
        # Regression: clean() previously raised ValueError on the unsaved root.
        design = Design(
            title="First", site=self.site, status=DesignStatusChoices.STATUS_APPROVED
        )
        design.full_clean()  # must not raise

    def test_racks_can_be_added(self):
        design = Design.objects.create(title="Scoped", site=self.site)
        design.racks.add(*self.racks)
        self.assertEqual(
            set(design.racks.all()),
            set(self.racks),
        )

    def test_same_site_racks_validate(self):
        # Racks in the design's own site pass validation.
        design = Design.objects.create(title="Scoped", site=self.site)
        design.racks.add(*self.racks)
        design.full_clean()  # must not raise

    def test_rack_from_other_site_rejected(self):
        other_site = Site.objects.create(name="Other Site", slug="other-site")
        foreign_rack = Rack.objects.create(name="Foreign Rack", site=other_site)
        design = Design.objects.create(title="Scoped", site=self.site)
        design.racks.add(foreign_rack)
        with self.assertRaises(ValidationError) as ctx:
            design.full_clean()
        self.assertIn("racks", ctx.exception.message_dict)

    def test_seed_logic_pre_seeds_from_placements(self):
        # Mirrors the 0005 data migration's seed query: a design with placements
        # targeting in-site racks should end up scoping exactly those racks.
        design = Design.objects.create(title="Seed me", site=self.site)
        DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[0],
            target_position=10,
        )
        rack_ids = list(
            Rack.objects.filter(
                pk__in=DesignPlacement.objects.filter(
                    design=design, target_rack__isnull=False
                )
                .values_list("target_rack_id", flat=True)
                .distinct(),
                site_id=design.site_id,
            ).values_list("pk", flat=True)
        )
        design.racks.add(*rack_ids)
        self.assertEqual(set(design.racks.all()), {self.racks[0]})


class DesignPlacementTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]
        cls.design = Design.objects.create(title="Plan", site=cls.site)

    def test_add_requires_device_type(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            target_rack=self.racks[1],
            target_position=10,
        )
        with self.assertRaises(ValidationError):
            placement.full_clean()

    def test_add_rejects_existing_device(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device=self.devices[0],
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=10,
        )
        with self.assertRaises(ValidationError):
            placement.full_clean()

    def test_valid_add(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=10,
        )
        placement.full_clean()  # should not raise

    def test_move_requires_device(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=10,
        )
        with self.assertRaises(ValidationError):
            placement.full_clean()

    def test_valid_move(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[1],
            target_position=10,
        )
        placement.full_clean()  # should not raise

    def test_remove_needs_no_target(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.devices[0],
        )
        placement.full_clean()  # should not raise

    def test_add_rejects_occupied_slot(self):
        # U1 in Rack 1 is occupied by Device 1.
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[0],
            target_position=1,
        )
        with self.assertRaises(ValidationError):
            placement.full_clean()

    def test_move_onto_slot_vacated_by_persisted_remove_is_valid(self):
        """Single-placement validation (no batch context) reads the design's
        already-persisted move/remove rows to know which devices vacated their
        real slots. A persisted remove of Device 2 frees U2 for Device 1."""
        # Device 2 sits at Rack1/U2; a persisted remove vacates that slot.
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.devices[1],
        )
        move = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],  # really at U1
            target_rack=self.racks[0],
            target_position=2,  # into the slot the remove freed
            target_face="front",
        )
        move.full_clean()  # should not raise

    def test_move_onto_slot_held_by_unvacated_device_is_invalid(self):
        """Without any move/remove vacating it, U2 is still held by Device 2 →
        moving Device 1 onto it must still fail (the relaxation is scoped)."""
        move = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[0],
            target_position=2,
            target_face="front",
        )
        with self.assertRaises(ValidationError):
            move.full_clean()

    def test_move_with_no_position_is_a_valid_tray_target(self):
        """A move with target_rack set and target_position=None is a dismount
        to the tray (spec §9.5) -- no slot to validate, so it must pass."""
        move = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[1],
        )
        move.full_clean()  # should not raise
        self.assertIsNone(move.target_position)

    def test_add_with_no_position_is_a_valid_tray_target(self):
        """A palette-add with no target_position plans a new off-rack device
        (spec §9.3 palette -> tray) and must pass validation."""
        add = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[0],
        )
        add.full_clean()  # should not raise

    def test_tray_target_in_other_site_rejected(self):
        """A tray target must still stay within the design's site (spec §9.5)
        even though there is no slot to check."""
        other_site = Site.objects.create(name="Other Site 2", slug="other-site-2")
        foreign_rack = Rack.objects.create(name="Foreign Rack 2", site=other_site)
        move = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=foreign_rack,
        )
        with self.assertRaises(ValidationError) as ctx:
            move.full_clean()
        self.assertIn("target_rack", ctx.exception.message_dict)

    def test_power_config_defaults_to_none(self):
        placement = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=10,
        )
        self.assertIsNone(placement.power_config)

    def test_power_config_round_trips_custom_fields_only(self):
        # power_config is now the CUSTOM-FIELD bridge only -- no inline "feed"
        # (a PDU's electricals come from the bound feed, not this JSON).
        config = {
            "source": "copy_rack",
            "copied_from": {
                "rack_id": self.racks[0].pk,
                "rack_name": self.racks[0].name,
                "device_id": self.devices[0].pk,
                "device_name": self.devices[0].name,
            },
            "custom_fields": {"pdu_scheme": "2x1PH2Banks"},
        }
        placement = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=10,
            power_config=config,
        )
        placement.refresh_from_db()
        self.assertEqual(placement.power_config, config)

    def _pdu_add(self, **kwargs):
        return DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=10,
            **kwargs,
        )

    def test_bound_feed_none_when_unbound(self):
        self.assertIsNone(self._pdu_add().bound_feed)

    def test_bound_feed_resolves_real_feed(self):
        panel = PowerPanel.objects.create(site=self.site, name="Panel 1")
        feed = PowerFeed.objects.create(power_panel=panel, name="Feed A", amperage=32)
        placement = self._pdu_add(real_power_feed=feed)
        self.assertEqual(placement.bound_feed, feed)

    def test_bound_feed_resolves_planned_feed(self):
        feed = DesignPowerFeed.objects.create(
            design=self.design, rack=self.racks[1], name="Feed A"
        )
        placement = self._pdu_add(planned_power_feed=feed)
        self.assertEqual(placement.bound_feed, feed)

    def test_power_source_device_fk_round_trips_and_defaults_null(self):
        # A planned PDU may inherit cf live from a real source device via FK.
        self.assertIsNone(self._pdu_add().power_source_device)
        placement = self._pdu_add(power_source_device=self.devices[0])
        placement.refresh_from_db()
        self.assertEqual(placement.power_source_device, self.devices[0])
        # cf is read live off the source device (no snapshot).
        self.assertEqual(
            dict(placement.power_source_device.cf), dict(self.devices[0].cf)
        )

    def test_cannot_reference_source_device_and_carry_manual_cf(self):
        # cf come from a referenced device OR manual power_config, never both.
        placement = self._pdu_add(
            power_source_device=self.devices[0],
            power_config={"custom_fields": {"warranty_type": "gold"}},
        )
        with self.assertRaises(ValidationError):
            placement.full_clean()

    def test_source_device_alone_is_valid(self):
        placement = self._pdu_add(power_source_device=self.devices[0])
        placement.full_clean()  # no manual cf -> fine

    def test_cannot_bind_both_real_and_planned_feed(self):
        panel = PowerPanel.objects.create(site=self.site, name="Panel 1")
        real = PowerFeed.objects.create(power_panel=panel, name="Feed A", amperage=32)
        planned = DesignPowerFeed.objects.create(
            design=self.design, rack=self.racks[1], name="Feed A"
        )
        placement = self._pdu_add()
        placement.real_power_feed = real
        placement.planned_power_feed = planned
        with self.assertRaises(ValidationError):
            placement.full_clean()


class DesignPowerFeedTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.design = Design.objects.create(title="Plan", site=cls.site)

    def test_defaults_mirror_dcim_powerfeed(self):
        feed = DesignPowerFeed.objects.create(
            design=self.design, rack=self.racks[0], name="Feed A"
        )
        # Field names + value domains mirror dcim.PowerFeed so bound_feed is uniform.
        self.assertEqual(feed.voltage, 230)
        self.assertEqual(feed.amperage, 16)
        self.assertEqual(feed.phase, "single-phase")
        self.assertEqual(feed.supply, "ac")

    def test_round_trips(self):
        feed = DesignPowerFeed.objects.create(
            design=self.design, rack=self.racks[0], name="Feed B",
            voltage=400, amperage=32, phase="three-phase", supply="ac",
        )
        feed.refresh_from_db()
        self.assertEqual((feed.voltage, feed.amperage, feed.phase), (400, 32, "three-phase"))

    def test_unique_design_rack_name(self):
        DesignPowerFeed.objects.create(design=self.design, rack=self.racks[0], name="Feed A")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DesignPowerFeed.objects.create(design=self.design, rack=self.racks[0], name="Feed A")

    def test_same_name_different_rack_allowed(self):
        DesignPowerFeed.objects.create(design=self.design, rack=self.racks[0], name="Feed A")
        DesignPowerFeed.objects.create(design=self.design, rack=self.racks[1], name="Feed A")
        self.assertEqual(DesignPowerFeed.objects.filter(name="Feed A").count(), 2)

    def test_cascade_on_design_delete(self):
        DesignPowerFeed.objects.create(design=self.design, rack=self.racks[0], name="Feed A")
        self.design.delete()
        self.assertEqual(DesignPowerFeed.objects.count(), 0)


class DesignRackPowerTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.design = Design.objects.create(title="Plan", site=cls.site)

    def test_power_config_defaults_to_none(self):
        rack_power = DesignRackPower.objects.create(design=self.design, rack=self.racks[0])
        self.assertIsNone(rack_power.power_config)

    def test_power_config_round_trips(self):
        config = {
            "source": "manual",
            "custom_fields": {"power_limitation": 8000, "pdu_location": "top"},
        }
        rack_power = DesignRackPower.objects.create(
            design=self.design, rack=self.racks[0], power_config=config
        )
        rack_power.refresh_from_db()
        self.assertEqual(rack_power.power_config, config)

    def test_unique_design_rack_constraint(self):
        DesignRackPower.objects.create(design=self.design, rack=self.racks[0])
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DesignRackPower.objects.create(design=self.design, rack=self.racks[0])
