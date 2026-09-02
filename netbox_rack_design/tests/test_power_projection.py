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
    PowerOutletTemplate,
    PowerPort,
    PowerPortTemplate,
    Rack,
    Site,
)
from django.test import TestCase, override_settings

from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignPlacement, DesignPowerFeed
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
    def test_powered_device_without_draw_is_unknown_and_named(self):
        # A device that HAS a power port but no draw value (spec §1.3): counted
        # as 0 W but FLAGGED in the unknown-draw tally -- distinct from the
        # unconnected (cabling-gap) flag, so the UI can say WHICH powered devices
        # lack draw data rather than silently under-reporting.
        mfr = Manufacturer.objects.get(slug="pwr-mfr")
        dt = DeviceType.objects.create(
            manufacturer=mfr, model="PWR-Ports-No-Draw",
            slug="pwr-ports-no-draw", u_height=1, is_full_depth=False)
        PowerPortTemplate.objects.create(device_type=dt, name="PSU1")  # no draw
        Device.objects.create(
            name="pwr-nodraw", device_type=dt, site=self.site, rack=self.rack,
            position=20, face="front", status="active", role=self.role)
        elev = self._elev()
        slots = [s for s in elev.front if s["label"] == "pwr-nodraw"]
        self.assertEqual(slots[0]["draw_w"], 0.0)
        self.assertFalse(slots[0]["draw_known"])
        self.assertIn("pwr-nodraw", elev.power["unknown_devices"])
        self.assertGreaterEqual(elev.power["unknown_draw_count"], 1)
        # Passive gear (no power ports) is a known 0 and stays OUT of the tally.
        self.assertNotIn("pwr-unknown", elev.power["unknown_devices"])

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

    # --- capacity from PLANNED feeds (greenfield rack) ---------------------

    def _planned_feed(self, name, *, voltage=230, amperage=32):
        return DesignPowerFeed.objects.create(
            design=self.design, rack=self.rack, name=name,
            voltage=voltage, amperage=amperage,
        )

    @override_settings(PLUGINS_CONFIG=_cfg(power_capacity_default_w=1000))
    def test_planned_feeds_supply_capacity_on_a_greenfield_rack(self):
        """A rack with no real PowerFeeds but with PLANNED feeds must size its
        capacity from those, not from the flat fallback.

        The greenfield flow (docs/pdu-distribution-spec.md §6.1) has the user
        define DesignPowerFeeds and bind planned PDUs to them; leaving the bar on
        the 1000 W default then painted a planned rack critical-red while its own
        banks read comfortably green -- the two power views contradicting each
        other on the same screen.
        """
        self._planned_feed("Feed A")
        self._planned_feed("Feed B")
        power = self._elev().power
        # 2 x (230 V x 32 A) x 80% max utilization = 11776 W, mirroring what
        # dcim.PowerFeed.available_power computes for the same electricals.
        self.assertEqual(power["capacity_w"], 11776)
        self.assertEqual(power["draw_w"], 150.0)
        self.assertEqual(power["state"], "ok")

    @override_settings(PLUGINS_CONFIG=_cfg(power_capacity_default_w=1000))
    def test_three_phase_planned_feed_uses_the_phase_rate(self):
        self._planned_feed("Feed A", voltage=400, amperage=16)
        DesignPowerFeed.objects.filter(design=self.design).update(phase="three-phase")
        # breaker_watts = round(400 x 16 x 1.732) = 11085 W, derated 80% = 8868 W.
        self.assertEqual(self._elev().power["capacity_w"], 8868)

    @override_settings(PLUGINS_CONFIG=_cfg(power_capacity_default_w=1000))
    def test_planned_feed_of_another_design_does_not_count(self):
        other = Design.objects.create(title="PWR other", site=self.site)
        DesignPowerFeed.objects.create(
            design=other, rack=self.rack, name="Feed A", voltage=230, amperage=32)
        # This design plans no feeds -> still the flat fallback.
        self.assertEqual(self._elev().power["capacity_w"], 1000)

    @override_settings(PLUGINS_CONFIG=_cfg(power_capacity_default_w=1000))
    def test_no_feeds_at_all_still_falls_back_to_the_configured_default(self):
        self.assertEqual(self._elev().power["capacity_w"], 1000)

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


