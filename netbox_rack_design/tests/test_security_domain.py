"""
Security regression tests for the domain layer (1.2.2): models, apply,
distribution, projection, workorder, naming, planning_fields.

Each test drives the ATTACK (a re-parent out of an approved design, a malformed
JSON blob, an object-constrained user, markup in a name, a foreign-site
reference) and asserts the layer refuses or degrades -- never raises a bare 500
and never discloses another design's data.
"""

import contextlib
import copy
import csv
import io
import itertools
import signal
import uuid
from types import SimpleNamespace
from unittest import mock

from core.models import ObjectChange, ObjectType
from dcim.choices import DeviceStatusChoices
from dcim.models import Cable, Device, DeviceBay, Location, PowerFeed, PowerPanel, Rack, Site
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from netbox.context import current_request, events_queue
from users.models import ObjectPermission, User
from utilities.exceptions import PermissionsViolation
from utilities.testing import create_test_device

from .. import apply, distribution, models, naming, projection, workorder
from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..forms import DesignForm
from ..models import (
    Design,
    DesignGroup,
    DesignPlacement,
    DesignPowerFeed,
    DesignRackPower,
    DesignStep,
    PlannedRack,
)
from ..projection import project_rack
from .test_apply import ApplyTestCase
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


@contextlib.contextmanager
def _request_as(user):
    """Run the body as if inside a request by ``user``: NetBox's request context
    (what ``clean()`` reads to know who is looking, and what queues webhook
    events and changelog rows) is set exactly as its middleware sets it."""
    request = SimpleNamespace(id=uuid.uuid4(), user=user)
    token = current_request.set(request)
    events_queue.set({})
    try:
        yield request
    finally:
        current_request.reset(token)
        events_queue.set({})


