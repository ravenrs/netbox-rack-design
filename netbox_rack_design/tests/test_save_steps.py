"""
``POST .../designs/<pk>/save-steps/`` -- PLAN-execution-steps.md Sec. 6: the
Execution plan tab replaces a design's whole step layout in one call.
"""

from core.models import ObjectChange
from dcim.choices import SubdeviceRoleChoices
from dcim.models import Device, DeviceBayTemplate, DeviceRole, DeviceType, Manufacturer, Rack, Site
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase

from ..choices import DesignPlacementKindChoices as Kind
from ..models import DesignPlacement, DesignStep
from .utils import make_design


class SaveStepsTest(APITestCase):
    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="SV Site", slug="sv-site")
        mfr = Manufacturer.objects.create(name="SV Mfr", slug="sv-mfr")
        role = DeviceRole.objects.create(name="SV Role", slug="sv-role")
        cls.rack = Rack.objects.create(name="SV A", site=cls.site, u_height=42)
        dt = DeviceType.objects.create(manufacturer=mfr, model="SV-Srv", slug="sv-srv", u_height=1)
        cls.dt = dt
        chassis_type = DeviceType.objects.create(
            manufacturer=mfr, model="SV-Chassis", slug="sv-chassis", u_height=2,
            subdevice_role=SubdeviceRoleChoices.ROLE_PARENT)
        DeviceBayTemplate.objects.create(device_type=chassis_type, name="b1")
        blade_type = DeviceType.objects.create(
            manufacturer=mfr, model="SV-Blade", slug="sv-blade", u_height=0,
            subdevice_role=SubdeviceRoleChoices.ROLE_CHILD)
        old = Device.objects.create(
            name="sv-old", device_type=dt, site=cls.site, rack=cls.rack, position=1,
            face="front", status="active", role=role)

        cls.design = make_design(title="SV plan", site=cls.site)
        d = cls.design

        def add(name, pos, dtype=None):
            return DesignPlacement.objects.create(
                design=d, kind=Kind.KIND_ADD, device_type=dtype or dt, target_rack=cls.rack,
                target_position=pos, target_face="front", proposed_name=name)

        cls.add1 = add("sv-1", 10)
        cls.add2 = add("sv-2", 12)
        cls.rem = DesignPlacement.objects.create(design=d, kind=Kind.KIND_REMOVE, device=old)
        cls.chassis = add("sv-chassis", 20, chassis_type)
        cls.blade = DesignPlacement.objects.create(
            design=d, kind=Kind.KIND_ADD, device_type=blade_type,
            parent_placement=cls.chassis, target_bay_name="b1", proposed_name="sv-blade")

        cls.other = make_design(title="SV other", site=cls.site)
        cls.foreign = DesignPlacement.objects.create(
            design=cls.other, kind=Kind.KIND_REMOVE, device=old)

    def _url(self, design=None):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-save-steps",
            kwargs={"pk": (design or self.design).pk})

    def _save(self, steps, design=None, expect=status.HTTP_200_OK):
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design")
        resp = self.client.post(
            self._url(design), {"steps": steps}, format="json", **self.header)
        self.assertHttpStatus(resp, expect)
        return resp

    def _layout(self):
        return [
            [p.pk for p in DesignPlacement.objects.filter(step=s).order_by("step_order")]
            for s in DesignStep.objects.filter(design=self.design).order_by("index")
        ]

    def test_creates_steps_in_order(self):
        self._save([
            {"id": None, "title": "Window 1", "placements": [self.rem.pk]},
            {"id": None, "title": "Window 2", "placements": [self.add2.pk, self.add1.pk]},
        ])
        steps = list(DesignStep.objects.filter(design=self.design).order_by("index"))
        self.assertEqual([(s.index, s.title) for s in steps], [(1, "Window 1"), (2, "Window 2")])
        self.assertEqual(self._layout(), [[self.rem.pk], [self.add2.pk, self.add1.pk]])
        # unlisted placements stay unscheduled
        self.assertIsNone(DesignPlacement.objects.get(pk=self.chassis.pk).step_id)

    def test_response_lists_saved_steps(self):
        resp = self._save([{"id": None, "title": "W", "placements": [self.add1.pk]}])
        out = resp.data["steps"]
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["index"], 1)
        self.assertEqual(out[0]["placements"], [self.add1.pk])
        self.assertEqual(out[0]["id"], DesignStep.objects.get(design=self.design).pk)

    def test_replace_reorders_reindexes_and_deletes(self):
        s1 = DesignStep.objects.create(design=self.design, index=1, title="a")
        s2 = DesignStep.objects.create(design=self.design, index=2, title="b")
        s3 = DesignStep.objects.create(design=self.design, index=3, title="c")
        DesignPlacement.objects.filter(pk=self.add1.pk).update(step=s1)
        DesignPlacement.objects.filter(pk=self.rem.pk).update(step=s3)
        # swap s3 first, drop s2, keep s1 second
        self._save([
            {"id": s3.pk, "title": "c2", "placements": [self.rem.pk]},
            {"id": s1.pk, "title": "a", "placements": [self.add1.pk, self.add2.pk]},
        ])
        self.assertFalse(DesignStep.objects.filter(pk=s2.pk).exists())
        self.assertEqual(DesignStep.objects.get(pk=s3.pk).index, 1)
        self.assertEqual(DesignStep.objects.get(pk=s3.pk).title, "c2")
        self.assertEqual(DesignStep.objects.get(pk=s1.pk).index, 2)
        self.assertEqual(self._layout(), [[self.rem.pk], [self.add1.pk, self.add2.pk]])

    def test_unlisted_placements_become_unscheduled(self):
        s1 = DesignStep.objects.create(design=self.design, index=1)
        DesignPlacement.objects.filter(pk=self.add1.pk).update(step=s1, step_order=3)
        self._save([{"id": s1.pk, "title": "", "placements": [self.add2.pk]}])
        p = DesignPlacement.objects.get(pk=self.add1.pk)
        self.assertIsNone(p.step_id)
        self.assertEqual(p.step_order, 0)

    def test_empty_list_clears_the_plan(self):
        s1 = DesignStep.objects.create(design=self.design, index=1)
        DesignPlacement.objects.filter(pk=self.add1.pk).update(step=s1)
        self._save([])
        self.assertFalse(DesignStep.objects.filter(design=self.design).exists())
        self.assertIsNone(DesignPlacement.objects.get(pk=self.add1.pk).step_id)

    def test_foreign_placement_is_refused(self):
        self._save([{"id": None, "title": "", "placements": [self.foreign.pk]}],
                   expect=status.HTTP_400_BAD_REQUEST)
        self.assertFalse(DesignStep.objects.filter(design=self.design).exists())

    def test_blade_is_refused(self):
        self._save([{"id": None, "title": "", "placements": [self.blade.pk]}],
                   expect=status.HTTP_400_BAD_REQUEST)
        self.assertFalse(DesignStep.objects.filter(design=self.design).exists())

    def test_duplicate_placement_is_refused(self):
        self._save([{"id": None, "title": "", "placements": [self.add1.pk, self.add1.pk]}],
                   expect=status.HTTP_400_BAD_REQUEST)

    def test_foreign_step_id_is_refused(self):
        foreign = DesignStep.objects.create(design=self.other, index=1)
        self._save([{"id": foreign.pk, "title": "", "placements": []}],
                   expect=status.HTTP_400_BAD_REQUEST)

    def test_failed_save_leaves_the_old_layout(self):
        s1 = DesignStep.objects.create(design=self.design, index=1, title="keep")
        DesignPlacement.objects.filter(pk=self.add1.pk).update(step=s1)
        self._save(
            [{"id": s1.pk, "title": "changed", "placements": [self.add2.pk, self.blade.pk]}],
            expect=status.HTTP_400_BAD_REQUEST)
        self.assertEqual(DesignStep.objects.get(pk=s1.pk).title, "keep")
        self.assertEqual(DesignPlacement.objects.get(pk=self.add1.pk).step_id, s1.pk)

    def test_view_only_user_gets_403(self):
        self.add_permissions("netbox_rack_design.view_design")
        resp = self.client.post(
            self._url(), {"steps": []}, format="json", **self.header)
        self.assertHttpStatus(resp, status.HTTP_403_FORBIDDEN)

    def test_approved_design_can_still_be_planned(self):
        from ..choices import DesignStatusChoices
        type(self.design).objects.filter(pk=self.design.pk).update(
            status=DesignStatusChoices.STATUS_APPROVED)
        self._save([{"id": None, "title": "", "placements": [self.add1.pk]}])
        self.assertEqual(DesignStep.objects.filter(design=self.design).count(), 1)


    def _changes(self, model, pk):
        return ObjectChange.objects.filter(
            changed_object_type=ContentType.objects.get_for_model(model),
            changed_object_id=pk)

    def test_changelog_records_only_the_rows_that_changed(self):
        s1 = DesignStep.objects.create(design=self.design, index=1, title="a")
        DesignPlacement.objects.filter(pk=self.add1.pk).update(step=s1, step_order=1)
        DesignPlacement.objects.filter(pk=self.add2.pk).update(step=s1, step_order=2)
        before = ObjectChange.objects.count()
        # add1 / add2 keep step and order; rem moves in from the unscheduled
        # list; chassis stays unscheduled.
        self._save([{"id": s1.pk, "title": "a",
                     "placements": [self.add1.pk, self.add2.pk, self.rem.pk]}])
        self.assertEqual(self._changes(DesignPlacement, self.rem.pk).count(), 1)
        change = self._changes(DesignPlacement, self.rem.pk).get()
        self.assertEqual(change.action, "update")
        self.assertEqual(change.prechange_data["step"], None)
        self.assertEqual(change.postchange_data["step"], s1.pk)
        self.assertEqual(self._changes(DesignPlacement, self.add1.pk).count(), 0)
        self.assertEqual(self._changes(DesignPlacement, self.add2.pk).count(), 0)
        self.assertEqual(self._changes(DesignPlacement, self.chassis.pk).count(), 0)
        self.assertEqual(self._changes(DesignStep, s1.pk).count(), 0)
        self.assertEqual(ObjectChange.objects.count() - before, 1)

    def test_changelog_records_a_retitled_and_a_new_step(self):
        s1 = DesignStep.objects.create(design=self.design, index=1, title="a")
        self._save([
            {"id": s1.pk, "title": "renamed", "placements": []},
            {"id": None, "title": "new", "placements": []},
        ])
        self.assertEqual(self._changes(DesignStep, s1.pk).get().action, "update")
        self.assertEqual(self._changes(DesignStep, s1.pk).get().postchange_data["title"], "renamed")
        new = DesignStep.objects.get(design=self.design, index=2)
        self.assertEqual(self._changes(DesignStep, new.pk).get().action, "create")

    def test_a_reorder_records_the_reindexed_steps_with_their_old_index(self):
        s1 = DesignStep.objects.create(design=self.design, index=1, title="a")
        s2 = DesignStep.objects.create(design=self.design, index=2, title="b")
        self._save([
            {"id": s2.pk, "title": "b", "placements": []},
            {"id": s1.pk, "title": "a", "placements": []},
        ])
        change = self._changes(DesignStep, s1.pk).get()
        self.assertEqual(change.prechange_data["index"], 1)
        self.assertEqual(change.postchange_data["index"], 2)
