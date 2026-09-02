"""
Model-level tests for NetBox Rack Design.

These cover behaviour that the generic suites do NOT exercise: the custom
``clean()`` validation rules, sequence auto-assignment, and string/URL helpers.
The CRUD/permissions/changelog matrix lives in test_api.py and test_views.py.
"""

from dcim.models import DeviceType, PowerFeed, PowerPanel, Rack, Site
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

    def test_based_on_other_site_rejected(self):
        # A chain across two sites is meaningless (PLAN-design-chains.md gap
        # 1): placements are site-scoped, so a child in a different site could
        # never actually replay an ancestor's placements into its own racks.
        # The form already rejects this (forms.py); the model must too, since
        # the REST API / GraphQL / bulk import / a shell script all bypass the
        # form.
        other_site = Site.objects.create(name="Other Site 2", slug="other-site-2")
        parent = Design.objects.create(
            title="Parent", site=other_site, status=DesignStatusChoices.STATUS_APPROVED
        )
        child = Design(title="Child", site=self.site, based_on=parent)
        with self.assertRaises(ValidationError) as ctx:
            child.full_clean()
        self.assertIn("based_on", ctx.exception.message_dict)

    def test_based_on_same_site_validates(self):
        parent = Design.objects.create(
            title="Parent", site=self.site, status=DesignStatusChoices.STATUS_APPROVED
        )
        child = Design(title="Child", site=self.site, based_on=parent)
        child.full_clean()  # must not raise

    # --- baseline_chain() ----------------------------------------------------

    def test_baseline_chain_empty_with_no_parent(self):
        design = Design.objects.create(title="Orphan", site=self.site)
        self.assertEqual(design.baseline_chain(), [])

    def test_baseline_chain_single_parent(self):
        a = Design.objects.create(title="A", site=self.site)
        b = Design.objects.create(title="B", site=self.site, based_on=a)
        self.assertEqual(b.baseline_chain(), [a])

    def test_baseline_chain_excludes_self(self):
        a = Design.objects.create(title="A", site=self.site)
        b = Design.objects.create(title="B", site=self.site, based_on=a)
        self.assertNotIn(b, b.baseline_chain())

    def test_baseline_chain_three_deep_oldest_first(self):
        a = Design.objects.create(title="A", site=self.site)
        b = Design.objects.create(title="B", site=self.site, based_on=a)
        c = Design.objects.create(title="C", site=self.site, based_on=b)
        self.assertEqual(c.baseline_chain(), [a, b])

    def test_baseline_chain_detects_cycle(self):
        a = Design.objects.create(title="A", site=self.site)
        b = Design.objects.create(title="B", site=self.site, based_on=a)
        # Force a cycle directly at the DB level (bypassing clean()), the way
        # a pre-existing row (saved before the guard existed) could already
        # be broken.
        Design.objects.filter(pk=a.pk).update(based_on=b)
        a.refresh_from_db()
        with self.assertRaises(ValueError):
            a.baseline_chain()

    # --- based_on / depends_on cycle guards in clean() (G7) ------------------

    def test_cannot_be_based_on_indirect_ancestor(self):
        a = Design.objects.create(title="A", site=self.site)
        b = Design.objects.create(title="B", site=self.site, based_on=a)
        a.based_on = b
        with self.assertRaises(ValidationError):
            a.full_clean()

    def test_depends_on_self_rejected(self):
        design = Design.objects.create(title="A", site=self.site)
        design.depends_on.add(design)
        with self.assertRaises(ValidationError):
            design.full_clean()

    def test_depends_on_indirect_cycle_rejected(self):
        a = Design.objects.create(title="A", site=self.site)
        b = Design.objects.create(title="B", site=self.site)
        a.depends_on.add(b)
        b.depends_on.add(a)
        with self.assertRaises(ValidationError):
            a.full_clean()

    def test_depends_on_cycle_check_skipped_when_unsaved(self):
        # M2M cannot be read on an unsaved instance -- clean() must not blow up
        # on a brand-new design just because depends_on can't be queried yet.
        design = Design(title="New", site=self.site)
        design.full_clean()  # must not raise

    # --- is_frozen (§2.2) -----------------------------------------------------

    def test_is_frozen_true_when_approved(self):
        design = Design.objects.create(
            title="A", site=self.site, status=DesignStatusChoices.STATUS_APPROVED
        )
        self.assertTrue(design.is_frozen)

    def test_is_frozen_false_when_draft(self):
        design = Design.objects.create(title="A", site=self.site)
        self.assertFalse(design.is_frozen)

    # --- children (§2.2 / un-approve guard) -----------------------------------

    def test_children_lists_designs_based_on_this_one(self):
        a = Design.objects.create(
            title="A", site=self.site, status=DesignStatusChoices.STATUS_APPROVED
        )
        b = Design.objects.create(title="B", site=self.site, based_on=a)
        self.assertEqual(list(a.children), [b])

    def test_unapproving_blocked_with_children(self):
        a = Design.objects.create(
            title="A", site=self.site, status=DesignStatusChoices.STATUS_APPROVED
        )
        Design.objects.create(title="B", site=self.site, based_on=a)
        a.status = DesignStatusChoices.STATUS_DRAFT
        with self.assertRaises(ValidationError):
            a.full_clean()

    def test_unapproving_allowed_without_children(self):
        a = Design.objects.create(
            title="A", site=self.site, status=DesignStatusChoices.STATUS_APPROVED
        )
        a.status = DesignStatusChoices.STATUS_DRAFT
        a.full_clean()  # must not raise

    # --- rack scope frozen once approved (§2.2/G4, hole 2) --------------------
    #
    # `racks` is the one field the task calls out as part of "what was
    # approved": the API's `add-rack`/`remove-rack` actions already refuse it
    # on a frozen design, and this closes the same hole at the model layer for
    # every other write path. A many-to-many is never read/written through
    # clean() -- it goes straight to its own through-table -- so ordinary
    # model validation genuinely cannot observe a PENDING racks change the way
    # it observes a pending scalar field. The one channel that IS visible at
    # clean() time is `instance._m2m_values`, which NetBox's own
    # `ValidatedModelSerializer.validate()` (netbox/api/serializers/base.py)
    # stashes the incoming M2M values on the instance and calls full_clean()
    # BEFORE actually applying them -- exactly the REST API path this test
    # exercises directly. The HTML edit form has no such side channel (Django's
    # ModelForm never touches an instance's m2m before calling its
    # full_clean()), so DesignForm.clean() (test_views.py) carries the
    # equivalent form-layer check, mirroring the existing same-site split.

    def test_racks_change_rejected_on_approved_design(self):
        design = Design.objects.create(
            title="Approved scope", site=self.site,
            status=DesignStatusChoices.STATUS_APPROVED,
        )
        design.racks.set([self.racks[0]])
        # Simulate the REST API's ValidatedModelSerializer: the new value is
        # stashed on the instance, the m2m itself is untouched.
        design._m2m_values = {"racks": [self.racks[1]]}
        with self.assertRaises(ValidationError) as ctx:
            design.full_clean()
        self.assertIn("racks", ctx.exception.message_dict)

    def test_racks_unchanged_value_allowed_on_approved_design(self):
        # Re-submitting the SAME scope (e.g. a PATCH that touches other
        # fields but echoes racks back unchanged) must not be treated as a
        # scope change.
        design = Design.objects.create(
            title="Approved scope, no-op", site=self.site,
            status=DesignStatusChoices.STATUS_APPROVED,
        )
        design.racks.set([self.racks[0]])
        design._m2m_values = {"racks": [self.racks[0]]}
        design.full_clean()  # must not raise

    def test_racks_change_allowed_on_draft_design(self):
        design = Design.objects.create(title="Draft scope", site=self.site)
        design.racks.set([self.racks[0]])
        design._m2m_values = {"racks": [self.racks[1]]}
        design.full_clean()  # must not raise

    def test_racks_omitted_from_m2m_values_skips_check(self):
        # A write that never mentions `racks` at all (e.g. a PATCH touching
        # only `summary`) must not be mistaken for a scope change.
        design = Design.objects.create(
            title="Approved, racks untouched", site=self.site,
            status=DesignStatusChoices.STATUS_APPROVED,
        )
        design.racks.set([self.racks[0]])
        design._m2m_values = {}
        design.full_clean()  # must not raise

    def test_racks_change_ignored_when_no_m2m_values_present(self):
        # A plain `design.racks.set(...)` followed by `full_clean()` (a shell
        # script, a data migration) has no `_m2m_values` to compare against --
        # this documents that gap rather than pretending it's closed. It is a
        # KNOWN gap (mirrors the racks/site check's own documented CREATE gap).
        design = Design.objects.create(
            title="Approved, direct M2M write", site=self.site,
            status=DesignStatusChoices.STATUS_APPROVED,
        )
        design.racks.set([self.racks[0]])
        design.full_clean()  # does not raise -- the check has nothing to compare

    def test_status_change_allowed_on_approved_design(self):
        # The escape hatch itself must stay open: un-approving (with no
        # children) must not be blocked by the new racks check just because
        # `racks` is untouched.
        design = Design.objects.create(
            title="Un-approve me", site=self.site,
            status=DesignStatusChoices.STATUS_APPROVED,
        )
        design.status = DesignStatusChoices.STATUS_DRAFT
        design.full_clean()  # must not raise

    def test_summary_and_link_editable_on_approved_design(self):
        design = Design.objects.create(
            title="Metadata still editable", site=self.site,
            status=DesignStatusChoices.STATUS_APPROVED,
        )
        design.summary = "Updated summary"
        design.link = "https://example.com/ticket/1"
        design.full_clean()  # must not raise

    def test_racks_settable_on_create_of_approved_design(self):
        # The CREATE path must not be broken by the frozen-racks check: a
        # brand-new design has no persisted row yet (no `pk`), so there is no
        # "approved scope" to protect against changing.
        design = Design(
            title="New and approved", site=self.site,
            status=DesignStatusChoices.STATUS_APPROVED,
        )
        design._m2m_values = {"racks": [self.racks[0]]}
        design.full_clean()  # must not raise

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

    # --- frozen design (§2.2) --------------------------------------------------

    def test_create_placement_rejected_on_approved_design(self):
        approved = Design.objects.create(
            title="Approved", site=self.site, status=DesignStatusChoices.STATUS_APPROVED
        )
        placement = DesignPlacement(
            design=approved,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=10,
        )
        with self.assertRaises(ValidationError):
            placement.full_clean()

    def test_edit_placement_rejected_once_design_is_approved(self):
        # A dedicated design, not the shared `self.design` fixture: mutating a
        # setUpTestData object's attributes leaks across test methods within
        # the class (only the DB rolls back between tests, not the in-memory
        # Python object), so this test builds its own design to flip.
        design = Design.objects.create(title="Flip me", site=self.site)
        placement = DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=10,
        )
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()
        placement.target_position = 11
        with self.assertRaises(ValidationError):
            placement.full_clean()

    def test_edit_placement_allowed_after_design_returns_to_draft(self):
        design = Design.objects.create(title="Flip me back", site=self.site)
        placement = DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=10,
        )
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()
        design.status = DesignStatusChoices.STATUS_DRAFT
        design.save()
        placement.target_position = 11
        placement.full_clean()  # must not raise

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


