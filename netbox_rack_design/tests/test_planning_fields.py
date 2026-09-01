"""
Tests for the config-declared planning fields (``netbox_rack_design.planning_fields``).

The rule these all enforce: **no custom field is ever hardcoded in the plugin**.
A deployment declares its own fields in ``PLUGINS_CONFIG["placement_fields"]``,
and every layer -- model validation, the REST surface, the naming engine, the
discovery endpoint -- reads that schema rather than any field name of its own.
"""

import json

from dcim.models import DeviceRole, DeviceType, Manufacturer, Rack, Site
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from extras.models import CustomField
from users.models import ObjectPermission
from utilities.testing import APITestCase, create_test_user

from .. import planning_fields
from ..choices import DesignPlacementKindChoices
from ..models import Design, DesignPlacement

# One deployment's declaration: two scalar fields, one of them a rail default.
# The cf names live ONLY here, exactly as they would in a real PLUGINS_CONFIG.
PLACEMENT_FIELDS = [
    {
        "key": "hw_class",
        "label": "HW class",
        "type": "choice",
        "choices": ["gp", "storage", "gpu"],
        "target": "cf.acme_hw_class",
        "rail": True,
    },
    {
        "key": "burn_in_hours",
        "label": "Burn-in (h)",
        "type": "number",
        "target": "cf.acme_burn_in",
    },
]


def _cfg(placement_fields=PLACEMENT_FIELDS, **extra):
    cfg = {"placement_fields": placement_fields}
    cfg.update(extra)
    return {"netbox_rack_design": cfg}


class PlacementFieldSchemaTest(TestCase):
    """Descriptor loading and validation."""

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_schema_normalises_defaults(self):
        schema = {f["key"]: f for f in planning_fields.placement_field_schema()}
        self.assertEqual(schema["hw_class"]["label"], "HW class")
        self.assertEqual(schema["hw_class"]["kinds"], ("add", "move"))
        self.assertTrue(schema["hw_class"]["rail"])
        self.assertFalse(schema["hw_class"]["required"])
        # A descriptor with no explicit label/type falls back to key/text.
        self.assertEqual(schema["burn_in_hours"]["type"], "number")

    @override_settings(PLUGINS_CONFIG=_cfg([]))
    def test_no_configuration_means_no_fields(self):
        self.assertEqual(planning_fields.placement_field_schema(), [])

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_public_schema_withholds_the_target(self):
        # `target` names a real custom field: apply-time plumbing, not part of
        # the contract an editor or API client needs.
        for field in planning_fields.public_placement_field_schema():
            self.assertNotIn("target", field)
        self.assertIn("target", planning_fields.placement_field_schema()[0])

    @override_settings(PLUGINS_CONFIG=_cfg([{"label": "No key"}]))
    def test_descriptor_without_a_key_is_a_startup_error(self):
        # A silently ignored field is worse than an error: the planner would see
        # an input that stores nothing.
        with self.assertRaises(ImproperlyConfigured):
            planning_fields.placement_field_schema()

    @override_settings(PLUGINS_CONFIG=_cfg([{"key": "k", "type": "colour"}]))
    def test_unknown_type_is_a_startup_error(self):
        with self.assertRaises(ImproperlyConfigured):
            planning_fields.placement_field_schema()

    @override_settings(PLUGINS_CONFIG=_cfg([{"key": "k", "type": "choice"}]))
    def test_choice_without_choices_is_a_startup_error(self):
        with self.assertRaises(ImproperlyConfigured):
            planning_fields.placement_field_schema()