@contextlib.contextmanager
def _deadline(seconds=5):
    """Turn a runaway loop into a test failure instead of a hung suite."""
    def _boom(signum, frame):
        raise AssertionError(f"still running after {seconds}s (unbounded loop)")
    previous = signal.signal(signal.SIGALRM, _boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


_viewer_serial = itertools.count(1)


def _viewer(title_prefix="Vis"):
    """A user who may view only designs whose title starts with ``title_prefix``."""
    user = User.objects.create_user(username=f"viewer-{next(_viewer_serial)}")
    _grant(user, Design, ["view"], {"title__startswith": title_prefix})
    return User.objects.get(pk=user.pk)


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

    def _reparent_error(self, row, target):
        row = type(row).objects.get(pk=row.pk)
        row.design = target
        with self.assertRaises(ValidationError) as cm:
            row.full_clean()
        # The re-parent guard keys its error on ``design`` (a frozen TARGET
        # would raise a non-field error instead, so this pins the right guard).
        self.assertIn("design", cm.exception.message_dict)

    def test_placement_cannot_leave_an_approved_design(self):
        frozen = self._design("Frozen")
        placement = self._add(frozen)
        self._approve(frozen)
        self._reparent_error(placement, self._design("Draft target"))

    def test_feed_cannot_leave_an_approved_design(self):
        frozen = self._design("Frozen")
        feed = DesignPowerFeed.objects.create(design=frozen, rack=self.racks[0], name="Feed A")
        self._approve(frozen)
        self._reparent_error(feed, self._design("Draft target"))

    def test_step_cannot_leave_an_approved_design(self):
        frozen = self._design("Frozen")
        step = DesignStep.objects.create(design=frozen, index=1)
        self._approve(frozen)
        self._reparent_error(step, self._design("Draft target"))

    def test_moving_between_draft_designs_still_works(self):
        a = self._design("Draft A")
        b = self._design("Draft B")
        placement = DesignPlacement.objects.get(pk=self._add(a).pk)
        placement.design = b
        placement.full_clean()  # must not raise

    def test_feed_moving_between_draft_designs_still_works(self):
        a = self._design("Draft A")
        b = self._design("Draft B")
        feed = DesignPowerFeed.objects.create(design=a, rack=self.racks[0], name="Feed A")
        feed.design = b
        feed.full_clean()  # must not raise

    def test_step_moving_between_draft_designs_still_works(self):
        a = self._design("Draft A")
        b = self._design("Draft B")
        step = DesignStep.objects.create(design=a, index=1)
        step.design = b
        step.full_clean()  # must not raise


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


class UnsavedDesignIncomingScopeTest(DomainBase):
    """A REST create validates an UNSAVED design carrying its incoming M2M
    values: the site-dependent checks run on those values alone."""

    def _unsaved(self, **m2m):
        design = Design(title="New")
        design._m2m_values = {"sites": [self.site], **m2m}
        return design

    def test_foreign_rack_planned_rack_and_parent_are_each_refused(self):
        far = Location.objects.create(name="Far", slug="far", site=self.other_site)
        foreign_planned = PlannedRack.objects.create(name="PR-far", u_height=42, location=far)
        far_parent = make_design(title="Far parent", site=self.other_site, status=DesignStatusChoices.STATUS_APPROVED)
        cases = (
            ("racks", {"racks": [self.other_rack]}, {}),
            ("planned_racks", {"planned_racks": [foreign_planned]}, {}),
            ("based_on", {}, {"based_on": far_parent}),
        )
        for field, m2m, attrs in cases:
            with self.subTest(field=field):
                design = self._unsaved(**m2m)
                for key, value in attrs.items():
                    setattr(design, key, value)
                with self.assertRaises(ValidationError) as cm:
                    design.full_clean()
                self.assertIn(field, cm.exception.message_dict)

    def test_without_incoming_values_an_unsaved_design_is_not_judged(self):
        Design(title="Plain").full_clean()  # no pk and no REST side channel: nothing to read

    def test_in_site_values_pass(self):
        near = Location.objects.create(name="Near", slug="near", site=self.site)
        planned = PlannedRack.objects.create(name="PR-near", u_height=42, location=near)
        self._unsaved(racks=[self.racks[0]], planned_racks=[planned]).full_clean()


class DesignFormScopeTest(DomainBase):
    """The HTML edit form: ``depends_on`` is cycle-checked on the submitted
    values (the model cannot see an m2m the form has not applied yet), and
    narrowing the sites may not strand a planned rack outside them."""

    def _data(self, design, **extra):
        return {"title": design.title, "sites": [self.site.pk],
                "status": DesignStatusChoices.STATUS_DRAFT, **extra}

    def test_a_depends_on_cycle_is_a_form_error(self):
        a = self._design("A")
        b = self._design("B")
        a.depends_on.add(b)
        form = DesignForm(data=self._data(b, depends_on=[a.pk]), instance=b)
        self.assertFalse(form.is_valid())
        self.assertIn("depends_on", form.errors)
        self.assertIn("Cycle", str(form.errors["depends_on"]))

    def test_a_depends_on_chain_without_a_cycle_is_fine(self):
        a = self._design("A")
        b = self._design("B")
        b.depends_on.add(a)
        c = self._design("C")
        form = DesignForm(data=self._data(c, depends_on=[b.pk]), instance=c)
        self.assertTrue(form.is_valid(), form.errors)

    def test_a_planned_rack_left_outside_the_sites_is_a_form_error(self):
        design = self._design("Scoped")
        design.sites.add(self.other_site)
        far = Location.objects.create(name="Far", slug="far", site=self.other_site)
        design.planned_racks.add(PlannedRack.objects.create(name="PR-far", u_height=42, location=far))
        form = DesignForm(data=self._data(design), instance=design)  # drops other_site
        self.assertFalse(form.is_valid())
        self.assertIn("sites", form.errors)
        self.assertIn("PR-far", str(form.errors["sites"]))

    def test_keeping_the_planned_racks_site_is_fine(self):
        design = self._design("Scoped")
        near = Location.objects.create(name="Near", slug="near", site=self.site)
        design.planned_racks.add(PlannedRack.objects.create(name="PR-near", u_height=42, location=near))
        form = DesignForm(data=self._data(design), instance=design)
        self.assertTrue(form.is_valid(), form.errors)


class DependsOnWalkBoundTest(DomainBase):

    def test_a_chain_deeper_than_the_lineage_cap_is_refused_not_walked_forever(self):
        depth = models.MAX_LINEAGE_DEPTH + 5
        chain = [Design.objects.create(title=f"D{i}") for i in range(depth)]
        through = Design.depends_on.through
        through.objects.bulk_create(
            through(from_design_id=chain[i].pk, to_design_id=chain[i + 1].pk) for i in range(depth - 1))
        head = chain[0]
        head.sites.add(self.site)
        head._m2m_values = {"depends_on": [chain[1]]}
        with _deadline(20), CaptureQueriesContext(connection) as ctx, self.assertRaises(ValidationError) as cm:
            head.full_clean()
        self.assertIn("depends_on", cm.exception.message_dict)
        self.assertLessEqual(len(ctx.captured_queries), models.MAX_LINEAGE_DEPTH + 20)


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

    def _prechange(self, obj):
        return list(
            ObjectChange.objects.filter(
                changed_object_type=ObjectType.objects.get_for_model(obj),
                changed_object_id=obj.pk, action="update",
            ).values_list("prechange_data", flat=True)
        )

    def test_existing_device_is_snapshotted_before_it_is_changed(self):
        design = self._design("Snapshot plan")
        self._remove(design, self.devices[1])
        self._approve(design)
        original = Device.snapshot
        with _request_as(self.superuser):
            with mock.patch.object(Device, "snapshot", autospec=True, side_effect=original) as snap:
                result = apply.run(design, self.superuser)
            changes = self._prechange(self.devices[1])
        self.assertTrue(result.ok, result.problems)
        self.assertIn(self.devices[1].pk, [call.args[0].pk for call in snap.call_args_list])
        # ... and the changelog row really carries the BEFORE state.
        self.assertEqual(len(changes), 1, changes)
        self.assertEqual(changes[0]["status"], DeviceStatusChoices.STATUS_ACTIVE)

    def test_planned_rack_is_snapshotted_before_realization(self):
        design, planned = self._planned_design("Snap rack")
        original = PlannedRack.snapshot
        with _request_as(self.superuser):
            with mock.patch.object(PlannedRack, "snapshot", autospec=True, side_effect=original) as snap:
                result = apply.run(design, self.superuser)
            changes = self._prechange(planned)
        self.assertTrue(result.ok, result.problems)
        self.assertIn(planned.pk, [call.args[0].pk for call in snap.call_args_list])
        self.assertEqual(len(changes), 1, changes)
        self.assertIsNone(changes[0]["realized_rack"])

    def test_adopted_real_rack_is_not_rewritten_but_the_planned_rack_is_snapshotted(self):
        design, planned = self._planned_design("Adopt rack")
        existing = Rack.objects.create(
            name=planned.name, location=planned.location, site=self.site, u_height=42)
        with _request_as(self.superuser):
            result = apply.run(design, self.superuser)
            changes = self._prechange(planned)
        self.assertTrue(result.ok, result.problems)
        planned.refresh_from_db()
        self.assertEqual(planned.realized_rack_id, existing.pk)  # adopted, not created
        self.assertEqual(Rack.objects.filter(name=planned.name).count(), 1)
        self.assertEqual(len(changes), 1, changes)
        self.assertIsNone(changes[0]["realized_rack"])

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


class RefsAreJudgedWhenTheyChangeTest(DomainBase):
    """NetBox may move a device, rack or feed to another site long after a
    design referenced it. That must not make the design's own row unsaveable (or
    unversionable); only a reference that is NEW or CHANGED is checked."""

    def _move(self, design):
        return DesignPlacement.objects.create(
            design=design, kind=MOVE, device=self.devices[0], target_rack=self.racks[1],
            target_position=30, target_face="front")

    def _relocate_device(self):
        Device.objects.filter(pk=self.devices[0].pk).update(site=self.other_site)

    def test_a_placement_whose_device_moved_sites_still_validates(self):
        placement = self._move(self._design())
        self._relocate_device()
        placement = DesignPlacement.objects.get(pk=placement.pk)
        placement.target_position = 31
        placement.full_clean()

    def test_changing_the_device_to_a_foreign_one_is_still_refused(self):
        placement = self._move(self._design())
        placement.device = create_test_device("foreign-chg", site=self.other_site)
        with self.assertRaises(ValidationError) as cm:
            placement.full_clean()
        self.assertIn("device", cm.exception.message_dict)

    def test_a_new_foreign_ref_on_an_existing_row_is_refused(self):
        placement = self._add(self._design(), 20)
        placement.power_source_device = create_test_device("foreign-pdu-2", site=self.other_site)
        with self.assertRaises(ValidationError) as cm:
            placement.full_clean()
        self.assertIn("power_source_device", cm.exception.message_dict)

    def test_moving_a_placement_into_another_design_rechecks_every_ref(self):
        placement = self._move(self._design("One"))
        self._relocate_device()
        other = make_design(title="Elsewhere", site=self.site)
        placement = DesignPlacement.objects.get(pk=placement.pk)
        placement.design = other
        with self.assertRaises(ValidationError) as cm:
            placement.full_clean()
        self.assertIn("device", cm.exception.message_dict)

    def test_a_new_version_copies_rows_whose_references_moved_away(self):
        from ..versioning import new_version

        design = self._design("Versioned")
        self._move(design)
        DesignPowerFeed.objects.create(design=design, rack=self.racks[0], name="Feed-A")
        DesignRackPower.objects.create(design=design, rack=self.racks[0], power_config={})
        self._relocate_device()
        Rack.objects.filter(pk=self.racks[0].pk).update(site=self.other_site)
        clone = new_version(Design.objects.get(pk=design.pk))
        self.assertEqual(clone.placements.count(), 1)
        self.assertEqual(clone.planned_feeds.count(), 1)
        self.assertEqual(clone.rack_power.count(), 1)

    def test_a_feed_whose_rack_moved_sites_still_validates_but_a_new_rack_is_refused(self):
        feed = DesignPowerFeed.objects.create(design=self._design(), rack=self.racks[0], name="Feed-B")
        Rack.objects.filter(pk=self.racks[0].pk).update(site=self.other_site)
        feed = DesignPowerFeed.objects.get(pk=feed.pk)
        feed.voltage = 230
        feed.full_clean()
        feed.rack = self.other_rack
        with self.assertRaises(ValidationError) as cm:
            feed.full_clean()
        self.assertIn("rack", cm.exception.message_dict)

    def test_rack_power_follows_the_same_rule(self):
        row = DesignRackPower.objects.create(design=self._design(), rack=self.racks[0], power_config={})
        Rack.objects.filter(pk=self.racks[0].pk).update(site=self.other_site)
        row = DesignRackPower.objects.get(pk=row.pk)
        row.full_clean()
        row.rack = Rack.objects.get(pk=self.other_rack.pk)
        with self.assertRaises(ValidationError) as cm:
            row.full_clean()
        self.assertIn("rack", cm.exception.message_dict)


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


# --- 8. chain refusals name the ancestor, like core's breadcrumbs ---------------------------------

class ChainRefusalNamesAncestorsTest(DomainBase):

    def _lineage(self, grand_title, parent_title):
        grand = self._design(grand_title)  # draft: refuses the chain
        parent = self._design(parent_title, based_on=grand)
        self._approve(parent)
        child = self._design("Vis child", based_on=parent)
        return grand, parent, child

    def test_the_refusal_names_a_hidden_ancestor(self):
        grand, _parent, child = self._lineage("Hidden grand", "Vis parent")
        _chain, refusal = projection.resolve_baseline_chain(child)
        self.assertEqual(refusal["source_design"], grand)
        self.assertEqual(refusal["kind"], "ancestor_not_approved")
        self.assertIn("Hidden grand", refusal["detail"])

    def test_apply_plan_names_a_hidden_ancestor(self):
        _grand, _parent, child = self._lineage("Hidden grand", "Vis parent")
        self._approve(child)
        user = _viewer()
        _grant(user, Device, ["add", "change"])
        result = apply.plan(child, user)
        self.assertTrue(any("Hidden grand" in p for p in result.problems), result.problems)

    def test_cycle_members_are_named(self):
        a = self._design("Hidden A")
        b = self._design("Vis B", based_on=a)
        c = self._design("Vis C", based_on=b)
        Design.objects.filter(pk=a.pk).update(based_on=c)  # a -> c -> b -> a
        c.refresh_from_db()
        _chain, refusal = projection.resolve_baseline_chain(c)
        self.assertEqual(refusal["kind"], "chain_broken")
        self.assertIn("Hidden A", refusal["detail"])


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


# --- 10. naming: a name collision counts every design ---------------------------------

class NamingCollisionTest(DomainBase):
    """Core's uniqueness errors count objects the reader cannot view
    ("Device with this name already exists"), and so does this check."""

    def test_a_device_name_counts(self):
        self.assertTrue(naming.name_exists_in_site("Device 1", self.site))

    def test_another_designs_planned_name_counts(self):
        design = self._design("Secret plan")
        self._add(design, 20, name="secret-name")
        placement = DesignPlacement.objects.get(design=design)
        self.assertTrue(naming.name_exists_in_site("secret-name", placement.site))


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


# --- 12. DesignGroup parent walk is bounded ---------------------------------------------------------------

class DesignGroupParentWalkTest(TestCase):

    def test_a_cycle_in_existing_rows_does_not_hang_clean(self):
        a = DesignGroup.objects.create(name="Group A")
        b = DesignGroup.objects.create(name="Group B", parent=a)
        DesignGroup.objects.filter(pk=a.pk).update(parent=b)  # a <-> b, bypassing clean()
        newcomer = DesignGroup(name="Group C", parent=DesignGroup.objects.get(pk=a.pk))
        with _deadline():
            with self.assertRaises(ValidationError) as cm:
                newcomer.full_clean()
        self.assertIn("parent", cm.exception.message_dict)

    def test_self_parenting_is_still_rejected(self):
        a = DesignGroup.objects.create(name="Group A")
        b = DesignGroup.objects.create(name="Group B", parent=a)
        a.parent = b
        with self.assertRaises(ValidationError) as cm:
            a.full_clean()
        self.assertIn("parent", cm.exception.message_dict)

    def test_an_absurdly_deep_chain_is_refused(self):
        parent = None
        for index in range(models.MAX_LINEAGE_DEPTH + 5):
            parent = DesignGroup.objects.create(name=f"Deep {index}", parent=parent)
        with _deadline(20):
            with self.assertRaises(ValidationError) as cm:
                DesignGroup(name="Deep leaf", parent=parent).full_clean()
        self.assertIn("parent", cm.exception.message_dict)

    def test_an_ordinary_nesting_is_fine(self):
        a = DesignGroup.objects.create(name="Group A")
        b = DesignGroup.objects.create(name="Group B", parent=a)
        DesignGroup(name="Group C", parent=b).full_clean()


# --- 13. clean() messages name the designs involved, as core's messages do --------------------------------------

class MessagesNameTheDesignsTest(DomainBase):
    """NetBox core words its validation errors with the names of the objects
    involved (``Rack X does not belong to site Y``), whoever is reading, and so
    do these: no message is redacted by who is asking. The checks still run."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from dcim.models import DeviceBayTemplate, DeviceType
        cls.chassis_type = DeviceType.objects.create(
            manufacturer=cls.device_type.manufacturer, model="Chassis-S", slug="chassis-s",
            u_height=2, subdevice_role="parent",
        )
        DeviceBayTemplate.objects.create(device_type=cls.chassis_type, name="bay1")
        cls.blade_type = DeviceType.objects.create(
            manufacturer=cls.device_type.manufacturer, model="Blade-S", slug="blade-s",
            u_height=0, subdevice_role="child",
        )

    def _texts(self, exc):
        return " | ".join(exc.messages)

    def _readers(self):
        """Every kind of reader: no request, a constrained user, a superuser."""
        return (
            contextlib.nullcontext(), _request_as(_viewer()),
            _request_as(User.objects.create_superuser(username=f"root-{User.objects.count()}")),
        )

    # -- the un-approve guard ---------------------------------------------

    def test_unapprove_guard_names_every_child(self):
        parent = self._design("Vis parent")
        self._approve(parent)
        for title in ("Vis child", "Hidden child one", "Hidden child two"):
            self._design(title, based_on=parent)
        for reader in self._readers():
            with reader:
                with self.assertRaises(ValidationError) as cm:
                    p = Design.objects.get(pk=parent.pk)
                    p.status = DesignStatusChoices.STATUS_DRAFT
                    p.full_clean()
            text = self._texts(cm.exception)
            for title in ("Vis child", "Hidden child one", "Hidden child two"):
                self.assertIn(title, text)

    # -- lineage cycles ----------------------------------------------------------

    def _cycle_through_a_hidden_design(self):
        """a -> (b based on hidden x based on a): setting ``a.based_on = b`` closes a loop."""
        a = self._design("Vis A")
        x = self._design("Hidden X", based_on=a)
        b = self._design("Vis B", based_on=x)
        a = Design.objects.get(pk=a.pk)
        a.based_on = b
        return a

    def test_based_on_cycle_message_names_the_whole_loop(self):
        self._cycle_through_a_hidden_design()
        for reader in self._readers():
            design = Design.objects.get(title="Vis A")
            design.based_on = Design.objects.get(title="Vis B")
            with reader:
                with self.assertRaises(ValidationError) as cm:
                    design.full_clean()
            text = self._texts(cm.exception)
            self.assertIn("Cycle", text)
            self.assertIn("Vis B", text)
            self.assertIn("Hidden X", text)

    # -- depends_on cycles -------------------------------------------------------------

    def test_depends_on_cycle_names_every_hop(self):
        a = self._design("Vis A")
        hop = self._design("Hidden hop")
        hop.depends_on.add(a)
        for reader in self._readers():
            a = Design.objects.get(pk=a.pk)
            a._m2m_values = {"depends_on": [hop]}  # the edge this write is about to add
            with reader:
                with self.assertRaises(ValidationError) as cm:
                    a.full_clean()
            self.assertIn("Hidden hop", self._texts(cm.exception))

    # -- placement references to other designs' rows --------------------------------------

    def _foreign_add(self):
        secret = self._design("Secret design")
        return secret, self._add(secret, 10, name="secret-srv")

    def _errors_naming(self, placement, title):
        for reader in self._readers():
            with reader:
                with self.assertRaises(ValidationError) as cm:
                    placement.full_clean()
            self.assertIn(title, str(cm.exception.message_dict))
        return cm.exception.message_dict

    def test_base_placement_of_a_foreign_design_is_refused_and_names_it(self):
        _secret, base = self._foreign_add()
        placement = DesignPlacement(
            design=self._design("Vis mine"), kind=MOVE, base_placement=base,
            target_rack=self.racks[0], target_position=20, target_face="front",
        )
        errors = self._errors_naming(placement, "Secret design")
        self.assertIn("base_placement", errors)

    def test_planned_power_feed_of_a_foreign_design_is_refused_and_names_it(self):
        secret = self._design("Secret design")
        feed = DesignPowerFeed.objects.create(design=secret, rack=self.racks[0], name="Secret feed")
        placement = DesignPlacement(
            design=self._design("Vis mine"), kind=ADD, device_type=self.device_type,
            target_rack=self.racks[0], target_position=20, target_face="front",
            proposed_name="pdu-q", device_role=self.device_role, planned_power_feed=feed,
        )
        errors = self._errors_naming(placement, "Secret design")
        self.assertIn("planned_power_feed", errors)

    def test_base_parent_placement_of_a_foreign_design_is_refused_and_names_it(self):
        secret = self._design("Secret design")
        chassis = self._add(secret, 30, name="secret-chassis", rack=self.racks[1])
        placement = DesignPlacement(
            design=self._design("Vis mine"), kind=ADD, device_type=self.blade_type,
            proposed_name="blade-q", device_role=self.device_role,
            base_parent_placement=chassis, target_bay_name="bay1",
        )
        errors = self._errors_naming(placement, "Secret design")
        self.assertIn("base_parent_placement", errors)

    def test_a_target_unit_claimed_upstream_names_the_ancestor(self):
        parent = self._design("Secret parent")
        self._add(parent, 10, name="up-srv")
        self._approve(parent)
        child = self._design("Vis child", based_on=parent)
        placement = DesignPlacement(
            design=child, kind=ADD, device_type=self.device_type,
            target_rack=self.racks[0], target_position=10, target_face="front",
            proposed_name="mine", device_role=self.device_role,
        )
        errors = self._errors_naming(placement, "Secret parent")
        self.assertIn("target_position", errors)

    def test_a_broken_lineage_in_a_placement_check_names_the_loop(self):
        a = self._design("Hidden A")
        b = self._design("Vis B", based_on=a)
        Design.objects.filter(pk=a.pk).update(based_on=b)  # a <-> b
        child = self._design("Vis C", based_on=b)
        child = Design.objects.get(pk=child.pk)  # fresh: the cached parents hide the loop
        _secret, base = self._foreign_add()
        placement = DesignPlacement(
            design=child, kind=MOVE, base_placement=base,
            target_rack=self.racks[0], target_position=20, target_face="front",
        )
        errors = self._errors_naming(placement, "Hidden A")
        self.assertIn("base_placement", errors)


# --- 14. real racks / panels / chassis are scoped to design.sites ------------------------------------------------

class SiteScopeOfPlannedPowerAndBaysTest(DomainBase):

    def test_planned_feed_on_a_foreign_site_rack_is_refused(self):
        feed = DesignPowerFeed(design=self._design(), rack=self.other_rack, name="Feed X")
        with self.assertRaises(ValidationError) as cm:
            feed.full_clean()
        self.assertIn("rack", cm.exception.message_dict)

    def test_planned_feed_on_a_foreign_site_power_panel_is_refused(self):
        panel = PowerPanel.objects.create(site=self.other_site, name="Foreign panel")
        feed = DesignPowerFeed(
            design=self._design(), rack=self.racks[0], name="Feed X", power_panel=panel)
        with self.assertRaises(ValidationError) as cm:
            feed.full_clean()
        self.assertIn("power_panel", cm.exception.message_dict)

    def test_planned_feed_inside_the_design_sites_is_fine(self):
        panel = PowerPanel.objects.create(site=self.site, name="Own panel")
        DesignPowerFeed(
            design=self._design(), rack=self.racks[0], name="Feed X", power_panel=panel
        ).full_clean()

    def test_rack_power_on_a_foreign_site_rack_is_refused(self):
        row = DesignRackPower(design=self._design(), rack=self.other_rack, power_config={})
        with self.assertRaises(ValidationError) as cm:
            row.full_clean()
        self.assertIn("rack", cm.exception.message_dict)

    def test_rack_power_inside_the_design_sites_is_fine(self):
        DesignRackPower(
            design=self._design(), rack=self.racks[0], power_config={}).full_clean()

    # -- the chassis a blade goes into (R4) --------------------------------------------

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from dcim.models import DeviceBayTemplate, DeviceType
        cls.chassis_type = DeviceType.objects.create(
            manufacturer=cls.device_type.manufacturer, model="Chassis-R", slug="chassis-r",
            u_height=2, subdevice_role="parent",
        )
        DeviceBayTemplate.objects.create(device_type=cls.chassis_type, name="bay1")
        cls.blade_type = DeviceType.objects.create(
            manufacturer=cls.device_type.manufacturer, model="Blade-R", slug="blade-r",
            u_height=0, subdevice_role="child",
        )

    def _blade_into(self, design, chassis):
        return DesignPlacement(
            design=design, kind=ADD, device_type=self.blade_type, proposed_name="blade-r",
            device_role=self.device_role, target_bay=chassis.devicebays.get(name="bay1"),
            target_bay_name="bay1",
        )

    def test_a_rackless_chassis_in_a_foreign_site_is_refused(self):
        chassis = Device.objects.create(
            name="rackless-chassis", device_type=self.chassis_type, role=self.device_role,
            site=self.other_site)
        with self.assertRaises(ValidationError) as cm:
            self._blade_into(self._design(), chassis).full_clean()
        self.assertIn("target_bay", cm.exception.message_dict)

    def test_a_rackless_chassis_in_the_design_site_is_fine(self):
        chassis = Device.objects.create(
            name="rackless-chassis", device_type=self.chassis_type, role=self.device_role,
            site=self.site)
        self._blade_into(self._design(), chassis).full_clean()

    def test_a_racked_chassis_in_a_foreign_site_is_still_refused(self):
        chassis = Device.objects.create(
            name="racked-chassis", device_type=self.chassis_type, role=self.device_role,
            site=self.other_site, rack=self.other_rack, position=5, face="front")
        with self.assertRaises(ValidationError) as cm:
            self._blade_into(self._design(), chassis).full_clean()
        self.assertIn("target_bay", cm.exception.message_dict)


# --- 15. apply: rollback discards queued events; post-save re-checks -----------------------------------------------

class ApplyRollbackEventsTest(ApplyTestCase):
    """Every rollback path of ``apply.run`` must also clear NetBox's queued
    webhook events, or the request's tail flushes events for rows that no longer
    exist."""

    def _design_with_one_add(self, title="Evented"):
        design = self._design(title)
        self._add(design, 10, name=f"srv-{title.lower()}")
        self._approve(design)
        return design

    def _constrained_user(self, name="evt-user"):
        user = User.objects.create_user(username=name)
        _grant(user, Device, ["add"], {"name": "never-matches"})
        return User.objects.get(pk=user.pk)

    def _fail_after_real_writes(self, exc):
        original = apply._execute

        def execute(design, user, result):
            original(design, user, result)  # the real writes queue real events
            raise exc
        return mock.patch.object(apply, "_execute", side_effect=execute)

    def test_control_a_successful_run_queues_events(self):
        design = self._design_with_one_add("Control")
        with _request_as(self.superuser):
            result = apply.run(design, self.superuser)
            queued = dict(events_queue.get())
        self.assertTrue(result.ok, result.problems)
        self.assertTrue(any(key.startswith("dcim.device:") for key in queued), queued)

    def test_permissions_violation_discards_queued_events(self):
        design = self._design_with_one_add("PermViol")
        user = self._constrained_user()
        with _request_as(user):
            with self.assertRaises(PermissionsViolation):
                apply.run(design, user)
            queued = dict(events_queue.get())
        self.assertEqual(queued, {})

    def test_integrity_error_discards_queued_events(self):
        design = self._design_with_one_add("Integrity")
        with _request_as(self.superuser):
            with self._fail_after_real_writes(IntegrityError("dup")):
                result = apply.run(design, self.superuser)
            queued = dict(events_queue.get())
        self.assertFalse(result.ok)
        self.assertEqual(queued, {})
        self.assertFalse(Device.objects.filter(name="srv-integrity").exists())

    def test_any_other_exception_discards_queued_events_and_propagates(self):
        design = self._design_with_one_add("Boom")
        with _request_as(self.superuser):
            with self._fail_after_real_writes(RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    apply.run(design, self.superuser)
            queued = dict(events_queue.get())
        self.assertEqual(queued, {})
        self.assertFalse(Device.objects.filter(name="srv-boom").exists())

    def _greenfield_design_with_feed(self):
        location = Location.objects.create(name="Loc DF6", slug="loc-df6", site=self.site)
        planned = PlannedRack.objects.create(name="PR DF6", location=location, u_height=20)
        panel = PowerPanel.objects.create(site=self.site, name="PP-df6")
        design = self._design("Greenfield DF6")
        DesignPlacement.objects.create(
            design=design, kind=ADD, device_type=self.device_type, target_planned_rack=planned,
            target_position=5, target_face="front", proposed_name="srv-df6",
            device_role=self.device_role)
        DesignPowerFeed.objects.create(
            design=design, planned_rack=planned, power_panel=panel, name="Feed DF6",
            voltage=230, amperage=16)
        return self._approve(design)

    def test_a_rolled_back_run_does_not_report_rows_that_no_longer_exist(self):
        for exc in (ValidationError("nope"), IntegrityError("dup")):
            with self.subTest(exc=type(exc).__name__):
                design = Design.objects.filter(title="Greenfield DF6").first() or self._greenfield_design_with_feed()
                with self._fail_after_real_writes(exc):
                    result = apply.run(design, self.superuser)
                self.assertFalse(result.ok)
                self.assertFalse(Rack.objects.filter(name="PR DF6").exists())
                self.assertFalse(PowerFeed.objects.filter(name="Feed DF6").exists())
                self.assertTrue(result.resolved_racks and result.feeds)
                self.assertTrue(all(e.rack is None for e in result.resolved_racks))
                self.assertTrue(all(e.feed is None and e.rack is None for e in result.feeds))
                self.assertTrue(all(e.device is None for e in result.created))

    def test_validation_error_discards_queued_events(self):
        design = self._design_with_one_add("Invalid")
        with _request_as(self.superuser):
            with self._fail_after_real_writes(ValidationError("nope")):
                result = apply.run(design, self.superuser)
            queued = dict(events_queue.get())
        self.assertFalse(result.ok)
        self.assertEqual(queued, {})
        self.assertFalse(Device.objects.filter(name="srv-invalid").exists())


class ApplyPostSaveRecheckTest(ApplyTestCase):
    """PowerFeed (add), Cable (add) and DeviceBay (change) rows apply writes are
    re-checked against the user's object constraints AFTER they exist, like the
    devices and racks already are."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from dcim.models import DeviceBayTemplate, DeviceType, PowerPortTemplate
        cls.panel = PowerPanel.objects.create(site=cls.site, name="PP-recheck")
        cls.pdu_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer, model="PDU-recheck", slug="pdu-recheck", u_height=0)
        PowerPortTemplate.objects.create(device_type=cls.pdu_type, name="input")
        cls.chassis_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer, model="Chassis-recheck", slug="chassis-recheck",
            u_height=2, subdevice_role="parent")
        for name in ("bay1", "bay2"):
            DeviceBayTemplate.objects.create(device_type=cls.chassis_type, name=name)
        cls.blade_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer, model="Blade-recheck", slug="blade-recheck",
            u_height=0, subdevice_role="child")

    def _user(self, name, *grants):
        user = User.objects.create_user(username=name)
        for model, actions, constraints in grants:
            _grant(user, model, actions, constraints)
        return User.objects.get(pk=user.pk)

    # -- feed + cable ------------------------------------------------------------

    def _feed_design(self, title):
        design = self._design(title)
        feed = DesignPowerFeed.objects.create(
            design=design, rack=self.racks[0], name=f"Feed {title}",
            power_panel=self.panel, voltage=230, amperage=16)
        DesignPlacement.objects.create(
            design=design, kind=ADD, device_type=self.pdu_type, target_rack=self.racks[0],
            target_position=None, target_face="", proposed_name=f"pdu-{title.lower()}",
            device_role=self.device_role, planned_power_feed=feed)
        self._approve(design)
        return design

    def test_control_unconstrained_user_applies_feed_and_cable(self):
        design = self._feed_design("Control")
        user = self._user(
            "recheck-ok", (Device, ["add"], None), (PowerFeed, ["add"], None), (Cable, ["add"], None))
        result = apply.run(design, user)
        self.assertTrue(result.ok, result.problems)
        self.assertTrue(PowerFeed.objects.filter(name="Feed Control").exists())

    def test_created_feed_outside_the_add_constraint_aborts_the_run(self):
        design = self._feed_design("Feedcon")
        user = self._user(
            "recheck-feed", (Device, ["add"], None), (Cable, ["add"], None),
            (PowerFeed, ["add"], {"name": "never-matches"}))
        with self.assertRaises(PermissionsViolation):
            apply.run(design, user)
        self.assertFalse(PowerFeed.objects.filter(name="Feed Feedcon").exists())
        self.assertFalse(Device.objects.filter(name="pdu-feedcon").exists())

    def test_created_cable_outside_the_add_constraint_aborts_the_run(self):
        design = self._feed_design("Cablecon")
        user = self._user(
            "recheck-cable", (Device, ["add"], None), (PowerFeed, ["add"], None),
            (Cable, ["add"], {"status": "connected"}))  # apply creates planned cables
        with self.assertRaises(PermissionsViolation):
            apply.run(design, user)
        self.assertFalse(PowerFeed.objects.filter(name="Feed Cablecon").exists())
        self.assertEqual(Cable.objects.count(), 0)

    # -- device bay ------------------------------------------------------------------

    def _real_chassis(self):
        chassis = Device.objects.create(
            name="recheck-chassis", device_type=self.chassis_type, role=self.device_role,
            site=self.site, rack=self.racks[0], position=30, face="front")
        return chassis, {bay.name: bay for bay in chassis.devicebays.all()}

    def _blade_design(self, title, bay):
        design = self._design(title)
        placement = DesignPlacement.objects.create(
            design=design, kind=ADD, device_type=self.blade_type, proposed_name=f"blade-{title.lower()}",
            device_role=self.device_role, target_bay=bay, target_bay_name=bay.name)
        self._approve(design)
        return design, placement

    def test_control_unconstrained_user_installs_a_blade(self):
        _chassis, bays = self._real_chassis()
        design, _pl = self._blade_design("Bayok", bays["bay1"])
        user = self._user("recheck-bay-ok", (Device, ["add"], None), (DeviceBay, ["change"], None))
        result = apply.run(design, user)
        self.assertTrue(result.ok, result.problems)

    def test_installing_into_a_bay_outside_the_change_constraint_aborts_the_run(self):
        _chassis, bays = self._real_chassis()
        design, _pl = self._blade_design("Baycon", bays["bay1"])
        user = self._user(
            "recheck-bay", (Device, ["add"], None),
            (DeviceBay, ["change"], {"name": "never-matches"}))
        with self.assertRaises(PermissionsViolation):
            apply.run(design, user)
        self.assertFalse(Device.objects.filter(name="blade-baycon").exists())
        bays["bay1"].refresh_from_db()
        self.assertIsNone(bays["bay1"].installed_device_id)

    def test_moving_a_blade_to_another_bay_outside_the_constraint_aborts_the_run(self):
        _chassis, bays = self._real_chassis()
        design, placement = self._blade_design("Baymove", bays["bay1"])
        self.assertTrue(apply.run(design, self.superuser).ok)
        DesignPlacement.objects.filter(pk=placement.pk).update(
            target_bay=bays["bay2"], target_bay_name="bay2")
        user = self._user(
            "recheck-bay-move", (Device, ["add", "change"], None),
            (DeviceBay, ["change"], {"name": "bay1"}))  # may touch bay1, not bay2
        with self.assertRaises(PermissionsViolation):
            apply.run(Design.objects.get(pk=design.pk), user)
        bays["bay2"].refresh_from_db()
        self.assertIsNone(bays["bay2"].installed_device_id)


    # -- the paths that CHANGE what exists: a drift correction, the bay a blade
    # leaves, a revert --------------------------------------------------------

    def test_a_correction_that_leaves_the_change_constraint_aborts_the_run(self):
        design = self._design("Drift")
        self._add(design, 10, name="drift-srv")
        self._approve(design)
        device = apply.run(design, self.superuser).created[0].device
        Device.objects.filter(pk=device.pk).update(name="hand-edited")
        user = self._user("drift-user", (Device, ["change"], {"name__startswith": "hand-"}))
        with self.assertRaises(PermissionsViolation):
            apply.run(design, user)
        device.refresh_from_db()
        self.assertEqual(device.name, "hand-edited")

    def test_the_corrected_device_passes_when_the_constraint_allows_it(self):
        design = self._design("Drift-ok")
        self._add(design, 10, name="drift-ok-srv")
        self._approve(design)
        device = apply.run(design, self.superuser).created[0].device
        Device.objects.filter(pk=device.pk).update(name="hand-edited-ok")
        user = self._user("drift-ok-user", (Device, ["change"], {"name__contains": "-"}))
        self.assertTrue(apply.run(design, user).ok)
        device.refresh_from_db()
        self.assertEqual(device.name, "drift-ok-srv")

    def test_vacating_a_bay_outside_the_change_constraint_aborts_the_run(self):
        _chassis, bays = self._real_chassis()
        design, placement = self._blade_design("Bayleave", bays["bay1"])
        self.assertTrue(apply.run(design, self.superuser).ok)
        DesignPlacement.objects.filter(pk=placement.pk).update(
            target_bay=bays["bay2"], target_bay_name="bay2")
        user = self._user(
            "bay-leave", (Device, ["add", "change"], None),
            (DeviceBay, ["change"], {"name": "bay2"}))  # may fill bay2, not empty bay1
        with self.assertRaises(PermissionsViolation):
            apply.run(Design.objects.get(pk=design.pk), user)
        for bay in bays.values():
            bay.refresh_from_db()
        self.assertIsNotNone(bays["bay1"].installed_device_id)
        self.assertIsNone(bays["bay2"].installed_device_id)

    def test_a_revert_that_leaves_the_change_constraint_aborts_the_run(self):
        design = self._design("Revert")
        placement = self._remove(design, self.devices[1])
        self._approve(design)
        self.assertTrue(apply.run(design, self.superuser).ok)
        self.devices[1].refresh_from_db()
        flagged = self.devices[1].status
        self.assertNotEqual(flagged, DeviceStatusChoices.STATUS_ACTIVE)
        placement.delete()  # cancels the removal: apply must restore the prior status
        user = self._user("revert-user", (Device, ["change"], {"status": flagged}))
        with self.assertRaises(PermissionsViolation):
            apply.run(Design.objects.get(pk=design.pk), user)
        self.devices[1].refresh_from_db()
        self.assertEqual(self.devices[1].status, flagged)