class BayPowerTestCase(TestCase):
    """Power for a chassis and the blades in its bays.

    Rule (mirrors what core does structurally): the chassis's own draw WINS --
    its PSUs are what the PDU actually feeds, and the blades hang off its
    outlets, so counting both double-counts. When the chassis has no resolvable
    draw the blades are rolled up instead, so a chassis modelled without PSUs
    still reports the load it carries.
    """

    @classmethod
    def setUpTestData(cls):
        from dcim.choices import SubdeviceRoleChoices
        from dcim.models import DeviceBayTemplate

        cls.site = Site.objects.create(name="Bay PWR", slug="bay-pwr")
        mfr = Manufacturer.objects.create(name="Bay Mfr", slug="bay-mfr")
        cls.rack = Rack.objects.create(name="Bay Rack", site=cls.site, u_height=42)
        cls.role = DeviceRole.objects.create(name="Bay Role", slug="bay-role")

        # A chassis type WITH its own PSU draw (600 W), 2 bays.
        cls.chassis_psu = DeviceType.objects.create(
            manufacturer=mfr, model="Chassis-PSU", slug="chassis-psu",
            u_height=2, is_full_depth=False,
            subdevice_role=SubdeviceRoleChoices.ROLE_PARENT)
        PowerPortTemplate.objects.create(
            device_type=cls.chassis_psu, name="PSU1", allocated_draw=600)
        DeviceBayTemplate.objects.create(device_type=cls.chassis_psu, name="b1")
        DeviceBayTemplate.objects.create(device_type=cls.chassis_psu, name="b2")

        # A chassis type with NO power data at all, 2 bays.
        cls.chassis_bare = DeviceType.objects.create(
            manufacturer=mfr, model="Chassis-Bare", slug="chassis-bare",
            u_height=2, is_full_depth=False,
            subdevice_role=SubdeviceRoleChoices.ROLE_PARENT)
        DeviceBayTemplate.objects.create(device_type=cls.chassis_bare, name="c1")
        DeviceBayTemplate.objects.create(device_type=cls.chassis_bare, name="c2")

        # A blade type drawing 150 W.
        cls.blade_type = DeviceType.objects.create(
            manufacturer=mfr, model="Blade-PWR", slug="blade-pwr",
            u_height=0, subdevice_role=SubdeviceRoleChoices.ROLE_CHILD)
        PowerPortTemplate.objects.create(
            device_type=cls.blade_type, name="PSU1", allocated_draw=150)

        cls.design = Design.objects.create(title="Bay power", site=cls.site)

    def _chassis(self, device_type, position):
        return Device.objects.create(
            name=f"chassis-{position}", device_type=device_type, site=self.site,
            rack=self.rack, position=position, face="front", status="active",
            role=self.role)

    def _blade_into(self, bay, name):
        blade = Device.objects.create(
            name=name, device_type=self.blade_type, site=self.site,
            rack=self.rack, position=None, status="active", role=self.role)
        bay.installed_device = blade
        bay.save()
        return blade

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_chassis_with_its_own_draw_wins_over_its_blades(self):
        chassis = self._chassis(self.chassis_psu, 10)
        self._blade_into(chassis.devicebays.get(name="b1"), "blade-1")
        self._blade_into(chassis.devicebays.get(name="b2"), "blade-2")

        elev = project_rack(self.design, self.rack)
        slot = next(s for s in elev.front if s["label"] == chassis.name)
        # 600 W from the chassis PSU -- NOT 600 + 150 + 150.
        self.assertEqual(slot["draw_w"], 600.0)
        self.assertEqual(elev.power["draw_w"], 600.0)
        # the blades still report their own figure, flagged as already counted
        self.assertTrue(all(b["draw_included_in_parent"] for b in slot["bays"]))
        self.assertEqual(slot["bays"][0]["draw_w"], 150.0)

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_chassis_without_draw_rolls_its_blades_up(self):
        chassis = self._chassis(self.chassis_bare, 20)
        self._blade_into(chassis.devicebays.get(name="c1"), "blade-3")
        self._blade_into(chassis.devicebays.get(name="c2"), "blade-4")

        elev = project_rack(self.design, self.rack)
        slot = next(s for s in elev.front if s["label"] == chassis.name)
        self.assertEqual(slot["draw_w"], 300.0)
        self.assertTrue(slot["draw_known"])
        self.assertEqual(elev.power["draw_w"], 300.0)
        self.assertFalse(any(b["draw_included_in_parent"] for b in slot["bays"]))

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_planned_blade_counts_toward_the_rack(self):
        """A blade planned into a bay of a draw-less chassis is load that will
        exist once applied, so it must appear in the projected total."""
        chassis = self._chassis(self.chassis_bare, 30)
        bay = chassis.devicebays.get(name="c1")
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.blade_type, target_rack=self.rack,
            target_bay=bay, target_bay_name="c1", proposed_name="planned-blade")

        elev = project_rack(self.design, self.rack)
        slot = next(s for s in elev.front if s["label"] == chassis.name)
        self.assertEqual(slot["draw_w"], 150.0)
        self.assertEqual(elev.power["draw_w"], 150.0)

    @override_settings(PLUGINS_CONFIG=_cfg())
    def test_empty_draw_less_chassis_draws_nothing(self):
        self._chassis(self.chassis_bare, 40)
        elev = project_rack(self.design, self.rack)
        slot = next(s for s in elev.front if s["label"] == "chassis-40")
        self.assertEqual(slot["draw_w"], 0.0)


