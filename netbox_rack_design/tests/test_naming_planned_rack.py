"""
Tests for T3.2b (PLAN-templates.md D29): the naming engine must see a
``PlannedRack``, not silently render a blank rack token.

Before the fix, ``_AddDevicePlaceholderProxy.rack`` /
``_MoveDeviceProxy.rack`` returned ``placement.target_rack`` only. A
placement targeting a ``PlannedRack`` sets ``target_planned_rack`` instead,
so ``.rack`` came back ``None`` -- and nothing crashed, because every mode
degrades a missing rack differently (``_SafeFormatter`` -> "", ``script``
mode -> the sequence fallback, ``sequence`` mode never reads it at all).
These tests pin the FIXED behaviour: a planned-rack placement's
``{device.rack.*}`` tokens resolve exactly like a real-rack placement's.
"""

from dcim.models import Location
from django.test import TestCase, override_settings

from .. import naming_example
from ..choices import DesignPlacementKindChoices
from ..models import Design, DesignPlacement, PlannedRack
from ..naming import generate_name
from .utils import create_dcim_environment


def planned_rack_script_fn(placement):
    """Script-mode probe: returns a name that PROVES the placement's rack was
    read (never the ``_sequence_name`` fallback), so a regression that leaves
    ``.rack`` as ``None`` -- or a script that crashes on it -- is visible
    immediately rather than degrading silently to ``"<design title>-<n>"``.
    """
    return f"rack-seen:{placement.target_planned_rack.name}"


def _plugins_config(**overrides):
    cfg = {
        "naming_mode": "sequence",
        "naming_template": "{device.rack.name}-{n}",
        "naming_script": "",
    }
    cfg.update(overrides)
    return {"netbox_rack_design": cfg}


class NamingPlannedRackTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]
        cls.device_role = env["device_role"]
        cls.tenant = env["tenant"]
        cls.devices = env["devices"]

        cls.location = Location.objects.create(
            site=cls.site, name="Location 1", slug="location-1"
        )
        cls.planned_rack = PlannedRack.objects.create(
            name="Planned Rack 1", u_height=42, location=cls.location,
        )

        cls.design = Design.objects.create(title="DC-Build", site=cls.site)

        # 'add' into a PLANNED rack -- the primary case D29 exists for.
        cls.p_add_planned = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            device_role=cls.device_role,
            tenant=cls.tenant,
            target_planned_rack=cls.planned_rack,
            target_position=10,
            target_face="front",
        )
        # 'add' into a REAL rack -- the regression guard.
        cls.p_add_real = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            device_role=cls.device_role,
            tenant=cls.tenant,
            target_rack=cls.racks[1],
            target_position=11,
            target_face="front",
        )
        # 'move' into a PLANNED rack.
        cls.p_move_planned = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.devices[0],
            target_planned_rack=cls.planned_rack,
            target_position=12,
            target_face="front",
        )

    # --- the failing-before-fix case -----------------------------------

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template", naming_template="{device.rack.name}-{n}"
        )
    )
    def test_template_planned_rack_name_token(self):
        # This is the token that came out blank ("-3") before the fix.
        self.assertEqual(
            generate_name(self.p_add_planned, index=3),
            "Planned Rack 1-3",
        )

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template", naming_template="{device.rack.name}-{n}"
        )
    )
    def test_template_real_rack_name_token_regression(self):
        # A real-rack 'add' must still resolve exactly as before.
        self.assertEqual(
            generate_name(self.p_add_real, index=4),
            "Rack 2-4",
        )

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template",
            naming_template="{device.rack.site.name}",
        )
    )
    def test_template_planned_rack_site_via_location(self):
        # PlannedRack.site is a property that derefs location.site -- prove
        # the template context reaches it through the same dotted path a
        # real dcim.Rack uses.
        self.assertEqual(generate_name(self.p_add_planned), "Site 1")

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template",
            naming_template="[{device.rack.cf.anything}]-{n}",
        )
    )
    def test_template_planned_rack_cf_renders_empty_not_raise(self):
        # A planned rack has no rack custom fields. This must render empty,
        # not raise -- and it must NOT be papered over with an invented
        # fallback value.
        self.assertEqual(generate_name(self.p_add_planned, index=7), "[]-7")

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="script",
            naming_script=(
                "netbox_rack_design.tests.test_naming_planned_rack."
                "planned_rack_script_fn"
            ),
        )
    )
    def test_script_mode_sees_planned_rack_not_fallback(self):
        # Before the fix this couldn't be exercised this way: any script that
        # dereferenced .rack on a real dcim.Device/proxy got None and either
        # crashed (-> sequence fallback) or silently produced a blank rack
        # segment. Here the script reads target_planned_rack directly (as a
        # real deployment script would) to prove the placement itself -- not
        # just the template proxy -- carries a planned rack that resolves.
        self.assertEqual(
            generate_name(self.p_add_planned),
            f"rack-seen:{self.planned_rack.name}",
        )
        # And prove it is NOT the sequence fallback, which would have this
        # shape instead.
        self.assertNotEqual(
            generate_name(self.p_add_planned),
            f"{self.design.title}-{1}",
        )

    @override_settings(PLUGINS_CONFIG=_plugins_config(naming_mode="sequence"))
    def test_sequence_mode_unaffected_by_planned_rack(self):
        # sequence mode never reads .rack at all -- pin that a planned-rack
        # placement gets an ordinary sequence name, unaffected either way.
        self.assertEqual(
            generate_name(self.p_add_planned, index=2), "DC-Build-2"
        )

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template", naming_template="{device.rack.name}-{n}"
        )
    )
    def test_template_move_into_planned_rack(self):
        # _MoveDeviceProxy.rack must resolve the same way as the add proxy.
        self.assertEqual(
            generate_name(self.p_move_planned, index=5),
            "Planned Rack 1-5",
        )

    # --- naming_example.py's script helpers stay planned-rack-safe -----

    def test_naming_example_rack_token_for_planned_rack(self):
        self.assertEqual(
            naming_example._rack_token(self.p_add_planned), "plannedrack1"
        )

    def test_naming_example_site_slug_for_planned_rack(self):
        self.assertEqual(
            naming_example._site_slug(self.p_add_planned), "site-1"
        )

    def test_naming_example_build_name_for_planned_rack(self):
        # End-to-end: build_name() must not blow up and must not produce an
        # empty rack segment for a device dropped into a planned rack.
        name = naming_example.build_name(self.p_add_planned)
        self.assertTrue(name.startswith("site-1-role-1-"))
