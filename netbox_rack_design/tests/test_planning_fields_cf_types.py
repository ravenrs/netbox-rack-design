"""
Placement fields bound to real device custom fields, one per NetBox type.

A planning field whose ``target`` is ``cf.<name>`` takes that custom field's
own definition -- type, choice set, related object type, bounds, regex -- so
what the plugin accepts is exactly what NetBox accepts, and apply writes it
onto the device (docs/planning-fields.md).
"""

import copy
import json

from core.models import ObjectType
from dcim.models import Device, Site
from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from extras.models import CustomField, CustomFieldChoiceSet

from .. import apply, planning_fields
from ..choices import DesignPlacementKindChoices
from ..models import DesignPlacement
from .test_apply import ApplyTestCase

# One descriptor per custom-field type; the cf names exist only here.
TYPES = {
    "text": {}, "longtext": {}, "integer": {}, "decimal": {}, "boolean": {},
    "date": {}, "datetime": {}, "url": {}, "json": {}, "select": {},
    "multiselect": {}, "object": {}, "multiobject": {},
}
FIELDS = [
    {"key": f"k_{t}", "label": t.capitalize(), "target": f"cf.acme_{t}"} for t in TYPES
] + [
    {"key": "k_missing", "label": "Missing", "target": "cf.acme_does_not_exist"},
    {"key": "k_serial", "label": "Serial", "target": "serial"},
]


def _cfg():
    cfg = copy.deepcopy(settings.PLUGINS_CONFIG)
    cfg.setdefault("netbox_rack_design", {})["placement_fields"] = FIELDS
    return cfg


def _make_custom_fields():
    device_ot = ObjectType.objects.get_for_model(Device)
    site_ot = ObjectType.objects.get_for_model(Site)
    choice_set = CustomFieldChoiceSet.objects.create(
        name="acme-tiers", extra_choices=[["gold", "Gold tier"], ["silver", "Silver tier"]],
    )
    made = {}
    for cf_type in TYPES:
        kwargs = {}
        if cf_type in ("select", "multiselect"):
            kwargs["choice_set"] = choice_set
        if cf_type in ("object", "multiobject"):
            kwargs["related_object_type"] = site_ot
        if cf_type == "integer":
            kwargs.update(validation_minimum=1, validation_maximum=48)
        if cf_type == "text":
            kwargs["validation_regex"] = r"^[a-z-]+$"
        cf = CustomField.objects.create(name=f"acme_{cf_type}", type=cf_type, **kwargs)
        cf.object_types.set([device_ot])
        made[cf_type] = cf
    return made


def _field(key):
    return next(f for f in planning_fields.placement_field_schema() if f["key"] == key)


@override_settings(PLUGINS_CONFIG=_cfg())
class BoundSchemaTest(TestCase):
    """The schema takes each custom field's own definition."""

    @classmethod
    def setUpTestData(cls):
        _make_custom_fields()
        cls.site = Site.objects.create(name="Hub", slug="hub")

    def test_the_custom_fields_type_replaces_the_declared_one(self):
        for cf_type in TYPES:
            self.assertEqual(_field(f"k_{cf_type}")["type"], cf_type)

    def test_select_choices_and_labels_come_from_the_choice_set(self):
        field = _field("k_select")
        self.assertEqual(field["choices"], ["gold", "silver"])
        self.assertEqual(field["choice_labels"], {"gold": "Gold tier", "silver": "Silver tier"})

    def test_an_object_field_publishes_its_rest_list_not_its_model(self):
        public = {f["key"]: f for f in planning_fields.public_placement_field_schema()}
        self.assertEqual(public["k_object"]["api_url"], "/api/dcim/sites/")
        self.assertTrue(public["k_multiobject"]["multiple"])
        self.assertNotIn("model", public["k_object"])
        self.assertNotIn("cf", public["k_object"])
        self.assertNotIn("target", public["k_object"])
        json.dumps(public)      # the editor embeds it as JSON

    def test_integer_bounds_are_published(self):
        field = _field("k_integer")
        self.assertEqual((field["min"], field["max"]), (1, 48))

    def test_a_target_naming_no_custom_field_keeps_the_declared_type(self):
        self.assertEqual(_field("k_missing")["type"], "text")
        self.assertEqual(planning_fields.unbound_cf_targets(), [("Missing", "acme_does_not_exist")])