class ChainCapacityTestCase(TestCase):
    """PLAN-design-chains.md G5 item 1: rack capacity across a design chain.

    ``_rack_capacity_w`` must count an approved ancestor's planned feeds, not
    just this design's own -- the same all-or-nothing rule the placement
    replay uses (§9.2): a non-approved/implemented ancestor, or a broken
    lineage, contributes nothing and the projection reports the refusal as a
    conflict instead of a plausible number.
    """

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="PWR Chain Site", slug="pwr-chain-site")
        cls.rack = Rack.objects.create(name="PWR Chain Rack", site=cls.site, u_height=42)

    def _design(self, title, *, based_on=None):
        return Design.objects.create(title=title, site=self.site, based_on=based_on)

    def _approve(self, design):
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()
        return design

    def _feed(self, design, name, **kw):
        kw.setdefault("voltage", 230)
        kw.setdefault("amperage", 32)
        return DesignPowerFeed.objects.create(design=design, rack=self.rack, name=name, **kw)

    @override_settings(PLUGINS_CONFIG=_cfg(power_capacity_default_w=1000))
    def test_ancestor_approved_feed_raises_child_capacity(self):
        a = self._design("Network sweep IDS-1000")
        self._feed(a, "Feed A")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        power = project_rack(b, self.rack).power
        # 230V x 32A x 80% max utilization = 5888 W (mirrors PowerFeed.available_power).
        self.assertEqual(power["capacity_w"], 5888)

    @override_settings(PLUGINS_CONFIG=_cfg(power_capacity_default_w=1000))
    def test_refused_chain_contributes_no_capacity_and_a_conflict(self):
        a = self._design("Network sweep IDS-1000")
        self._feed(a, "Feed A")
        # `a` stays draft -- never approved.
        b = self._design("Server build IDS-2000", based_on=a)

        elev = project_rack(b, self.rack)
        # The ancestor's feed must NOT count: falls back to the flat default.
        self.assertEqual(elev.power["capacity_w"], 1000)
        self.assertIn("ancestor_not_approved", [c["kind"] for c in elev.conflicts])

    @override_settings(PLUGINS_CONFIG=_cfg(power_capacity_default_w=1000))
    def test_implemented_ancestor_contributes_no_capacity_and_a_conflict(self):
        a = self._design("Network sweep IDS-1000")
        self._feed(a, "Feed A")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)
        a.status = DesignStatusChoices.STATUS_IMPLEMENTED
        a.save()

        elev = project_rack(b, self.rack)
        self.assertEqual(elev.power["capacity_w"], 1000)
        self.assertIn("ancestor_implemented", [c["kind"] for c in elev.conflicts])

    @override_settings(PLUGINS_CONFIG=_cfg(power_capacity_default_w=1000))
    def test_own_and_ancestor_feed_both_count_without_double_counting(self):
        a = self._design("Network sweep IDS-1000")
        self._feed(a, "Feed A")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)
        self._feed(b, "Feed B")

        power = project_rack(b, self.rack).power
        # 2 x 5888 W: the ancestor's feed and the child's own feed are two
        # DIFFERENT rows -- each must be counted exactly once.
        self.assertEqual(power["capacity_w"], 11776)

    @override_settings(PLUGINS_CONFIG=_cfg(power_capacity_default_w=1000))
    def test_three_deep_chain_sums_every_approved_ancestor_feed_once(self):
        a = self._design("Network sweep IDS-1000")
        self._feed(a, "Feed A")
        self._approve(a)
        b = self._design("Storage build IDS-2000", based_on=a)
        self._feed(b, "Feed B")
        self._approve(b)
        c = self._design("Server build IDS-3000", based_on=b)

        power = project_rack(c, self.rack).power
        # A's feed + B's feed, each counted once: 2 x 5888 = 11776.
        self.assertEqual(power["capacity_w"], 11776)

    @override_settings(PLUGINS_CONFIG=_cfg(power_capacity_default_w=1000))
    def test_unchained_design_capacity_is_unchanged(self):
        """Regression guard: a design with no `based_on` projects exactly as
        before -- only its own feeds count, chain-awareness costs nothing."""
        solo = self._design("Solo build IDS-9000")
        self._feed(solo, "Feed A")

        power = project_rack(solo, self.rack).power
        self.assertEqual(power["capacity_w"], 5888)


