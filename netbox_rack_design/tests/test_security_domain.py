"""
Security regression tests for the domain layer (1.2.2): models, apply,
distribution, projection, workorder, naming, planning_fields.

Each test drives the ATTACK (a re-parent out of an approved design, a malformed
JSON blob, an object-constrained user, markup in a name, a foreign-site
reference) and asserts the layer refuses or degrades -- never raises a bare 500
and never discloses another design's data.
"""

import copy
import csv
import io
from unittest import mock

from core.models import ObjectType
from dcim.choices import DeviceStatusChoices
from dcim.models import Device, Location, PowerFeed, PowerPanel, Rack, Site
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from users.models import ObjectPermission, User
from utilities.exceptions import PermissionsViolation
from utilities.testing import create_test_device

from .. import apply, distribution, models, naming, planning_fields, projection, workorder
from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import (
    Design,
    DesignPlacement,
    DesignPowerFeed,
    DesignRackPower,
    DesignStep,
    PlannedRack,
)
from ..projection import project_rack
from .test_apply import ApplyTestCase
from .test_planning_fields_cf_types import _cfg as _cf_types_cfg
from .test_planning_fields_cf_types import _field as _cf_types_field
from .test_planning_fields_cf_types import _make_custom_fields
from .utils import create_dcim_environment, make_design

ADD = DesignPlacementKindChoices.KIND_ADD
MOVE = DesignPlacementKindChoices.KIND_MOVE
REMOVE = DesignPlacementKindChoices.KIND_REMOVE


def _grant(user, model, actions, constraints=None):
    """Give ``user`` an ObjectPermission on ``model`` (optionally constrained)."""
    permission = ObjectPermission(
        name=f"sec-{model.__name__}-{'-'.join(actions)}-{user.pk}",
        actions=list(actions), constraints=constraints,
    )
    permission.save()
    permission.users.add(user)
    permission.object_types.add(ObjectType.objects.get_for_model(model))
    return permission


def _without_peer_conflicts():
    cfg = copy.deepcopy(settings.PLUGINS_CONFIG)
    cfg.setdefault("netbox_rack_design", {})["peer_conflicts_enabled"] = False
    return cfg


class DomainBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]
        cls.device_type = env["device_type"]
        cls.device_role = env["device_role"]
        cls.other_site = Site.objects.create(name="Other site", slug="other-site")
        cls.other_rack = Rack.objects.create(name="Other rack", site=cls.other_site)

    def _design(self, title="Plan", **kw):
        return make_design(title=title, site=self.site, **kw)

    def _approve(self, design):
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()
        return design

    def _add(self, design, position=20, *, name="add-1", rack=None, **kw):
        return DesignPlacement.objects.create(
            design=design, kind=ADD, device_type=self.device_type,
            target_rack=rack or self.racks[0], target_position=position,
            target_face="front", proposed_name=name, device_role=self.device_role, **kw,
        )


# --- 1. re-parent guard -------------------------------------------------------

class ReparentOutOfApprovedTest(DomainBase):

    def test_placement_cannot_leave_an_approved_design(self):
        frozen = self._design("Frozen")
        placement = self._add(frozen)
        self._approve(frozen)
        draft = self._design("Draft target")
        placement = DesignPlacement.objects.get(pk=placement.pk)
        placement.design = draft
        with self.assertRaises(ValidationError):
            placement.full_clean()

    def test_feed_cannot_leave_an_approved_design(self):
        frozen = self._design("Frozen")
        feed = DesignPowerFeed.objects.create(design=frozen, rack=self.racks[0], name="Feed A")
        self._approve(frozen)
        draft = self._design("Draft target")
        feed = DesignPowerFeed.objects.get(pk=feed.pk)
        feed.design = draft
        with self.assertRaises(ValidationError):
            feed.full_clean()

    def test_step_cannot_leave_an_approved_design(self):
        frozen = self._design("Frozen")
        step = DesignStep.objects.create(design=frozen, index=1)
        self._approve(frozen)
        draft = self._design("Draft target")
        step = DesignStep.objects.get(pk=step.pk)
        step.design = draft
        with self.assertRaises(ValidationError):
            step.full_clean()

    def test_moving_between_draft_designs_still_works(self):
        a = self._design("Draft A")
        b = self._design("Draft B")
        placement = DesignPlacement.objects.get(pk=self._add(a).pk)
        placement.design = b
        placement.full_clean()  # must not raise


