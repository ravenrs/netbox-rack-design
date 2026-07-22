"""
Tests for the power-distribution loader (``netbox_rack_design.distribution``).

Covers the two modes (none / script), the graceful fallback contract (empty /
unimportable / non-callable path, or a raising script -> ``None`` + warning), the
normalized consumer list handed to a script, and that the whole path is
read-only (no dcim writes).

The reference *algorithm* (distribution_example) is tested separately in
``test_distribution_example.py``; here we only exercise the plumbing.
"""

from dcim.choices import PowerFeedPhaseChoices
from dcim.models import (
    Cable,
    Device,
    DeviceRole,
    DeviceType,
    PowerFeed,
    PowerOutlet,
    PowerOutletTemplate,
    PowerPanel,
    PowerPort,
)
from django.test import TestCase, override_settings
from utilities.testing import create_test_device

from ..choices import DesignPlacementKindChoices
from ..distribution import (
    DEFAULT_DISTRIBUTION_MODE,
    apply_rack_power_override,
    build_native,
    devices_from_elevation,
    generate_distribution,
)
from ..models import Design, DesignPlacement, DesignPowerFeed, DesignRackPower
from ..projection import project_rack
from .utils import create_dcim_environment

# Sentinel object a script can hand back so we can assert it is returned verbatim.
SENTINEL_DISTRIBUTION = {"scheme": "test", "pdus": {}, "rack": {}}


def sample_distribution_fn(rack, devices):
    """Module-level callable for ``script`` mode (must be importable). Records the
    arguments it was called with so tests can inspect the normalized inputs."""
    sample_distribution_fn.calls.append((rack, devices))
    return SENTINEL_DISTRIBUTION


sample_distribution_fn.calls = []


not_callable_value = "I am a string, not a function"


def raising_distribution_fn(rack, devices):
    """Module-level callable that always raises, to exercise the runtime-error
    fallback in ``script`` mode."""
    raise RuntimeError("boom")


def _plugins_config(**overrides):
    """Build a PLUGINS_CONFIG dict for the plugin with distribution overrides."""
    cfg = {
        "distribution_mode": "none",
        "distribution_script": "",
    }
    cfg.update(overrides)
    return {"netbox_rack_design": cfg}


_FN = "netbox_rack_design.tests.test_distribution.sample_distribution_fn"
_RAISING = "netbox_rack_design.tests.test_distribution.raising_distribution_fn"
_NOT_CALLABLE = "netbox_rack_design.tests.test_distribution.not_callable_value"


class DistributionLoaderTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]
        cls.device_role = env["device_role"]
        cls.devices = env["devices"]

        cls.design = Design.objects.create(title="DC-Build", site=cls.site)
        # One planned add so the elevation has at least one drawing consumer.
        cls.p_add = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            device_role=cls.device_role,
            target_rack=cls.racks[0],
            target_position=10,
            target_face="front",
            proposed_name="planned-sw1",
        )

    def setUp(self):
        sample_distribution_fn.calls = []

    def _elevation(self):
        return project_rack(self.design, self.racks[0])

    # --- default / none mode ----------------------------------------------

    def test_default_mode_is_none(self):
        self.assertEqual(DEFAULT_DISTRIBUTION_MODE, "none")

    @override_settings(PLUGINS_CONFIG=_plugins_config(distribution_mode="none"))
    def test_none_mode_returns_none(self):
        self.assertIsNone(generate_distribution(self._elevation()))

    @override_settings(PLUGINS_CONFIG=_plugins_config(distribution_mode="none"))
    def test_unknown_mode_returns_none(self):
        # Any non-"script" mode is treated as none.
        self.assertIsNone(generate_distribution(self._elevation(), mode="bogus"))

    # --- script mode: happy path ------------------------------------------

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(distribution_mode="script", distribution_script=_FN)
    )
    def test_script_mode_returns_script_result(self):
        elevation = self._elevation()  # this itself invokes the script (Phase 2)
        sample_distribution_fn.calls = []
        result = generate_distribution(elevation)
        self.assertIs(result, SENTINEL_DISTRIBUTION)
        self.assertEqual(len(sample_distribution_fn.calls), 1)

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(distribution_mode="none", distribution_script=_FN)
    )
    def test_mode_override_forces_script(self):
        # An explicit mode arg overrides the configured mode.
        result = generate_distribution(self._elevation(), mode="script")
        self.assertIs(result, SENTINEL_DISTRIBUTION)

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(distribution_mode="script", distribution_script=_FN)
    )
    def test_script_receives_rack_and_consumer_list(self):
        elevation = self._elevation()
        generate_distribution(elevation)
        rack, devices = sample_distribution_fn.calls[0]
        self.assertEqual(rack, self.racks[0])
        # The planned add appears as a normalized consumer entry.
        names = {d["name"] for d in devices}
        self.assertIn("planned-sw1", names)
        entry = next(d for d in devices if d["name"] == "planned-sw1")
        self.assertEqual(entry["status"], "planned")
        self.assertEqual(entry["role"], "role-1")
        self.assertEqual(entry["face"], "front")
        self.assertIsNone(entry["device"])  # an add has no real device yet
        self.assertEqual(entry["device_type"], self.device_type)
        self.assertIn("draw_w", entry)
        self.assertIn("draw_known", entry)

    # --- script mode: graceful fallback -----------------------------------

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(distribution_mode="script", distribution_script="")
    )
    def test_empty_script_path_falls_back_to_none(self):
        with self.assertLogs("netbox_rack_design.distribution", level="WARNING"):
            self.assertIsNone(generate_distribution(self._elevation()))

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            distribution_mode="script",
            distribution_script="netbox_rack_design.does_not_exist.fn",
        )
    )
    def test_unimportable_script_falls_back_to_none(self):
        with self.assertLogs("netbox_rack_design.distribution", level="WARNING"):
            self.assertIsNone(generate_distribution(self._elevation()))

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            distribution_mode="script", distribution_script=_NOT_CALLABLE
        )
    )
    def test_non_callable_script_falls_back_to_none(self):
        with self.assertLogs("netbox_rack_design.distribution", level="WARNING"):
            self.assertIsNone(generate_distribution(self._elevation()))

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            distribution_mode="script", distribution_script=_RAISING
        )
    )
    def test_raising_script_falls_back_to_none(self):
        with self.assertLogs("netbox_rack_design.distribution", level="WARNING"):
            self.assertIsNone(generate_distribution(self._elevation()))

    # --- devices_from_elevation directly ----------------------------------

    def test_devices_from_elevation_dedupes_and_normalizes(self):
        devices = devices_from_elevation(self._elevation())
        # Exactly one entry per planned consumer (the add), no duplicates.
        keys = [(d["name"], d["face"]) for d in devices]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all("draw_w" in d and "power_ports" in d for d in devices))

    # --- project_rack integration (Phase 2) -------------------------------

    @override_settings(PLUGINS_CONFIG=_plugins_config(distribution_mode="none"))
    def test_project_rack_distribution_none_in_none_mode(self):
        # The power bundle always carries a "distribution" key; None in none mode.
        elevation = self._elevation()
        self.assertIn("distribution", elevation.power)
        self.assertIsNone(elevation.power["distribution"])

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(distribution_mode="script", distribution_script=_FN)
    )
    def test_project_rack_attaches_script_distribution(self):
        elevation = self._elevation()
        self.assertIs(elevation.power["distribution"], SENTINEL_DISTRIBUTION)