class ValidatePlanningDataTest(TestCase):
    """What a planner (or an API client) may send."""

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_values_are_coerced_to_their_declared_type(self):
        cleaned = planning_fields.validate_planning_data(
            {"hw_class": "gpu", "burn_in_hours": "24"}, "add"
        )
        self.assertEqual(cleaned, {"hw_class": "gpu", "burn_in_hours": 24})

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_blank_values_are_dropped_rather_than_stored(self):
        self.assertEqual(planning_fields.validate_planning_data({"hw_class": ""}, "add"), {})

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ValidationError):
            planning_fields.validate_planning_data({"nope": 1}, "add")

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_value_outside_choices_is_rejected(self):
        with self.assertRaises(ValidationError):
            planning_fields.validate_planning_data({"hw_class": "quantum"}, "add")

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_non_numeric_value_for_a_number_field_is_rejected(self):
        with self.assertRaises(ValidationError):
            planning_fields.validate_planning_data({"burn_in_hours": "soon"}, "add")

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_a_move_may_carry_a_planned_override(self):
        # A design relocating a device may also say what it becomes -- the same
        # pair of kinds device_role and tenant are allowed on.
        self.assertEqual(
            planning_fields.validate_planning_data({"hw_class": "gpu"}, "move"),
            {"hw_class": "gpu"},
        )

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_field_cannot_be_set_on_a_kind_outside_its_kinds(self):
        # A removal takes none: re-attributing gear you are decommissioning
        # means nothing.
        with self.assertRaises(ValidationError):
            planning_fields.validate_planning_data({"hw_class": "gpu"}, "remove")

    @override_settings(PLUGINS_CONFIG=_cfg(
        [{"key": "hw_class", "type": "text", "required": True}]
    ))
    def test_required_field_must_carry_a_value(self):
        with self.assertRaises(ValidationError):
            planning_fields.validate_planning_data({}, "add")

    @override_settings(PLUGINS_CONFIG=_cfg([]))
    def test_a_deployment_configuring_nothing_rejects_any_data(self):
        # No silent-ignore path anywhere.
        with self.assertRaises(ValidationError):
            planning_fields.validate_planning_data({"hw_class": "gpu"}, "add")


class ReadPlanningFieldsTest(TestCase):
    """The older ``planning_fields`` read-side bridge, now living in core."""

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="S1", slug="s1")
        cf = CustomField.objects.create(name="acme_power_cap", type="integer")
        cf.object_types.set([__import__(
            "core.models", fromlist=["ObjectType"]
        ).ObjectType.objects.get_for_model(Rack)])
        cls.rack = Rack.objects.create(name="R1", site=cls.site, u_height=42)
        cls.rack.custom_field_data["acme_power_cap"] = 9000
        cls.rack.save()

    @override_settings(PLUGINS_CONFIG={"netbox_rack_design": {"planning_fields": {
        "rack": [{"key": "power_limitation", "type": "number",
                  "source": "cf.acme_power_cap"}],
    }}})
    def test_a_sites_own_cf_name_reaches_the_generic_key(self):
        self.assertEqual(
            planning_fields.read_planning_fields("rack", self.rack),
            {"power_limitation": 9000},
        )

    @override_settings(PLUGINS_CONFIG={"netbox_rack_design": {}})
    def test_empty_schema_returns_nothing(self):
        self.assertEqual(planning_fields.read_planning_fields("rack", self.rack), {})

    def test_source_grammar_walks_native_attributes(self):
        self.assertEqual(planning_fields.resolve_source(self.rack, "site.name"), "S1")
        self.assertIsNone(planning_fields.resolve_source(self.rack, "site.nope"))
        self.assertIsNone(planning_fields.resolve_source(None, "site.name"))


class _PlacementFixture:
    """A design + rack + device type, enough to build a valid `add`."""

    @classmethod
    def build_fixture(cls):
        site = Site.objects.create(name="S-pf", slug="s-pf")
        manufacturer = Manufacturer.objects.create(name="M-pf", slug="m-pf")
        device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="T-pf", slug="t-pf", u_height=1
        )
        DeviceRole.objects.create(name="Server-pf", slug="server-pf")
        rack = Rack.objects.create(name="R-pf", site=site, u_height=42)
        design = Design.objects.create(title="D-pf", site=site)
        design.racks.add(rack)
        return site, device_type, rack, design


class PlacementModelPlanningDataTest(TestCase, _PlacementFixture):
    """``DesignPlacement.clean()`` validates and normalises the blob."""

    @classmethod
    def setUpTestData(cls):
        cls.site, cls.device_type, cls.rack, cls.design = cls.build_fixture()

    def _add(self, **kwargs):
        return DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.rack,
            target_position=10,
            target_face="front",
            **kwargs,
        )

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_valid_data_is_stored_normalised(self):
        placement = self._add(planning_data={"burn_in_hours": "24"})
        placement.full_clean()
        placement.save()
        placement.refresh_from_db()
        self.assertEqual(placement.planning_data, {"burn_in_hours": 24})

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_empty_data_is_stored_as_null(self):
        placement = self._add(planning_data={})
        placement.full_clean()
        placement.save()
        placement.refresh_from_db()
        self.assertIsNone(placement.planning_data)

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_unknown_key_fails_validation(self):
        placement = self._add(planning_data={"not_declared": 1})
        with self.assertRaises(ValidationError) as ctx:
            placement.full_clean()
        self.assertIn("planning_data", ctx.exception.message_dict)