# --- 2. power_config shape ----------------------------------------------------

class PowerConfigShapeTest(DomainBase):

    def _placement(self, design, power_config):
        return DesignPlacement(
            design=design, kind=ADD, device_type=self.device_type,
            target_rack=self.racks[0], target_position=20, target_face="front",
            proposed_name="pdu-x", device_role=self.device_role, power_config=power_config,
        )

    def test_placement_rejects_non_dict_power_config(self):
        design = self._design()
        for bad in (["x"], "x", 7, [1, 2]):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self._placement(design, bad).full_clean()

    def test_placement_rejects_non_dict_custom_fields(self):
        design = self._design()
        for bad in ([1], "x", 3):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self._placement(design, {"custom_fields": bad}).full_clean()

    def test_placement_accepts_null_and_dict_shapes(self):
        design = self._design()
        for good in (None, {}, {"custom_fields": None}, {"custom_fields": {"a": 1}}):
            with self.subTest(good=good):
                self._placement(design, good).full_clean()

    def test_rack_power_rejects_bad_shapes(self):
        design = self._design()
        for bad in (["x"], {"custom_fields": [1]}, "x"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                DesignRackPower(design=design, rack=self.racks[0], power_config=bad).full_clean()

    def test_readers_tolerate_rows_that_bypassed_clean(self):
        design = self._design()
        placement = self._add(design)
        rack_power = DesignRackPower.objects.create(design=design, rack=self.racks[0])
        DesignPlacement.objects.filter(pk=placement.pk).update(power_config=["x"])
        DesignRackPower.objects.filter(pk=rack_power.pk).update(power_config=[1])
        placement.refresh_from_db()
        self.assertEqual(distribution._placement_custom_fields(placement), {})
        merged, _conflict = DesignRackPower.effective_custom_fields(design, self.racks[0])
        self.assertEqual(merged, {})
        DesignRackPower.objects.filter(pk=rack_power.pk).update(power_config={"custom_fields": [1]})
        merged, _conflict = DesignRackPower.effective_custom_fields(design, self.racks[0])
        self.assertEqual(merged, {})

    def test_engine_does_not_raise_on_a_malformed_blob(self):
        design = self._design()
        placement = self._add(design)
        DesignPlacement.objects.filter(pk=placement.pk).update(power_config=["x"])
        elevation = project_rack(design, self.racks[0])
        for mode in ("builtin", "script"):
            with self.subTest(mode=mode):
                _result, status = distribution.generate_distribution_status(elevation, mode=mode)
                self.assertIn(status["state"], ("ok", "empty", "failed"))

    def test_pre_engine_failure_degrades_to_failed_state(self):
        design = self._design()
        self._add(design)
        elevation = project_rack(design, self.racks[0])
        with mock.patch.object(
            distribution, "apply_rack_power_override", side_effect=RuntimeError("boom")
        ):
            result, status = distribution.generate_distribution_status(elevation, mode="script")
        self.assertIsNone(result)
        self.assertEqual(status["state"], "failed")
        self.assertIn("boom", status["detail"])


# --- 3. depends_on cycle guard / incoming racks ------------------------------------

class DependsOnGuardTest(DomainBase):

    def test_incoming_depends_on_cycle_is_rejected(self):
        a = self._design("A")
        b = self._design("B")
        a.depends_on.add(b)
        b._m2m_values = {"depends_on": [a]}  # what ValidatedModelSerializer stashes
        with self.assertRaises(ValidationError):
            b.full_clean()

    def test_incoming_depends_on_that_removes_the_edge_is_fine(self):
        a = self._design("A")
        b = self._design("B")
        a.depends_on.add(b)
        b.depends_on.add(a)  # persisted cycle ...
        b._m2m_values = {"depends_on": []}  # ... which this write removes
        b.full_clean()

    def test_diamond_ladder_is_linear_not_exponential(self):
        layers = 10
        below = []
        for level in range(layers):
            row = [self._design(f"L{level}-{i}") for i in range(2)]
            for node in row:
                node.depends_on.set(below)
            below = row
        top = self._design("Top")
        top.depends_on.set(below)
        with CaptureQueriesContext(connection) as ctx:
            top.full_clean()
        self.assertLess(len(ctx.captured_queries), 80, "walk must be O(V+E), not O(paths)")

    def test_incoming_racks_are_site_checked(self):
        design = self._design()
        design._m2m_values = {"racks": [self.other_rack]}
        with self.assertRaises(ValidationError) as cm:
            design.full_clean()
        self.assertIn("racks", cm.exception.message_dict)

    def test_incoming_racks_in_the_design_site_pass(self):
        design = self._design()
        design._m2m_values = {"racks": [self.racks[0]]}
        design.full_clean()


# --- 4. root / version lineage ------------------------------------------------------

class RootGuardTest(DomainBase):

    def test_root_cannot_be_self(self):
        design = self._design()
        design.root = design
        with self.assertRaises(ValidationError) as cm:
            design.full_clean()
        self.assertIn("root", cm.exception.message_dict)

    def test_root_cannot_itself_have_a_root(self):
        top = self._design("Top")
        mid = self._design("Mid", root=top, version=2)
        leaf = self._design("Leaf")
        leaf.root = mid
        with self.assertRaises(ValidationError) as cm:
            leaf.full_clean()
        self.assertIn("root", cm.exception.message_dict)

    def test_ordinary_version_chain_still_valid(self):
        top = self._design("Top")
        v2 = self._design("V2", root=top, version=2)
        v2.full_clean()


# --- 5. apply -------------------------------------------------------------------------

class ApplyHardeningTest(ApplyTestCase):

    def _user(self, name):
        return User.objects.create_user(username=name)

    # a. add_rack for a rack apply must create ---------------------------------

    def _planned_design(self, title="Greenfield"):
        location = Location.objects.create(name=f"Loc {title}", slug=f"loc-{title.lower()}", site=self.site)
        planned = PlannedRack.objects.create(name=f"PR {title}", location=location, u_height=20)
        design = make_design(title=title, site=self.site)
        DesignPlacement.objects.create(
            design=design, kind=ADD, device_type=self.device_type,
            target_planned_rack=planned, target_position=5, target_face="front",
            proposed_name=f"srv-{title.lower()}", device_role=self.device_role,
        )
        self._approve(design)
        return design, planned

    def test_plan_requires_add_rack_to_realize_a_planned_rack(self):
        user = self._user("no-add-rack")
        _grant(user, Device, ["add"])
        design, _planned = self._planned_design()
        result = apply.plan(design, user)
        self.assertFalse(result.ok)
        self.assertTrue(any("create racks" in p for p in result.problems), result.problems)

    def test_run_rechecks_add_rack_constraint_after_the_rack_exists(self):
        user = self._user("constrained-add-rack")
        _grant(user, Device, ["add"])
        _grant(user, Rack, ["add"], {"name": "never-matches"})
        design, _planned = self._planned_design()
        racks_before = Rack.objects.count()
        with self.assertRaises(PermissionsViolation):
            apply.run(design, user)
        self.assertEqual(Rack.objects.count(), racks_before)

    # b. realized_rack from another site is not trusted ------------------------

    def test_foreign_site_realized_rack_is_not_trusted(self):
        other_site = Site.objects.create(name="Victim site", slug="victim-site")
        victim = Rack.objects.create(name="Victim rack", site=other_site)
        design, planned = self._planned_design("Hijack")
        PlannedRack.objects.filter(pk=planned.pk).update(realized_rack=victim)
        planned.refresh_from_db()
        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)
        device = result.created[0].device
        self.assertNotEqual(device.rack_id, victim.pk)
        self.assertEqual(device.rack.site_id, self.site.pk)

    def test_planned_rack_clean_rejects_a_foreign_realized_rack(self):
        other_site = Site.objects.create(name="Victim site", slug="victim-site")
        victim = Rack.objects.create(name="Victim rack", site=other_site)
        _design, planned = self._planned_design("Hijack2")
        planned.realized_rack = victim
        with self.assertRaises(ValidationError) as cm:
            planned.full_clean()
        self.assertIn("realized_rack", cm.exception.message_dict)

    # c. post-save restrict re-check ---------------------------------------------

    def test_created_device_outside_the_add_constraint_aborts_the_run(self):
        user = self._user("constrained-add")
        _grant(user, Device, ["add"], {"name": "never-matches"})
        design = self._design("Add plan")
        self._add(design, 10, name="srv-new")
        self._approve(design)
        devices_before = Device.objects.count()
        with self.assertRaises(PermissionsViolation):
            apply.run(design, user)
        self.assertEqual(Device.objects.count(), devices_before)

    def test_removal_that_leaves_the_change_constraint_aborts_the_run(self):
        user = self._user("constrained-change")
        _grant(user, Device, ["change"], {"status": "active"})
        design = self._design("Remove plan")
        self._remove(design, self.devices[1])
        self._approve(design)
        with self.assertRaises(PermissionsViolation):
            apply.run(design, user)
        self.devices[1].refresh_from_db()
        self.assertEqual(self.devices[1].status, DeviceStatusChoices.STATUS_ACTIVE)

    # d. snapshot before mutating ---------------------------------------------------

    def test_existing_device_is_snapshotted_before_it_is_changed(self):
        design = self._design("Snapshot plan")
        self._remove(design, self.devices[1])
        self._approve(design)
        original = Device.snapshot
        with mock.patch.object(Device, "snapshot", autospec=True, side_effect=original) as snap:
            result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)
        self.assertIn(self.devices[1].pk, [call.args[0].pk for call in snap.call_args_list])

    def test_adopted_planned_rack_is_snapshotted_before_realization(self):
        design, planned = self._planned_design("Snap rack")
        original = PlannedRack.snapshot
        with mock.patch.object(PlannedRack, "snapshot", autospec=True, side_effect=original) as snap:
            result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)
        self.assertIn(planned.pk, [call.args[0].pk for call in snap.call_args_list])

    # e. chain move with no real device ------------------------------------------------

    def test_move_of_an_ancestor_planned_device_is_a_problem_not_a_500(self):
        parent = self._design("Parent")
        base = self._add(parent, 10, name="parent-srv")
        self._approve(parent)
        child = self._design("Child", based_on=parent)
        DesignPlacement.objects.create(
            design=child, kind=MOVE, base_placement=base,
            target_rack=self.racks[0], target_position=20, target_face="front",
        )
        self._approve(child)
        result = apply.plan(child, self.superuser)
        self.assertFalse(result.ok)
        self.assertTrue(any("planned by an ancestor" in p for p in result.problems), result.problems)

    # f. IntegrityError -> problem ----------------------------------------------------------

    def test_integrity_error_becomes_a_problem(self):
        design = self._design("Racy plan")
        self._add(design, 10, name="srv-racy")
        self._approve(design)
        with mock.patch.object(apply, "_execute", side_effect=IntegrityError("duplicate key")):
            result = apply.run(design, self.superuser)
        self.assertFalse(result.ok)
        self.assertTrue(any("nothing was applied" in p for p in result.problems), result.problems)



