"""The Design "Execution plan" tab (PLAN-execution-steps.md Sec. 6)."""

from django.urls import reverse
from utilities.testing import TestCase

from ..choices import DesignPlacementKindChoices as Kind
from ..models import DesignPlacement, DesignStep
from .utils import create_dcim_environment, make_design


class ExecutionPlanViewTest(TestCase):
    user_permissions = ("netbox_rack_design.view_design",)

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.rack = env["racks"][0]
        cls.device = env["devices"][0]
        cls.design = make_design(title="Plan tab", site=env["site"])
        cls.design.racks.set([cls.rack])
        cls.remove = DesignPlacement.objects.create(
            design=cls.design, kind=Kind.KIND_REMOVE, device=cls.device)

    def _get(self):
        return self.client.get(reverse(
            "plugins:netbox_rack_design:design_execution_plan", kwargs={"pk": self.design.pk}))

    def test_viewer_gets_the_tab_with_create_plan_when_no_steps(self):
        resp = self._get()
        self.assertHttpStatus(resp, 200)
        body = resp.content.decode()
        self.assertIn("rd-plan-data", body)
        self.assertIn("execution_plan.js", body)
        # the bootstrap carries the action but no steps; the client draws "Create plan"
        self.assertEqual(resp.context["plan"]["steps"], [])
        self.assertEqual([a["id"] for a in resp.context["plan"]["actions"]], [self.remove.pk])
        # a viewer cannot edit
        self.assertFalse(resp.context["plan"]["canEdit"])
        self.assertNotIn("data-rd-plan-auto", body)

    def test_steps_are_in_the_bootstrap_when_present(self):
        step = DesignStep.objects.create(design=self.design, index=1, title="Window 1")
        DesignPlacement.objects.filter(pk=self.remove.pk).update(step=step, step_order=1)
        resp = self._get()
        self.assertHttpStatus(resp, 200)
        plan = resp.context["plan"]
        self.assertEqual([(s["id"], s["title"]) for s in plan["steps"]], [(step.pk, "Window 1")])
        self.assertEqual(plan["actions"][0]["step"], step.pk)
        self.assertEqual(plan["actions"][0]["kind"], "remove")
        self.assertIn("Window 1", self._get().content.decode())

    def test_editor_sees_the_edit_controls(self):
        self.add_permissions("netbox_rack_design.change_design")
        resp = self._get()
        self.assertTrue(resp.context["plan"]["canEdit"])
        self.assertIn("data-rd-plan-auto", resp.content.decode())


class ExecutionPlanCreationOrderTest(TestCase):
    """E6: the default plan order is the order the placements were created in."""

    user_permissions = ("netbox_rack_design.view_design",)

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        rack = env["racks"][0]
        design = make_design(title="Creation order", site=env["site"])
        design.racks.set([rack])
        cls.design = design
        # deliberately NOT removes -> moves -> adds
        cls.move = DesignPlacement.objects.create(
            design=design, kind=Kind.KIND_MOVE, device=env["devices"][0],
            target_rack=rack, target_position=10, target_face="front")
        cls.add = DesignPlacement.objects.create(
            design=design, kind=Kind.KIND_ADD, device_type=env["device_type"],
            device_role=env["device_role"], target_rack=rack, target_position=12,
            target_face="front", proposed_name="new-one")
        cls.remove = DesignPlacement.objects.create(
            design=design, kind=Kind.KIND_REMOVE, device=env["devices"][1])

    def test_action_rows_come_in_creation_order(self):
        resp = self.client.get(reverse(
            "plugins:netbox_rack_design:design_execution_plan", kwargs={"pk": self.design.pk}))
        self.assertHttpStatus(resp, 200)
        actions = resp.context["plan"]["actions"]
        expected = [self.move.pk, self.add.pk, self.remove.pk]
        self.assertEqual([a["id"] for a in actions], expected)
        self.assertEqual([a["seq"] for a in actions], [1, 2, 3])


class ExecutionPlanViewNoPermTest(TestCase):
    user_permissions = ()

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.design = make_design(title="Plan tab noperm", site=env["site"])

    def test_user_without_view_permission_is_refused(self):
        resp = self.client.get(reverse(
            "plugins:netbox_rack_design:design_execution_plan", kwargs={"pk": self.design.pk}))
        self.assertEqual(resp.status_code, 403)


# --- the plan is not connected to Apply ---------------------------------------

from dcim.models import Device  # noqa: E402

from .. import apply as apply_engine  # noqa: E402
from ..choices import DesignStatusChoices  # noqa: E402


class StepsDoNotAffectApplyTest(TestCase):
    """A design WITH steps still applies atomically, ignoring the steps."""

    user_permissions = (
        "netbox_rack_design.view_design", "netbox_rack_design.change_design",
        "dcim.add_device", "dcim.change_device", "dcim.delete_device", "dcim.view_device",
    )

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.rack = env["racks"][0]
        cls.design = make_design(title="Step apply", site=env["site"])
        cls.s1 = DesignStep.objects.create(design=cls.design, index=1, title="W1")
        cls.s2 = DesignStep.objects.create(design=cls.design, index=2, title="W2")
        for step, pos, name in ((cls.s1, 20, "step-one-srv"), (cls.s2, 22, "step-two-srv")):
            DesignPlacement.objects.create(
                design=cls.design, kind=Kind.KIND_ADD, device_type=env["device_type"],
                device_role=env["device_role"], target_rack=cls.rack, target_position=pos,
                target_face="front", proposed_name=name, step=step, step_order=1)
        cls.design.status = DesignStatusChoices.STATUS_APPROVED
        cls.design.save()

    def test_apply_run_writes_every_step_at_once(self):
        result = apply_engine.run(self.design, self.user)
        self.assertTrue(result.ok, getattr(result, "problems", None))
        self.assertTrue(Device.objects.filter(name="step-one-srv").exists())
        self.assertTrue(Device.objects.filter(name="step-two-srv").exists())

    def test_tab_bootstrap_has_no_apply_urls(self):
        resp = self.client.get(reverse(
            "plugins:netbox_rack_design:design_execution_plan", kwargs={"pk": self.design.pk}))
        plan = resp.context["plan"]
        self.assertNotIn("apply", plan["urls"])
        self.assertNotIn("revert", plan["urls"])
        self.assertNotIn("canApply", plan)
        self.assertEqual(plan["urls"]["workOrder"],
                         reverse("plugins-api:netbox_rack_design-api:design-work-order",
                                 kwargs={"pk": self.design.pk}))
