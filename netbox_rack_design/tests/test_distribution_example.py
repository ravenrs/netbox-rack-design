"""
Tests for the shipped reference distribution script
(``netbox_rack_design.distribution_example.build``).

Phase E: the reference script now runs the **same** algorithm as ``distribution_
mode = "builtin"`` (``distribution.build_native``) over the shared helpers
(``distribution._collect_pdus`` and friends) -- bank from the outlet PORT name
(``"<bank>/<port>"``), feed-leg from the PDU's *bound* feed (a real
``dcim.PowerFeed`` or a planned ``DesignPowerFeed``), never PDU-name parsing.
Custom fields (``pdu_location``, ``power_limitation``) are read generically via
the ``planning_fields`` config bridge (docs/pdu-distribution-spec.md Sec 5)
through ``distribution_example.read_planning_field(s)``, so this file also
proves a site's arbitrary cf name reaches the script through that bridge.

The ``devices`` argument is built as the normalized consumer dicts the loader
would hand over (see ``distribution.devices_from_elevation``), so the algorithm
is exercised directly without a full projection, except in the end-to-end test.
"""

import json

from core.models import ObjectType
from dcim.choices import PowerFeedPhaseChoices
from dcim.models import (
    Cable,
    Device,
    DeviceRole,
    DeviceType,
    Manufacturer,
    PowerFeed,
    PowerOutlet,
    PowerOutletTemplate,
    PowerPanel,
    PowerPort,
    PowerPortTemplate,
    Rack,
    Site,
)
from django.core.serializers.json import DjangoJSONEncoder
from django.test import TestCase, override_settings
from extras.models import CustomField

from ..choices import DesignPlacementKindChoices
from ..distribution import devices_from_elevation
from ..distribution_example import build, read_planning_field, read_planning_fields
from ..models import Design, DesignPlacement, DesignPowerFeed
from ..projection import project_rack


def _script_cfg(**planning_fields):
    cfg = {
        "distribution_mode": "script",
        "distribution_script": "netbox_rack_design.distribution_example.build",
    }
    if planning_fields:
        cfg["planning_fields"] = planning_fields
    return {"netbox_rack_design": cfg}


def _consumer(name, u, draw, psus, status="active", role="server", known=True):
    """A normalized consumer entry (matches distribution.devices_from_elevation)."""
    return {
        "name": name,
        "role": role,
        "status": status,
        "u_position": u,
        "face": "front",
        "draw_w": draw,
        "draw_known": known,
        "power_ports": [{"name": f"ps{i}", "draw": 0, "connected": None}
                        for i in range(1, psus + 1)],
        "device": None,
        "device_type": None,
    }


class DistributionExampleTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="D Site", slug="d-site")
        mfr = Manufacturer.objects.create(name="D Mfr", slug="d-mfr")
        cls.pdu_type = DeviceType.objects.create(
            manufacturer=mfr, model="D PDU", slug="d-pdu", u_height=0)
        cls.pdu_role = DeviceRole.objects.create(name="PDU", slug="pdu")

        # Custom fields the "cf read through planning_fields" test maps.
        rack_ot = ObjectType.objects.get_for_model(Rack)
        for cf_name in ("my_pdu_side", "my_power_cap"):
            cf = CustomField.objects.create(name=cf_name, type="text", required=False)
            cf.object_types.set([rack_ot])

        cls.rack = Rack.objects.create(name="D Rack", site=cls.site, u_height=10)

        cls.power_panel = PowerPanel.objects.create(site=cls.site, name="D Panel")
        # Two real feeds -- the redundant legs. 230V x 32A = 7360W input breaker
        # -> 3680W per bank (2 banks).
        cls.feed_a = PowerFeed.objects.create(
            power_panel=cls.power_panel, name="Feed A", voltage=230, amperage=32,
            phase=PowerFeedPhaseChoices.PHASE_SINGLE,
        )
        cls.feed_b = PowerFeed.objects.create(
            power_panel=cls.power_panel, name="Feed B", voltage=230, amperage=32,
            phase=PowerFeedPhaseChoices.PHASE_SINGLE,
        )

        cls.design = Design.objects.create(title="D Plan", site=cls.site)

        # Two REAL PDUs cabled to the two feeds -- feed/leg comes from the
        # binding (the native cable path), not from the PDU's name.
        cls.pdu_a = cls._make_real_pdu("d-pdu-r1-1", cls.feed_a)
        cls.pdu_b = cls._make_real_pdu("d-pdu-r1-2", cls.feed_b)

    @classmethod
    def _make_real_pdu(cls, name, feed):
        pdu = Device.objects.create(
            name=name, device_type=cls.pdu_type, site=cls.site, rack=cls.rack,
            role=cls.pdu_role, status="active")
        PowerOutlet.objects.create(device=pdu, name="1/1")
        PowerOutlet.objects.create(device=pdu, name="2/1")
        power_port = PowerPort.objects.create(device=pdu, name="Input")
        Cable(a_terminations=[power_port], b_terminations=[feed]).save()
        return pdu

    def _set_cf(self, **cf):
        for k, v in cf.items():
            self.rack.custom_field_data[k] = v
        self.rack.save()
        self.rack.refresh_from_db()
        # Rack.cf is a cached_property; drop the cache so a re-set value is seen.
        self.rack.__dict__.pop("cf", None)

    # --- topology parsing (feed from binding, bank from outlet name) -------

    def test_no_pdus_returns_none(self):
        empty = Rack.objects.create(name="Empty", site=self.site, u_height=10)
        self.assertIsNone(build(empty, []))

    def test_parses_feed_from_binding_and_banks_from_outlet_name(self):
        dist = build(self.rack, [])
        self.assertEqual(set(dist["pdus"]), {"d-pdu-r1-1", "d-pdu-r1-2"})
        a = dist["pdus"]["d-pdu-r1-1"]
        self.assertEqual(a["feed_source"], "real")
        self.assertEqual(a["power_bank_count"], 2)
        self.assertEqual(set(a["banks"]), {"1", "2"})
        self.assertEqual(a["banks"]["1"]["max_power"], 3680)  # 230*32 / 2
        self.assertEqual(a["allocated_draw"], 230 * 32)
        # Two distinct feeds -> two distinct legs (never split by PDU name).
        b = dist["pdus"]["d-pdu-r1-2"]
        self.assertNotEqual(a["feed_letter"], b["feed_letter"])

    # --- planned PDU bound to a planned DesignPowerFeed --------------------

    def test_planned_pdu_bound_to_design_power_feed_has_right_breaker(self):
        planned_feed = DesignPowerFeed.objects.create(
            design=self.design, rack=self.rack, name="Feed P",
            voltage=230, amperage=16, phase=PowerFeedPhaseChoices.PHASE_SINGLE,
        )
        planned_type = DeviceType.objects.create(
            manufacturer=self.pdu_type.manufacturer, model="Planned PDU",
            slug="planned-pdu", u_height=0)
        PowerOutletTemplate.objects.create(device_type=planned_type, name="1/1")
        PowerOutletTemplate.objects.create(device_type=planned_type, name="2/1")
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=planned_type, device_role=self.pdu_role,
            target_rack=self.rack, target_position=None,
            proposed_name="planned-pdu-p", planned_power_feed=planned_feed,
        )
        elevation = project_rack(self.design, self.rack)
        dist = build(self.rack, devices_from_elevation(elevation))
        self.assertIn("planned-pdu-p", dist["pdus"])
        entry = dist["pdus"]["planned-pdu-p"]
        self.assertEqual(entry["feed_source"], "planned")
        self.assertEqual(entry["allocated_draw"], 230 * 16)
        self.assertEqual(entry["banks"]["1"]["max_power"], int(230 * 16 / 2))
        # Real PDUs untouched alongside the planned one.
        self.assertIn("d-pdu-r1-1", dist["pdus"])
        self.assertIn("d-pdu-r1-2", dist["pdus"])

    # --- charging (device on the PDU's units charges the right bank) -------

    def test_two_psu_device_charges_both_legs_full(self):
        self._set_cf(my_pdu_side="bottom")
        # Unit 2 -> bank 1 (bottom: units 1-5 = bank 1). 2 PSUs -> both legs.
        dist = build(self.rack, [_consumer("srv", 2, 500, psus=2)])
        self.assertEqual(dist["pdus"]["d-pdu-r1-1"]["banks"]["1"]["allocated_power"], 500)
        self.assertEqual(dist["pdus"]["d-pdu-r1-2"]["banks"]["1"]["allocated_power"], 500)

    def test_single_psu_device_charges_one_leg_only(self):
        dist = build(self.rack, [_consumer("srv1", 8, 300, psus=1, status="planned")])
        # Default direction (bottom, no cf set): unit 8 -> bank 2 (units 6-10).
        self.assertEqual(dist["pdus"]["d-pdu-r1-1"]["banks"]["2"]["planned_power"], 300)
        self.assertEqual(dist["pdus"]["d-pdu-r1-2"]["banks"]["2"]["planned_power"], 0)

    def test_unknown_draw_not_charged(self):
        dist = build(self.rack, [_consumer("mystery", 2, 0, psus=2, known=False)])
        self.assertEqual(dist["pdus"]["d-pdu-r1-1"]["banks"]["1"]["allocated_power"], 0)

    def test_pdu_role_consumer_is_skipped(self):
        dist = build(self.rack, [_consumer("a-pdu", 2, 999, psus=2, role="pdu")])
        self.assertEqual(dist["pdus"]["d-pdu-r1-1"]["banks"]["1"]["allocated_power"], 0)

    def test_cabled_device_charges_via_outlet_bank(self):
        """A real device with a PowerPort cabled to a PDU's outlet is charged
        to that outlet's bank directly -- no U-position lookup needed."""
        srv_type = DeviceType.objects.create(
            manufacturer=self.pdu_type.manufacturer, model="Srv Cabled",
            slug="srv-cabled", u_height=1, is_full_depth=False)
        srv_role = DeviceRole.objects.create(name="Server", slug="server")
        srv = Device.objects.create(
            name="cabled-srv", device_type=srv_type, site=self.site,
            rack=self.rack, position=9, face="front", status="active",
            role=srv_role)
        pp = PowerPort.objects.create(device=srv, name="PSU1", allocated_draw=250)
        outlet = PowerOutlet.objects.get(device=self.pdu_a, name="2/1")
        Cable(a_terminations=[pp], b_terminations=[outlet]).save()

        entry = {
            "name": "cabled-srv", "role": "server", "status": "active",
            "u_position": 9, "face": "front", "draw_w": 250.0, "draw_known": True,
            "power_ports": [{"name": "PSU1", "draw": 250, "connected": "2/1"}],
            "device": srv, "device_type": srv_type,
        }
        dist = build(self.rack, [entry])
        # Bank 2 on pdu_a, regardless of U-position based bank slicing.
        self.assertEqual(dist["pdus"]["d-pdu-r1-1"]["banks"]["2"]["allocated_power"], 250)

    # --- pdu_location (via planning_fields) flips the unit->bank direction -

    def test_default_direction_is_bottom_with_no_planning_fields_configured(self):
        # No cf set at all, no planning_fields config -> defaults to "bottom".
        dist = build(self.rack, [_consumer("srv", 3, 400, psus=2)])
        self.assertEqual(dist["pdu_location"], "bottom")
        self.assertEqual(dist["pdus"]["d-pdu-r1-1"]["banks"]["1"]["allocated_power"], 400)

    @override_settings(PLUGINS_CONFIG=_script_cfg(rack=[
        {"key": "pdu_location", "label": "PDU location", "type": "choice",
         "choices": ["top", "bottom"], "source": "cf.my_pdu_side"},
    ]))
    def test_pdu_location_flips_bank_for_a_unit_via_planning_fields(self):
        # Unit 3: bottom -> bank 1 (units 1-5); top -> bank 2 (reversed 10..1).
        self._set_cf(my_pdu_side="bottom")
        bottom = build(self.rack, [_consumer("srv", 3, 400, psus=2)])
        self.assertEqual(bottom["pdus"]["d-pdu-r1-1"]["banks"]["1"]["allocated_power"], 400)
        self.assertEqual(bottom["pdus"]["d-pdu-r1-1"]["banks"]["2"]["allocated_power"], 0)

        self._set_cf(my_pdu_side="top")
        top = build(self.rack, [_consumer("srv", 3, 400, psus=2)])
        self.assertEqual(top["pdus"]["d-pdu-r1-1"]["banks"]["2"]["allocated_power"], 400)
        self.assertEqual(top["pdus"]["d-pdu-r1-1"]["banks"]["1"]["allocated_power"], 0)

    # --- breaker overload + rack limitation (limitation via planning_fields) -

    def test_overload_flags_alarm(self):
        # 4000 W on a 3680 W bank -> overload + alarm.
        dist = build(self.rack, [_consumer("hog", 2, 4000, psus=2)])
        bank = dist["pdus"]["d-pdu-r1-1"]["banks"]["1"]
        self.assertEqual(bank["state"], "overload")
        self.assertTrue(dist["rack"]["alarm"])
        self.assertTrue(any("exceeds breaker" in w for w in dist["rack"]["warnings"]))

    @override_settings(PLUGINS_CONFIG=_script_cfg(rack=[
        {"key": "power_limitation", "label": "Power limitation (W)",
         "type": "number", "source": "cf.my_power_cap"},
    ]))
    def test_power_limitation_via_planning_fields_flags_alarm(self):
        self._set_cf(my_power_cap="0.5")  # 500 W cap
        # 400 W on each of two legs -> 800 W rack total > 500 W cap.
        dist = build(self.rack, [_consumer("srv", 2, 400, psus=2)])
        self.assertTrue(dist["rack"]["alarm"])
        self.assertTrue(any("power limitation" in w for w in dist["rack"]["warnings"]))

    def test_util_pct_computed(self):
        dist = build(self.rack, [_consumer("srv", 2, 1840, psus=2)])  # 50% of 3680
        self.assertEqual(dist["pdus"]["d-pdu-r1-1"]["banks"]["1"]["util_pct"], 50.0)

    # --- the planning_fields config-bridge resolver itself ------------------

    def test_read_planning_field_reads_custom_field_by_dotted_path(self):
        self._set_cf(my_power_cap="1.5")
        value = read_planning_field(
            {"key": "power_limitation", "source": "cf.my_power_cap"},
            "cf.my_power_cap", self.rack,
        )
        self.assertEqual(value, "1.5")

    def test_read_planning_field_missing_cf_returns_none(self):
        value = read_planning_field({}, "cf.does_not_exist", self.rack)
        self.assertIsNone(value)

    def test_read_planning_field_empty_source_returns_none(self):
        self.assertIsNone(read_planning_field({}, "", self.rack))

    @override_settings(PLUGINS_CONFIG=_script_cfg(rack=[
        {"key": "power_limitation", "label": "Power limitation (W)",
         "type": "number", "source": "cf.my_power_cap"},
        {"key": "pdu_location", "label": "PDU location", "type": "choice",
         "choices": ["top", "bottom"], "source": "cf.my_pdu_side"},
    ]))
    def test_read_planning_fields_maps_custom_cf_names_to_generic_keys(self):
        # A site's arbitrary cf names ("my_power_cap"/"my_pdu_side") reach the
        # script under the algorithm's generic keys, via the config bridge --
        # this file never hardcodes the site's cf name.
        self._set_cf(my_power_cap="2.0", my_pdu_side="top")
        mapped = read_planning_fields("rack", self.rack)
        self.assertEqual(mapped, {"power_limitation": "2.0", "pdu_location": "top"})

    def test_read_planning_fields_empty_schema_returns_empty_dict(self):
        # Default plugin config: planning_fields = {} -> no mapped keys at all.
        self.assertEqual(read_planning_fields("rack", self.rack), {})
        self.assertEqual(read_planning_fields("pdu", self.rack), {})

    # --- end-to-end through project_rack + JSON delivery -------------------

    @override_settings(PLUGINS_CONFIG=_script_cfg(rack=[
        {"key": "pdu_location", "label": "PDU location", "type": "choice",
         "choices": ["top", "bottom"], "source": "cf.my_pdu_side"},
    ]))
    def test_project_rack_script_mode_end_to_end_and_json_serializable(self):
        """The full path: project_rack in script mode attaches the example
        script's Distribution, and it survives json_script serialization (the
        DjangoJSONEncoder the template uses -- catches non-serializable values
        like a Decimal u_position leaking into a bank's device line)."""
        self._set_cf(my_pdu_side="bottom")
        srv_type = DeviceType.objects.create(
            manufacturer=self.pdu_type.manufacturer, model="Srv", slug="srv",
            u_height=1, is_full_depth=False)
        for i in (1, 2):
            PowerPortTemplate.objects.create(
                device_type=srv_type, name=f"PSU{i}", allocated_draw=400)
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=srv_type, target_rack=self.rack,
            target_position=3, target_face="front", proposed_name="new-srv")

        elevation = project_rack(self.design, self.rack)
        dist = elevation.power["distribution"]
        self.assertIsNotNone(dist)
        self.assertIn("d-pdu-r1-1", dist["pdus"])
        # Device draw = sum of its two 400 W PSU templates = 800 W; charged full
        # to each of both legs' bank 1 (planned, unit 3, bottom).
        self.assertEqual(
            dist["pdus"]["d-pdu-r1-1"]["banks"]["1"]["planned_power"], 800.0)
        self.assertEqual(
            dist["pdus"]["d-pdu-r1-2"]["banks"]["1"]["planned_power"], 800.0)
        # Must serialize exactly as the template's {{ ...|json_script }} does.
        blob = json.dumps(dist, cls=DjangoJSONEncoder)
        self.assertIn("new-srv", blob)
