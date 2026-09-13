"""
Tests for T5.1 (PLAN-templates.md D30): a ``distribution_script`` must not
choke on a ``PlannedRack``, and the documented contract (``distribution.py``'s
module docstring) must hold in practice.

Before this task ``distribution.py``'s script path called ``fn(rack,
devices)`` with no guard and no documented contract, so a script written
against a real rack could raise on ``rack.devices`` the moment a planned
rack's stamped PDUs made it non-empty -- a live path (stamping a template
places PDUs in a planned rack), not a theoretical one. Separately,
``power_limitation``/``pdu_location`` silently came back empty for a planned
rack to any script reading them the way ``planning_fields.read_planning_fields``
does (it prefers ``custom_field_data`` over ``.cf``, and
``apply_rack_power_override`` used to patch only ``.cf``).

Chosen contract (see ``distribution.py``'s module docstring): the engine
passes the rack object through UNCHANGED -- real ``dcim.Rack`` or
``PlannedRack`` -- rather than wrapping it in a proxy or skipping script mode
for a planned rack. The engine's OWN device/PDU discovery
(``devices_from_elevation`` / ``_collect_pdus``) already goes through
``rackinfo`` helpers that are planned-rack-safe, so no engine-level guard is
needed there; what changes here is (a) the contract is written down, (b) the
shipped example scripts are confirmed safe under it, and (c) the rack-cf
override reaches every access pattern a script may reasonably use.
"""

from dcim.choices import PowerFeedPhaseChoices
from dcim.models import DeviceRole, DeviceType, Location, PowerOutletTemplate
from django.test import TestCase, override_settings

from ..choices import DesignPlacementKindChoices
from ..distribution import (
    _collect_pdus,
    apply_rack_power_override,
    generate_distribution_status,
)
from ..models import Design, DesignPlacement, DesignPowerFeed, DesignRackPower, PlannedRack
from ..projection import project_rack
from ..rackinfo import is_planned
from .utils import create_dcim_environment

# --- module-level script probes (must be importable dotted paths) ----------

_SCRIPT_CALLS = []


def safe_contract_script(rack, devices):
    """A script that reads ONLY the contract-safe attributes documented in
    ``distribution.py``'s module docstring (name/u_height/location/site/pk),
    plus the shared PDU-discovery helper -- never ``rack.devices`` directly.
    Must work UNCHANGED for a real rack and a ``PlannedRack``."""
    _SCRIPT_CALLS.append({
        "rack": rack,
        "name": rack.name,
        "u_height": rack.u_height,
        "location": rack.location,
        "site": rack.site,
        "pk": rack.pk,
        "is_planned": is_planned(rack),
    })
    pdus = _collect_pdus(rack, devices)
    if not pdus:
        return None
    return {
        "scheme": "", "pdu_location": None, "pdus": pdus,
        "rack": {"power_limitation_w": None, "power_consumption_w": 0.0,
                 "alarm": False, "warnings": []},
    }


_CF_CALLS = []


def cf_reading_script(rack, devices):
    """Records the rack's effective cf as the engine hands it over, so a test
    can assert ``power_limitation`` came from ``DesignRackPower``, not raw
    ``rack.cf`` (empty by definition for a ``PlannedRack``)."""
    _CF_CALLS.append({
        "cf": dict(rack.cf),
        "custom_field_data": dict(rack.custom_field_data),
    })
    pdus = _collect_pdus(rack, devices)
    if not pdus:
        return None
    return {
        "scheme": "", "pdu_location": None, "pdus": pdus,
        "rack": {"power_limitation_w": None, "power_consumption_w": 0.0,
                 "alarm": False, "warnings": []},
    }


_SAFE_FN = "netbox_rack_design.tests.test_distribution_planned_rack.safe_contract_script"
_CF_FN = "netbox_rack_design.tests.test_distribution_planned_rack.cf_reading_script"


def _plugins_config(**overrides):
    cfg = {"distribution_mode": "script", "distribution_script": _SAFE_FN}
    cfg.update(overrides)
    return {"netbox_rack_design": cfg}


class DistributionPlannedRackTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.manufacturer = env["manufacturer"]
        cls.racks = env["racks"]

        cls.pdu_role = DeviceRole.objects.create(name="PDU T5.1", slug="pdu")
        cls.pdu_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer, model="Planned PDU Type T5.1",
            slug="planned-pdu-type-t51", u_height=0,
        )
        PowerOutletTemplate.objects.create(device_type=cls.pdu_type, name="1/1")
        PowerOutletTemplate.objects.create(device_type=cls.pdu_type, name="2/1")

        cls.location = Location.objects.create(
            name="Location T5.1", slug="location-t51", site=cls.site,
        )
        cls.planned_rack = PlannedRack.objects.create(
            name="Planned R1", location=cls.location, u_height=12,
        )
        cls.design = Design.objects.create(title="Greenfield T5.1", site=cls.site)

    def _plan_pdu(self, rack, planned_rack_target=True, feed_watts=(230, 16)):
        """Create a planned PDU add on ``rack`` bound to a fresh
        ``DesignPowerFeed`` so ``_collect_pdus`` resolves it -- the "stamping
        a template puts real PDUs in the plan" scenario D30 describes."""
        voltage, amperage = feed_watts
        feed_kwargs = {
            "design": self.design,
            "name": f"Feed-{rack.pk}-{'p' if planned_rack_target else 'r'}",
            "voltage": voltage, "amperage": amperage,
            "phase": PowerFeedPhaseChoices.PHASE_SINGLE,
        }
        if planned_rack_target:
            feed = DesignPowerFeed.objects.create(planned_rack=rack, **feed_kwargs)
        else:
            feed = DesignPowerFeed.objects.create(rack=rack, **feed_kwargs)
        placement_kwargs = {
            "design": self.design, "kind": DesignPlacementKindChoices.KIND_ADD,
            "device_type": self.pdu_type, "device_role": self.pdu_role,
            "target_position": None, "proposed_name": f"pdu-{rack.pk}",
            "planned_power_feed": feed,
        }
        if planned_rack_target:
            placement_kwargs["target_planned_rack"] = rack
        else:
            placement_kwargs["target_rack"] = rack
        return DesignPlacement.objects.create(**placement_kwargs)

    # --- the failing-before-fix case: script runs on a planned rack with PDUs --

    @override_settings(PLUGINS_CONFIG=_plugins_config(distribution_script=_SAFE_FN))
    def test_script_mode_not_failed_for_planned_rack_with_pdus(self):
        self._plan_pdu(self.planned_rack)
        elevation = project_rack(self.design, self.planned_rack)
        result, status = generate_distribution_status(elevation, mode="script")
        self.assertNotEqual(status["state"], "failed", status)
        self.assertEqual(status["state"], "ok", status)
        self.assertIsNotNone(result)
        self.assertIn(f"pdu-{self.planned_rack.pk}", result["pdus"])

    @override_settings(PLUGINS_CONFIG=_plugins_config(distribution_script=_SAFE_FN))
    def test_safe_contract_script_receives_planned_rack_unchanged(self):
        """Chosen option (3): pass the rack through unchanged, no proxy. The
        script gets the ACTUAL ``PlannedRack`` instance, and every attribute
        the contract promises resolves without raising."""
        self._plan_pdu(self.planned_rack)
        elevation = project_rack(self.design, self.planned_rack)
        _SCRIPT_CALLS.clear()
        generate_distribution_status(elevation, mode="script")
        self.assertEqual(len(_SCRIPT_CALLS), 1)
        call = _SCRIPT_CALLS[0]
        self.assertIs(call["rack"], elevation.rack)
        self.assertIsInstance(call["rack"], PlannedRack)
        self.assertTrue(call["is_planned"])
        self.assertEqual(call["name"], "Planned R1")
        self.assertEqual(call["u_height"], 12)
        self.assertEqual(call["location"], self.location)
        self.assertEqual(call["site"], self.site)
        self.assertEqual(call["pk"], self.planned_rack.pk)

    @override_settings(PLUGINS_CONFIG=_plugins_config(distribution_script=_SAFE_FN))
    def test_safe_contract_script_unchanged_for_real_rack_too(self):
        """Regression: the SAME script, unmodified, still works for a real
        rack -- the contract is additive, not a special case."""
        real_rack = self.racks[0]
        self._plan_pdu(real_rack, planned_rack_target=False)
        elevation = project_rack(self.design, real_rack)
        _SCRIPT_CALLS.clear()
        result, status = generate_distribution_status(elevation, mode="script")
        self.assertEqual(status["state"], "ok", status)
        call = _SCRIPT_CALLS[0]
        self.assertFalse(call["is_planned"])
        self.assertEqual(call["name"], real_rack.name)

    # --- rack.devices is NOT part of the contract -------------------------

    def test_rack_devices_raises_on_planned_rack(self):
        """Pins WHY the contract excludes ``rack.devices``: ``PlannedRack``
        has no such manager at all -- a script that bypasses the shared
        helpers and calls it directly gets a loud ``AttributeError``, caught
        by the engine and surfaced as ``state: "failed"``."""
        with self.assertRaises(AttributeError):
            self.planned_rack.devices  # noqa: B018

    # --- power_limitation must come from DesignRackPower, never rack.cf ----

    def test_raw_cf_is_empty_for_planned_rack(self):
        # Rack power custom fields are registered against dcim.rack's content
        # type, not plannedrack's (distribution.py's module docstring / D30).
        self.assertEqual(dict(self.planned_rack.cf), {})

    @override_settings(PLUGINS_CONFIG=_plugins_config(distribution_script=_CF_FN))
    def test_power_limitation_resolves_from_design_rack_power(self):
        DesignRackPower.objects.create(
            design=self.design, planned_rack=self.planned_rack,
            power_config={"custom_fields": {"power_limitation": 8000, "pdu_location": "top"}},
        )
        self._plan_pdu(self.planned_rack)
        elevation = project_rack(self.design, self.planned_rack)
        _CF_CALLS.clear()
        generate_distribution_status(elevation, mode="script")
        self.assertEqual(len(_CF_CALLS), 1)
        seen = _CF_CALLS[0]
        # Both access patterns a script may reasonably use must see the merge:
        # `.cf` directly, and `.custom_field_data` (what planning_fields'
        # generic cf reader prefers -- see distribution.apply_rack_power_override).
        self.assertEqual(seen["cf"].get("power_limitation"), 8000)
        self.assertEqual(seen["custom_field_data"].get("power_limitation"), 8000)
        self.assertEqual(seen["cf"].get("pdu_location"), "top")
        self.assertEqual(seen["custom_field_data"].get("pdu_location"), "top")

    def test_apply_rack_power_override_patches_custom_field_data_too(self):
        """Direct unit test of the fix: ``apply_rack_power_override`` must
        refresh BOTH ``.cf`` and ``.custom_field_data`` on the in-memory rack,
        for either rack kind, so a reader using either access pattern sees the
        same effective values. Never persisted."""
        DesignRackPower.objects.create(
            design=self.design, planned_rack=self.planned_rack,
            power_config={"custom_fields": {"power_limitation": 5000}},
        )
        elevation = project_rack(self.design, self.planned_rack)
        apply_rack_power_override(elevation)
        self.assertEqual(elevation.rack.cf.get("power_limitation"), 5000)
        self.assertEqual(elevation.rack.custom_field_data.get("power_limitation"), 5000)

    # --- regression: a real rack's DesignRackPower override is unaffected --

    def test_real_rack_override_unchanged(self):
        DesignRackPower.objects.create(
            design=self.design, rack=self.racks[1],
            power_config={"custom_fields": {"power_limitation": 3000}},
        )
        elevation = project_rack(self.design, self.racks[1])
        apply_rack_power_override(elevation)
        self.assertEqual(elevation.rack.cf.get("power_limitation"), 3000)
        self.assertEqual(elevation.rack.custom_field_data.get("power_limitation"), 3000)