class PlannedPduPowerConfigTestCase(TestCase):
    """A planned PDU add's persisted ``power_config`` rides along on its
    ``devices_from_elevation`` entry (Phase C task 1)."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]

        cls.pdu_role = DeviceRole.objects.create(name="PDU", slug="pdu")
        cls.design = Design.objects.create(title="DC-Build", site=cls.site)
        # power_config is now the MANUAL cf bridge only -- no inline feed.
        cls.power_config = {
            "source": "manual",
            "custom_fields": {"pdu_scheme": "2x1PH2Banks"},
        }
        cls.p_pdu_add = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            device_role=cls.pdu_role,
            target_rack=cls.racks[0],
            target_position=42,
            target_face="front",
            proposed_name="planned-pdu-a1",
            power_config=cls.power_config,
        )

    def test_planned_pdu_entry_carries_power_config(self):
        elevation = project_rack(self.design, self.racks[0])
        devices = devices_from_elevation(elevation)
        entry = next(d for d in devices if d["name"] == "planned-pdu-a1")
        self.assertEqual(entry["role"], "pdu")
        self.assertIsNone(entry["device"])
        self.assertEqual(entry["device_type"], self.device_type)
        self.assertEqual(entry["power_config"], self.power_config)

    def test_manual_custom_fields_resolved_on_entry(self):
        # The source-agnostic ``custom_fields`` = the manual power_config cf.
        elevation = project_rack(self.design, self.racks[0])
        devices = devices_from_elevation(elevation)
        entry = next(d for d in devices if d["name"] == "planned-pdu-a1")
        self.assertEqual(entry["custom_fields"], {"pdu_scheme": "2x1PH2Banks"})

    def test_custom_fields_read_live_from_power_source_device(self):
        # A planned PDU that REFERENCES a real PDU inherits that device's cf live
        # (no manual power_config), resolved onto the entry's ``custom_fields``.
        source = create_test_device(
            "src-pdu-b1", site=self.site, rack=self.racks[0], position=None, face="",
        )
        referenced = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            device_role=self.pdu_role,
            target_rack=self.racks[0],
            target_position=40,
            target_face="front",
            proposed_name="planned-pdu-b1",
            power_source_device=source,
        )
        self.addCleanup(referenced.delete)
        elevation = project_rack(self.design, self.racks[0])
        devices = devices_from_elevation(elevation)
        entry = next(d for d in devices if d["name"] == "planned-pdu-b1")
        self.assertEqual(entry["custom_fields"], dict(source.cf))

    def test_real_device_entry_has_no_power_config(self):
        # A plain existing/added non-PDU entry (no placement.power_config) reads
        # None -- confirms the key is always present and doesn't break existing
        # devices.
        elevation = project_rack(self.design, self.racks[0])
        devices = devices_from_elevation(elevation)
        existing = next(d for d in devices if d["name"] == "Device 1")
        self.assertIsNone(existing["power_config"])


class ApplyRackPowerOverrideTestCase(TestCase):
    """``apply_rack_power_override`` merges ``DesignRackPower.power_config``
    over the in-memory rack's ``cf`` without persisting anything (Phase C task
    2)."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.design = Design.objects.create(title="DC-Build", site=cls.site)
        cls.rack_power = DesignRackPower.objects.create(
            design=cls.design,
            rack=cls.racks[0],
            power_config={
                "source": "manual",
                "custom_fields": {"power_limitation": 8000, "pdu_location": "top"},
            },
        )

    def _elevation(self):
        return project_rack(self.design, self.racks[0])

    def test_override_merges_custom_fields_over_rack_cf(self):
        elevation = self._elevation()
        original_cf = dict(elevation.rack.cf)
        apply_rack_power_override(elevation)
        merged = elevation.rack.cf
        self.assertEqual(merged.get("power_limitation"), 8000)
        self.assertEqual(merged.get("pdu_location"), "top")
        # Any other cf keys the rack already had are preserved.
        for key, value in original_cf.items():
            if key not in ("power_limitation", "pdu_location"):
                self.assertEqual(merged.get(key), value)

    def test_override_does_not_persist_to_db(self):
        elevation = self._elevation()
        apply_rack_power_override(elevation)
        self.assertEqual(elevation.rack.cf.get("power_limitation"), 8000)
        from dcim.models import Rack

        reloaded = Rack.objects.get(pk=self.racks[0].pk)
        self.assertNotEqual(reloaded.cf.get("power_limitation"), 8000)
        self.assertNotEqual(reloaded.cf.get("pdu_location"), "top")

    def test_no_override_is_a_noop(self):
        # A rack with no DesignRackPower row: rack.cf is untouched.
        elevation = project_rack(self.design, self.racks[1])
        original_cf = dict(elevation.rack.cf)
        apply_rack_power_override(elevation)
        self.assertEqual(elevation.rack.cf, original_cf)

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(distribution_mode="script", distribution_script=_FN)
    )
    def test_script_mode_sees_effective_rack_cf(self):
        # Reached via generate_distribution() in "script" mode: the script's
        # `rack` argument reflects the merged cf.
        elevation = self._elevation()
        sample_distribution_fn.calls = []
        generate_distribution(elevation)
        rack, _devices = sample_distribution_fn.calls[0]
        self.assertEqual(rack.cf.get("power_limitation"), 8000)
        self.assertEqual(rack.cf.get("pdu_location"), "top")


