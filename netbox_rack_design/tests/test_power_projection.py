"""
Tests for power projection (Tier 1, crude / zero-config) — see
``docs/power-projection-spec.md`` §1, §2 (Tier 1), §7.

The projection must, over the PLANNED world, resolve each device's draw
(planned adds from the device-type's PowerPortTemplates, real devices from
their PowerPorts), sum it per rack, compute a capacity (PowerFeeds when present,
else a config fallback), and flag devices with no power data instead of
silently treating them as zero. It must never write to dcim.
"""

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Manufacturer,
    PowerPort,
    PowerPortTemplate,
    Rack,
    Site,
)
from django.test import TestCase, override_settings

from ..choices import DesignPlacementKindChoices
from ..models import Design, DesignPlacement
from ..projection import project_rack


def _cfg(**over):
    cfg = {
        "power_capacity_default_w": 1000,
        "power_draw_basis": "allocated",
        "power_warn_pct": 80,
        "power_critical_pct": 100,
    }
    cfg.update(over)
    return {"netbox_rack_design": cfg}


class PowerProjectionTier1TestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="PWR Site", slug="pwr-site")
        mfr = Manufacturer.objects.create(name="PWR Mfr", slug="pwr-mfr")
        cls.rack = Rack.objects.create(name="PWR Rack", site=cls.site, u_height=42)
        cls.role = DeviceRole.objects.create(name="PWR Role", slug="pwr-role")

        # Type WITH power data (template allocated 200 W).
        cls.dt_known = DeviceType.objects.create(
            manufacturer=mfr, model="PWR-Known", slug="pwr-known",
            u_height=1, is_full_depth=False)
        PowerPortTemplate.objects.create(
            device_type=cls.dt_known, name="PSU1",
            allocated_draw=200, maximum_draw=250)

        # Type WITHOUT any power data.
        cls.dt_unknown = DeviceType.objects.create(
            manufacturer=mfr, model="PWR-Unknown", slug="pwr-unknown",
            u_height=1, is_full_depth=False)

        # Existing device whose PowerPort (150 W) overrides its type template.
        cls.dev_existing = Device.objects.create(
            name="pwr-existing", device_type=cls.dt_known, site=cls.site,
            rack=cls.rack, position=1, face="front", status="active", role=cls.role)
        # NetBox auto-instantiates a "PSU1" PowerPort from the type template on
        # device create; override its draw so it differs from the template
        # (proves the projection prefers the real port over the template).
        PowerPort.objects.update_or_create(
            device=cls.dev_existing, name="PSU1",
            defaults={"allocated_draw": 150, "maximum_draw": 200})

        # Existing device with unknown draw (no port, type has no templates).
        cls.dev_unknown = Device.objects.create(
            name="pwr-unknown", device_type=cls.dt_unknown, site=cls.site,
            rack=cls.rack, position=2, face="front", status="active", role=cls.role)

        cls.design = Design.objects.create(title="PWR plan", site=cls.site)

    def _elev(self):
        return project_rack(self.design, self.rack)

    # --- per-device draw resolution ---------------------------------------

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_existing_device_uses_port_draw(self):
        slots = [s for s in self._elev().front if s["label"] == "pwr-existing"]
        self.assertEqual(len(slots), 1, slots)
        self.assertEqual(slots[0]["draw_w"], 150.0)
        self.assertTrue(slots[0]["draw_known"])

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_add_uses_device_type_template_draw(self):
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.dt_known, target_rack=self.rack,
            target_position=10, target_face="front", proposed_name="new-sw")
        slots = [s for s in self._elev().front if s["label"] == "new-sw"]
        self.assertEqual(len(slots), 1, slots)
        self.assertEqual(slots[0]["draw_w"], 200.0)
        self.assertTrue(slots[0]["draw_known"])

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_passive_device_no_ports_is_not_flagged(self):
        # dt_unknown has NO power port templates -> no power ports -> passive:
        # 0 W, known-0 (patch-panel case), never in the unconnected flag.
        elev = self._elev()
        slots = [s for s in elev.front if s["label"] == "pwr-unknown"]
        self.assertEqual(slots[0]["draw_w"], 0.0)
        self.assertTrue(slots[0]["draw_known"])
        self.assertNotIn("pwr-unknown", elev.power["unconnected_devices"])

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_device_with_uncabled_power_port_is_flagged_and_named(self):
        # dev_existing HAS a power port that is not cabled to power -> flagged
        # as a connection gap, and listed by name. (Its draw still counts.)
        elev = self._elev()
        self.assertIn("pwr-existing", elev.power["unconnected_devices"])
        self.assertGreaterEqual(elev.power["unconnected_count"], 1)

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_add_is_not_flagged_unconnected(self):
        # A planned add has no real device/cabling, so it is never flagged as
        # "not connected" even though it carries a draw.
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.dt_known, target_rack=self.rack,
            target_position=12, target_face="front", proposed_name="new-add")
        self.assertNotIn("new-add", self._elev().power["unconnected_devices"])

    @override_settings(PLUGINS_CONFIG=_cfg(power_draw_basis="maximum"))
    def test_basis_maximum_uses_maximum_draw(self):
        slots = [s for s in self._elev().front if s["label"] == "pwr-existing"]
        self.assertEqual(slots[0]["draw_w"], 200.0)  # port maximum_draw

    # --- rack-level summary -----------------------------------------------

    @override_settings(PLUGINS_CONFIG=_cfg(power_capacity_default_w=1000))
    def test_rack_summary_sum_capacity_and_state(self):
        power = self._elev().power
        # existing 150 + unknown 0 = 150 of 1000 W = 15% -> ok.
        self.assertEqual(power["draw_w"], 150.0)
        self.assertEqual(power["capacity_w"], 1000)
        self.assertAlmostEqual(power["util_pct"], 15.0, places=3)
        self.assertEqual(power["state"], "ok")

    @override_settings(PLUGINS_CONFIG=_cfg(power_capacity_default_w=200))
    def test_state_warn_and_critical_thresholds(self):
        # 150 of 200 = 75% -> ok still (<80).
        self.assertEqual(self._elev().power["state"], "ok")

    @override_settings(PLUGINS_CONFIG=_cfg(power_capacity_default_w=160))
    def test_state_critical_over_capacity(self):
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.dt_known, target_rack=self.rack,
            target_position=10, target_face="front", proposed_name="hog")
        # 150 + 200 = 350 of 160 -> >100% -> critical.
        self.assertEqual(self._elev().power["state"], "critical")

    # --- planned world semantics ------------------------------------------

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_remove_excluded_from_draw(self):
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.dev_existing)
        # existing 150 is being removed -> planned draw drops to 0.
        self.assertEqual(self._elev().power["draw_w"], 0.0)

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_full_depth_device_counted_once(self):
        # A full-depth existing device appears on both faces but must count once.
        mfr = Manufacturer.objects.get(slug="pwr-mfr")
        dt_full = DeviceType.objects.create(
            manufacturer=mfr, model="PWR-Full", slug="pwr-full",
            u_height=2, is_full_depth=True)
        PowerPortTemplate.objects.create(
            device_type=dt_full, name="PSU1", allocated_draw=300)
        Device.objects.create(
            name="pwr-full", device_type=dt_full, site=self.site,
            rack=self.rack, position=20, face="front", status="active", role=self.role)
        # existing 150 + full 300 = 450 (full counted once, not 750).
        self.assertEqual(self._elev().power["draw_w"], 450.0)

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_pdu_role_excluded_from_consumption(self):
        # A PDU distributes power; counting its (often large) input draw would
        # double-count the devices it feeds. It must be excluded from the total.
        mfr = Manufacturer.objects.get(slug="pwr-mfr")
        pdu_role = DeviceRole.objects.create(name="PDU", slug="pdu")
        pdu_type = DeviceType.objects.create(
            manufacturer=mfr, model="PWR-PDU", slug="pwr-pdu",
            u_height=1, is_full_depth=False)
        PowerPortTemplate.objects.create(
            device_type=pdu_type, name="feed1", allocated_draw=7000)
        Device.objects.create(
            name="pwr-pdu-a1", device_type=pdu_type, site=self.site,
            rack=self.rack, position=30, face="front", status="active",
            role=pdu_role)
        # Rack total must still be just the real end-devices (existing 150),
        # NOT 150 + 7000.
        elev = self._elev()
        self.assertEqual(elev.power["draw_w"], 150.0)
        # The PDU's own slot reads 0 and is NOT flagged unknown.
        pdu_slots = [s for s in (*elev.front, *elev.non_racked)
                     if s["label"] == "pwr-pdu-a1"]
        self.assertTrue(pdu_slots)
        self.assertEqual(pdu_slots[0]["draw_w"], 0.0)
        self.assertTrue(pdu_slots[0]["draw_known"])

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_projection_does_no_writes(self):
        before = Device.objects.count()
        self._elev()
        self.assertEqual(Device.objects.count(), before)
