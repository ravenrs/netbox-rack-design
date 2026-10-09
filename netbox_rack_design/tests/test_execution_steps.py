"""
Tests for the execution-plan steps data model, versioning and REST API
(PLAN-execution-steps.md Sec. 3, Phase 1): ``DesignStep``, ``DesignPlacement.step``
/ ``step_order``, the copy in ``new_version`` and the ``design-steps`` endpoint.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase

from ..choices import DesignPlacementKindChoices
from ..models import DesignPlacement, DesignStep
from ..versioning import new_version
from .utils import create_dcim_environment, make_design


def _add(design, rack, position, **kw):
    return DesignPlacement.objects.create(
        design=design,
        kind=DesignPlacementKindChoices.KIND_ADD,
        device_type=kw.pop("device_type"),
        target_rack=rack,
        target_position=position,
        target_face="front",
        **kw,
    )


class DesignStepModelTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]
        cls.design = make_design(title="Plan", site=env["site"])
        cls.other = make_design(title="Other", site=env["site"])

    def test_create_and_str(self):
        plain = DesignStep.objects.create(design=self.design, index=1)
        titled = DesignStep.objects.create(design=self.design, index=2, title="Window 2")
        self.assertEqual(str(plain), "Step 1")
        self.assertEqual(str(titled), "Step 2 – Window 2")
        self.assertEqual(list(self.design.steps.all()), [plain, titled])

    def test_index_is_unique_per_design(self):
        DesignStep.objects.create(design=self.design, index=1)
        DesignStep.objects.create(design=self.other, index=1)  # other design: fine
        with self.assertRaises(IntegrityError), transaction.atomic():
            DesignStep.objects.create(design=self.design, index=1)

    def test_placement_defaults_to_unscheduled(self):
        placement = _add(self.design, self.racks[0], 10, device_type=self.device_type)
        self.assertIsNone(placement.step)
        self.assertEqual(placement.step_order, 0)

    def test_deleting_a_step_unschedules_its_placements(self):
        step = DesignStep.objects.create(design=self.design, index=1)
        placement = _add(self.design, self.racks[0], 10, device_type=self.device_type, step=step)
        step.delete()
        placement.refresh_from_db()
        self.assertIsNone(placement.step)

    def test_clean_accepts_a_step_of_the_same_design(self):
        step = DesignStep.objects.create(design=self.design, index=1)
        placement = _add(self.design, self.racks[0], 10, device_type=self.device_type, step=step)
        placement.full_clean()

    def test_clean_rejects_a_step_of_another_design(self):
        foreign = DesignStep.objects.create(design=self.other, index=1)
        placement = _add(self.design, self.racks[0], 10, device_type=self.device_type)
        placement.step = foreign
        with self.assertRaises(ValidationError) as ctx:
            placement.full_clean()
        self.assertIn("step", ctx.exception.message_dict)

    def test_clean_rejects_a_step_on_a_blade(self):
        step = DesignStep.objects.create(design=self.design, index=1)
        chassis = _add(self.design, self.racks[0], 10, device_type=self.device_type, step=step)
        blade = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            parent_placement=chassis,
            target_bay_name="Bay 1",
            step=step,
        )
        with self.assertRaises(ValidationError) as ctx:
            blade.full_clean()
        self.assertIn("step", ctx.exception.message_dict)


class DesignStepVersioningTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.design = make_design(title="Plan", site=env["site"])
        cls.step1 = DesignStep.objects.create(
            design=cls.design, index=1, title="Window 1",
        )
        cls.step2 = DesignStep.objects.create(design=cls.design, index=2)
        cls.p1 = _add(
            cls.design, env["racks"][0], 10, device_type=env["device_type"],
            step=cls.step1, step_order=3,
        )
        cls.p2 = _add(
            cls.design, env["racks"][0], 12, device_type=env["device_type"], step=cls.step2,
        )
        cls.unscheduled = _add(
            cls.design, env["racks"][0], 14, device_type=env["device_type"],
        )

    def test_new_version_copies_steps(self):
        clone = new_version(self.design)
        steps = list(clone.steps.order_by("index"))
        self.assertEqual([(s.index, s.title) for s in steps], [(1, "Window 1"), (2, "")])
        self.assertFalse({s.pk for s in steps} & {self.step1.pk, self.step2.pk})

    def test_new_version_remaps_placement_steps(self):
        clone = new_version(self.design)
        by_pos = {p.target_position: p for p in clone.placements.all()}
        step_by_index = {s.index: s for s in clone.steps.all()}
        self.assertEqual(by_pos[10].step_id, step_by_index[1].pk)
        self.assertEqual(by_pos[10].step_order, 3)
        self.assertEqual(by_pos[12].step_id, step_by_index[2].pk)
        self.assertIsNone(by_pos[14].step_id)
        for placement in by_pos.values():
            if placement.step_id:
                self.assertEqual(placement.step.design_id, clone.pk)


class DesignStepAPITest(APITestCase):
    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]
        cls.design = make_design(title="Plan", site=env["site"])
        cls.step1 = DesignStep.objects.create(design=cls.design, index=1, title="A")
        cls.step2 = DesignStep.objects.create(design=cls.design, index=2, title="B")
        cls.other_design = make_design(title="Other", site=env["site"])
        DesignStep.objects.create(design=cls.other_design, index=1)

    def _list_url(self):
        return reverse("plugins-api:netbox_rack_design-api:designstep-list")

    def _detail_url(self, step):
        return reverse("plugins-api:netbox_rack_design-api:designstep-detail", kwargs={"pk": step.pk})

    def test_list_filters_by_design(self):
        self.add_permissions("netbox_rack_design.view_designstep")
        response = self.client.get(f"{self._list_url()}?design_id={self.design.pk}", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            sorted(response.data["results"][0]),
            sorted(["id", "url", "display", "design", "index", "title",
                    "created", "last_updated"]),
        )

    def test_create(self):
        self.add_permissions("netbox_rack_design.add_designstep")
        response = self.client.post(
            self._list_url(),
            {"design": self.design.pk, "index": 3, "title": "C"},
            format="json", **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        step = DesignStep.objects.get(design=self.design, index=3)
        self.assertEqual(step.title, "C")

    def test_create_duplicate_index_is_rejected(self):
        self.add_permissions("netbox_rack_design.add_designstep")
        response = self.client.post(
            self._list_url(),
            {"design": self.design.pk, "index": 1},
            format="json", **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_patch_title(self):
        self.add_permissions("netbox_rack_design.change_designstep")
        response = self.client.patch(
            self._detail_url(self.step1),
            {"title": "Renamed"},
            format="json", **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.step1.refresh_from_db()
        self.assertEqual(self.step1.title, "Renamed")

    def test_placement_exposes_and_accepts_step(self):
        placement = _add(
            self.design, self.racks[0], 10, device_type=self.device_type, step=self.step1,
        )
        self.add_permissions(
            "netbox_rack_design.view_designplacement", "netbox_rack_design.change_designplacement",
        )
        url = reverse(
            "plugins-api:netbox_rack_design-api:designplacement-detail",
            kwargs={"pk": placement.pk},
        )
        response = self.client.get(url, **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["step"]["id"], self.step1.pk)
        self.assertEqual(response.data["step_order"], 0)

        response = self.client.patch(
            url, {"step": self.step2.pk, "step_order": 5}, format="json", **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placement.refresh_from_db()
        self.assertEqual(placement.step_id, self.step2.pk)
        self.assertEqual(placement.step_order, 5)

        list_url = reverse("plugins-api:netbox_rack_design-api:designplacement-list")
        response = self.client.get(f"{list_url}?step_id={self.step2.pk}", **self.header)
        self.assertEqual([r["id"] for r in response.data["results"]], [placement.pk])