class ChainDistributionTestCase(TestCase):
    """PLAN-design-chains.md G5 item 4: an inherited planned PDU must
    contribute its banks to the per-bank distribution engine, the same as one
    planned in this design -- it is part of the child's world (§9.2)."""

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="PWR Chain Dist Site", slug="pwr-chain-dist-site")
        cls.rack = Rack.objects.create(name="PWR Chain Dist Rack", site=cls.site, u_height=42)
        mfr = Manufacturer.objects.create(name="PWR Chain Mfr", slug="pwr-chain-mfr")
        cls.pdu_role = DeviceRole.objects.create(name="Chain PDU", slug="pdu")
        cls.pdu_type = DeviceType.objects.create(
            manufacturer=mfr, model="Chain PDU Type", slug="chain-pdu-type", u_height=0,
        )
        PowerOutletTemplate.objects.create(device_type=cls.pdu_type, name="1/1")
        PowerOutletTemplate.objects.create(device_type=cls.pdu_type, name="2/1")

    def _design(self, title, *, based_on=None):
        return Design.objects.create(title=title, site=self.site, based_on=based_on)

    def _approve(self, design):
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()
        return design

    @override_settings(PLUGINS_CONFIG=_cfg(distribution_mode="builtin"))
    def test_inherited_planned_pdu_contributes_its_banks(self):
        a = self._design("Network sweep IDS-1000")
        feed = DesignPowerFeed.objects.create(
            design=a, rack=self.rack, name="Feed A", voltage=230, amperage=32,
        )
        DesignPlacement.objects.create(
            design=a, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.pdu_type, device_role=self.pdu_role,
            target_rack=self.rack, target_position=None,
            proposed_name="chain-pdu-1", planned_power_feed=feed,
        )
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        elev = project_rack(b, self.rack)
        dist = elev.power["distribution"]
        self.assertIsNotNone(dist, elev.power.get("distribution_status"))
        self.assertIn("chain-pdu-1", dist["pdus"])
        entry = dist["pdus"]["chain-pdu-1"]
        self.assertEqual(entry["feed_source"], "planned")
        self.assertEqual(entry["allocated_draw"], 230 * 32)
        self.assertTrue(entry["banks"])
        self.assertEqual(elev.power["draw_w"], 0.0)
