"""Tests for the apply engine (``apply.py``): the plan()/run() pair that
materializes an approved design's placements as real ``dcim.Device`` rows.

Mirrors the fixture and helper style already established for chain behaviour
in ``test_projection.py`` (``_design``/``_approve``/``_add`` etc.): build the
placement layer while the design is still draft (an approved design's
placements are frozen -- ``DesignPlacement.clean()``), then flip ``status``
directly and ``save()`` to approve, bypassing ``full_clean()`` exactly the
way the real approve action does.
"""

from core.models import ObjectType
from dcim.choices import DeviceFaceChoices, DeviceStatusChoices
from dcim.models import Device
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from users.models import ObjectPermission, User

from .. import apply
from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignApply, DesignPlacement
from .utils import create_dcim_environment


class ApplyTestCase(TestCase):
    """Shared fixture + helpers for every test below."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]  # half-depth, u_height=1
        cls.device_role = env["device_role"]
        cls.tenant = env["tenant"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]  # Device 1 @ Rack1/U1/front, Device 2 @ Rack1/U2/front
        cls.manufacturer = env["manufacturer"]
        cls.superuser = User.objects.create_superuser(username="apply-super")

    def _design(self, title="Plan", *, based_on=None, site=None):
        return Design.objects.create(title=title, site=site or self.site, based_on=based_on)

    def _approve(self, design):
        """Approve a design AFTER its placements exist (see module docstring)."""
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()
        return design

    def _add(self, design, position, *, name="add-1", rack=None, device_type=None,
              face="front", role=None, tenant=None):
        return DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=device_type or self.device_type,
            target_rack=rack or self.racks[0],
            target_position=position,
            target_face=face,
            proposed_name=name,
            device_role=role,
            tenant=tenant,
        )

    def _move(self, design, device, position, *, name="", rack=None, face="front",
               role=None, tenant=None):
        return DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=device,
            target_rack=rack or self.racks[0],
            target_position=position,
            target_face=face,
            proposed_name=name,
            device_role=role,
            tenant=tenant,
        )

    def _remove(self, design, device):
        return DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=device,
        )


# --- preconditions -----------------------------------------------------------

class PreconditionTestCase(ApplyTestCase):

    def test_draft_design_reports_problem(self):
        design = self._design("Draft plan")
        self._add(design, 10)  # note: NOT approved
        result = apply.plan(design, self.superuser)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("not approved" in p for p in result.problems), result.problems,
        )

    def test_rejected_design_reports_problem(self):
        design = self._design("Rejected plan")
        self._add(design, 10)
        design.status = DesignStatusChoices.STATUS_REJECTED
        design.save()
        result = apply.plan(design, self.superuser)
        self.assertFalse(result.ok)
        self.assertTrue(any("rejected" in p for p in result.problems), result.problems)

    def test_ancestor_with_unapplied_placements_reports_problem(self):
        a = self._design("IDS-1000")
        self._add(a, 10, name="srv-a")
        self._approve(a)  # approved, but never applied

        b = self._design("IDS-2000", based_on=a)
        self._add(b, 11, name="srv-b")
        self._approve(b)

        result = apply.plan(b, self.superuser)
        self.assertFalse(result.ok)
        self.assertTrue(
            any(str(a) in p and "apply it first" in p for p in result.problems),
            result.problems,
        )

    def test_chain_broken_reports_problem(self):
        a = self._design("Still draft ancestor")
        self._add(a, 10, name="srv-a")  # a is never approved

        b = self._design("Child", based_on=a)
        self._add(b, 11, name="srv-b")
        self._approve(b)

        result = apply.plan(b, self.superuser)
        self.assertFalse(result.ok)
        self.assertTrue(any(str(a) in p for p in result.problems), result.problems)

    def test_missing_add_permission_reports_problem(self):
        design = self._design("Needs perm")
        self._add(design, 10, name="srv-noperm")
        self._approve(design)
        user = User.objects.create_user(username="no-perm-user")

        result = apply.plan(design, user)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("permission to create devices" in p and str(self.site) in p
                for p in result.problems),
            result.problems,
        )

    def test_change_permission_scoped_to_wrong_site_is_reported(self):
        # A user WITH change_device -- but constrained to a DIFFERENT site --
        # must be refused for a removal in THIS design's site. Proves the
        # per-object honouring: a blanket has_perm('dcim.change_device') check
        # alone would have missed this.
        other_site = self.site.__class__.objects.create(name="Other site", slug="other-site")
        user = User.objects.create_user(username="wrong-site-user")
        permission = ObjectPermission(
            name="constrained-change", actions=["change"],
            constraints={"site_id": other_site.pk},
        )
        permission.save()
        permission.users.add(user)
        permission.object_types.add(ObjectType.objects.get_for_model(Device))

        design = self._design("Removal needs perm")
        self._remove(design, self.devices[1])
        self._approve(design)

        result = apply.plan(design, user)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("permission to modify device" in p and self.devices[1].name in p
                for p in result.problems),
            result.problems,
        )

    def test_bay_placement_reports_problem_not_skipped(self):
        design = self._design("Has a blade")
        chassis = self._add(design, 20, name="chassis-1")
        blade = DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            parent_placement=chassis,
            target_bay_name="bay1",
            proposed_name="blade-1",
        )
        self._approve(design)

        result = apply.plan(design, self.superuser)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("blade-1" in p and "blade placements cannot be applied yet" in p
                for p in result.problems),
            result.problems,
        )
        # Never silently dropped from the plan: the OTHER (non-bay) create is
        # still reported normally alongside the problem.
        self.assertEqual({c.placement.pk for c in result.created}, {chassis.pk})
        self.assertNotIn(blade.pk, [c.placement.pk for c in result.created])

    def test_several_problems_reported_together(self):
        a = self._design("Unapproved ancestor for combo test")
        self._add(a, 10, name="srv-combo-a")  # never approved -> chain broken

        design = self._design("Combo", based_on=a)
        self._add(design, 11, name="srv-combo-b")
        chassis = self._add(design, 30, name="chassis-combo")
        DesignPlacement.objects.create(
            design=design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, parent_placement=chassis,
            target_bay_name="bay1", proposed_name="blade-combo",
        )
        design.status = DesignStatusChoices.STATUS_REJECTED
        design.save()

        result = apply.plan(design, self.superuser)
        self.assertFalse(result.ok)
        self.assertGreaterEqual(len(result.problems), 3, result.problems)
        joined = " | ".join(result.problems)
        self.assertIn("rejected", joined)
        self.assertIn(str(a), joined)
        self.assertIn("blade placements cannot be applied yet", joined)


# --- occupancy / naming pre-checks -------------------------------------------

class ConflictTestCase(ApplyTestCase):

    def test_occupancy_conflict_reports_problem_and_blocks_only_that_placement(self):
        design = self._design("Occupancy clash")
        blocked = self._add(design, 1, name="new-at-u1")  # U1/front is Device 1's slot
        clear = self._add(design, 15, name="new-at-u15")
        self._approve(design)

        result = apply.plan(design, self.superuser)
        self.assertFalse(result.ok)
        self.assertTrue(
            any("occupied by" in p and self.devices[0].name in p for p in result.problems),
            result.problems,
        )
        created_pks = {c.placement.pk for c in result.created}
        self.assertEqual(created_pks, {clear.pk})
        self.assertNotIn(blocked.pk, created_pks)

    def test_name_conflict_reports_problem(self):
        design = self._design("Name clash")
        self._add(design, 12, name=self.devices[0].name)  # already taken in the site
        self._approve(design)

        result = apply.plan(design, self.superuser)
        self.assertFalse(result.ok)
        self.assertTrue(
            any(self.devices[0].name in p and "already used by another device" in p
                for p in result.problems),
            result.problems,
        )
        self.assertEqual(result.created, [])


# --- add / move / remove device creation -------------------------------------

class ExecutionTestCase(ApplyTestCase):

    def test_add_creates_device_with_role_tenant_slot_status(self):
        design = self._design("Add one")
        self._add(design, 10, name="new-srv", role=self.device_role, tenant=self.tenant)
        self._approve(design)

        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(len(result.created), 1)
        device = result.created[0].device
        self.assertIsNotNone(device)
        self.assertEqual(device.name, "new-srv")
        self.assertEqual(device.device_type, self.device_type)
        self.assertEqual(device.role, self.device_role)
        self.assertEqual(device.tenant, self.tenant)
        self.assertEqual(device.rack, self.racks[0])
        self.assertEqual(device.position, 10)
        self.assertEqual(device.face, DeviceFaceChoices.FACE_FRONT)
        self.assertEqual(device.status, "planned")
        self.assertEqual(DesignApply.objects.filter(design=design, device=device).count(), 1)

    def test_move_keep_name_creates_decorated_planned_device(self):
        design = self._design("Move keep name")
        source = self.devices[0]
        self._move(design, source, 10)  # no proposed_name -> keep-name move
        self._approve(design)

        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)
        device = result.created[0].device
        self.assertEqual(device.name, f"{design.title}-{source.name}")
        self.assertEqual(device.device_type, source.device_type)

        # The real source device is left completely untouched.
        source.refresh_from_db()
        self.assertEqual(source.status, "active")
        self.assertEqual(source.rack, self.racks[0])
        self.assertEqual(source.position, 1)

    def test_move_rename_creates_device_with_proposed_name(self):
        design = self._design("Move rename")
        source = self.devices[1]
        self._move(design, source, 12, name="renamed-srv")
        self._approve(design)

        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(result.created[0].device.name, "renamed-srv")

    def test_role_and_tenant_carry_over_when_not_overridden(self):
        source = self.devices[0]
        source.role = self.device_role
        source.tenant = self.tenant
        source.save()

        design = self._design("Carry over")
        self._move(design, source, 13)  # no override
        self._approve(design)

        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)
        device = result.created[0].device
        self.assertEqual(device.role, self.device_role)
        self.assertEqual(device.tenant, self.tenant)

    def test_role_and_tenant_override_when_set(self):
        source = self.devices[0]
        other_role = self.device_role.__class__.objects.create(name="Other role", slug="other-role")
        other_tenant = self.tenant.__class__.objects.create(name="Other tenant", slug="other-tenant")
        source.role = self.device_role
        source.tenant = self.tenant
        source.save()

        design = self._design("Override")
        self._move(design, source, 14, role=other_role, tenant=other_tenant)
        self._approve(design)

        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)
        device = result.created[0].device
        self.assertEqual(device.role, other_role)
        self.assertEqual(device.tenant, other_tenant)

    def test_removal_flags_status_and_records_prior(self):
        target = self.devices[1]
        target.status = DeviceStatusChoices.STATUS_OFFLINE
        target.save()

        design = self._design("Remove one")
        self._remove(design, target)
        self._approve(design)

        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(len(result.removed), 1)
        target.refresh_from_db()
        self.assertEqual(target.status, "decommissioning")
        row = DesignApply.objects.get(design=design, device=target)
        self.assertEqual(row.prior_device_status, DeviceStatusChoices.STATUS_OFFLINE)
        # The device's slot is untouched -- only status changed.
        self.assertEqual(target.rack, self.racks[0])
        self.assertEqual(target.position, 2)


# --- idempotency --------------------------------------------------------------

class IdempotencyTestCase(ApplyTestCase):

    def test_second_run_is_a_noop(self):
        design = self._design("Idempotent")
        self._add(design, 10, name="idem-srv", role=self.device_role)
        self._remove(design, self.devices[1])
        self._approve(design)

        first = apply.run(design, self.superuser)
        self.assertTrue(first.ok, first.problems)

        device_count = Device.objects.count()
        row_count = DesignApply.objects.count()

        second = apply.run(design, self.superuser)
        self.assertTrue(second.ok, second.problems)
        self.assertEqual(second.created, [])
        self.assertEqual(second.updated, [])
        self.assertEqual(second.removed, [])
        self.assertEqual(second.deleted, [])
        self.assertEqual(second.reverted, [])
        self.assertEqual(Device.objects.count(), device_count)
        self.assertEqual(DesignApply.objects.count(), row_count)

    def test_drifted_device_is_corrected_by_second_run(self):
        design = self._design("Drift correction")
        self._add(design, 10, name="drift-srv", role=self.device_role)
        self._approve(design)

        first = apply.run(design, self.superuser)
        device = first.created[0].device

        device.name = "hand-edited-name"
        device.save()

        second = apply.run(design, self.superuser)
        self.assertTrue(second.ok, second.problems)
        self.assertEqual(len(second.updated), 1)
        self.assertIn("name", second.updated[0].changes)
        device.refresh_from_db()
        self.assertEqual(device.name, "drift-srv")

    def test_row_with_deleted_device_is_recreated(self):
        design = self._design("Recreate after manual delete")
        self._add(design, 10, name="recreate-srv", role=self.device_role)
        self._approve(design)

        first = apply.run(design, self.superuser)
        old_device_pk = first.created[0].device.pk
        first.created[0].device.delete()

        row = DesignApply.objects.get(design=design)
        self.assertIsNone(row.device_id)

        second = apply.run(design, self.superuser)
        self.assertTrue(second.ok, second.problems)
        self.assertEqual(len(second.created), 1)
        self.assertTrue(second.created[0].recreated)
        new_device = second.created[0].device
        self.assertIsNotNone(new_device)
        self.assertNotEqual(new_device.pk, old_device_pk)
        self.assertEqual(new_device.name, "recreate-srv")
        row.refresh_from_db()
        self.assertEqual(row.device_id, new_device.pk)


# --- cleanup -------------------------------------------------------------------

class CleanupTestCase(ApplyTestCase):

    def test_cleanup_deletes_planned_device_whose_placement_is_gone(self):
        design = self._design("Cleanup delete")
        placement = self._add(design, 10, name="to-be-cancelled", role=self.device_role)
        self._approve(design)

        first = apply.run(design, self.superuser)
        device_pk = first.created[0].device.pk

        placement.delete()  # cancels the plan -- the apply row goes orphaned (SET_NULL)

        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(len(result.deleted), 1)
        self.assertEqual(result.deleted[0].device_name, "to-be-cancelled")
        self.assertFalse(Device.objects.filter(pk=device_pk).exists())
        self.assertEqual(DesignApply.objects.filter(design=design).count(), 0)

    def test_cleanup_restores_exact_prior_status_not_active(self):
        target = self.devices[1]
        target.status = DeviceStatusChoices.STATUS_OFFLINE
        target.save()

        design = self._design("Cleanup revert")
        placement = self._remove(design, target)
        self._approve(design)

        first = apply.run(design, self.superuser)
        self.assertTrue(first.ok, first.problems)
        target.refresh_from_db()
        self.assertEqual(target.status, "decommissioning")

        placement.delete()  # cancels the removal -- apply row goes orphaned

        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(len(result.reverted), 1)
        target.refresh_from_db()
        self.assertEqual(
            target.status, DeviceStatusChoices.STATUS_OFFLINE,
            "must restore the EXACT prior status, never guess 'active'",
        )
        self.assertEqual(DesignApply.objects.filter(design=design).count(), 0)


# --- all-or-nothing ------------------------------------------------------------

class RollbackTestCase(ApplyTestCase):

    def test_mid_run_failure_rolls_everything_back(self):
        design = self._design("Rollback me")
        # Valid: has a role, will succeed and be written first.
        self._add(design, 10, name="ok-one", role=self.device_role)
        # Invalid: no role, and nothing to carry a role over from -- Device.role
        # is a required field, so full_clean() raises during _execute(), AFTER
        # the first device has already been saved in the same transaction.
        self._add(design, 11, name="bad-two")
        self._approve(design)

        pre_check = apply.plan(design, self.superuser)
        self.assertTrue(pre_check.ok, pre_check.problems)  # plan() does not catch this

        with self.assertRaises(ValidationError):
            apply.run(design, self.superuser)

        self.assertFalse(Device.objects.filter(name="ok-one").exists())
        self.assertFalse(Device.objects.filter(name="bad-two").exists())
        self.assertEqual(DesignApply.objects.filter(design=design).count(), 0)


# --- plan() purity -------------------------------------------------------------

class PlanPurityTestCase(ApplyTestCase):

    def test_plan_performs_no_writes(self):
        design = self._design("Pure inspection")
        self._add(design, 10, name="pure-srv")
        self._remove(design, self.devices[1])
        self._approve(design)

        before_device_pks = set(Device.objects.values_list("pk", flat=True))
        before_row_pks = set(DesignApply.objects.values_list("pk", flat=True))

        with transaction.atomic():
            apply.plan(design, self.superuser)
            transaction.set_rollback(True)

        self.assertEqual(set(Device.objects.values_list("pk", flat=True)), before_device_pks)
        self.assertEqual(set(DesignApply.objects.values_list("pk", flat=True)), before_row_pks)
        self.devices[1].refresh_from_db()
        self.assertEqual(self.devices[1].status, "active")


# --- query budget ---------------------------------------------------------------

class QueryBudgetTestCase(ApplyTestCase):

    def test_precheck_query_count_does_not_grow_with_placement_count(self):
        small = self._design("Small")
        for i in range(2):
            self._add(small, 10 + i, name=f"small-{i}", rack=self.racks[1])
        self._approve(small)

        large = self._design("Large")
        for i in range(20):
            self._add(large, 10 + i, name=f"large-{i}", rack=self.racks[1])
        self._approve(large)

        # Warm the user's permission cache first (has_perm caches per-user
        # instance), so it does not masquerade as a query-count difference.
        apply.plan(small, self.superuser)
        apply.plan(large, self.superuser)

        with CaptureQueriesContext(connection) as small_ctx:
            result_small = apply.plan(small, self.superuser)
        with CaptureQueriesContext(connection) as large_ctx:
            result_large = apply.plan(large, self.superuser)

        self.assertTrue(result_small.ok, result_small.problems)
        self.assertTrue(result_large.ok, result_large.problems)
        self.assertEqual(len(result_small.created), 2)
        self.assertEqual(len(result_large.created), 20)
        self.assertEqual(
            len(large_ctx.captured_queries), len(small_ctx.captured_queries),
            "plan()'s query count must not scale with the number of placements",
        )