class StalePlacementTestCase(TestCase):
    """Deleting a real ``dcim.Device`` must SET_NULL (not CASCADE) the FK of any
    ``move``/``remove`` placement referencing it, and the ``pre_delete`` receiver
    in signals.py must stamp ``stale``/``stale_device_name`` so the loss is
    reported instead of the row vanishing outright (the historical CASCADE bug)."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]  # Device 1 @ Rack1/U1/front, Device 2 @ Rack1/U2/front
        cls.design = Design.objects.create(title="Plan", site=cls.site)

    def test_device_delete_flags_move_and_remove_as_stale_not_cascade(self):
        # Regression: DesignPlacement.device used to be on_delete=CASCADE, so
        # deleting the real device silently deleted these rows. Assert they
        # SURVIVE, device-less, flagged stale, with the name captured.
        move = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[1],
            target_position=5,
        )
        remove = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.devices[0],
        )
        device_name = self.devices[0].name
        self.devices[0].delete()

        move.refresh_from_db()
        remove.refresh_from_db()
        self.assertIsNone(move.device_id)
        self.assertTrue(move.stale)
        self.assertEqual(move.stale_device_name, device_name)
        self.assertIsNone(remove.device_id)
        self.assertTrue(remove.stale)
        self.assertEqual(remove.stale_device_name, device_name)

    def test_unrelated_designs_and_add_placements_are_unaffected(self):
        other_design = Design.objects.create(title="Other plan", site=self.site)
        other_move = DesignPlacement.objects.create(
            design=other_design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[1],
            target_rack=self.racks[1],
            target_position=6,
        )
        add = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=7,
        )
        # Delete a device neither placement references.
        self.devices[0].delete()

        other_move.refresh_from_db()
        add.refresh_from_db()
        self.assertFalse(other_move.stale)
        self.assertEqual(other_move.device_id, self.devices[1].pk)
        self.assertFalse(add.stale)
        self.assertIsNone(add.device_id)

    def test_stale_placement_passes_full_clean(self):
        move = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[1],
            target_position=5,
        )
        self.devices[0].delete()
        move.refresh_from_db()
        move.full_clean()  # must not raise -- a stale row must remain savable

    def test_move_without_device_and_not_stale_is_rejected(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            target_rack=self.racks[1],
            target_position=5,
        )
        with self.assertRaises(ValidationError) as ctx:
            placement.full_clean()
        self.assertIn("device", ctx.exception.message_dict)

    def test_remove_without_device_and_not_stale_is_rejected(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
        )
        with self.assertRaises(ValidationError) as ctx:
            placement.full_clean()
        self.assertIn("device", ctx.exception.message_dict)

    def test_add_cannot_be_stale(self):
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=5,
            stale=True,
        )
        with self.assertRaises(ValidationError) as ctx:
            placement.full_clean()
        self.assertIn("stale", ctx.exception.message_dict)

    def test_repointing_a_stale_placement_clears_the_flag(self):
        move = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[1],
            target_position=5,
        )
        self.devices[0].delete()
        move.refresh_from_db()
        self.assertTrue(move.stale)

        move.device = self.devices[1]
        move.full_clean()
        self.assertFalse(move.stale)
        self.assertEqual(move.stale_device_name, "")
        move.save()
        move.refresh_from_db()
        self.assertFalse(move.stale)
        self.assertEqual(move.stale_device_name, "")

    def test_stale_move_skips_target_slot_validation(self):
        # A stale row's target (Rack 1 / U2, occupied by Device 2) would fail
        # slot validation if it were checked -- clean() must return early.
        move = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[0],
            target_position=2,  # occupied by Device 2
            target_face="front",
        )
        self.devices[0].delete()
        move.refresh_from_db()
        move.full_clean()  # must not raise despite the occupied target

    def test_design_stale_placements_returns_only_stale_rows(self):
        move = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[1],
            target_position=5,
        )
        live_remove = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.devices[1],
        )
        self.devices[0].delete()
        move.refresh_from_db()

        stale = list(self.design.stale_placements)
        self.assertEqual(stale, [move])
        self.assertNotIn(live_remove, stale)


class BasePlacementTestCase(TestCase):
    """``base_placement`` (PLAN-design-chains.md G2): a move/remove in a child
    design may act on a device that is not yet real -- it exists only as an
    ancestor design's planned 'add' -- by referencing that placement instead
    of a real ``device``."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]

        # Parent design: drafted, populated, THEN approved -- approving first
        # would freeze it before these fixture placements could be created.
        cls.parent_design = Design.objects.create(title="Parent", site=cls.site)
        cls.upstream_add = DesignPlacement.objects.create(
            design=cls.parent_design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            target_rack=cls.racks[0],
            target_position=5,
            proposed_name="upstream-node",
        )
        cls.upstream_move = DesignPlacement.objects.create(
            design=cls.parent_design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.devices[0],
            target_rack=cls.racks[1],
            target_position=9,
        )
        cls.parent_design.status = DesignStatusChoices.STATUS_APPROVED
        cls.parent_design.save()

        cls.child_design = Design.objects.create(
            title="Child", site=cls.site, based_on=cls.parent_design
        )

    def test_move_with_base_placement_and_no_device_is_valid(self):
        move = DesignPlacement(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=self.upstream_add,
            target_rack=self.racks[1],
            target_position=10,
        )
        move.full_clean()  # should not raise

    def test_remove_with_base_placement_and_no_device_is_valid(self):
        remove = DesignPlacement(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            base_placement=self.upstream_add,
        )
        remove.full_clean()  # should not raise

    def test_device_and_base_placement_both_set_is_rejected(self):
        move = DesignPlacement(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[1],
            base_placement=self.upstream_add,
            target_rack=self.racks[1],
            target_position=10,
        )
        with self.assertRaises(ValidationError) as ctx:
            move.full_clean()
        self.assertIn("base_placement", ctx.exception.message_dict)

    def test_base_placement_outside_ancestor_chain_is_rejected(self):
        unrelated_design = Design.objects.create(title="Unrelated", site=self.site)
        unrelated_add = DesignPlacement.objects.create(
            design=unrelated_design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[0],
            target_position=6,
        )
        move = DesignPlacement(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=unrelated_add,
            target_rack=self.racks[1],
            target_position=10,
        )
        with self.assertRaises(ValidationError) as ctx:
            move.full_clean()
        self.assertIn("base_placement", ctx.exception.message_dict)
        self.assertIn(str(unrelated_design), str(ctx.exception))

    def test_base_placement_in_same_design_is_rejected(self):
        # A placement in the SAME design is not an ancestor either -- the
        # chain excludes self.
        same_design_add = DesignPlacement.objects.create(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[0],
            target_position=7,
        )
        move = DesignPlacement(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=same_design_add,
            target_rack=self.racks[1],
            target_position=10,
        )
        with self.assertRaises(ValidationError) as ctx:
            move.full_clean()
        self.assertIn("base_placement", ctx.exception.message_dict)

    def test_base_placement_must_point_at_add(self):
        # An ancestor move/remove already acts on a real device -- a
        # downstream design references that device directly via ``device``,
        # so a base_placement pointing at a non-'add' row is meaningless.
        move = DesignPlacement(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=self.upstream_move,
            target_rack=self.racks[1],
            target_position=10,
        )
        with self.assertRaises(ValidationError) as ctx:
            move.full_clean()
        self.assertIn("base_placement", ctx.exception.message_dict)

    def test_add_with_base_placement_is_rejected(self):
        add = DesignPlacement(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            base_placement=self.upstream_add,
            target_rack=self.racks[1],
            target_position=11,
        )
        with self.assertRaises(ValidationError) as ctx:
            add.full_clean()
        self.assertIn("base_placement", ctx.exception.message_dict)

    def test_deleting_upstream_placement_leaves_downstream_stale(self):
        # Regression target for G2: base_placement is SET_NULL, not CASCADE --
        # deleting the upstream 'add' must not delete this downstream move.
        move = DesignPlacement.objects.create(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=self.upstream_add,
            target_rack=self.racks[1],
            target_position=12,
        )
        upstream_name = self.upstream_add.proposed_name
        self.upstream_add.delete()

        move.refresh_from_db()
        self.assertIsNone(move.base_placement_id)
        self.assertTrue(move.stale)
        self.assertEqual(move.stale_device_name, upstream_name)

    def test_repointing_a_base_placement_stale_row_clears_the_flag(self):
        move = DesignPlacement.objects.create(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=self.upstream_add,
            target_rack=self.racks[1],
            target_position=13,
        )
        self.upstream_add.delete()
        move.refresh_from_db()
        self.assertTrue(move.stale)

        move.device = self.devices[1]
        move.full_clean()
        self.assertFalse(move.stale)
        self.assertEqual(move.stale_device_name, "")