# --- 6. placement refs inside design.sites ---------------------------------------------------

class PlacementRefsWithinSitesTest(DomainBase):

    def _move_of(self, design, device):
        return DesignPlacement(
            design=design, kind=MOVE, device=device, target_rack=self.racks[1],
            target_position=30, target_face="front",
        )

    def test_move_of_a_foreign_site_device_is_rejected(self):
        foreign = create_test_device("foreign-1", site=self.other_site)
        with self.assertRaises(ValidationError) as cm:
            self._move_of(self._design(), foreign).full_clean()
        self.assertIn("device", cm.exception.message_dict)

    def test_remove_of_a_foreign_site_device_is_rejected(self):
        foreign = create_test_device("foreign-2", site=self.other_site)
        placement = DesignPlacement(design=self._design(), kind=REMOVE, device=foreign)
        with self.assertRaises(ValidationError) as cm:
            placement.full_clean()
        self.assertIn("device", cm.exception.message_dict)

    def test_move_of_an_in_site_device_is_fine(self):
        self._move_of(self._design(), self.devices[0]).full_clean()

    def test_foreign_power_source_device_is_rejected(self):
        foreign = create_test_device("foreign-pdu", site=self.other_site)
        placement = DesignPlacement(
            design=self._design(), kind=ADD, device_type=self.device_type,
            target_rack=self.racks[0], target_position=20, target_face="front",
            proposed_name="pdu-y", device_role=self.device_role, power_source_device=foreign,
        )
        with self.assertRaises(ValidationError) as cm:
            placement.full_clean()
        self.assertIn("power_source_device", cm.exception.message_dict)

    def test_foreign_real_power_feed_is_rejected(self):
        panel = PowerPanel.objects.create(site=self.other_site, name="Foreign panel")
        feed = PowerFeed.objects.create(power_panel=panel, name="Foreign feed")
        placement = DesignPlacement(
            design=self._design(), kind=ADD, device_type=self.device_type,
            target_rack=self.racks[0], target_position=20, target_face="front",
            proposed_name="pdu-z", device_role=self.device_role, real_power_feed=feed,
        )
        with self.assertRaises(ValidationError) as cm:
            placement.full_clean()
        self.assertIn("real_power_feed", cm.exception.message_dict)

    def test_in_site_real_power_feed_is_fine(self):
        panel = PowerPanel.objects.create(site=self.site, name="Panel")
        feed = PowerFeed.objects.create(power_panel=panel, name="Feed")
        DesignPlacement(
            design=self._design(), kind=ADD, device_type=self.device_type,
            target_rack=self.racks[0], target_position=20, target_face="front",
            proposed_name="pdu-w", device_role=self.device_role, real_power_feed=feed,
        ).full_clean()


