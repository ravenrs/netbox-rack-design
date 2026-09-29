"""
Planning fields travel with templates.

A template placement stores ``planning_data`` like a design's add. It is
filled when a template is taken from what already stands in a rack (the
devices' own custom fields, a move's planned overrides on top), shown and
edited on the template placement's own NetBox form and page, and carried on
by a stamp (docs/templates.md, docs/planning-fields.md).
"""

import copy

from core.models import ObjectType
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site
from django import forms
from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from extras.models import CustomField, CustomFieldChoiceSet
from rest_framework import status
from utilities.testing import APITestCase

from ..choices import DesignPlacementKindChoices
from ..forms import TemplatePlacementForm
from ..models import DesignPlacement, Template, TemplatePlacement
from .utils import make_design

FIELDS = [
    {"key": "burn_in", "label": "Burn-in hours", "target": "cf.tp_burn_in"},
    {"key": "tier", "label": "Hardware tier", "target": "cf.tp_tier"},
    {"key": "staging", "label": "Staging site", "target": "cf.tp_staging"},
]


def _cfg():
    cfg = copy.deepcopy(settings.PLUGINS_CONFIG)
    cfg.setdefault("netbox_rack_design", {})["placement_fields"] = FIELDS
    return cfg


def _custom_fields():
    device_ot = ObjectType.objects.get_for_model(Device)
    tiers = CustomFieldChoiceSet.objects.create(
        name="tp-tiers", extra_choices=[["gold", "Gold"], ["silver", "Silver"]])
    for name, kind, extra in (
        ("tp_burn_in", "integer", {}),
        ("tp_tier", "select", {"choice_set": tiers}),
        ("tp_staging", "object", {"related_object_type": ObjectType.objects.get_for_model(Site)}),
    ):
        cf = CustomField.objects.create(name=name, type=kind, **extra)
        cf.object_types.set([device_ot])


class _Fixture:
    @classmethod
    def build(cls):
        _custom_fields()
        cls.site = Site.objects.create(name="TP Site", slug="tp-site")
        cls.staging = Site.objects.create(name="TP Staging", slug="tp-staging")
        cls.mfr = Manufacturer.objects.create(name="TP Mfr", slug="tp-mfr")
        cls.device_type = DeviceType.objects.create(
            manufacturer=cls.mfr, model="TP Server", slug="tp-server", u_height=1)
        cls.role = DeviceRole.objects.create(name="TP Role", slug="tp-role")
        cls.rack = Rack.objects.create(name="TP Rack", site=cls.site, u_height=10)

    def _device(self, name, position, **cf):
        device = Device.objects.create(
            name=name, site=self.site, rack=self.rack, device_type=self.device_type,
            role=self.role, position=position, face="front")
        device.custom_field_data.update(cf)
        device.save()
        return device


