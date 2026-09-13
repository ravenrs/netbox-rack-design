"""
Model-level tests for the template models (PLAN-templates.md §2, task T2.1):
``TemplateGroup``, ``Template``, ``TemplatePlacement``.

Mirrors ``test_planned_rack.py`` for shape: identity/ordering, ``clean()``
validation, and that ``get_absolute_url()`` actually resolves for all three
(the whole reason the view/urls/table/form/search/GraphQL/REST wiring exists
-- a ``NetBoxModel`` with no registered URL raises ``NoReverseMatch`` the
moment anything links to it, changelog included).

Also covers the REST list/detail/create round-trip for all three, since that
wiring is part of this task too.
"""

from dcim.choices import SubdeviceRoleChoices
from dcim.models import DeviceBayTemplate, DeviceType
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase

from ..choices import TemplatePlacementAnchorChoices
from ..models import Template, TemplateGroup, TemplatePlacement
from .utils import create_dcim_environment


class TemplateGroupTestCase(TestCase):
    def test_create_and_str(self):
        group = TemplateGroup.objects.create(name="Compute pod", description="spine+leaf+storage")
        self.assertEqual(str(group), "Compute pod")

    def test_get_absolute_url_resolves(self):
        group = TemplateGroup.objects.create(name="Compute pod")
        self.assertTrue(group.get_absolute_url())


class TemplateTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.group = TemplateGroup.objects.create(name="Product pod")

    def test_create_bare_template(self):
        template = Template.objects.create(name="Standard ToR", u_height=42)
        self.assertEqual(str(template), "Standard ToR")
        self.assertIsNone(template.group)

    def test_two_templates_share_a_group_and_keep_order(self):
        t1 = Template.objects.create(name="Spine", group=self.group, order=1, u_height=4)
        t2 = Template.objects.create(name="Leaf", group=self.group, order=2, u_height=4)
        self.assertEqual(t1.group_id, self.group.pk)
        self.assertEqual(t2.group_id, self.group.pk)
        self.assertEqual(t1.order, 1)
        self.assertEqual(t2.order, 2)

    def test_clean_rejects_u_height_below_min(self):
        template = Template(name="Bad", u_height=0)
        with self.assertRaises(ValidationError):
            template.clean()

    def test_clean_rejects_u_height_above_max(self):
        template = Template(name="Bad", u_height=101)
        with self.assertRaises(ValidationError):
            template.clean()

    def test_clean_accepts_normal_u_height(self):
        template = Template(name="Fine", u_height=47)
        template.clean()  # must not raise

    def test_get_absolute_url_resolves(self):
        template = Template.objects.create(name="Standard ToR", u_height=42)
        self.assertTrue(template.get_absolute_url())


class TemplatePlacementTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.device_type = env["device_type"]
        cls.device_role = env["device_role"]
        cls.tenant = env["tenant"]
        cls.template = Template.objects.create(name="Standard ToR", u_height=42)
        cls.other_template = Template.objects.create(name="Other template", u_height=42)

        manufacturer = cls.device_type.manufacturer
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

    def test_create_placement_with_order_and_anchor(self):
        p1 = TemplatePlacement.objects.create(
            template=self.template, device_type=self.device_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_TOP, order=1,
        )
        p2 = TemplatePlacement.objects.create(
            template=self.template, device_type=self.device_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_BOTTOM, order=2,
        )
        ordered = list(self.template.placements.order_by("order"))
        self.assertEqual(ordered, [p1, p2])

    def test_role_and_tenant_are_carried(self):
        p = TemplatePlacement.objects.create(
            template=self.template, device_type=self.device_type,
            device_role=self.device_role, tenant=self.tenant,
        )
        p.refresh_from_db()
        self.assertEqual(p.device_role_id, self.device_role.pk)
        self.assertEqual(p.tenant_id, self.tenant.pk)

    def test_anchor_accepts_only_top_or_bottom(self):
        p = TemplatePlacement(template=self.template, device_type=self.device_type, anchor="middle")
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_anchor_top_is_valid(self):
        p = TemplatePlacement(template=self.template, device_type=self.device_type, anchor="top")
        p.full_clean()  # must not raise

    def test_anchor_bottom_is_valid(self):
        p = TemplatePlacement(template=self.template, device_type=self.device_type, anchor="bottom")
        p.full_clean()  # must not raise

    def test_label_is_optional_and_free_text(self):
        p = TemplatePlacement.objects.create(
            template=self.template, device_type=self.device_type, label="leaf A",
        )
        p.refresh_from_db()
        self.assertEqual(p.label, "leaf A")
        # Nothing on the model ever turns label into a name -- there is no
        # name field at all (D9/D22): the naming engine assigns names at
        # stamp time, which is out of scope for this task.
        self.assertFalse(hasattr(p, "proposed_name"))
        self.assertFalse(hasattr(p, "name"))

    def test_label_optional_blank(self):
        p = TemplatePlacement.objects.create(template=self.template, device_type=self.device_type)
        self.assertEqual(p.label, "")

    def test_power_fields_do_not_exist(self):
        # D8: everything a kind=add DesignPlacement carries EXCEPT rack,
        # position and power.
        p = TemplatePlacement(template=self.template, device_type=self.device_type)
        for field in ("power_config", "power_source_device", "planned_power_feed", "target_rack", "target_position"):
            self.assertFalse(hasattr(p, field), f"TemplatePlacement must not carry {field!r}")

    # --- D10: chassis / blade nesting -----------------------------------

    def test_chassis_with_two_blades_round_trips(self):
        chassis = TemplatePlacement.objects.create(
            template=self.template, device_type=self.chassis_type, order=1,
        )
        blade_a = TemplatePlacement.objects.create(
            template=self.template, device_type=self.blade_type,
            parent_placement=chassis, target_bay_name="bay-a", order=2,
        )
        blade_b = TemplatePlacement.objects.create(
            template=self.template, device_type=self.blade_type,
            parent_placement=chassis, target_bay_name="bay-b", order=3,
        )
        blade_a.refresh_from_db()
        blade_b.refresh_from_db()
        self.assertEqual(blade_a.parent_placement_id, chassis.pk)
        self.assertEqual(blade_b.parent_placement_id, chassis.pk)
        self.assertEqual(set(chassis.bay_children.values_list("pk", flat=True)), {blade_a.pk, blade_b.pk})

    def test_blade_without_parent_placement_is_refused(self):
        """D10: a blade may not exist in a template without its chassis."""
        p = TemplatePlacement(template=self.template, device_type=self.blade_type)
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_blade_bay_name_must_exist_on_chassis_type(self):
        chassis = TemplatePlacement.objects.create(template=self.template, device_type=self.chassis_type)
        p = TemplatePlacement(
            template=self.template, device_type=self.blade_type,
            parent_placement=chassis, target_bay_name="bay-does-not-exist",
        )
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_parent_placement_in_different_template_is_refused(self):
        chassis_in_other_template = TemplatePlacement.objects.create(
            template=self.other_template, device_type=self.chassis_type,
        )
        p = TemplatePlacement(
            template=self.template, device_type=self.blade_type,
            parent_placement=chassis_in_other_template, target_bay_name="bay-a",
        )
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("parent_placement", ctx.exception.message_dict)

    def test_parent_placement_absent_from_template_is_refused(self):
        """
        A parent_placement that does not belong to THIS template is refused --
        exercised here via a placement that lives in a completely different
        template (so it is, from this template's point of view, simply not
        one of its own placements at all).
        """
        foreign_chassis = TemplatePlacement.objects.create(
            template=self.other_template, device_type=self.chassis_type,
        )
        p = TemplatePlacement(
            template=self.template, device_type=self.blade_type,
            parent_placement=foreign_chassis, target_bay_name="bay-a",
        )
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_parent_placement_cannot_be_self(self):
        p = TemplatePlacement.objects.create(template=self.template, device_type=self.chassis_type)
        p.parent_placement = p
        p.target_bay_name = "bay-a"
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_parent_placement_must_be_parent_device_type(self):
        not_a_chassis = TemplatePlacement.objects.create(
            template=self.template, device_type=self.device_type,
        )
        p = TemplatePlacement(
            template=self.template, device_type=self.blade_type,
            parent_placement=not_a_chassis, target_bay_name="bay-a",
        )
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_get_absolute_url_resolves(self):
        p = TemplatePlacement.objects.create(template=self.template, device_type=self.device_type)
        self.assertTrue(p.get_absolute_url())

    def test_unknown_planning_field_rejected(self):
        p = TemplatePlacement(
            template=self.template, device_type=self.device_type,
            planning_data={"no-such-field": "x"},
        )
        with self.assertRaises(ValidationError):
            p.full_clean()