@override_settings(PLUGINS_CONFIG=_cfg())
class CoercionPerTypeTest(TestCase):
    """What a planner submits becomes the JSON NetBox stores -- or a 400."""

    @classmethod
    def setUpTestData(cls):
        _make_custom_fields()
        cls.site = Site.objects.create(name="Hub", slug="hub")
        cls.site2 = Site.objects.create(name="Edge", slug="edge")

    def ok(self, key, submitted, stored):
        cleaned = planning_fields.validate_planning_data({key: submitted}, "add")
        self.assertEqual(cleaned, {key: stored})

    def refused(self, key, submitted):
        with self.assertRaises(ValidationError):
            planning_fields.validate_planning_data({key: submitted}, "add")

    def test_integer(self):
        self.ok("k_integer", "24", 24)
        self.refused("k_integer", "2.5")
        self.refused("k_integer", "64")          # over the field's maximum

    def test_decimal(self):
        self.ok("k_decimal", "2.5", 2.5)
        self.refused("k_decimal", "lots")

    def test_boolean(self):
        self.ok("k_boolean", "true", True)
        self.ok("k_boolean", "false", False)
        self.refused("k_boolean", "maybe")

    def test_date_and_datetime(self):
        self.ok("k_date", "2026-10-01", "2026-10-01")
        self.refused("k_date", "2026-13-45")
        self.ok("k_datetime", "2026-10-01T09:30", "2026-10-01T09:30:00")

    def test_text_follows_the_fields_regex(self):
        self.ok("k_text", "net-team", "net-team")
        self.refused("k_text", "Net Team")

    def test_json(self):
        self.ok("k_json", '{"rows": 2}', {"rows": 2})
        self.refused("k_json", "{not json")

    def test_select_and_multiselect(self):
        self.ok("k_select", "gold", "gold")
        self.refused("k_select", "bronze")
        self.ok("k_multiselect", ["gold", "silver"], ["gold", "silver"])
        self.ok("k_multiselect", "gold, silver", ["gold", "silver"])
        self.refused("k_multiselect", ["gold", "bronze"])

    def test_object_and_multiobject_store_ids_that_exist(self):
        self.ok("k_object", str(self.site.pk), self.site.pk)
        self.refused("k_object", "999999")
        self.refused("k_object", "hub")
        self.ok("k_multiobject", [str(self.site.pk), self.site2.pk], [self.site.pk, self.site2.pk])
        self.refused("k_multiobject", [self.site.pk, 999999])

    def test_display_uses_labels_and_names(self):
        self.assertEqual(planning_fields.display_value(_field("k_select"), "gold"), "Gold tier")
        self.assertEqual(
            planning_fields.display_value(_field("k_multiselect"), ["gold", "silver"]),
            "Gold tier, Silver tier",
        )
        self.assertEqual(planning_fields.display_value(_field("k_object"), self.site.pk), "Hub")
        self.assertEqual(planning_fields.display_value(_field("k_boolean"), False), "No")
        self.assertEqual(planning_fields.display_value(_field("k_json"), {"a": 1}), '{"a": 1}')


