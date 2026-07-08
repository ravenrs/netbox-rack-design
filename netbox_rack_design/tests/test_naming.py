"""
Tests for the naming-convention engine (``netbox_rack_design.naming``).

Covers the three modes (sequence / template / script), the dotted-path template
context for both an *add* (placement-backed proxy) and a *move/remove* (real
dcim.Device), safe traversal of missing/blank attributes, ordinal ordering, and
the read-only collision check (which must perform NO dcim writes).
"""

from dcim.models import Device
from django.test import TestCase, override_settings

from ..choices import DesignPlacementKindChoices
from ..models import Design, DesignPlacement
from ..naming import (
    generate_name,
    name_exists_in_site,
    placement_ordinal,
)
from .utils import create_dcim_environment


def sample_naming_fn(placement):
    """Module-level callable used to exercise ``script`` mode (must be importable)."""
    return f"script:{placement.pk}"


not_callable_value = "I am a string, not a function"


def _plugins_config(**overrides):
    """Build a PLUGINS_CONFIG dict for the plugin with the given naming overrides."""
    cfg = {
        "naming_mode": "sequence",
        "naming_template": "{design.name}-{n}",
        "naming_script": "",
    }
    cfg.update(overrides)
    return {"netbox_rack_design": cfg}


class NamingEngineTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]
        cls.device_role = env["device_role"]
        cls.tenant = env["tenant"]
        cls.devices = env["devices"]

        cls.design = Design.objects.create(title="DC-Build", site=cls.site)

        # Three placements with ascending target positions -> deterministic order.
        cls.p_add = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            device_role=cls.device_role,
            tenant=cls.tenant,
            target_rack=cls.racks[1],
            target_position=10,
            target_face="front",
            proposed_name="planned-sw1",
        )
        cls.p_move = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.devices[0],
            target_rack=cls.racks[1],
            target_position=20,
        )
        cls.p_remove = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=cls.devices[1],
        )

    # --- ordinals ----------------------------------------------------------

    def test_placement_ordinal_ordering(self):
        # Ordered by Meta.ordering = (design, target_position, pk). The remove
        # placement has target_position=None, which sorts first under NULLS.
        ordered = list(self.design.placements.values_list("pk", flat=True))
        self.assertEqual(
            [placement_ordinal(p) for p in (self.p_remove, self.p_add, self.p_move)],
            [ordered.index(self.p_remove.pk) + 1,
             ordered.index(self.p_add.pk) + 1,
             ordered.index(self.p_move.pk) + 1],
        )
        # Ordinals are a contiguous 1..N permutation.
        self.assertEqual(
            sorted(placement_ordinal(p) for p in (self.p_add, self.p_move, self.p_remove)),
            [1, 2, 3],
        )

    # --- sequence mode -----------------------------------------------------

    @override_settings(PLUGINS_CONFIG=_plugins_config(naming_mode="sequence"))
    def test_sequence_mode(self):
        for p in (self.p_add, self.p_move, self.p_remove):
            self.assertEqual(generate_name(p), f"DC-Build-{placement_ordinal(p)}")

    @override_settings(PLUGINS_CONFIG=_plugins_config(naming_mode="sequence"))
    def test_sequence_mode_explicit_index(self):
        # An explicit index bypasses the ordinal query.
        self.assertEqual(generate_name(self.p_add, index=7), "DC-Build-7")

    # --- template mode -----------------------------------------------------

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template", naming_template="{design.name}-{n}"
        )
    )
    def test_template_design_name_alias(self):
        # {design.name} resolves to the design title.
        self.assertEqual(generate_name(self.p_add, index=1), "DC-Build-1")

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template", naming_template="{design.title}-{design.site.name}"
        )
    )
    def test_template_dotted_design_paths(self):
        self.assertEqual(generate_name(self.p_add), "DC-Build-Site 1")

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template",
            naming_template="{device.site.name}-{device.device_type.model}-{n}",
        )
    )
    def test_template_dotted_device_paths_for_add(self):
        # The add proxy resolves device.* from the placement.
        self.assertEqual(
            generate_name(self.p_add, index=3),
            "Site 1-Device Type 1-3",
        )

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template",
            naming_template="{device.site.name}-{device.rack.name}-{device.device_type.model}-{n}",
        )
    )
    def test_template_dotted_device_paths_for_move(self):
        # A real dcim.Device resolves the same dotted paths.
        self.assertEqual(
            generate_name(self.p_move, index=2),
            "Site 1-Rack 1-Device Type 1-2",
        )

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template",
            naming_template="{device.role.name}",
        )
    )
    def test_template_role_for_add_and_move(self):
        # add proxy -> placement.device_role; move -> real device.role
        self.assertEqual(generate_name(self.p_add), "Role 1")
        self.assertEqual(generate_name(self.p_move), "Device Role 1")

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template",
            naming_template="[{device.tenant.name}]-{n}",
        )
    )
    def test_template_blank_attribute_yields_empty_string(self):
        # The move device has no tenant -> {device.tenant.name} -> "" (no raise).
        self.assertEqual(generate_name(self.p_move, index=5), "[]-5")

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template",
            naming_template="{device.bogus.attr}-{design.nope}-{n}",
        )
    )
    def test_template_missing_attribute_never_raises(self):
        # Entirely unknown attribute paths render empty rather than raising.
        self.assertEqual(generate_name(self.p_add, index=9), "--9")

    # --- script mode -------------------------------------------------------

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="script",
            naming_script="netbox_rack_design.tests.test_naming.sample_naming_fn",
        )
    )
    def test_script_mode(self):
        self.assertEqual(generate_name(self.p_add), f"script:{self.p_add.pk}")

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(naming_mode="script", naming_script="")
    )
    def test_script_mode_empty_path_raises(self):
        with self.assertRaises(ValueError):
            generate_name(self.p_add)

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="script", naming_script="no.such.module.fn"
        )
    )
    def test_script_mode_bad_path_raises(self):
        with self.assertRaises(ValueError):
            generate_name(self.p_add)

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="script",
            naming_script="netbox_rack_design.tests.test_naming.not_callable_value",
        )
    )
    def test_script_mode_not_callable_raises(self):
        with self.assertRaises(ValueError):
            generate_name(self.p_add)

    # --- collision check ---------------------------------------------------

    def test_name_exists_vs_real_device(self):
        # "Device 1" is a real device in the site.
        self.assertTrue(name_exists_in_site("Device 1", self.site))
        self.assertFalse(name_exists_in_site("nope-not-here", self.site))

    def test_name_exists_vs_other_placement_proposed_name(self):
        # p_add.proposed_name == "planned-sw1" lives in this design's site.
        self.assertTrue(name_exists_in_site("planned-sw1", self.site))
        # Excluding the owning placement makes it invisible to the check.
        self.assertFalse(
            name_exists_in_site("planned-sw1", self.site, exclude_placement=self.p_add)
        )

    def test_name_exists_blank_and_none_inputs(self):
        self.assertFalse(name_exists_in_site("", self.site))
        self.assertFalse(name_exists_in_site("Device 1", None))

    def test_collision_check_does_no_writes(self):
        # The engine must never mutate dcim: device count is unchanged after a
        # full pass of name generation + collision checks.
        before = Device.objects.count()
        for p in (self.p_add, self.p_move, self.p_remove):
            generate_name(p)
            name_exists_in_site(p.proposed_name or "x", self.site, exclude_placement=p)
        self.assertEqual(Device.objects.count(), before)