# --- 16. apply: messages about other designs ------------------------------------------------------------------------

class ApplyMessagesTest(ApplyTestCase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from dcim.models import DeviceBayTemplate, DeviceType
        cls.chassis_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer, model="Chassis-msg", slug="chassis-msg",
            u_height=2, subdevice_role="parent")
        DeviceBayTemplate.objects.create(device_type=cls.chassis_type, name="bay1")
        cls.blade_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer, model="Blade-msg", slug="blade-msg",
            u_height=0, subdevice_role="child")

    def _lineage(self, grand_title):
        """grand (approved, a chassis never applied) -> parent -> child (a blade into the chassis)."""
        grand = self._design(grand_title)
        chassis = self._add(grand, 30, name="grand-chassis", device_type=self.chassis_type)
        self._approve(grand)
        parent = self._design("Vis parent", based_on=grand)
        self._approve(parent)
        child = self._design("Vis child", based_on=parent)
        DesignPlacement.objects.create(
            design=child, kind=ADD, device_type=self.blade_type, proposed_name="blade-msg",
            device_role=self.device_role, base_parent_placement=chassis, target_bay_name="bay1")
        self._approve(child)
        return grand, parent, child

    def _grants(self, user):
        _grant(user, Device, ["add", "change"])
        return User.objects.get(pk=user.pk)

    def test_unapplied_ancestors_are_named_whoever_applies(self):
        # Like core's own messages: the title is shown, the reader's view
        # permission on that design does not change the sentence.
        _grand, _parent, child = self._lineage("Hidden grand")
        for user in (self._grants(_viewer()), User.objects.create_superuser(username="apply-root")):
            result = apply.plan(child, user)
            self.assertFalse(result.ok)
            joined = " | ".join(result.problems)
            self.assertIn("Hidden grand", joined)
            self.assertNotIn("(restricted design)", joined)

    def test_blade_into_an_unapplied_chassis_names_its_design(self):
        _grand, _parent, child = self._lineage("Hidden grand")
        problems = apply.plan(child, self._grants(_viewer())).problems
        blade = [p for p in problems if "blade-msg" in p]
        self.assertTrue(blade, problems)
        self.assertIn("Hidden grand", " ".join(blade))