class BuildNativeTestCase(TestCase):
    """``distribution_mode = "builtin"`` -- the base (announced) distribution
    builder (docs/pdu-distribution-spec.md §0/§2): bank from the outlet port
    name, feed/leg from the binding (real cabled ``PowerFeed`` or bound
    ``DesignPowerFeed``), no config/script required."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.manufacturer = env["manufacturer"]
        cls.racks = env["racks"]

        cls.pdu_role = DeviceRole.objects.create(name="PDU", slug="pdu")
        cls.pdu_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer, model="PDU Type", slug="pdu-type", u_height=0,
        )
        cls.planned_pdu_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer, model="Planned PDU Type",
            slug="planned-pdu-type", u_height=0,
        )
        PowerOutletTemplate.objects.create(device_type=cls.planned_pdu_type, name="1/1")
        PowerOutletTemplate.objects.create(device_type=cls.planned_pdu_type, name="2/1")

        cls.power_panel = PowerPanel.objects.create(site=cls.site, name="Panel 1")
        cls.feed_a = PowerFeed.objects.create(
            power_panel=cls.power_panel, name="Feed A", voltage=230, amperage=32,
            phase=PowerFeedPhaseChoices.PHASE_SINGLE,
        )
        cls.feed_b = PowerFeed.objects.create(
            power_panel=cls.power_panel, name="Feed B", voltage=230, amperage=32,
            phase=PowerFeedPhaseChoices.PHASE_SINGLE,
        )

        cls.design = Design.objects.create(title="DC-Build", site=cls.site)

    @classmethod
    def _make_real_pdu(cls, name, feed=None, rack=None):
        pdu = Device.objects.create(
            name=name, device_type=cls.pdu_type, site=cls.site,
            rack=rack or cls.racks[0], role=cls.pdu_role, status="active",
        )
        PowerOutlet.objects.create(device=pdu, name="1/1")
        PowerOutlet.objects.create(device=pdu, name="2/1")
        if feed is not None:
            power_port = PowerPort.objects.create(device=pdu, name="Input")
            Cable(a_terminations=[power_port], b_terminations=[feed]).save()
        return pdu

    def _planned_pdu_placement(self, name, *, real_power_feed=None, planned_power_feed=None,
                                rack=None):
        return DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.planned_pdu_type,
            device_role=self.pdu_role,
            target_rack=rack or self.racks[0],
            target_position=None,
            proposed_name=name,
            real_power_feed=real_power_feed,
            planned_power_feed=planned_power_feed,
        )

    def _elevation(self, rack=None):
        return project_rack(self.design, rack or self.racks[0])

    # --- builtin mode / none mode plumbing ----------------------------------

    def test_builtin_mode_returns_distribution_for_cabled_real_pdu(self):
        self._make_real_pdu("rack1-pdu-1", feed=self.feed_a)
        dist = generate_distribution(self._elevation(), mode="builtin")
        self.assertIsNotNone(dist)
        self.assertIn("rack1-pdu-1", dist["pdus"])
        # Exact Distribution shape (docs/pdu-distribution-spec.md §3).
        self.assertEqual(
            set(dist), {"scheme", "pdu_location", "pdus", "rack"},
        )
        entry = dist["pdus"]["rack1-pdu-1"]
        self.assertEqual(
            set(entry),
            {"feed_name", "feed_letter", "feed_source", "phase",
             "allocated_draw", "power_bank_count", "banks"},
        )
        bank = next(iter(entry["banks"].values()))
        self.assertEqual(
            set(bank),
            {"max_power", "allocated_power", "planned_power", "util_pct",
             "state", "units", "devices"},
        )
        self.assertEqual(
            set(dist["rack"]),
            {"power_limitation_w", "power_consumption_w", "alarm", "warnings"},
        )

    def test_none_mode_still_returns_none(self):
        self._make_real_pdu("rack1-pdu-1", feed=self.feed_a)
        self.assertIsNone(generate_distribution(self._elevation(), mode="none"))

    # --- feed model & binding ------------------------------------------------

    def test_real_pdu_cabled_to_feed_sizes_from_it(self):
        self._make_real_pdu("rack1-pdu-1", feed=self.feed_a)
        dist = build_native(self.racks[0], devices_from_elevation(self._elevation()))
        entry = dist["pdus"]["rack1-pdu-1"]
        self.assertEqual(entry["feed_source"], "real")
        self.assertEqual(entry["allocated_draw"], 230 * 32)

    def test_planned_pdu_bound_to_design_power_feed_sizes_from_it(self):
        planned_feed = DesignPowerFeed.objects.create(
            design=self.design, rack=self.racks[0], name="Feed P",
            voltage=230, amperage=16, phase=PowerFeedPhaseChoices.PHASE_SINGLE,
        )
        self._planned_pdu_placement("planned-pdu-p", planned_power_feed=planned_feed)
        elevation = self._elevation()
        dist = build_native(self.racks[0], devices_from_elevation(elevation))
        self.assertIsNotNone(dist)
        entry = dist["pdus"]["planned-pdu-p"]
        self.assertEqual(entry["feed_source"], "planned")
        self.assertEqual(entry["allocated_draw"], 230 * 16)

    def test_planned_pdu_bound_to_real_feed_sizes_from_it(self):
        self._planned_pdu_placement("planned-pdu-r", real_power_feed=self.feed_a)
        elevation = self._elevation()
        dist = build_native(self.racks[0], devices_from_elevation(elevation))
        entry = dist["pdus"]["planned-pdu-r"]
        self.assertEqual(entry["feed_source"], "real")
        self.assertEqual(entry["allocated_draw"], 230 * 32)

    def test_unbound_planned_pdu_degrades_cleanly(self):
        # No real_power_feed / planned_power_feed set at all.
        self._planned_pdu_placement("planned-pdu-unbound")
        elevation = self._elevation()
        # Must not raise; the only PDU is unresolvable -> no distribution.
        dist = build_native(self.racks[0], devices_from_elevation(elevation))
        self.assertIsNone(dist)

    def test_unbound_planned_pdu_omitted_alongside_a_resolvable_one(self):
        self._make_real_pdu("rack1-pdu-1", feed=self.feed_a)
        self._planned_pdu_placement("planned-pdu-unbound")
        elevation = self._elevation()
        dist = build_native(self.racks[0], devices_from_elevation(elevation))
        self.assertIsNotNone(dist)
        self.assertIn("rack1-pdu-1", dist["pdus"])
        self.assertNotIn("planned-pdu-unbound", dist["pdus"])

    # --- leg comes from the binding, not the PDU name -------------------------

    def test_two_feeds_produce_two_legs(self):
        self._planned_pdu_placement("pdu-on-a", real_power_feed=self.feed_a)
        self._planned_pdu_placement("pdu-on-b", real_power_feed=self.feed_b)
        elevation = self._elevation()
        dist = build_native(self.racks[0], devices_from_elevation(elevation))
        letter_a = dist["pdus"]["pdu-on-a"]["feed_letter"]
        letter_b = dist["pdus"]["pdu-on-b"]["feed_letter"]
        self.assertNotEqual(letter_a, letter_b)
        self.assertEqual({letter_a, letter_b}, {"a", "b"})

    def test_one_feed_produces_one_leg(self):
        self._planned_pdu_placement("pdu-1-on-a", real_power_feed=self.feed_a)
        self._planned_pdu_placement("pdu-2-on-a", real_power_feed=self.feed_a)
        elevation = self._elevation()
        dist = build_native(self.racks[0], devices_from_elevation(elevation))
        self.assertEqual(
            dist["pdus"]["pdu-1-on-a"]["feed_letter"],
            dist["pdus"]["pdu-2-on-a"]["feed_letter"],
        )

    # --- bank comes from the outlet port name ---------------------------------

    def test_bank_parsed_from_outlet_port_name(self):
        pdu = self._make_real_pdu("rack1-pdu-2", feed=self.feed_a)
        PowerOutlet.objects.create(device=pdu, name="5/1")
        dist = build_native(self.racks[0], devices_from_elevation(self._elevation()))
        self.assertEqual(set(dist["pdus"]["rack1-pdu-2"]["banks"]), {"1", "2", "5"})

    # --- apply_rack_power_override wiring -------------------------------------

    def test_rack_power_override_takes_effect_in_builtin_mode(self):
        self._make_real_pdu("rack1-pdu-1", feed=self.feed_a)
        DesignRackPower.objects.create(
            design=self.design, rack=self.racks[0],
            power_config={"custom_fields": {"pdu_location": "top"}},
        )
        dist = generate_distribution(self._elevation(), mode="builtin")
        self.assertIsNotNone(dist)
        self.assertEqual(dist["pdu_location"], "top")