# --- 7. peer disclosure -------------------------------------------------------------------------

class PeerDisclosureTest(DomainBase):

    def _scoped(self, title, rack=None):
        design = self._design(title)
        design.racks.add(rack or self.racks[1])
        return design

    def test_slot_claim_reports_our_unit_not_the_peers(self):
        tall = self.device_type.__class__.objects.create(
            manufacturer=self.device_type.manufacturer, model="Tall 2U", slug="tall-2u",
            u_height=2, is_full_depth=False,
        )
        peer = self._scoped("Peer Alpha")
        DesignPlacement.objects.create(
            design=peer, kind=ADD, device_type=tall, target_rack=self.racks[1],
            target_position=9, target_face="front", proposed_name="peer-srv",
            device_role=self.device_role,
        )
        ours = self._scoped("Ours Beta")
        self._add(ours, 10, name="our-srv", rack=self.racks[1])
        result = project_rack(ours, self.racks[1])
        claims = [c for c in result.conflicts if c["kind"] == "peer_slot_claim"]
        self.assertEqual(len(claims), 1, result.conflicts)
        self.assertIn("U10", claims[0]["detail"])
        self.assertNotIn("U9", claims[0]["detail"])

    def test_device_claim_does_not_disclose_the_peers_destination(self):
        peer = self._scoped("Peer Gamma")
        DesignPlacement.objects.create(
            design=peer, kind=MOVE, device=self.devices[0], target_rack=self.racks[1],
            target_position=33, target_face="front",
        )
        ours = self._scoped("Ours Delta")
        DesignPlacement.objects.create(
            design=ours, kind=MOVE, device=self.devices[0], target_rack=self.racks[1],
            target_position=12, target_face="front",
        )
        result = project_rack(ours, self.racks[1])
        claims = [c for c in result.conflicts if c["kind"] == "peer_device_claim"]
        self.assertEqual(len(claims), 1, result.conflicts)
        self.assertNotIn("U33", claims[0]["detail"])
        self.assertNotIn(str(self.racks[1]), claims[0]["detail"])

    def _applied_elsewhere(self):
        creator = self._design("Creator")
        self._add(creator, 10, name="new-srv", rack=self.racks[0])
        self._approve(creator)
        result = apply.run(creator, User.objects.create_superuser(username="peer-super"))
        self.assertTrue(result.ok, result.problems)
        return creator

    def test_reservation_is_reported_when_peer_conflicts_are_on(self):
        creator = self._applied_elsewhere()
        other = self._design("Other")
        slots = [s for s in project_rack(other, self.racks[0]).front
                 if s["u_position"] is not None and int(s["u_position"]) == 10]
        self.assertEqual(slots[0]["reserved_by_design_id"], creator.pk)

    def test_reservation_is_hidden_when_peer_conflicts_are_off(self):
        self._applied_elsewhere()
        other = self._design("Other")
        with override_settings(PLUGINS_CONFIG=_without_peer_conflicts()):
            slots = [s for s in project_rack(other, self.racks[0]).front
                     if s["u_position"] is not None and int(s["u_position"]) == 10]
        self.assertEqual(len(slots), 1)
        self.assertIsNone(slots[0]["reserved_by_design_id"])
        self.assertEqual(slots[0]["reserved_by_design_title"], "")