# --- 17. projection: chain conflicts name their source design ----------------------------------------------------------

class AncestorConflictsNameTheSourceTest(DomainBase):

    def _refusal_for(self, status, grand_title="Hidden grand"):
        grand = self._design(grand_title)
        grand.status = status
        grand.save()
        parent = self._design("Vis parent", based_on=grand)
        self._approve(parent)
        child = self._design("Vis child", based_on=parent)
        _chain, refusal = projection.resolve_baseline_chain(child)
        return grand, refusal

    def test_a_refusal_keeps_the_real_kind_and_names_the_ancestor_whoever_reads_it(self):
        for status, kind in (
            (DesignStatusChoices.STATUS_IMPLEMENTED, "ancestor_implemented"),
            (DesignStatusChoices.STATUS_DRAFT, "ancestor_not_approved"),
        ):
            grand, refusal = self._refusal_for(status, f"Hidden grand {kind}")
            self.assertEqual(refusal["kind"], kind)
            self.assertEqual(refusal["source_design"], grand)
            self.assertIn(grand.title, refusal["detail"])
            self.assertNotIn("source_design_restricted", refusal)

    def _unit_conflict(self):
        grand = self._design("Hidden grand")
        self._add(grand, 10, name="up-srv")
        self._approve(grand)
        parent = self._design("Vis parent", based_on=grand)
        self._approve(parent)
        child = self._design("Vis child", based_on=parent)
        self._add(child, 10, name="srv-mine")
        result = project_rack(child, self.racks[0])
        rows = [c for c in result.conflicts if c["kind"] == "unit_occupied"]
        self.assertEqual(len(rows), 1, result.conflicts)
        return grand, result.conflicts

    def test_unit_occupied_names_its_source_design_and_id(self):
        grand, conflicts = self._unit_conflict()
        self.assertEqual(
            next(c for c in conflicts if c["kind"] == "unit_occupied")["source_design"], grand)
        flat = projection.flatten_conflicts(self.racks[0], conflicts)
        entry = next(e for e in flat if e["kind"] == "unit_occupied")
        self.assertEqual(entry["source_design_id"], grand.pk)
        self.assertEqual(entry["source_design_name"], str(grand))
