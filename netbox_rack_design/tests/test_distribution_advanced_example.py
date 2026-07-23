"""
Tests for the richer public reference distribution script
(``netbox_rack_design.distribution_advanced_example.build``).

Mirrors ``tests/test_distribution_example.py``'s fixtures (two real PDUs each
bound to a distinct feed, banks from outlet names ``"1/1"``/``"2/1"``), but
exercises the delta this file adds over the minimal example: the computed
``scheme`` topology label, the ``planning_fields["pdu"]`` ``pdu_scheme``
override, and config-driven WARN/CRITICAL thresholds -- while confirming the
shared algorithm (bank assignment/charging) still parities with the minimal
example.
"""

from dcim.choices import PowerFeedPhaseChoices
from dcim.models import (
    Cable,
    Device,
    DeviceRole,
    DeviceType,
    Manufacturer,
    PowerFeed,
    PowerOutlet,
    PowerPanel,
    PowerPort,
    Rack,
    Site,
)
from django.test import TestCase, override_settings

from ..distribution_advanced_example import build


def _script_cfg(**extra):
    cfg = {
        "distribution_mode": "script",
        "distribution_script":
            "netbox_rack_design.distribution_advanced_example.build",
    }
    cfg.update(extra)
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


class DistributionAdvancedExampleTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="DA Site", slug="da-site")
        mfr = Manufacturer.objects.create(name="DA Mfr", slug="da-mfr")
        cls.pdu_type = DeviceType.objects.create(
            manufacturer=mfr, model="DA PDU", slug="da-pdu", u_height=0)
        cls.pdu_role = DeviceRole.objects.create(name="PDU", slug="pdu")

        cls.rack = Rack.objects.create(name="DA Rack", site=cls.site, u_height=10)

        cls.power_panel = PowerPanel.objects.create(site=cls.site, name="DA Panel")
        # Two real feeds -- the redundant legs. 230V x 32A = 7360W input breaker
        # -> 3680W per bank (2 banks each PDU).
        cls.feed_a = PowerFeed.objects.create(
            power_panel=cls.power_panel, name="Feed A", voltage=230, amperage=32,
            phase=PowerFeedPhaseChoices.PHASE_SINGLE,
        )
        cls.feed_b = PowerFeed.objects.create(
            power_panel=cls.power_panel, name="Feed B", voltage=230, amperage=32,
            phase=PowerFeedPhaseChoices.PHASE_SINGLE,
        )

        # Two REAL PDUs, each with two banks (outlets "1/1"/"2/1") -- bank
        # signature "2_2" -> scheme "2x1PH2Banks".
        cls.pdu_a = cls._make_real_pdu("da-pdu-r1-1", cls.feed_a)
        cls.pdu_b = cls._make_real_pdu("da-pdu-r1-2", cls.feed_b)

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

    # --- (a) computed scheme label ------------------------------------------

    def test_scheme_label_computed_from_bank_signature(self):
        dist = build(self.rack, [])
        # Two 2-bank PDUs -> signature "2_2" -> "2x1PH2Banks".
        self.assertEqual(dist["scheme"], "2x1PH2Banks")

    def test_no_pdus_returns_none(self):
        empty = Rack.objects.create(name="DA Empty", site=self.site, u_height=10)
        self.assertIsNone(build(empty, []))

    # --- (b) planning_fields["pdu"] pdu_scheme override ---------------------

    @override_settings(PLUGINS_CONFIG=_script_cfg(planning_fields={
        "pdu": [
            {"key": "pdu_scheme", "label": "PDU topology label", "type": "text",
             "source": "cf.pdu_scheme"},
        ],
    }))
    def test_pdu_scheme_planning_field_overrides_computed_label(self):
        pdu_entry = {
            "name": "da-pdu-r1-1",
            "role": "pdu",
            "device": self.pdu_a,
            "custom_fields": {},
        }
        # No cf set on the real device -> no override, falls back to computed.
        dist = build(self.rack, [pdu_entry])
        self.assertEqual(dist["scheme"], "2x1PH2Banks")

        # Now the PDU entry carries a resolved custom_fields override (as a
        # planned-PDU bridge would populate per spec Sec 6.5.3).
        pdu_entry_with_override = {
            "name": "da-pdu-r1-1",
            "role": "pdu",
            "device": self.pdu_a,
            "custom_fields": {"pdu_scheme": "CustomTopologyX"},
        }
        dist2 = build(self.rack, [pdu_entry_with_override])
        self.assertEqual(dist2["scheme"], "CustomTopologyX")

    def test_no_pdu_scheme_config_falls_back_to_computed_label(self):
        # Default plugin config: planning_fields = {} -> no "pdu" schema at
        # all, so the override lookup returns None regardless of any cf data.
        dist = build(self.rack, [])
        self.assertEqual(dist["scheme"], "2x1PH2Banks")

    # --- (c) config-driven thresholds move a bank's state -------------------

    def test_default_thresholds_flag_warn_at_80_pct(self):
        # 80% of 3680W breaker = 2944W -> "warn" under the default 80/100.
        dist = build(self.rack, [_consumer("srv", 2, 2944, psus=2)])
        self.assertEqual(dist["pdus"]["da-pdu-r1-1"]["banks"]["1"]["state"], "warn")

    @override_settings(PLUGINS_CONFIG=_script_cfg(power_warn_pct=50, power_critical_pct=70))
    def test_configured_thresholds_change_bank_state(self):
        # Same 2944W (80% of 3680W) load -> with warn=50/critical=70 this is
        # now "critical" instead of "warn".
        dist = build(self.rack, [_consumer("srv", 2, 2944, psus=2)])
        self.assertEqual(dist["pdus"]["da-pdu-r1-1"]["banks"]["1"]["state"], "critical")

    # --- (d) parity: still distributes devices to banks correctly ----------

    def test_two_psu_device_charges_both_legs_full(self):
        # Default direction (bottom, no cf): unit 2 -> bank 1 (units 1-5).
        dist = build(self.rack, [_consumer("srv", 2, 500, psus=2)])
        self.assertEqual(dist["pdus"]["da-pdu-r1-1"]["banks"]["1"]["allocated_power"], 500)
        self.assertEqual(dist["pdus"]["da-pdu-r1-2"]["banks"]["1"]["allocated_power"], 500)

    def test_single_psu_device_charges_one_leg_only(self):
        dist = build(self.rack, [_consumer("srv1", 8, 300, psus=1, status="planned")])
        # Default direction (bottom): unit 8 -> bank 2 (units 6-10).
        self.assertEqual(dist["pdus"]["da-pdu-r1-1"]["banks"]["2"]["planned_power"], 300)
        self.assertEqual(dist["pdus"]["da-pdu-r1-2"]["banks"]["2"]["planned_power"], 0)

    def test_overload_flags_alarm(self):
        dist = build(self.rack, [_consumer("hog", 2, 4000, psus=2)])
        bank = dist["pdus"]["da-pdu-r1-1"]["banks"]["1"]
        self.assertEqual(bank["state"], "overload")
        self.assertTrue(dist["rack"]["alarm"])