class NamingProxyCfTest(TestCase, _PlacementFixture):
    """A planned device's cf are visible to the naming engine."""

    @classmethod
    def setUpTestData(cls):
        cls.site, cls.device_type, cls.rack, cls.design = cls.build_fixture()

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_planned_device_cf_are_keyed_by_the_real_field_name(self):
        from ..naming import _AddDevicePlaceholderProxy

        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.rack,
            target_position=10,
            target_face="front",
            planning_data={"hw_class": "gpu"},
        )
        # Keyed by the DEPLOYMENT's cf name (the descriptor's target), so
        # `{device.cf[acme_hw_class]}` means the same for an add as for a real
        # device -- which is the whole point of the target mapping.
        self.assertEqual(
            _AddDevicePlaceholderProxy(placement).cf,
            {"acme_hw_class": "gpu", "acme_burn_in": None},
        )


class PlanningDataAPITest(APITestCase, _PlacementFixture):
    """Creating a placement with planning fields over the REST API."""

    @classmethod
    def setUpTestData(cls):
        cls.site, cls.device_type, cls.rack, cls.design = cls.build_fixture()

    def _grant(self):
        permission = ObjectPermission(
            name="pf", actions=["view", "add", "change", "delete"]
        )
        permission.save()
        permission.users.add(self.user)
        permission.object_types.add(
            __import__("core.models", fromlist=["ObjectType"]).ObjectType
            .objects.get_for_model(DesignPlacement)
        )

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_post_accepts_declared_fields(self):
        self._grant()
        url = reverse("plugins-api:netbox_rack_design-api:designplacement-list")
        response = self.client.post(url, {
            "design": self.design.pk,
            "kind": "add",
            "device_type": self.device_type.pk,
            "target_rack": self.rack.pk,
            "target_position": "10.0",
            "target_face": "front",
            "planning_data": {"hw_class": "gpu"},
        }, format="json", **self.header)
        self.assertHttpStatus(response, 201)
        self.assertEqual(
            DesignPlacement.objects.get(pk=response.data["id"]).planning_data,
            {"hw_class": "gpu"},
        )

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_post_rejects_an_undeclared_field(self):
        self._grant()
        url = reverse("plugins-api:netbox_rack_design-api:designplacement-list")
        response = self.client.post(url, {
            "design": self.design.pk,
            "kind": "add",
            "device_type": self.device_type.pk,
            "target_rack": self.rack.pk,
            "target_position": "11.0",
            "target_face": "front",
            "planning_data": {"undeclared": "x"},
        }, format="json", **self.header)
        self.assertHttpStatus(response, 400)

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_discovery_endpoint_publishes_the_schema_without_targets(self):
        url = reverse("plugins-api:netbox_rack_design-api:placement-fields")
        response = self.client.get(url, **self.header)
        self.assertHttpStatus(response, 200)
        keys = [f["key"] for f in response.data]
        self.assertEqual(keys, ["hw_class", "burn_in_hours"])
        self.assertNotIn("target", response.data[0])

    @override_settings(PLUGINS_CONFIG=_cfg([]))
    def test_discovery_endpoint_is_empty_when_nothing_is_configured(self):
        url = reverse("plugins-api:netbox_rack_design-api:placement-fields")
        response = self.client.get(url, **self.header)
        self.assertHttpStatus(response, 200)
        self.assertEqual(list(response.data), [])