@override_settings(PLUGINS_CONFIG=_cfg())
class ApplyWritesPlanningFieldsTest(ApplyTestCase):
    """Apply writes every planning value onto the device it creates."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.cfs = _make_custom_fields()
        cls.hub = Site.objects.create(name="Hub", slug="hub")

    VALUES = None

    def _values(self):
        return {
            "k_text": "net-team", "k_longtext": "two\nlines", "k_integer": 24,
            "k_decimal": 2.5, "k_boolean": False, "k_date": "2026-10-01",
            "k_datetime": "2026-10-01T09:30:00", "k_url": "https://example.com/rack",
            "k_json": {"rows": 2}, "k_select": "gold", "k_multiselect": ["gold", "silver"],
            "k_object": self.hub.pk, "k_multiobject": [self.hub.pk], "k_serial": "SN-42",
        }

    def test_an_add_carries_every_type_onto_the_new_device(self):
        design = self._design()
        placement = self._add(design, 10, name="planned-1")
        placement.planning_data = self._values()
        placement.full_clean()
        placement.save()
        self._approve(design)

        result = apply.run(design, self.superuser)

        self.assertTrue(result.ok, result.problems)
        device = Device.objects.get(name="planned-1")
        data = device.custom_field_data
        self.assertEqual(data["acme_integer"], 24)
        self.assertEqual(data["acme_boolean"], False)
        self.assertEqual(data["acme_select"], "gold")
        self.assertEqual(data["acme_multiselect"], ["gold", "silver"])
        self.assertEqual(data["acme_object"], self.hub.pk)
        self.assertEqual(data["acme_multiobject"], [self.hub.pk])
        self.assertEqual(data["acme_json"], {"rows": 2})
        self.assertEqual(data["acme_date"], "2026-10-01")
        self.assertEqual(device.serial, "SN-42")
        # And NetBox itself reads them back as its own types.
        self.assertEqual(device.cf["acme_object"], self.hub)

    def test_a_required_custom_field_set_in_the_design_lets_apply_through(self):
        self.cfs["integer"].required = True
        self.cfs["integer"].save()
        design = self._design()
        placement = self._add(design, 10, name="planned-2")
        placement.planning_data = {"k_integer": 12}
        placement.save()
        self._approve(design)

        result = apply.run(design, self.superuser)

        self.assertTrue(result.ok, result.problems)
        self.assertEqual(Device.objects.get(name="planned-2").custom_field_data["acme_integer"], 12)

    def test_a_required_custom_field_left_unset_is_a_problem_not_a_500(self):
        self.cfs["integer"].required = True
        self.cfs["integer"].save()
        design = self._design()
        self._add(design, 10, name="planned-3")
        self._approve(design)

        result = apply.run(design, self.superuser)

        self.assertFalse(result.ok)
        self.assertTrue(any("acme_integer" in p for p in result.problems), result.problems)
        self.assertFalse(Device.objects.filter(name="planned-3").exists())

    def test_a_value_for_a_custom_field_that_does_not_exist_is_reported(self):
        design = self._design()
        placement = self._add(design, 10, name="planned-4")
        placement.planning_data = {"k_missing": "x"}
        placement.save()
        self._approve(design)

        result = apply.plan(design, self.superuser)

        self.assertFalse(result.ok)
        self.assertTrue(any("acme_does_not_exist" in p for p in result.problems), result.problems)

    def test_a_move_keeps_the_devices_own_fields_and_takes_the_designs(self):
        source = self.devices[0]
        source.custom_field_data.update({"acme_text": "old-team", "acme_url": "https://example.com/a"})
        source.save()
        design = self._design()
        move = self._move(design, source, 20, name="moved-1")
        move.planning_data = {"k_text": "new-team"}
        move.save()
        self._approve(design)

        result = apply.run(design, self.superuser)

        self.assertTrue(result.ok, result.problems)
        successor = Device.objects.get(name="moved-1")
        self.assertEqual(successor.custom_field_data["acme_text"], "new-team")
        self.assertEqual(successor.custom_field_data["acme_url"], "https://example.com/a")

    def test_a_changed_value_is_written_back_on_the_next_apply(self):
        design = self._design()
        placement = self._add(design, 10, name="planned-5")
        placement.planning_data = {"k_select": "gold"}
        placement.save()
        self._approve(design)
        self.assertTrue(apply.run(design, self.superuser).ok)

        DesignPlacement.objects.filter(pk=placement.pk).update(planning_data={"k_select": "silver"})
        result = apply.run(design, self.superuser)

        self.assertTrue(result.ok, result.problems)
        self.assertEqual(Device.objects.get(name="planned-5").custom_field_data["acme_select"], "silver")


@override_settings(PLUGINS_CONFIG=_cfg())
class NamingSeesPlannedObjectsTest(ApplyTestCase):
    """``{device.cf[x]}`` means the same object for a planned add as for a
    real device."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        _make_custom_fields()
        cls.hub = Site.objects.create(name="Hub", slug="hub")

    def test_an_object_field_is_the_object(self):
        from ..naming import _AddDevicePlaceholderProxy

        placement = DesignPlacement(
            design=self._design(), kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_rack=self.racks[0], target_position=10,
            target_face="front", planning_data={"k_object": self.hub.pk, "k_date": "2026-10-01"},
        )
        cf = _AddDevicePlaceholderProxy(placement).cf
        self.assertEqual(cf["acme_object"], self.hub)
        self.assertEqual(str(cf["acme_date"]), "2026-10-01")
