"""Tests for template provenance on DesignPlacement (PLAN-templates.md §3, D20).

A ``DesignPlacement`` stamped from a ``Template`` records which template it
came from and the template's ``version`` at that moment -- enough to answer
"which racks use the standard ToR?" and to warn that the template has since
changed. Re-sync / diff-against-template is explicitly DEFERRED (D20); this
module only covers the fields, the SET_NULL contract, the version mechanism,
and the REST round trip. Nothing here populates the fields from a real stamp
-- wiring provenance into the stamp/save path is a separate task.
"""

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase

from ..choices import DesignPlacementKindChoices
from ..models import DesignPlacement, Template, TemplatePlacement
from .utils import create_dcim_environment, make_design


class DesignPlacementProvenanceTestCase(TestCase):
    """Model-level coverage: recording provenance, and the SET_NULL contract."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.rack = env["racks"][1]
        cls.design = make_design(title="Provenance design", site=cls.site)
        cls.template = Template.objects.create(name="Standard ToR", u_height=42)

    def test_placement_records_from_template_and_version(self):
        placement = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.rack,
            target_position=1,
            from_template=self.template,
            from_template_version=self.template.version,
        )
        placement.refresh_from_db()
        self.assertEqual(placement.from_template_id, self.template.pk)
        self.assertEqual(placement.from_template_version, 1)

    def test_fields_are_optional(self):
        """An ordinary hand-placed device never had a template -- both fields
        must accept null, or every non-stamped placement would break."""
        placement = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.rack,
            target_position=2,
        )
        self.assertIsNone(placement.from_template_id)
        self.assertIsNone(placement.from_template_version)

    def test_deleting_template_leaves_placement_standing(self):
        """SET_NULL, not CASCADE (D20): deleting a Template must never delete
        the placements that were stamped from it -- they are real plans that
        stand on their own once stamped."""
        placement = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.rack,
            target_position=3,
            from_template=self.template,
            from_template_version=self.template.version,
        )
        self.template.delete()
        placement.refresh_from_db()
        self.assertIsNone(placement.from_template_id)
        # The version snapshot is untouched -- it recorded history, not a
        # live reference, and losing the FK does not erase what was known.
        self.assertEqual(placement.from_template_version, 1)
        # The rest of the placement is entirely unaffected.
        self.assertEqual(placement.device_type_id, self.device_type.pk)


class TemplateVersionBumpTestCase(TestCase):
    """The version mechanism (D20, "yours to decide"): an explicit counter on
    Template, bumped whenever a TemplatePlacement under it is added, edited,
    or removed. NOT ``last_updated`` -- see Template.version's docstring for
    why: a child placement's save never touches the parent row, so
    ``last_updated`` would not move for exactly the case that matters."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.device_type = env["device_type"]

    def test_version_starts_at_one(self):
        template = Template.objects.create(name="Fresh", u_height=42)
        self.assertEqual(template.version, 1)

    def test_version_bumps_on_placement_add(self):
        template = Template.objects.create(name="Adds", u_height=42)
        self.assertEqual(template.version, 1)
        TemplatePlacement.objects.create(template=template, device_type=self.device_type)
        template.refresh_from_db()
        self.assertEqual(template.version, 2)

    def test_version_bumps_on_placement_edit(self):
        template = Template.objects.create(name="Edits", u_height=42)
        placement = TemplatePlacement.objects.create(
            template=template, device_type=self.device_type, order=1,
        )
        template.refresh_from_db()
        version_after_create = template.version

        placement.order = 2
        placement.save()
        template.refresh_from_db()
        self.assertEqual(template.version, version_after_create + 1)

    def test_version_bumps_on_placement_delete(self):
        template = Template.objects.create(name="Deletes", u_height=42)
        placement = TemplatePlacement.objects.create(template=template, device_type=self.device_type)
        template.refresh_from_db()
        version_after_create = template.version

        placement.delete()
        template.refresh_from_db()
        self.assertEqual(template.version, version_after_create + 1)

    def test_version_unaffected_by_unrelated_template(self):
        """Editing one template's placements must not bump a different
        template's version."""
        template_a = Template.objects.create(name="A", u_height=42)
        template_b = Template.objects.create(name="B", u_height=42)
        TemplatePlacement.objects.create(template=template_a, device_type=self.device_type)
        template_b.refresh_from_db()
        self.assertEqual(template_b.version, 1)


class DesignPlacementProvenanceAPITest(APITestCase):
    """REST round trip: from_template accepts a raw pk on write and renders
    nested on read, mirroring target_planned_rack's own round-trip test."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.rack = env["racks"][1]
        cls.design = make_design(title="Provenance API design", site=cls.site)
        cls.template = Template.objects.create(name="Standard ToR API", u_height=42)

    def test_create_placement_with_from_template_by_pk(self):
        self.add_permissions(
            "netbox_rack_design.add_designplacement", "netbox_rack_design.view_designplacement",
        )
        url = reverse("plugins-api:netbox_rack_design-api:designplacement-list")
        data = {
            "design": self.design.pk,
            "kind": "add",
            "device_type": self.device_type.pk,
            "target_rack": self.rack.pk,
            "target_position": 1,
            "target_face": "front",
            "proposed_name": "stamped-1",
            "from_template": self.template.pk,
            "from_template_version": self.template.version,
        }
        response = self.client.post(url, data, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_201_CREATED)

        placement = DesignPlacement.objects.get(pk=response.data["id"])
        self.assertEqual(placement.from_template_id, self.template.pk)
        self.assertEqual(placement.from_template_version, 1)

        # Read back: from_template renders NESTED, not a bare pk, and the
        # version snapshot rides along as a plain integer.
        detail_url = reverse(
            "plugins-api:netbox_rack_design-api:designplacement-detail",
            kwargs={"pk": placement.pk},
        )
        response = self.client.get(detail_url, **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["from_template"]["id"], self.template.pk)
        self.assertEqual(response.data["from_template"]["name"], self.template.name)
        self.assertEqual(response.data["from_template_version"], 1)

    def test_placement_without_template_reads_null(self):
        self.add_permissions(
            "netbox_rack_design.add_designplacement", "netbox_rack_design.view_designplacement",
        )
        placement = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.rack,
            target_position=2,
        )
        url = reverse(
            "plugins-api:netbox_rack_design-api:designplacement-detail",
            kwargs={"pk": placement.pk},
        )
        response = self.client.get(url, **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertIsNone(response.data["from_template"])
        self.assertIsNone(response.data["from_template_version"])