class PlannedPowerFeedChainTestCase(TestCase):
    """``planned_power_feed`` across a design chain (PLAN-design-chains.md G5
    item 3): a child's PDU may bind to an ancestor's ``DesignPowerFeed`` --
    that ancestor's layer has already happened from the child's point of view
    -- but not to a feed belonging to an unrelated design or a descendant.
    Mirrors ``base_placement``'s own-chain validation (``_validate_base_placement``).
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.racks = env["racks"]

        cls.parent_design = Design.objects.create(title="Parent", site=cls.site)
        cls.parent_feed = DesignPowerFeed.objects.create(
            design=cls.parent_design, rack=cls.racks[0], name="Feed A",
        )
        cls.parent_design.status = DesignStatusChoices.STATUS_APPROVED
        cls.parent_design.save()

        cls.child_design = Design.objects.create(
            title="Child", site=cls.site, based_on=cls.parent_design
        )

    def _pdu_add(self, design, **kwargs):
        return DesignPlacement(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=10,
            **kwargs,
        )

    def test_binding_own_designs_feed_is_valid(self):
        own_feed = DesignPowerFeed.objects.create(
            design=self.child_design, rack=self.racks[0], name="Feed B",
        )
        placement = self._pdu_add(self.child_design, planned_power_feed=own_feed)
        placement.full_clean()  # must not raise

    def test_binding_an_ancestors_feed_is_valid(self):
        placement = self._pdu_add(self.child_design, planned_power_feed=self.parent_feed)
        placement.full_clean()  # must not raise

    def test_binding_an_unrelated_designs_feed_is_rejected(self):
        unrelated = Design.objects.create(title="Unrelated", site=self.site)
        unrelated_feed = DesignPowerFeed.objects.create(
            design=unrelated, rack=self.racks[0], name="Feed U",
        )
        placement = self._pdu_add(self.child_design, planned_power_feed=unrelated_feed)
        with self.assertRaises(ValidationError) as ctx:
            placement.full_clean()
        self.assertIn("planned_power_feed", ctx.exception.message_dict)
        self.assertIn(str(unrelated), str(ctx.exception))

    def test_binding_a_descendants_feed_is_rejected(self):
        # A draft MIDDLE design must not bind to a feed planned by its OWN
        # (also draft) child -- from the middle design's point of view the
        # child hasn't happened yet. Uses a three-level chain because the
        # class-level parent is frozen (approved) and could never accept a
        # new placement at all, which would test the freeze guard instead.
        middle = Design.objects.create(
            title="Middle", site=self.site, based_on=self.parent_design
        )
        grandchild = Design.objects.create(
            title="Grandchild", site=self.site, based_on=middle
        )
        descendant_feed = DesignPowerFeed.objects.create(
            design=grandchild, rack=self.racks[0], name="Feed C",
        )
        placement = self._pdu_add(middle, planned_power_feed=descendant_feed)
        with self.assertRaises(ValidationError) as ctx:
            placement.full_clean()
        self.assertIn("planned_power_feed", ctx.exception.message_dict)

    def test_planned_power_feed_set_null_on_feed_delete(self):
        own_feed = DesignPowerFeed.objects.create(
            design=self.child_design, rack=self.racks[0], name="Feed D",
        )
        placement = DesignPlacement.objects.create(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=11,
            planned_power_feed=own_feed,
        )
        own_feed.delete()
        placement.refresh_from_db()
        self.assertIsNone(placement.planned_power_feed_id)


class DesignRackPowerChainTestCase(TestCase):
    """``DesignRackPower`` across a design chain (PLAN-design-chains.md G5
    item 2): a child inherits an approved ancestor's rack-power override, and
    may itself override any key -- the same shape as a child re-planning an
    inherited placement (§2.2: the ancestor's row cannot change underneath
    the child once it is approved, so live resolution is safe)."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]

    def _design(self, title, *, based_on=None):
        return Design.objects.create(title=title, site=self.site, based_on=based_on)

    def _approve(self, design):
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()
        return design

    def test_child_inherits_approved_ancestors_override(self):
        a = self._design("Network sweep IDS-1000")
        DesignRackPower.objects.create(
            design=a, rack=self.racks[0],
            power_config={"custom_fields": {"power_limitation": 8000}},
        )
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        merged, conflict = DesignRackPower.effective_custom_fields(b, self.racks[0])
        self.assertIsNone(conflict)
        self.assertEqual(merged, {"power_limitation": 8000})

    def test_child_override_wins_over_ancestor(self):
        a = self._design("Network sweep IDS-1000")
        DesignRackPower.objects.create(
            design=a, rack=self.racks[0],
            power_config={"custom_fields": {"power_limitation": 8000, "pdu_location": "top"}},
        )
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)
        DesignRackPower.objects.create(
            design=b, rack=self.racks[0],
            power_config={"custom_fields": {"power_limitation": 6000}},
        )

        merged, conflict = DesignRackPower.effective_custom_fields(b, self.racks[0])
        self.assertIsNone(conflict)
        # Child's own key overrides; the ancestor's other key still comes through.
        self.assertEqual(merged, {"power_limitation": 6000, "pdu_location": "top"})

    def test_non_approved_ancestor_contributes_nothing_but_own_override_still_applies(self):
        a = self._design("Network sweep IDS-1000")
        DesignRackPower.objects.create(
            design=a, rack=self.racks[0],
            power_config={"custom_fields": {"power_limitation": 8000}},
        )
        # `a` stays draft.
        b = self._design("Server build IDS-2000", based_on=a)
        DesignRackPower.objects.create(
            design=b, rack=self.racks[0],
            power_config={"custom_fields": {"pdu_location": "top"}},
        )

        merged, conflict = DesignRackPower.effective_custom_fields(b, self.racks[0])
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict["kind"], "ancestor_not_approved")
        self.assertEqual(merged, {"pdu_location": "top"})

    def test_unchained_design_reads_only_its_own_override(self):
        solo = self._design("Solo build IDS-9000")
        DesignRackPower.objects.create(
            design=solo, rack=self.racks[0],
            power_config={"custom_fields": {"power_limitation": 5000}},
        )
        merged, conflict = DesignRackPower.effective_custom_fields(solo, self.racks[0])
        self.assertIsNone(conflict)
        self.assertEqual(merged, {"power_limitation": 5000})