# --- 8. chain refusal redaction ----------------------------------------------------------------

class ChainRefusalRedactionTest(DomainBase):

    def _viewer(self, name="viewer"):
        user = User.objects.create_user(username=name)
        _grant(user, Design, ["view"], {"title__startswith": "Vis"})
        return user

    def _lineage(self, grand_title, parent_title):
        grand = self._design(grand_title)  # draft: refuses the chain
        parent = self._design(parent_title, based_on=grand)
        self._approve(parent)
        child = self._design("Vis child", based_on=parent)
        return grand, parent, child

    def test_default_call_is_unchanged(self):
        grand, _parent, child = self._lineage("Hidden grand", "Vis parent")
        _chain, refusal = projection.resolve_baseline_chain(child)
        self.assertEqual(refusal["source_design"], grand)
        self.assertIn("Hidden grand", refusal["detail"])

    def test_hidden_ancestor_is_redacted_for_a_restricted_user(self):
        _grand, _parent, child = self._lineage("Hidden grand", "Vis parent")
        _chain, refusal = projection.resolve_baseline_chain(child, user=self._viewer())
        self.assertIsNone(refusal["source_design"])
        self.assertTrue(refusal["source_design_restricted"])
        self.assertNotIn("Hidden grand", refusal["detail"])
        self.assertIn("(restricted design)", refusal["detail"])

    def test_visible_ancestor_keeps_its_title(self):
        grand, _parent, child = self._lineage("Vis grand", "Vis parent")
        _chain, refusal = projection.resolve_baseline_chain(child, user=self._viewer())
        self.assertEqual(refusal["source_design"], grand)
        self.assertIn("Vis grand", refusal["detail"])

    def test_direct_parent_keeps_its_title_even_when_hidden(self):
        # DIRECT links show the title (NetBox core parity); only lists are redacted.
        grand = self._design("Vis grand")
        self._approve(grand)
        parent = self._design("Hidden parent", based_on=grand)  # draft
        child = self._design("Vis child", based_on=parent)
        _chain, refusal = projection.resolve_baseline_chain(child, user=self._viewer())
        self.assertEqual(refusal["source_design"], parent)
        self.assertIn("Hidden parent", refusal["detail"])

    def test_redact_conflicts_helper_matches(self):
        _grand, _parent, child = self._lineage("Hidden grand", "Vis parent")
        _chain, refusal = projection.resolve_baseline_chain(child)
        redacted = projection.redact_conflicts([refusal], child, self._viewer())
        self.assertIsNone(redacted[0]["source_design"])
        self.assertNotIn("Hidden grand", redacted[0]["detail"])
        self.assertEqual(refusal["source_design"].title, "Hidden grand")  # input untouched

    def test_apply_plan_does_not_name_a_hidden_ancestor(self):
        _grand, _parent, child = self._lineage("Hidden grand", "Vis parent")
        self._approve(child)
        user = self._viewer()
        _grant(user, Device, ["add", "change"])
        result = apply.plan(child, user)
        self.assertFalse(any("Hidden grand" in p for p in result.problems), result.problems)

    def test_hidden_cycle_members_are_not_named(self):
        a = self._design("Hidden A")
        b = self._design("Vis B", based_on=a)
        c = self._design("Vis C", based_on=b)
        Design.objects.filter(pk=a.pk).update(based_on=c)  # a -> c -> b -> a
        c.refresh_from_db()
        _chain, refusal = projection.resolve_baseline_chain(c, user=self._viewer())
        self.assertEqual(refusal["kind"], "chain_broken")
        self.assertNotIn("Hidden A", refusal["detail"])


