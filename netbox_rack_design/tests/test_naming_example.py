"""
Tests for the shipped, stock-runnable example naming script
(``netbox_rack_design.naming_example``).

These pin the two patterns the example demonstrates -- a flat family counter and
A/B phase-paired PDU slots -- including the crucial "count unsaved same-session
siblings" behaviour that keeps consecutive palette drops from colliding.
"""

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site
from django.test import TestCase, override_settings

from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignPlacement
from ..naming import generate_name
from ..naming_example import build_name


def _plugins_config():
    return {
        "netbox_rack_design": {
            "naming_mode": "script",
            "naming_script": "netbox_rack_design.naming_example.build_name",
        }
    }


class NamingExampleTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="AMS1", slug="ams1")
        mfr = Manufacturer.objects.create(name="Generic", slug="generic")
        cls.rack = Rack.objects.create(name="R42", site=cls.site)
        cls.sw_type = DeviceType.objects.create(
            manufacturer=mfr, model="Switch X", slug="switch-x", u_height=1)
        cls.pdu_type = DeviceType.objects.create(
            manufacturer=mfr, model="PDU X", slug="pdu-x", u_height=0)
        cls.sw_role = DeviceRole.objects.create(name="Leaf Switch", slug="leaf-switch")
        cls.pdu_role = DeviceRole.objects.create(name="PDU", slug="pdu")

        cls.design = Design.objects.create(title="Build-1", site=cls.site)

    def _add(self, device_type, device_role, **extra):
        """An UNSAVED add placement (pk=None) -- the shape the preview API builds."""
        return DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=device_type,
            device_role=device_role,
            target_rack=self.rack,
            target_position=extra.get("position", 10),
            target_face=extra.get("face", "front"),
        )

    # --- flat family counter ----------------------------------------------

    def test_switch_first_in_family(self):
        p = self._add(self.sw_type, self.sw_role)
        self.assertEqual(build_name(p), "ams1-leaf-switch-1")

    def test_switch_continues_from_existing_device(self):
        # A real device already occupies -1 -> the next is -2.
        Device.objects.create(
            name="ams1-leaf-switch-1", device_type=self.sw_type, role=self.sw_role,
            site=self.site, status="active")
        p = self._add(self.sw_type, self.sw_role)
        self.assertEqual(build_name(p), "ams1-leaf-switch-2")

    def test_switch_counts_pending_siblings(self):
        # Two unsaved same-session adds must not collide: the second counts the
        # first's proposed name (as the preview API injects it).
        p = self._add(self.sw_type, self.sw_role)
        p._rd_pending_names = ["ams1-leaf-switch-1", "ams1-leaf-switch-2"]
        self.assertEqual(build_name(p), "ams1-leaf-switch-3")

    def test_no_role_falls_back_to_device_type_slug(self):
        # A palette add with no role selected still gets a clean, single-dash
        # name from the device-type slug -- never an empty segment.
        p = self._add(self.sw_type, None)
        self.assertEqual(build_name(p), "ams1-switch-x-1")

    # --- PDU phase pairs ---------------------------------------------------

    def test_pdu_phase_pairs_sequence(self):
        p = self._add(self.pdu_type, self.pdu_role, position=None)
        self.assertEqual(build_name(p), "ams1-pdu-rr42-a1")
        # Simulate the a1..b2 fill via injected pending siblings.
        for pending, expect in (
            (["ams1-pdu-rr42-a1"], "ams1-pdu-rr42-b1"),
            (["ams1-pdu-rr42-a1", "ams1-pdu-rr42-b1"], "ams1-pdu-rr42-a2"),
            (["ams1-pdu-rr42-a1", "ams1-pdu-rr42-b1", "ams1-pdu-rr42-a2"],
             "ams1-pdu-rr42-b2"),
        ):
            p._rd_pending_names = pending
            self.assertEqual(build_name(p), expect)

    def test_pdu_continues_from_existing_devices(self):
        # Real a1/b1 already exist -> the next PDU is a2, not a1.
        for nm in ("ams1-pdu-rr42-a1", "ams1-pdu-rr42-b1"):
            Device.objects.create(
                name=nm, device_type=self.pdu_type, role=self.pdu_role,
                site=self.site, status="active")
        p = self._add(self.pdu_type, self.pdu_role, position=None)
        self.assertEqual(build_name(p), "ams1-pdu-rr42-a2")

    # --- through the engine (script mode end-to-end) -----------------------

    @override_settings(PLUGINS_CONFIG=_plugins_config())
    def test_engine_script_mode_uses_example(self):
        p = self._add(self.sw_type, self.sw_role)
        self.assertEqual(generate_name(p), "ams1-leaf-switch-1")

    # --- chain-aware counting (PLAN-design-chains.md Sec 3.4) --------------

    def test_family_continues_past_an_ancestor_designs_names(self):
        """The example's counter is chain-aware too: a design baselined on an
        APPROVED design continues its families instead of restarting them, and
        the ancestor's row is matched by the name it SETTLES to (its stored
        name carries that design's planning prefix)."""
        base = Design.objects.create(
            title="Base IDS-1000", site=self.site,
            status=DesignStatusChoices.STATUS_APPROVED,
        )
        child = Design.objects.create(
            title="Child IDS-2000", site=self.site, based_on=base,
        )
        DesignPlacement.objects.create(
            design=base,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.sw_type,
            device_role=self.sw_role,
            target_rack=self.rack,
            target_position=20,
            target_face="front",
            proposed_name="IDS-1000_ams1-leaf-switch-4",
        )
        p = self._add(self.sw_type, self.sw_role)
        p.design = child
        self.assertEqual(build_name(p), "ams1-leaf-switch-5")
        # A design of its own still starts at 1 -- nothing leaks sideways.
        self.assertEqual(build_name(self._add(self.sw_type, self.sw_role)),
                         "ams1-leaf-switch-1")