@override_settings(PLUGINS_CONFIG=_cfg())
class TemplateTakesDeviceValuesTest(APITestCase, _Fixture):
    """Saving a rack with real equipment as a template keeps what its
    devices' own custom fields hold."""

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        cls.build()

    def _perms(self):
        self.add_permissions("netbox_rack_design.add_template", "netbox_rack_design.view_design")

    def test_from_a_real_rack(self):
        self._perms()
        self._device("tp-1", 1, tp_burn_in=48, tp_tier="gold", tp_staging=self.staging.pk)

        resp = self.client.post(
            reverse("plugins-api:netbox_rack_design-api:template-from-rack"),
            {"rack_id": self.rack.pk, "name": "From real"}, format="json", **self.header)

        self.assertHttpStatus(resp, status.HTTP_201_CREATED)
        placement = Template.objects.get(pk=resp.data["template_id"]).placements.get()
        self.assertEqual(placement.planning_data,
                         {"burn_in": 48, "tier": "gold", "staging": self.staging.pk})

    def test_from_a_design_untouched_and_moved_devices(self):
        """An untouched real device gives its own values; a moved one its
        values with the design's planned override on top."""
        self._perms()
        self._device("tp-still", 1, tp_tier="silver")
        moved = self._device("tp-moved", 2, tp_tier="silver", tp_burn_in=12)
        design = make_design(title="TP Design", site=self.site)
        design.racks.add(self.rack)
        DesignPlacement.objects.create(
            design=design, kind=DesignPlacementKindChoices.KIND_MOVE, device=moved,
            target_rack=self.rack, target_position=10, target_face="front",
            planning_data={"tier": "gold"})

        resp = self.client.post(
            reverse("plugins-api:netbox_rack_design-api:template-from-design"),
            {"design": design.pk, "rack": f"r:{self.rack.pk}", "name": "From design"},
            format="json", **self.header)

        self.assertHttpStatus(resp, status.HTTP_201_CREATED)
        rows = [tp.planning_data for tp in
                Template.objects.get(pk=resp.data["template_id"]).placements.all()]
        self.assertIn({"tier": "silver"}, rows)
        self.assertIn({"tier": "gold", "burn_in": 12}, rows)

    def test_a_device_value_the_field_would_refuse_is_left_out(self):
        """Data older than a field's rules must not fail the whole template."""
        self._perms()
        self._device("tp-old", 1, tp_tier="silver")
        Device.objects.filter(name="tp-old").update(
            custom_field_data={"tp_tier": "bronze", "tp_burn_in": 24})

        resp = self.client.post(
            reverse("plugins-api:netbox_rack_design-api:template-from-rack"),
            {"rack_id": self.rack.pk, "name": "Old data"}, format="json", **self.header)

        self.assertHttpStatus(resp, status.HTTP_201_CREATED)
        placement = Template.objects.get(pk=resp.data["template_id"]).placements.get()
        self.assertEqual(placement.planning_data, {"burn_in": 24})


@override_settings(PLUGINS_CONFIG=_cfg())
class TemplatePlacementFormTest(TestCase, _Fixture):
    """The template placement's own NetBox form edits its planning fields."""

    @classmethod
    def setUpTestData(cls):
        cls.build()
        cls.template = Template.objects.create(name="TP Form")

    def test_one_typed_input_per_field(self):
        form = TemplatePlacementForm()
        self.assertIsInstance(form.fields["pf_burn_in"], forms.IntegerField)
        self.assertEqual(dict(form.fields["pf_tier"].choices)["gold"], "Gold")
        self.assertEqual(form.fields["pf_staging"].queryset.model, Site)

    def test_saving_the_form_stores_the_values(self):
        form = TemplatePlacementForm(data={
            "template": self.template.pk, "device_type": self.device_type.pk,
            "device_role": self.role.pk, "anchor": "bottom", "offset": "0", "order": 0, "face": "front",
            "pf_burn_in": "36", "pf_tier": "gold", "pf_staging": self.staging.pk,
        })
        self.assertTrue(form.is_valid(), form.errors)
        placement = form.save()
        placement.refresh_from_db()
        self.assertEqual(placement.planning_data,
                         {"burn_in": 36, "tier": "gold", "staging": self.staging.pk})

    def test_editing_prefills_the_stored_values(self):
        placement = TemplatePlacement.objects.create(
            template=self.template, device_type=self.device_type, anchor="bottom",
            order=0, face="front", planning_data={"tier": "silver", "burn_in": 8})
        form = TemplatePlacementForm(instance=placement)
        self.assertEqual(form.initial["pf_tier"], "silver")
        self.assertEqual(form.initial["pf_burn_in"], 8)

    def test_a_value_out_of_bounds_is_a_form_error(self):
        CustomField.objects.filter(name="tp_burn_in").update(validation_maximum=100)
        form = TemplatePlacementForm(data={
            "template": self.template.pk, "device_type": self.device_type.pk,
            "anchor": "bottom", "order": 0, "face": "front", "pf_burn_in": "500",
        })
        self.assertFalse(form.is_valid())

    def test_the_page_shows_them_by_label(self):
        placement = TemplatePlacement.objects.create(
            template=self.template, device_type=self.device_type, anchor="bottom",
            order=0, face="front",
            planning_data={"tier": "gold", "staging": self.staging.pk})
        from users.models import User
        self.client.force_login(User.objects.create_superuser(username="tp-admin"))
        html = self.client.get(placement.get_absolute_url()).content.decode()
        self.assertIn("Planning fields", html)
        self.assertIn("Gold", html)
        self.assertIn("TP Staging", html)