# --- 9. work order CSV -----------------------------------------------------------------------------

class WorkOrderCsvTest(DomainBase):

    @staticmethod
    def _act(name):
        return {
            "order": 1, "kind": "add", "device": {"name": name, "model": "=model"},
            "from": None, "to": {"rack": "@rack", "position": "5", "face": "front"},
            "power": None, "power_to": [], "status_after": "planned", "children": [],
        }

    def _rows(self, name, title="step"):
        data = {"steps": [{"index": 1, "title": title, "actions": [self._act(name)]}],
                "unscheduled": []}
        return list(csv.DictReader(io.StringIO(workorder.render_csv(data))))

    def test_formula_prefixes_are_neutralised(self):
        for evil in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"):
            with self.subTest(evil=evil):
                row = self._rows(evil, title=evil)[0]
                self.assertEqual(row["device"], "'" + evil)
                self.assertEqual(row["step_title"], "'" + evil)
        row = self._rows("srv")[0]
        self.assertEqual(row["to_rack"], "'@rack")
        self.assertEqual(row["type"], "'=model")

    def test_ordinary_cells_and_numbers_are_untouched(self):
        row = self._rows("srv-01")[0]
        self.assertEqual(row["device"], "srv-01")
        self.assertEqual(row["step"], "1")
        self.assertEqual(row["to_u"], "5")

    def test_target_face_must_be_a_real_face(self):
        design = self._design()
        placement = DesignPlacement(
            design=design, kind=ADD, device_type=self.device_type,
            target_rack=self.racks[0], target_position=20, target_face="=1+1",
            proposed_name="face-x", device_role=self.device_role,
        )
        with self.assertRaises(ValidationError) as cm:
            placement.full_clean()
        self.assertIn("target_face", cm.exception.message_dict)
        for face in ("front", "rear", ""):
            placement.target_face = face
            placement.full_clean()