class BayPlacementTestCase(TestCase):
    """Placing a blade into a chassis bay (docs: device bays / blades).

    Two cases, both required: the chassis already exists in DCIM (``target_bay``
    -> a real dcim.DeviceBay), or the chassis is itself an 'add' in the same
    design (``parent_placement`` + ``target_bay_name``, validated against the
    parent type's DeviceBayTemplates because no bay rows exist yet).
    """

    @classmethod
    def setUpTestData(cls):
        from dcim.choices import SubdeviceRoleChoices
        from dcim.models import Device, DeviceBayTemplate

        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.role = env["device_role"]
        cls.plain_type = env["device_type"]
        manufacturer = cls.plain_type.manufacturer

        cls.chassis_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="Chassis-T", slug="chassis-t",
            u_height=2, subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        )
        DeviceBayTemplate.objects.create(device_type=cls.chassis_type, name="bay-a")
        DeviceBayTemplate.objects.create(device_type=cls.chassis_type, name="bay-b")
        cls.blade_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="Blade-T", slug="blade-t",
            u_height=0, subdevice_role=SubdeviceRoleChoices.ROLE_CHILD,
        )

        cls.design = Design.objects.create(title="Bay plan", site=cls.site)
        cls.chassis = Device.objects.create(
            name="Real-Chassis", site=cls.site, rack=cls.racks[0], position=40,
            face="front", device_type=cls.chassis_type, role=cls.role,
        )
        # Core instantiates the DeviceBays from the type's DeviceBayTemplates when
        # the device is created -- fetch them rather than creating duplicates.
        cls.free_bay = cls.chassis.devicebays.get(name="bay-a")
        cls.taken_bay = cls.chassis.devicebays.get(name="bay-b")
        cls.sitting_blade = Device.objects.create(
            name="Sitting-Blade", site=cls.site, rack=cls.racks[0], position=None,
            device_type=cls.blade_type, role=cls.role,
        )
        cls.taken_bay.installed_device = cls.sitting_blade
        cls.taken_bay.save()

    def _blade_add(self, **kwargs):
        defaults = {
            "design": self.design,
            "kind": DesignPlacementKindChoices.KIND_ADD,
            "device_type": self.blade_type,
            "target_rack": self.racks[0],
        }
        defaults.update(kwargs)
        return DesignPlacement(**defaults)

    # --- case A: real chassis -------------------------------------------------

    def test_blade_into_a_real_free_bay(self):
        p = self._blade_add(target_bay=self.free_bay, target_bay_name="bay-a")
        p.full_clean()
        p.save()
        self.assertEqual(p.target_bay, self.free_bay)

    def test_blade_into_an_occupied_bay_is_rejected(self):
        p = self._blade_add(target_bay=self.taken_bay)
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("target_bay", ctx.exception.message_dict)

    def test_occupied_bay_is_free_if_the_design_removes_its_occupant(self):
        """Same projected-world rule the rack slots use: an occupant this design
        removes has already vacated, so the bay is available."""
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.sitting_blade,
        )
        p = self._blade_add(target_bay=self.taken_bay)
        p.full_clean()

    def test_bay_target_forbids_a_rack_position(self):
        p = self._blade_add(target_bay=self.free_bay, target_position=5)
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("target_position", ctx.exception.message_dict)

    def test_non_child_device_type_cannot_go_in_a_bay(self):
        p = self._blade_add(device_type=self.plain_type, target_bay=self.free_bay)
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("target_bay", ctx.exception.message_dict)

    def test_one_design_cannot_claim_the_same_real_bay_twice(self):
        self._blade_add(target_bay=self.free_bay).save()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._blade_add(target_bay=self.free_bay).save()

    # --- case B: chassis planned in the same design ---------------------------

    def test_blade_into_a_planned_chassis(self):
        chassis_p = DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.chassis_type, target_rack=self.racks[0],
            target_position=20, target_face="front",
        )
        p = self._blade_add(parent_placement=chassis_p, target_bay_name="bay-a")
        p.full_clean()
        p.save()
        self.assertEqual(list(chassis_p.bay_children.all()), [p])

    def test_planned_bay_name_must_exist_on_the_parent_type(self):
        chassis_p = DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.chassis_type, target_rack=self.racks[0],
            target_position=20, target_face="front",
        )
        p = self._blade_add(parent_placement=chassis_p, target_bay_name="nope")
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("target_bay_name", ctx.exception.message_dict)

    def test_planned_parent_must_be_a_parent_device_type(self):
        not_chassis = DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.plain_type, target_rack=self.racks[0],
            target_position=22, target_face="front",
        )
        p = self._blade_add(parent_placement=not_chassis, target_bay_name="bay-a")
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("parent_placement", ctx.exception.message_dict)

    def test_real_bay_and_planned_parent_are_mutually_exclusive(self):
        chassis_p = DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.chassis_type, target_rack=self.racks[0],
            target_position=20, target_face="front",
        )
        p = self._blade_add(parent_placement=chassis_p, target_bay=self.free_bay)
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_edit_form_accepts_a_real_bay_and_mirrors_its_name(self):
        """The generic create/edit form must be able to place a blade: the chassis
        selector only scopes the bay picker, and target_bay_name is filled from
        the chosen bay so consumers have one field to read."""
        from ..forms import DesignPlacementForm

        form = DesignPlacementForm(data={
            "design": self.design.pk,
            "kind": DesignPlacementKindChoices.KIND_ADD,
            "device_type": self.blade_type.pk,
            "target_rack": self.racks[0].pk,
            "chassis": self.chassis.pk,
            "target_bay": self.free_bay.pk,
        })
        self.assertTrue(form.is_valid(), form.errors.as_json())
        placement = form.save()
        self.assertEqual(placement.target_bay, self.free_bay)
        self.assertEqual(placement.target_bay_name, "bay-a")

    def test_edit_form_rejects_an_occupied_bay(self):
        from ..forms import DesignPlacementForm

        form = DesignPlacementForm(data={
            "design": self.design.pk,
            "kind": DesignPlacementKindChoices.KIND_ADD,
            "device_type": self.blade_type.pk,
            "target_rack": self.racks[0].pk,
            "chassis": self.chassis.pk,
            "target_bay": self.taken_bay.pk,
        })
        self.assertFalse(form.is_valid())

    def test_bay_name_without_a_target_is_rejected(self):
        p = self._blade_add(target_bay_name="bay-a", target_position=7, target_face="front")
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("target_bay_name", ctx.exception.message_dict)