class TemplateAPITest(APITestCase):
    """REST round-trip for all three models (list/detail/create)."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.device_type = env["device_type"]
        cls.group = TemplateGroup.objects.create(name="API pod")
        cls.template = Template.objects.create(name="API template", group=cls.group, u_height=42)
        cls.placement = TemplatePlacement.objects.create(
            template=cls.template, device_type=cls.device_type, order=1,
        )

    def test_template_group_list_and_detail(self):
        self.add_permissions("netbox_rack_design.view_templategroup")
        response = self.client.get(
            reverse("plugins-api:netbox_rack_design-api:templategroup-list"), **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["count"], 1)

        response = self.client.get(
            reverse(
                "plugins-api:netbox_rack_design-api:templategroup-detail",
                kwargs={"pk": self.group.pk},
            ),
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "API pod")

    def test_template_group_create(self):
        self.add_permissions("netbox_rack_design.add_templategroup")
        response = self.client.post(
            reverse("plugins-api:netbox_rack_design-api:templategroup-list"),
            {"name": "New pod"}, format="json", **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_201_CREATED)

    def test_template_list_and_detail(self):
        self.add_permissions("netbox_rack_design.view_template")
        response = self.client.get(
            reverse("plugins-api:netbox_rack_design-api:template-list"), **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["count"], 1)

        response = self.client.get(
            reverse(
                "plugins-api:netbox_rack_design-api:template-detail",
                kwargs={"pk": self.template.pk},
            ),
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "API template")
        self.assertEqual(response.data["group"]["id"], self.group.pk)

    def test_template_create(self):
        self.add_permissions("netbox_rack_design.add_template")
        response = self.client.post(
            reverse("plugins-api:netbox_rack_design-api:template-list"),
            {"name": "New template", "u_height": 24}, format="json", **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_201_CREATED)

    def test_template_placement_list_and_detail(self):
        self.add_permissions("netbox_rack_design.view_templateplacement")
        response = self.client.get(
            reverse("plugins-api:netbox_rack_design-api:templateplacement-list"), **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["count"], 1)

        response = self.client.get(
            reverse(
                "plugins-api:netbox_rack_design-api:templateplacement-detail",
                kwargs={"pk": self.placement.pk},
            ),
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["template"]["id"], self.template.pk)
        self.assertEqual(response.data["device_type"]["id"], self.device_type.pk)

    def test_template_placement_create(self):
        self.add_permissions("netbox_rack_design.add_templateplacement")
        response = self.client.post(
            reverse("plugins-api:netbox_rack_design-api:templateplacement-list"),
            {
                "template": self.template.pk,
                "device_type": self.device_type.pk,
                "anchor": "bottom",
                "order": 2,
                "label": "leaf A",
            },
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        placement = TemplatePlacement.objects.get(pk=response.data["id"])
        self.assertEqual(placement.label, "leaf A")
        self.assertEqual(placement.anchor, "bottom")