# --- 10. naming + planning_fields: optional user -----------------------------------------------------

class NamingUserScopeTest(DomainBase):

    def test_name_exists_without_user_is_unchanged(self):
        self.assertTrue(naming.name_exists_in_site("Device 1", self.site))

    def test_hidden_device_name_is_not_an_oracle_for_a_restricted_user(self):
        user = User.objects.create_user(username="no-view")
        self.assertFalse(naming.name_exists_in_site("Device 1", self.site, user=user))
        _grant(user, Device, ["view"])
        user = User.objects.get(pk=user.pk)  # drop the cached permissions
        self.assertTrue(naming.name_exists_in_site("Device 1", self.site, user=user))

    def test_hidden_design_placement_name_is_not_an_oracle(self):
        design = self._design("Secret plan")
        self._add(design, 20, name="secret-name")
        placement = DesignPlacement.objects.get(design=design)
        user = User.objects.create_user(username="design-blind")
        self.assertTrue(naming.name_exists_in_site("secret-name", placement.site))
        self.assertFalse(naming.name_exists_in_site("secret-name", placement.site, user=user))
        _grant(user, Design, ["view"])
        user = User.objects.get(pk=user.pk)  # drop the cached permissions
        self.assertTrue(naming.name_exists_in_site("secret-name", placement.site, user=user))