class BaseParentPlacementTestCase(TestCase):
    """``base_parent_placement`` (PLAN-design-chains.md G2, phase-3 gap): a child
    design may plan a blade into a chassis an ANCESTOR planned.

    The third and last way a placement names its parent, and the only one that
    crosses designs in the PARENT direction:

    * ``target_bay``            -- a real ``dcim.DeviceBay`` (chassis in DCIM);
    * ``parent_placement``      -- a chassis planned in THIS design;
    * ``base_parent_placement`` -- a chassis planned by an ANCESTOR design.

    Distinct from ``base_placement``, which also crosses designs but identifies
    the BLADE itself, not the chassis it goes into.
    """

    @classmethod
    def setUpTestData(cls):
        from dcim.choices import SubdeviceRoleChoices
        from dcim.models import Device, DeviceBayTemplate

        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.role = env["device_role"]
        cls.plain_type = env["device_type"]
        manufacturer = cls.plain_type.manufacturer

        cls.chassis_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="Chain-Chassis-M", slug="chain-chassis-m",
            u_height=2, subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        )
        DeviceBayTemplate.objects.create(device_type=cls.chassis_type, name="bay-a")
        DeviceBayTemplate.objects.create(device_type=cls.chassis_type, name="bay-b")
        cls.blade_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="Chain-Blade-M", slug="chain-blade-m",
            u_height=0, subdevice_role=SubdeviceRoleChoices.ROLE_CHILD,
        )
        # A real chassis, so the target_bay route is available for the
        # mutual-exclusivity cases.
        cls.real_chassis = Device.objects.create(
            name="Real-Chain-Chassis", site=cls.site, rack=cls.racks[0], position=40,
            face="front", device_type=cls.chassis_type, role=cls.role,
        )
        cls.real_bay = cls.real_chassis.devicebays.get(name="bay-a")

        # Ancestor: drafted, populated, THEN approved -- approval freezes it.
        cls.parent_design = Design.objects.create(title="Network parent", site=cls.site)
        cls.upstream_chassis = DesignPlacement.objects.create(
            design=cls.parent_design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.chassis_type,
            target_rack=cls.racks[0],
            target_position=10,
            target_face="front",
            proposed_name="upstream-chassis",
        )
        cls.upstream_plain = DesignPlacement.objects.create(
            design=cls.parent_design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.plain_type,
            target_rack=cls.racks[0],
            target_position=20,
            target_face="front",
            proposed_name="upstream-plain",
        )
        cls.upstream_move = DesignPlacement.objects.create(
            design=cls.parent_design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.real_chassis,
            target_rack=cls.racks[0],
            target_position=44,
            target_face="front",
        )
        cls.parent_design.status = DesignStatusChoices.STATUS_APPROVED
        cls.parent_design.save()

        cls.child_design = Design.objects.create(
            title="Server child", site=cls.site, based_on=cls.parent_design
        )

    def _blade_add(self, **kwargs):
        defaults = {
            "design": self.child_design,
            "kind": DesignPlacementKindChoices.KIND_ADD,
            "device_type": self.blade_type,
            "target_rack": self.racks[0],
        }
        defaults.update(kwargs)
        return DesignPlacement(**defaults)

    # --- the happy path -------------------------------------------------------

    def test_blade_into_an_ancestor_planned_chassis_is_valid(self):
        p = self._blade_add(
            base_parent_placement=self.upstream_chassis,
            target_bay_name="bay-a",
            proposed_name="child-blade",
        )
        p.full_clean()
        p.save()
        self.assertEqual(
            list(self.upstream_chassis.downstream_bay_children.all()), [p]
        )

    def test_move_of_an_inherited_blade_into_an_ancestor_planned_chassis(self):
        """The blade's identity (``base_placement``) and its new parent
        (``base_parent_placement``) are different questions, so a move may carry
        both."""
        upstream_blade = DesignPlacement.objects.create(
            design=self.parent_design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.blade_type,
            target_rack=self.racks[0],
            target_bay=self.real_bay,
            target_bay_name="bay-a",
            proposed_name="upstream-blade",
        )
        p = DesignPlacement(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=upstream_blade,
            base_parent_placement=self.upstream_chassis,
            target_rack=self.racks[0],
            target_bay_name="bay-b",
        )
        p.full_clean()
        p.save()

    # --- mutual exclusivity ---------------------------------------------------

    def test_base_parent_placement_and_target_bay_are_mutually_exclusive(self):
        p = self._blade_add(
            base_parent_placement=self.upstream_chassis,
            target_bay=self.real_bay,
            target_bay_name="bay-a",
        )
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_base_parent_placement_and_parent_placement_are_mutually_exclusive(self):
        own_chassis = DesignPlacement.objects.create(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.chassis_type,
            target_rack=self.racks[0],
            target_position=30,
            target_face="front",
        )
        p = self._blade_add(
            base_parent_placement=self.upstream_chassis,
            parent_placement=own_chassis,
            target_bay_name="bay-a",
        )
        with self.assertRaises(ValidationError):
            p.full_clean()

    # --- required bay name ----------------------------------------------------

    def test_base_parent_placement_requires_a_bay_name(self):
        p = self._blade_add(base_parent_placement=self.upstream_chassis)
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("target_bay_name", ctx.exception.message_dict)

    def test_base_parent_bay_name_must_exist_on_the_parent_type(self):
        p = self._blade_add(
            base_parent_placement=self.upstream_chassis, target_bay_name="nope"
        )
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("target_bay_name", ctx.exception.message_dict)

    # --- the parent must be a TRUE ancestor's add of a chassis type -----------

    def test_base_parent_placement_outside_the_ancestor_chain_is_rejected(self):
        unrelated = Design.objects.create(title="Unrelated", site=self.site)
        unrelated_chassis = DesignPlacement.objects.create(
            design=unrelated,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.chassis_type,
            target_rack=self.racks[0],
            target_position=50,
            target_face="front",
        )
        p = self._blade_add(
            base_parent_placement=unrelated_chassis, target_bay_name="bay-a"
        )
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("base_parent_placement", ctx.exception.message_dict)
        # The message must NAME the offending design, or the planner has no way
        # to tell which of several candidate designs is not in the chain.
        self.assertIn(
            str(unrelated), " ".join(ctx.exception.message_dict["base_parent_placement"])
        )

    def test_base_parent_placement_in_the_same_design_is_rejected(self):
        own_chassis = DesignPlacement.objects.create(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.chassis_type,
            target_rack=self.racks[0],
            target_position=32,
            target_face="front",
        )
        p = self._blade_add(
            base_parent_placement=own_chassis, target_bay_name="bay-a"
        )
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("base_parent_placement", ctx.exception.message_dict)

    def test_base_parent_placement_must_point_at_an_add(self):
        p = self._blade_add(
            base_parent_placement=self.upstream_move, target_bay_name="bay-a"
        )
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("base_parent_placement", ctx.exception.message_dict)

    def test_base_parent_placement_must_be_a_chassis_device_type(self):
        p = self._blade_add(
            base_parent_placement=self.upstream_plain, target_bay_name="bay-a"
        )
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("base_parent_placement", ctx.exception.message_dict)

    def test_blade_must_be_a_child_device_type(self):
        p = self._blade_add(
            device_type=self.plain_type,
            base_parent_placement=self.upstream_chassis,
            target_bay_name="bay-a",
        )
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_target_rack_must_be_the_planned_chassis_rack(self):
        p = self._blade_add(
            base_parent_placement=self.upstream_chassis,
            target_bay_name="bay-a",
            target_rack=self.racks[1],
        )
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("target_rack", ctx.exception.message_dict)

    def test_a_bay_placed_blade_takes_no_rack_position(self):
        p = self._blade_add(
            base_parent_placement=self.upstream_chassis,
            target_bay_name="bay-a",
            target_position=7,
            target_face="front",
        )
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("target_position", ctx.exception.message_dict)

    # --- uniqueness -----------------------------------------------------------

    def test_one_design_cannot_claim_the_same_inherited_bay_twice(self):
        """The analogue of ``unique_design_planned_bay`` for the cross-design
        route: the constraint exists so a design cannot CONTRADICT ITSELF about
        one bay. Nothing about that reasoning changes when the chassis lives in
        an ancestor, and ``base_parent_placement`` always points at the
        originating ``add`` (kind is enforced), so the triple is canonical."""
        DesignPlacement.objects.create(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.blade_type,
            target_rack=self.racks[0],
            base_parent_placement=self.upstream_chassis,
            target_bay_name="bay-a",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DesignPlacement.objects.create(
                    design=self.child_design,
                    kind=DesignPlacementKindChoices.KIND_ADD,
                    device_type=self.blade_type,
                    target_rack=self.racks[0],
                    base_parent_placement=self.upstream_chassis,
                    target_bay_name="bay-a",
                )

    def test_another_design_may_claim_the_same_inherited_bay(self):
        # Scoped to the design, exactly like the other two bay constraints: two
        # designs claiming one bay are competing proposals, not a contradiction.
        sibling = Design.objects.create(
            title="Sibling child", site=self.site, based_on=self.parent_design
        )
        for design in (self.child_design, sibling):
            DesignPlacement.objects.create(
                design=design,
                kind=DesignPlacementKindChoices.KIND_ADD,
                device_type=self.blade_type,
                target_rack=self.racks[0],
                base_parent_placement=self.upstream_chassis,
                target_bay_name="bay-a",
            )

    # --- staleness ------------------------------------------------------------

    def test_deleting_the_upstream_chassis_leaves_the_downstream_blade_flagged(self):
        """SET_NULL, never CASCADE (G2): cancelling the ancestor's chassis must
        not delete the child's blade -- it survives, inert and reportable, with
        the vanished chassis NAMED."""
        blade = DesignPlacement.objects.create(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.blade_type,
            target_rack=self.racks[0],
            base_parent_placement=self.upstream_chassis,
            target_bay_name="bay-a",
            proposed_name="orphan-blade",
        )
        upstream_name = self.upstream_chassis.proposed_name
        self.upstream_chassis.delete()

        blade.refresh_from_db()
        self.assertIsNone(blade.base_parent_placement_id)
        self.assertTrue(blade.stale)
        self.assertEqual(blade.stale_device_name, upstream_name)
        self.assertIn(blade, list(self.child_design.stale_placements))
        # And it stays SAVEABLE: rejecting a stale add here would hand back the
        # very data loss SET_NULL exists to prevent.
        blade.full_clean()

    def test_repointing_a_stale_bay_add_clears_the_flag(self):
        blade = DesignPlacement.objects.create(
            design=self.child_design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.blade_type,
            target_rack=self.racks[0],
            base_parent_placement=self.upstream_chassis,
            target_bay_name="bay-a",
        )
        self.upstream_chassis.delete()
        blade.refresh_from_db()
        self.assertTrue(blade.stale)

        blade.target_bay = self.real_bay
        blade.target_bay_name = "bay-a"
        blade.full_clean()
        self.assertFalse(blade.stale)
        self.assertEqual(blade.stale_device_name, "")

    def test_a_plain_add_still_cannot_be_stale(self):
        # The rule was "an add can never be stale" because an add referenced
        # nothing external. It now references one thing -- an ancestor's chassis
        # -- and ONLY the loss of that may flag it.
        p = self._blade_add(
            target_bay=self.real_bay, target_bay_name="bay-a", stale=True
        )
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("stale", ctx.exception.message_dict)


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

    # --- frozen design (§2.2/G4) ------------------------------------------------
    # DesignPowerFeed had NO clean() override at all, so a REST create/update
    # (or bulk import, or a shell script) on a planned feed belonging to an
    # approved design silently succeeded -- even though a planned feed sizes
    # its rack's capacity bar, so it changes what the approved plan claims
    # about power. `.objects.create()` bypasses clean() (Django doesn't call
    # it automatically), so these tests go through `full_clean()` directly,
    # exactly like the `DesignPlacement` frozen-design tests above.

    def test_create_rejected_on_approved_design(self):
        approved = Design.objects.create(
            title="Approved Feed Owner", site=self.site,
            status=DesignStatusChoices.STATUS_APPROVED,
        )
        feed = DesignPowerFeed(design=approved, rack=self.racks[0], name="Feed A")
        with self.assertRaises(ValidationError):
            feed.full_clean()

    def test_create_allowed_on_draft_design(self):
        feed = DesignPowerFeed(design=self.design, rack=self.racks[0], name="Feed A")
        feed.full_clean()  # must not raise

    def test_edit_rejected_once_design_is_approved(self):
        design = Design.objects.create(title="Flip me (feed)", site=self.site)
        feed = DesignPowerFeed.objects.create(design=design, rack=self.racks[0], name="Feed A")
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()
        feed.amperage = 32
        with self.assertRaises(ValidationError):
            feed.full_clean()

    def test_edit_allowed_after_design_returns_to_draft(self):
        design = Design.objects.create(title="Flip me back (feed)", site=self.site)
        feed = DesignPowerFeed.objects.create(design=design, rack=self.racks[0], name="Feed A")
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()
        design.status = DesignStatusChoices.STATUS_DRAFT
        design.save()
        feed.amperage = 32
        feed.full_clean()  # must not raise


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


class BaselineSlotValidationTestCase(TestCase):
    """Slot validation across a design chain (PLAN-design-chains.md G1).

    ``_validate_target_slot`` reuses ``Rack.get_available_units``, which knows
    only the REAL rack. In a chain that is not the world the child plans
    against: an ancestor's planned add occupies a U that is physically empty,
    and a real device the ancestor moved away still physically occupies the U it
    is leaving. Both must be honoured at validation time, or a collision the
    elevation draws is one the save path happily accepted.
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]  # Device 1 @ Rack1/U1, Device 2 @ Rack1/U2

        cls.parent = Design.objects.create(title="Parent", site=cls.site)
        cls.upstream_add = DesignPlacement.objects.create(
            design=cls.parent,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            target_rack=cls.racks[0],
            target_position=5,
            target_face="front",
            proposed_name="upstream-node",
        )
        # Device 1 leaves Rack 1 U1 entirely, for Rack 2 U9.
        DesignPlacement.objects.create(
            design=cls.parent,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.devices[0],
            target_rack=cls.racks[1],
            target_position=9,
            target_face="front",
        )
        cls.parent.status = DesignStatusChoices.STATUS_APPROVED
        cls.parent.save()

        cls.child = Design.objects.create(
            title="Child", site=cls.site, based_on=cls.parent
        )

    def _child_add(self, rack, position):
        return DesignPlacement(
            design=self.child,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=rack,
            target_position=position,
            target_face="front",
        )

    def test_add_onto_a_unit_an_ancestor_planned_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            self._child_add(self.racks[0], 5).full_clean()
        self.assertIn("target_position", ctx.exception.message_dict)
        self.assertIn(str(self.parent), str(ctx.exception))

    def test_add_onto_an_ancestors_move_target_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            self._child_add(self.racks[1], 9).full_clean()
        self.assertIn("target_position", ctx.exception.message_dict)

    def test_add_beside_the_baseline_is_accepted(self):
        self._child_add(self.racks[0], 6).full_clean()  # should not raise

    def test_add_onto_a_unit_an_ancestor_vacated_is_accepted(self):
        # U1 is physically occupied by Device 1, which the parent moves out.
        self._child_add(self.racks[0], 1).full_clean()  # should not raise

    def test_relocating_a_base_placement_identity_frees_its_upstream_unit(self):
        DesignPlacement.objects.create(
            design=self.child,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=self.upstream_add,
            target_rack=self.racks[0],
            target_position=7,
            target_face="front",
        )
        # The ancestor put it at U5; this design moved it to U7, so U5 is free.
        # (U7 is claimed by this design's OWN placement, which this validation
        # has never covered for any design, chained or not -- the save-layout
        # batch check owns intra-design collisions.)
        self._child_add(self.racks[0], 5).full_clean()  # should not raise

    def test_moving_a_base_placement_identity_onto_a_baseline_unit_is_rejected(self):
        move = DesignPlacement(
            design=self.child,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=self.upstream_add,
            target_rack=self.racks[1],
            target_position=9,  # where the ancestor parked Device 1
            target_face="front",
        )
        with self.assertRaises(ValidationError) as ctx:
            move.full_clean()
        self.assertIn("target_position", ctx.exception.message_dict)

    def test_a_design_with_no_parent_is_unaffected(self):
        standalone = Design.objects.create(title="Standalone", site=self.site)
        placement = DesignPlacement(
            design=standalone,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[0],
            target_position=5,  # the parent's planned U -- invisible here
            target_face="front",
        )
        placement.full_clean()  # should not raise
