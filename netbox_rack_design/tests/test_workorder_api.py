"""
``GET .../designs/<pk>/work-order/`` -- PLAN-execution-steps.md Sec. 10.1.

DRF reserves the ``format`` query parameter for its renderer selector, so the
body choice is ``?output=json|md|csv`` instead.
"""

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site
from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase

from ..choices import DesignPlacementKindChoices as Kind
from ..models import DesignPlacement, DesignStep
from .utils import make_design


class WorkOrderApiTest(APITestCase):
    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        site = Site.objects.create(name="WA Site", slug="wa-site")
        mfr = Manufacturer.objects.create(name="WA Mfr", slug="wa-mfr")
        role = DeviceRole.objects.create(name="WA Role", slug="wa-role")
        rack = Rack.objects.create(name="WA R1", site=site, u_height=42)
        dt = DeviceType.objects.create(manufacturer=mfr, model="WA-Srv", slug="wa-srv", u_height=1)
        old = Device.objects.create(
            name="wa-old", device_type=dt, site=site, rack=rack, position=1,
            face="front", status="active", role=role)
        cls.design = make_design(title="WA plan", site=site)
        s1 = DesignStep.objects.create(design=cls.design, index=1, title="Window 1")
        s2 = DesignStep.objects.create(design=cls.design, index=2, title="Window 2")
        DesignPlacement.objects.create(
            design=cls.design, kind=Kind.KIND_REMOVE, device=old, step=s1, step_order=1)
        DesignPlacement.objects.create(
            design=cls.design, kind=Kind.KIND_ADD, device_type=dt, target_rack=rack,
            target_position=10, target_face="front", proposed_name="wa-new",
            step=s2, step_order=1)

    def _get(self, query="", perms=True, pk=None):
        if perms:
            self.add_permissions("netbox_rack_design.view_design")
        url = reverse(
            "plugins-api:netbox_rack_design-api:design-work-order",
            kwargs={"pk": pk or self.design.pk})
        return self.client.get(url + query, **self.header)

    def test_json_all_steps(self):
        resp = self._get()
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        self.assertEqual([s["index"] for s in resp.data["steps"]], [1, 2])

    def test_json_one_step(self):
        resp = self._get("?step=2&output=json")
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        self.assertEqual([s["index"] for s in resp.data["steps"]], [2])
        self.assertEqual(resp.data["steps"][0]["actions"][0]["device"]["name"], "wa-new")
        self.assertEqual(resp.data["total_steps"], 2)
        self.assertEqual(resp.data["steps"][0]["total_steps"], 2)

    def test_markdown(self):
        resp = self._get("?step=1&output=md")
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        self.assertTrue(resp["Content-Type"].startswith("text/markdown"))
        body = resp.content.decode()
        self.assertIn("## Step 1", body)
        self.assertIn("wa-old", body)

    def test_csv(self):
        resp = self._get("?output=csv")
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        self.assertTrue(resp["Content-Type"].startswith("text/csv"))
        lines = resp.content.decode().strip().splitlines()
        self.assertTrue(lines[0].startswith("step,"))
        self.assertEqual(len(lines), 3)

    def test_unknown_step_is_404(self):
        self.assertHttpStatus(self._get("?step=9"), status.HTTP_404_NOT_FOUND)

    def test_bad_step_is_400(self):
        self.assertHttpStatus(self._get("?step=abc"), status.HTTP_400_BAD_REQUEST)

    def test_bad_output_is_400(self):
        self.assertHttpStatus(self._get("?output=xml"), status.HTTP_400_BAD_REQUEST)

    def test_requires_view_permission(self):
        self.assertHttpStatus(self._get(perms=False), status.HTTP_403_FORBIDDEN)

    def test_format_param_reaches_json_renderer(self):
        # ``format=json`` is DRF's own selector and is harmless.
        self.assertHttpStatus(self._get("?format=json"), status.HTTP_200_OK)

    def test_format_md_is_intercepted_by_drf(self):
        # Why the param is ``output``: DRF's URL_FORMAT_OVERRIDE swallows ``format``.
        self.assertHttpStatus(self._get("?format=md"), status.HTTP_404_NOT_FOUND)