@override_settings(PLUGINS_CONFIG=_cf_types_cfg())
class PlanningFieldsUserScopeTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        _make_custom_fields()
        cls.hidden_site = Site.objects.create(name="Hidden hub", slug="hidden-hub")

    def test_object_lookup_without_user_is_unchanged(self):
        field = _cf_types_field("k_object")
        self.assertEqual(planning_fields.coerce_value(field, self.hidden_site.pk), self.hidden_site.pk)

    def test_object_existence_is_not_an_oracle_for_a_restricted_user(self):
        field = _cf_types_field("k_object")
        user = User.objects.create_user(username="site-blind")
        with self.assertRaises(ValueError):
            planning_fields.coerce_value(field, self.hidden_site.pk, user=user)
        _grant(user, Site, ["view"])
        user = User.objects.get(pk=user.pk)  # drop the cached permissions
        self.assertEqual(
            planning_fields.coerce_value(field, self.hidden_site.pk, user=user), self.hidden_site.pk
        )

    def test_object_display_name_is_not_an_oracle_for_a_restricted_user(self):
        field = _cf_types_field("k_object")
        user = User.objects.create_user(username="site-blind-2")
        self.assertIn("Hidden hub", planning_fields.display_value(field, self.hidden_site.pk))
        shown = planning_fields.display_value(field, self.hidden_site.pk, user=user)
        self.assertNotIn("Hidden hub", shown)
        self.assertIn(f"#{self.hidden_site.pk}", shown)


# --- 11. descendant walk + depth cap ---------------------------------------------------------------------

class LineageBoundsTest(DomainBase):

    def test_descendant_walk_query_count_does_not_scale_with_children(self):
        root = self._design("Root")
        for index in range(25):
            self._design(f"Child {index}", based_on=root)
        with CaptureQueriesContext(connection) as ctx:
            excluded = projection._lineage_exclusion_ids(root)
        self.assertEqual(len(excluded), 26)
        self.assertLess(len(ctx.captured_queries), 8)

    def test_descendant_walk_still_covers_grandchildren(self):
        root = self._design("Root")
        child = self._design("Child", based_on=root)
        grand = self._design("Grand", based_on=child)
        self.assertEqual(projection._lineage_exclusion_ids(root), {root.pk, child.pk, grand.pk})

    def test_baseline_chain_depth_is_capped(self):
        a = self._design("A")
        b = self._design("B", based_on=a)
        c = self._design("C", based_on=b)
        with mock.patch.object(models, "MAX_LINEAGE_DEPTH", 1):
            with self.assertRaises(ValueError) as cm:
                c.baseline_chain()
            self.assertIn("deeper than", str(cm.exception))
            with self.assertRaises(ValidationError):
                c.full_clean()
        self.assertEqual(c.baseline_chain(), [a, b])