class SaveLayoutPlanningDataTest(APITestCase, _PlacementFixture):
    """The editor's bulk save action carries the same blob."""

    @classmethod
    def setUpTestData(cls):
        cls.site, cls.device_type, cls.rack, cls.design = cls.build_fixture()

    def _grant(self):
        user = create_test_user("pf-save")
        permission = ObjectPermission(name="pf-save", actions=["view", "add", "change", "delete"])
        permission.save()
        permission.users.add(self.user)
        for model in (Design, DesignPlacement):
            permission.object_types.add(
                __import__("core.models", fromlist=["ObjectType"]).ObjectType
                .objects.get_for_model(model)
            )
        return user

    def _save(self, planning_data):
        url = reverse(
            "plugins-api:netbox_rack_design-api:design-save-layout",
            kwargs={"pk": self.design.pk},
        )
        return self.client.post(url, {
            "design_id": self.design.pk,
            "racks": [{
                "rack_id": self.rack.pk,
                "front": [{
                    "kind": "add",
                    "device_type_id": self.device_type.pk,
                    "u_position": "10.0",
                    "planning_data": planning_data,
                }],
            }],
        }, format="json", **self.header)

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_save_layout_persists_planning_data(self):
        self._grant()
        response = self._save({"hw_class": "storage"})
        self.assertHttpStatus(response, 200)
        placement = DesignPlacement.objects.get(design=self.design, target_position=10)
        self.assertEqual(placement.planning_data, {"hw_class": "storage"})

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_save_layout_reports_an_invalid_value_rather_than_storing_it(self):
        self._grant()
        response = self._save({"hw_class": "quantum"})
        # The whole save is rejected: an invalid planning value is a validation
        # error like any other, not a silently dropped key.
        self.assertHttpStatus(response, 400)
        self.assertIn("is not one of", str(response.data["errors"]))
        self.assertFalse(
            DesignPlacement.objects.filter(design=self.design, target_position=10).exists()
        )


class SlotPlanningFilterTest(TestCase):
    """The hover-card attribute: one uniform row for every slot state."""

    @classmethod
    def setUpTestData(cls):
        from core.models import ObjectType
        from dcim.models import Device

        cls.site = Site.objects.create(name="S-hc", slug="s-hc")
        manufacturer = Manufacturer.objects.create(name="M-hc", slug="m-hc")
        cls.device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="T-hc", slug="t-hc", u_height=1
        )
        role = DeviceRole.objects.create(name="Server-hc", slug="server-hc")
        cls.rack = Rack.objects.create(name="R-hc", site=cls.site, u_height=42)
        cf = CustomField.objects.create(name="acme_hw_class", type="text")
        cf.object_types.set([ObjectType.objects.get_for_model(Device)])
        cls.device = Device.objects.create(
            name="dev-hc", device_type=cls.device_type, role=role,
            site=cls.site, rack=cls.rack, position=1, face="front",
        )
        cls.device.custom_field_data["acme_hw_class"] = "storage"
        cls.device.save()
        cls.design = Design.objects.create(title="D-hc", site=cls.site)
        cls.design.racks.add(cls.rack)

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_a_real_device_slot_reads_its_own_custom_field(self):
        from ..templatetags.rack_design import slot_planning

        # existing / move / remove all carry a real device -- the value comes
        # from the custom field the descriptor's `target` names.
        value = slot_planning({"device": self.device, "placement": None})
        self.assertEqual(json.loads(value), [["HW class", "storage"]])

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_a_planned_add_slot_reads_its_placement(self):
        from ..templatetags.rack_design import slot_planning

        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.rack,
            target_position=10,
            target_face="front",
            planning_data={"hw_class": "gpu", "burn_in_hours": 24},
        )
        value = slot_planning({"device": None, "placement": placement})
        self.assertEqual(
            json.loads(value),
            [["HW class", "gpu"], ["Burn-in (h)", "24"]],
        )

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_an_unset_field_is_omitted_rather_than_rendered_blank(self):
        from ..templatetags.rack_design import slot_planning

        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            planning_data={"hw_class": "gp"},
        )
        self.assertEqual(
            json.loads(slot_planning({"device": None, "placement": placement})),
            [["HW class", "gp"]],
        )

    @override_settings(PLUGINS_CONFIG=_cfg([]))
    def test_no_configuration_means_no_attribute(self):
        from ..templatetags.rack_design import slot_planning

        # "" so the template omits data-planning entirely.
        self.assertEqual(slot_planning({"device": self.device, "placement": None}), "")

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_a_moves_override_wins_over_the_devices_own_custom_field(self):
        from ..templatetags.rack_design import slot_planning

        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.device,
            target_rack=self.rack,
            target_position=10,
            target_face="front",
            planning_data={"hw_class": "gpu"},
        )
        # The device itself is "storage"; the design says it becomes "gpu".
        self.assertEqual(
            json.loads(slot_planning({"device": self.device, "placement": placement})),
            [["HW class", "gpu"]],
        )

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_a_move_without_an_override_still_shows_the_devices_value(self):
        from ..templatetags.rack_design import slot_planning

        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.device,
            target_rack=self.rack,
            target_position=10,
            target_face="front",
        )
        self.assertEqual(
            json.loads(slot_planning({"device": self.device, "placement": placement})),
            [["HW class", "storage"]],
        )
