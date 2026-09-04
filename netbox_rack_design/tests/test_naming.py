"""
Tests for the naming-convention engine (``netbox_rack_design.naming``).

Covers the three modes (sequence / template / script), the dotted-path template
context for both an *add* (placement-backed proxy) and a *move/remove* (real
dcim.Device), safe traversal of missing/blank attributes, ordinal ordering, and
the read-only collision check (which must perform NO dcim writes).
"""

import re

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings

from .. import naming_example, planning_fields
from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignPlacement
from ..naming import (
    SettledNameError,
    derive_prefix_token,
    generate_name,
    name_exists_in_site,
    pending_names,
    placement_ordinal,
    settled_name,
    settled_name_status,
    validate_naming_config,
)
from .utils import create_dcim_environment


def sample_naming_fn(placement):
    """Module-level callable used to exercise ``script`` mode (must be importable)."""
    return f"script:{placement.pk}"


not_callable_value = "I am a string, not a function"


def raising_naming_fn(placement):
    """Module-level callable that always raises, to exercise the runtime-error
    fallback in ``script`` mode."""
    raise RuntimeError("boom")


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

    # --- pending (in-editor, unsaved) names (user bug 2026-07-10) ----------

    def test_pending_names_helper(self):
        # Default: no attribute -> empty list; the injected attribute is
        # surfaced as-is (the same pattern as _projected_vacated_device_ids).
        placement = DesignPlacement(design=self.design)
        self.assertEqual(pending_names(placement), [])
        placement._rd_pending_names = ["a-1", "b-2"]
        self.assertEqual(pending_names(placement), ["a-1", "b-2"])

    @override_settings(PLUGINS_CONFIG=_plugins_config(naming_mode="sequence"))
    def test_sequence_mode_skips_pending_sibling_names(self):
        """Two unsaved same-session siblings must not receive the same
        sequence name: the built-in mode bumps past any pending name that
        matches its own '<title>-<digits>' family (the user's duplicate-name
        bug, reproduced at the engine level)."""
        placement = DesignPlacement(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
        )
        placement._rd_pending_names = ["DC-Build-7", "DC-Build-9", "unrelated-1"]
        # index=7 collides with a pending sibling; the highest pending family
        # ordinal is 9, so the next free is 10.
        self.assertEqual(generate_name(placement, index=7), "DC-Build-10")
        # No pending collision: the index is used untouched.
        placement._rd_pending_names = ["unrelated-1"]
        self.assertEqual(generate_name(placement, index=7), "DC-Build-7")

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
        # A real dcim.Device resolves the same dotted paths, but {device.rack}
        # resolves to the move's TARGET rack (Rack 2), never the source rack
        # the device currently sits in (Rack 1) -- see _MoveDeviceProxy.
        self.assertEqual(
            generate_name(self.p_move, index=2),
            "Site 1-Rack 2-Device Type 1-2",
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

    # A broken script config must NOT raise (which would 500 the preview
    # endpoint and leave a blank name): it falls back to the default sequence
    # name so a mis-configured or not-yet-loaded script degrades gracefully
    # (user requirement 2026-07-10). Each case asserts the sequence fallback.

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(naming_mode="script", naming_script="")
    )
    def test_script_mode_empty_path_falls_back_to_sequence(self):
        self.assertEqual(generate_name(self.p_add, index=4), "DC-Build-4")

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="script", naming_script="no.such.module.fn"
        )
    )
    def test_script_mode_bad_path_falls_back_to_sequence(self):
        self.assertEqual(generate_name(self.p_add, index=4), "DC-Build-4")

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="script",
            naming_script="netbox_rack_design.tests.test_naming.not_callable_value",
        )
    )
    def test_script_mode_not_callable_falls_back_to_sequence(self):
        self.assertEqual(generate_name(self.p_add, index=4), "DC-Build-4")

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="script",
            naming_script="netbox_rack_design.tests.test_naming.raising_naming_fn",
        )
    )
    def test_script_mode_runtime_error_falls_back_to_sequence(self):
        # A script that RAISES while computing a name also degrades to default.
        self.assertEqual(generate_name(self.p_add, index=4), "DC-Build-4")

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


def resolved_attrs_naming_fn(placement):
    """Module-level callable (script mode) that reads the RESOLVED role/tenant
    off the placement -- exactly what a real naming script is expected to do
    per PLAN-move-naming.md -- rather than duplicating the override/carry-over
    logic itself."""
    role = placement.resolved_role()
    tenant = placement.resolved_tenant()
    return f"{role.name if role else ''}:{tenant.name if tenant else ''}"


class MoveResolvedAttributesTestCase(TestCase):
    """Phase 2 (PLAN-move-naming.md): a 'move' placement's role/tenant are
    planned OVERRIDES (null means carry over the device's own value), and its
    rack/position/face are the TARGET, not the source. The naming engine --
    template mode's ``{device.*}`` context and any script handed the
    placement directly -- must see the RESOLVED values, never the raw
    device/override split."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]
        cls.device_role = env["device_role"]
        cls.tenant = env["tenant"]
        cls.devices = env["devices"]

        cls.pdu_role = DeviceRole.objects.create(name="PDU Role", slug="pdu-role")

        cls.design = Design.objects.create(title="Move-Naming", site=cls.site)

        # devices[0]: real device, own role "Device Role 1", no tenant,
        # currently Rack 1 / U1 / front (see create_dcim_environment).
        cls.p_move_plain = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.devices[0],
            target_rack=cls.racks[1],
            target_position=15,
            target_face="front",
        )
        cls.p_move_override = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.devices[1],
            device_role=cls.device_role,
            tenant=cls.tenant,
            target_rack=cls.racks[1],
            target_position=16,
            target_face="front",
        )
        cls.p_move_pdu_override = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.devices[0],
            device_role=cls.pdu_role,
            target_rack=cls.racks[1],
            target_position=18,
            target_face="front",
        )
        cls.p_add = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            device_role=cls.device_role,
            tenant=cls.tenant,
            target_rack=cls.racks[1],
            target_position=17,
            target_face="front",
            proposed_name="planned-add-1",
        )

        # base_placement fallback (G2): a move acting on an ancestor design's
        # still-planned 'add', with no override of its own -- the carry-over
        # source is that ancestor placement's own role/tenant.
        cls.parent_design = Design.objects.create(title="Parent-Naming", site=cls.site)
        cls.upstream_add = DesignPlacement.objects.create(
            design=cls.parent_design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            device_role=cls.device_role,
            tenant=cls.tenant,
            target_rack=cls.racks[0],
            target_position=5,
            proposed_name="upstream-node",
        )
        cls.parent_design.status = DesignStatusChoices.STATUS_APPROVED
        cls.parent_design.save()
        cls.child_design = Design.objects.create(
            title="Child-Naming", site=cls.site, based_on=cls.parent_design
        )
        cls.p_move_base_placement = DesignPlacement.objects.create(
            design=cls.child_design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=cls.upstream_add,
            target_rack=cls.racks[1],
            target_position=19,
            target_face="front",
        )

    # --- resolved_role() / resolved_tenant() model methods ------------------

    def test_resolved_role_and_tenant_use_the_override_when_set(self):
        self.assertEqual(self.p_move_override.resolved_role(), self.device_role)
        self.assertEqual(self.p_move_override.resolved_tenant(), self.tenant)

    def test_resolved_role_and_tenant_carry_over_the_device_when_omitted(self):
        self.assertEqual(self.p_move_plain.resolved_role(), self.devices[0].role)
        self.assertIsNone(self.devices[0].tenant)
        self.assertIsNone(self.p_move_plain.resolved_tenant())

    def test_resolved_role_and_tenant_for_add_is_the_planned_value_itself(self):
        # An 'add' has no device to fall back on -- the field IS the value
        # (regression guard: unaffected by the move override machinery).
        self.assertEqual(self.p_add.resolved_role(), self.device_role)
        self.assertEqual(self.p_add.resolved_tenant(), self.tenant)

    def test_resolved_role_and_tenant_fall_back_to_base_placement(self):
        # No override on the child move -> the ancestor's own planned add
        # supplies the carry-over value.
        self.assertEqual(
            self.p_move_base_placement.resolved_role(), self.device_role
        )
        self.assertEqual(
            self.p_move_base_placement.resolved_tenant(), self.tenant
        )

    def test_resolved_role_and_tenant_none_with_nothing_to_fall_back_on(self):
        detached = DesignPlacement(design=self.design, kind=DesignPlacementKindChoices.KIND_MOVE)
        self.assertIsNone(detached.resolved_role())
        self.assertIsNone(detached.resolved_tenant())

    # --- template mode -------------------------------------------------------

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template",
            naming_template="{device.role.name}|{device.tenant.name}",
        )
    )
    def test_template_move_override_role_and_tenant(self):
        self.assertEqual(
            generate_name(self.p_move_override, index=1), "Role 1|Tenant 1"
        )

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template",
            naming_template="{device.role.name}|{device.tenant.name}",
        )
    )
    def test_template_move_carries_over_role_and_tenant_when_omitted(self):
        self.assertEqual(
            generate_name(self.p_move_plain, index=1), "Device Role 1|"
        )

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template",
            naming_template="{device.rack.name}-{device.position}-{device.face}",
        )
    )
    def test_template_move_location_is_the_target_not_the_source(self):
        # devices[0] currently sits in Rack 1 / U1 / front -- the template
        # must render the TARGET (Rack 2 / U15 / front), never the source.
        self.assertEqual(
            generate_name(self.p_move_plain, index=1), "Rack 2-15-front"
        )

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template",
            naming_template="{device.role.name}|{device.tenant.name}",
        )
    )
    def test_template_move_base_placement_role_and_tenant(self):
        self.assertEqual(
            generate_name(self.p_move_base_placement, index=1), "Role 1|Tenant 1"
        )

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template", naming_template="{device.name}"
        )
    )
    def test_add_template_context_unaffected(self):
        # Regression guard: an 'add' still uses the placeholder proxy exactly
        # as before -- {device.name} is the proposed name, not any move logic.
        self.assertEqual(
            generate_name(self.p_add, index=1), "planned-add-1"
        )

    # --- script mode -----------------------------------------------------

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="script",
            naming_script=(
                "netbox_rack_design.tests.test_naming.resolved_attrs_naming_fn"
            ),
        )
    )
    def test_script_mode_move_sees_resolved_role_and_tenant(self):
        # _run_script hands the PLACEMENT straight to the script, so the
        # script itself must be able to call resolved_role()/resolved_tenant()
        # to get what the device WILL be.
        self.assertEqual(
            generate_name(self.p_move_override), "Role 1:Tenant 1"
        )
        self.assertEqual(
            generate_name(self.p_move_plain), "Device Role 1:"
        )

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="script",
            naming_script="netbox_rack_design.naming_example.build_name",
        )
    )
    def test_script_mode_naming_example_move_role_override_selects_pdu_branch(self):
        # naming_example._role_slug reads placement.device_role directly for
        # the override case, so this passes either way -- included as a
        # regression guard that the PDU override still steers the phase-pair
        # branch even though the source device's own role is not a PDU role.
        self.assertEqual(
            generate_name(self.p_move_pdu_override),
            "site-1-pdu-rrack2-a1",
        )

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="script",
            naming_script="netbox_rack_design.naming_example.build_name",
        )
    )
    def test_script_mode_naming_example_move_base_placement_role(self):
        # naming_example._role_slug is written against resolved_role(): a move
        # acting on a base_placement (no real device, no override of its own)
        # must resolve to the ANCESTOR placement's own planned role ("Role 1",
        # slug "role-1"), not fall through to "dev" for lack of a device to
        # read .role off of.
        self.assertEqual(
            generate_name(self.p_move_base_placement),
            "site-1-role-1-1",
        )


class SettledNameCollisionTestCase(TestCase):
    """``name_exists_in_site`` must compare SETTLED names, not just literal
    ``proposed_name`` strings (PLAN-design-chains.md Sec 3.4).

    Two reachable cases motivate this: a planner hand-typing a settled name
    while an ancestor still holds the prefixed planning name for the same
    device, and two SIBLING designs (blind to each other, Sec 2.1) each
    generating their own family's "-01" under a different project prefix --
    literal comparison never fires for either.
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.other_site = Site.objects.create(name="Site 2", slug="site-2")
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]
        cls.device_role = env["device_role"]

        # A design whose title carries a ticket-style prefix, IDS_TOKEN_RE
        # picks up "IDS-1234" via derive_prefix_token -- no naming config
        # override needed.
        cls.design_ancestor = Design.objects.create(
            title="Network sweep IDS-1234", site=cls.site,
        )
        cls.ancestor_placement = DesignPlacement.objects.create(
            design=cls.design_ancestor,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            device_role=cls.device_role,
            target_rack=cls.racks[1],
            target_position=30,
            target_face="front",
            proposed_name="IDS-1234_srv-01",
        )

        # Two siblings of one parent (never itself referenced -- only the
        # tree shape, not chain resolution, matters to this function), each
        # with its own project prefix, each having independently generated a
        # "-01" planning name for what settles to the SAME device name.
        cls.design_child_a = Design.objects.create(
            title="Server build IDS-1111", site=cls.site,
        )
        cls.child_a_placement = DesignPlacement.objects.create(
            design=cls.design_child_a,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            device_role=cls.device_role,
            target_rack=cls.racks[1],
            target_position=31,
            target_face="front",
            proposed_name="IDS-1111_srv-01",
        )
        cls.design_child_b = Design.objects.create(
            title="Server build IDS-2222", site=cls.site,
        )

        # An unrelated placement whose planning name happens to be a SHORT
        # string ("01") that a naive endswith prefilter could over-match
        # against a longer candidate ("web-01").
        cls.design_unrelated = Design.objects.create(
            title="Unrelated IDS-9999", site=cls.site,
        )
        cls.unrelated_short_placement = DesignPlacement.objects.create(
            design=cls.design_unrelated,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            device_role=cls.device_role,
            target_rack=cls.racks[1],
            target_position=32,
            target_face="front",
            proposed_name="01",
        )

        # A design used only for its own prefix token, to exercise the
        # candidate's-own-settled-form direction in isolation (no placement of
        # its own, so it cannot ALSO trigger a match through some other row).
        cls.design_reverse = Design.objects.create(
            title="Cabling IDS-5678", site=cls.site,
        )

        # An isolated design + placement whose settled name collides with
        # nothing else in this fixture, so `exclude_placement` can be proven
        # to suppress a row's collision with its OWN settled name.
        cls.design_isolated = Design.objects.create(
            title="Cabling IDS-6000", site=cls.site,
        )
        cls.isolated_placement = DesignPlacement.objects.create(
            design=cls.design_isolated,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            device_role=cls.device_role,
            target_rack=cls.racks[1],
            target_position=34,
            target_face="front",
            proposed_name="IDS-6000_excl-01",
        )

        # Same shape as the ancestor placement, but in a DIFFERENT site.
        cls.design_other_site = Design.objects.create(
            title="Other site IDS-1234", site=cls.other_site,
        )
        DesignPlacement.objects.create(
            design=cls.design_other_site,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            device_role=cls.device_role,
            target_position=33,
            target_face="front",
            proposed_name="IDS-1234_srv-99",
        )

    # --- the ancestor case: literal comparison misses it today -------------

    def test_hand_typed_settled_name_collides_with_ancestors_prefixed_row(self):
        # today: False (literal "srv-01" != "IDS-1234_srv-01"); required: True.
        self.assertTrue(name_exists_in_site("srv-01", self.site))

    # --- the sibling case: chain_placement_names's documented mitigation ---

    def test_sibling_auto_generated_names_collide_on_the_settled_plane(self):
        # Checked from B's side: B's own (not-yet-saved) planning name is
        # "IDS-2222_srv-01" -- auto-shaped exactly like A's -- and settles to
        # the same "srv-01" A already holds under ITS own prefix.
        self.assertTrue(
            name_exists_in_site(
                "IDS-2222_srv-01", self.site, design=self.design_child_b,
            )
        )

    # --- the reverse direction: candidate's settled form vs a real device --

    def test_candidates_settled_form_collides_with_a_real_device(self):
        Device.objects.create(
            name="db-01", device_type=self.device_type, role=self.device_role,
            site=self.site, status="active",
        )
        self.assertTrue(
            name_exists_in_site(
                "IDS-5678_db-01", self.site, design=self.design_reverse,
            )
        )

    # --- exclude_placement still excludes the row itself --------------------

    def test_exclude_placement_excludes_its_own_settled_name(self):
        # Without exclusion the row's own settled name ("excl-01") is a
        # genuine collision with itself.
        self.assertTrue(name_exists_in_site("excl-01", self.site))
        # Excluding it makes it invisible to its own settled-name check.
        self.assertFalse(
            name_exists_in_site(
                "excl-01", self.site, exclude_placement=self.isolated_placement,
            )
        )

    # --- no over-matching: a longer candidate does not match a short row ---

    def test_unrelated_longer_name_does_not_falsely_collide(self):
        # An over-eager `endswith` prefilter would make "web-01" collide with
        # the unrelated placement named "01". It must not.
        self.assertFalse(name_exists_in_site("web-01", self.site))

    # --- site scoping -------------------------------------------------------

    def test_different_site_does_not_collide(self):
        # The row settling to "srv-99" lives in `other_site`, not `site`.
        self.assertFalse(name_exists_in_site("srv-99", self.site))
        self.assertTrue(name_exists_in_site("srv-99", self.other_site))

    # --- performance: no full-site load, no per-row query, no chain walk ---

    def test_query_count_is_bounded_and_unaffected_by_a_chain(self):
        # This function already scans the WHOLE site regardless of design
        # (PLAN-design-chains.md Sec 3.4), so it never walks `based_on` --
        # the query shape must therefore be identical whether `design` sits
        # at the root of a chain or several layers deep. A candidate design
        # with an ancestor is settled with the SAME query budget as one with
        # none: 1 Device existence check + 1 literal-name existence check +
        # 1 narrow-prefilter fetch of endswith candidates, and settling each
        # prefiltered row costs 0 further queries because its `design` is
        # already attached via `select_related`.
        with self.assertNumQueries(3):
            name_exists_in_site("nope-unmatched", self.site)

        child = Design.objects.create(
            title="Cabling IDS-7000", site=self.site, based_on=self.design_ancestor,
        )
        with self.assertNumQueries(3):
            name_exists_in_site("nope-unmatched", self.site, design=child)

    # --- an unsettleable row never raises out of the check ------------------

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming={"settled_name": "netbox_rack_design.tests.test_naming.raising_settled_name_fn"}
        )
    )
    def test_unsettleable_row_does_not_raise(self):
        try:
            result = name_exists_in_site("srv-01", self.site)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"name_exists_in_site raised {exc!r} instead of degrading")
        # The ancestor row can no longer be settled (the configured hook
        # always raises), so it is compared under its planning name only --
        # which does not equal "srv-01" -- and the check degrades to False
        # rather than propagating the failure.
        self.assertFalse(result)


# --- settled names across a design chain (PLAN-design-chains.md Sec 3) -------


def sample_settled_name_fn(placement):
    """Module-level callable used to exercise a deployment-supplied
    ``naming["settled_name"]`` hook (must be importable)."""
    return f"settled:{placement.proposed_name}"


def raising_settled_name_fn(placement):
    """Module-level callable that always raises, to exercise the
    no-silent-failure rule for a broken settled-name hook."""
    raise RuntimeError("boom")


def none_returning_settled_name_fn(placement):
    """Module-level callable that returns a non-string, which is a failure: a
    settled name is a join key, so there is no sensible fallback."""
    return None


def _naming_config(**naming_overrides):
    """PLUGINS_CONFIG carrying only the ``naming`` sub-dict overrides."""
    return _plugins_config(naming=dict(naming_overrides))


class SettledNameTestCase(TestCase):
    """R1-R3 of PLAN-design-chains.md Sec 3.2.

    ``proposed_name`` carries a design's PLANNING name (``IDS-1234_old_name``);
    the settled name is what the device ends up called once that design is done
    (``old_name``). These tests pin the strip rule, because a subtly wrong strip
    silently corrupts every downstream name.
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]
        cls.device_role = env["device_role"]

        # A three-deep chain: C is based on B is based on A. Each design uses a
        # DIFFERENT project token, which is exactly why the token cannot be
        # derived from a single global convention.
        cls.design_a = Design.objects.create(
            title="Network sweep IDS-1000",
            site=cls.site,
            description="IDS-1000",
            custom_field_data={"project": "IDS-1000"},
        )
        cls.design_b = Design.objects.create(
            title="Server build IDS-2000",
            site=cls.site,
            based_on=cls.design_a,
            custom_field_data={"project": "IDS-2000"},
        )
        cls.design_c = Design.objects.create(
            title="Storage build IDS-3000",
            site=cls.site,
            based_on=cls.design_b,
            custom_field_data={"project": "IDS-3000"},
        )

    def _placement(self, design, proposed_name):
        """An unsaved placement: ``settled_name`` reads only design +
        ``proposed_name``, so no row is needed (same pattern as
        ``test_pending_names_helper``)."""
        return DesignPlacement(
            design=design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            proposed_name=proposed_name,
        )

    # --- the strip itself, token from the declared source ------------------

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_prefix_from_declared_source_is_stripped_underscore(self):
        placement = self._placement(self.design_a, "IDS-1000_old_name")
        self.assertEqual(settled_name(placement), "old_name")

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_prefix_from_declared_source_is_stripped_dash(self):
        placement = self._placement(self.design_a, "IDS-1000-old_name")
        self.assertEqual(settled_name(placement), "old_name")

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_name_without_prefix_is_returned_unchanged(self):
        placement = self._placement(self.design_a, "plain-device-1")
        self.assertEqual(settled_name(placement), "plain-device-1")

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_separator_is_required_so_a_longer_token_is_not_stripped(self):
        # Token is "IDS-1000"; "IDS-10005_x" only LOOKS like it starts with the
        # token. Without the separator requirement this would yield "5_x".
        placement = self._placement(self.design_a, "IDS-10005_x")
        self.assertEqual(settled_name(placement), "IDS-10005_x")

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_another_designs_prefix_is_not_stripped(self):
        # A resembles-but-is-not-this-design's-token prefix stays put: the token
        # must match THIS design's project.
        placement = self._placement(self.design_a, "IDS-2000_old_name")
        self.assertEqual(settled_name(placement), "IDS-2000_old_name")

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="description"))
    def test_prefix_source_may_be_a_native_attribute_path(self):
        # Nothing about the resolver is custom-field specific.
        placement = self._placement(self.design_a, "IDS-1000_old_name")
        self.assertEqual(settled_name(placement), "old_name")

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="design.cf.project"))
    def test_prefix_source_may_carry_the_design_root_token(self):
        # The plan documents "design.cf.<field>"; the leading root token is
        # optional, exactly as {design.x} is the template root.
        placement = self._placement(self.design_a, "IDS-1000_old_name")
        self.assertEqual(settled_name(placement), "old_name")

    # --- R2: prefixes never stack ------------------------------------------

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_r2_prefixes_never_stack(self):
        """A's planning name settles to ``old_name``; B re-moving that device
        prefixes the SETTLED name, never A's planning name."""
        in_a = self._placement(self.design_a, "IDS-1000_old_name")
        settled = settled_name(in_a)
        self.assertEqual(settled, "old_name")

        # B names off the settled input -> exactly one prefix.
        in_b = self._placement(self.design_b, f"IDS-2000_{settled}")
        self.assertEqual(in_b.proposed_name, "IDS-2000_old_name")
        self.assertNotIn("IDS-1000", in_b.proposed_name)
        # ... and B's own settled name is the same device identity again.
        self.assertEqual(settled_name(in_b), "old_name")

    # --- R3: de-prefix once per layer --------------------------------------

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_r3_three_deep_chain_strips_once_per_layer(self):
        # One placement per design in the chain; each strips ITS OWN token once.
        self.assertEqual(
            [settled_name(self._placement(d, f"{tok}_old_name"))
             for d, tok in (
                 (self.design_a, "IDS-1000"),
                 (self.design_b, "IDS-2000"),
                 (self.design_c, "IDS-3000"),
             )],
            ["old_name", "old_name", "old_name"],
        )

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_r3_repeated_token_is_stripped_exactly_once(self):
        # The device is genuinely called "IDS-1000_x" (someone baked the token
        # into the name); A's planning name doubles it. Stripping repeatedly --
        # once per chain layer, or with a loop -- would destroy the identity.
        placement = self._placement(self.design_a, "IDS-1000_IDS-1000_x")
        self.assertEqual(settled_name(placement), "IDS-1000_x")

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_settled_name_is_idempotent_on_an_already_settled_name(self):
        placement = self._placement(self.design_a, "old_name")
        self.assertEqual(settled_name(placement), "old_name")

    # --- adversarial inputs ------------------------------------------------

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_empty_proposed_name_settles_empty(self):
        self.assertEqual(settled_name(self._placement(self.design_a, "")), "")
        self.assertEqual(settled_name(self._placement(self.design_a, None)), "")

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_token_with_regex_metacharacters_is_matched_literally(self):
        design = Design.objects.create(
            title="Odd project",
            site=self.site,
            custom_field_data={"project": "IDS+1(2)"},
        )
        self.assertEqual(settled_name(self._placement(design, "IDS+1(2)_dev")), "dev")
        # The metacharacters are NOT treated as a pattern.
        self.assertEqual(settled_name(self._placement(design, "IDSX1X2X_dev")), "IDSX1X2X_dev")

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_name_that_is_only_the_prefix_is_left_alone(self):
        # No separator, nothing after it: there is no settled name to recover,
        # and returning "" would erase the identity.
        placement = self._placement(self.design_a, "IDS-1000")
        self.assertEqual(settled_name(placement), "IDS-1000")

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_prefix_source_resolving_to_a_number_is_used_as_a_token(self):
        design = Design.objects.create(
            title="Numeric project", site=self.site, custom_field_data={"project": 1234},
        )
        self.assertEqual(settled_name(self._placement(design, "1234_dev")), "dev")

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_prefix_match_is_case_insensitive(self):
        # The token is a project name, not a pattern, and the same project is
        # written both ways in the wild (a title-derived token is upper-cased
        # while a hand-typed planning name often is not). Refusing to match a
        # differently-cased SAME token would leave the planning prefix on an
        # inherited placement -- a silent R1 violation, which is exactly the
        # failure this whole hook exists to prevent.
        design = Design.objects.create(
            title="Mixed case", site=self.site, custom_field_data={"project": "IDS-4000"},
        )
        self.assertEqual(settled_name(self._placement(design, "ids-4000_dev")), "dev")
        self.assertEqual(settled_name(self._placement(design, "Ids-4000-dev")), "dev")
        # A DIFFERENT token is still not stripped, whatever its case.
        self.assertEqual(
            settled_name(self._placement(design, "ids-4001_dev")), "ids-4001_dev"
        )

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_padded_source_value_is_trimmed_before_matching(self):
        design = Design.objects.create(
            title="Padded", site=self.site, custom_field_data={"project": "  IDS-5000 "},
        )
        self.assertEqual(settled_name(self._placement(design, "IDS-5000_dev")), "dev")

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_exactly_one_separator_is_consumed(self):
        # A doubled separator is a typo in the planning name; the strip removes
        # the token and ONE separator and never more, so it can only ever give
        # back a suffix of the name the planner can see.
        placement = self._placement(self.design_a, "IDS-1000__old_name")
        self.assertEqual(settled_name(placement), "_old_name")

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_token_inside_the_name_is_not_touched(self):
        # The match is anchored: a token that is part of the device identity
        # rather than a leading prefix stays.
        placement = self._placement(self.design_a, "dev_IDS-1000_x")
        self.assertEqual(settled_name(placement), "dev_IDS-1000_x")

    # --- fallback to title derivation --------------------------------------

    @override_settings(PLUGINS_CONFIG=_naming_config())
    def test_prefix_source_unset_falls_back_to_title_derivation(self):
        placement = self._placement(self.design_a, "IDS-1000_old_name")
        self.assertEqual(settled_name(placement), "old_name")

    @override_settings(PLUGINS_CONFIG=_naming_config())
    def test_title_derivation_matches_naming_example(self):
        # Same derivation naming_example.build_name uses for its family prefix --
        # reused, not re-implemented.
        self.assertEqual(derive_prefix_token(self.design_a), "IDS-1000")
        self.assertEqual(
            derive_prefix_token(Design(title="ids1234 rebuild")), "IDS-1234"
        )
        # No IDS token in the title: build_name falls back to the whole title.
        self.assertEqual(derive_prefix_token(Design(title="no ticket")), "IDS-no ticket")

    @override_settings(PLUGINS_CONFIG=_naming_config())
    def test_title_derivation_leaves_an_unrelated_name_alone(self):
        design = Design.objects.create(title="Untracked work", site=self.site)
        placement = self._placement(design, "IDS-1000_old_name")
        self.assertEqual(settled_name(placement), "IDS-1000_old_name")

    # --- no silent failure --------------------------------------------------

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.missing"))
    def test_unresolvable_prefix_source_raises(self):
        placement = self._placement(self.design_a, "IDS-1000_old_name")
        with self.assertRaises(SettledNameError) as ctx:
            settled_name(placement)
        # The message must name the path and the design, not just "failed".
        self.assertIn("cf.missing", str(ctx.exception))
        self.assertIn(self.design_a.title, str(ctx.exception))

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.missing"))
    def test_unresolvable_prefix_source_reports_a_structured_status(self):
        placement = self._placement(self.design_a, "IDS-1000_old_name")
        name, status = settled_name_status(placement)
        # No plausible-but-wrong name is ever handed back.
        self.assertIsNone(name)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["engine"], "builtin")
        self.assertIn("cf.missing", status["detail"])

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_status_ok_carries_the_settled_name(self):
        name, status = settled_name_status(self._placement(self.design_a, "IDS-1000_x"))
        self.assertEqual(name, "x")
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["engine"], "builtin")

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_blank_resolved_token_raises(self):
        design = Design.objects.create(
            title="Blank project", site=self.site, custom_field_data={"project": "   "},
        )
        with self.assertRaises(SettledNameError):
            settled_name(self._placement(design, "IDS-1000_x"))

    # --- a deployment-supplied callable ------------------------------------

    @override_settings(
        PLUGINS_CONFIG=_naming_config(
            settled_name="netbox_rack_design.tests.test_naming.sample_settled_name_fn",
        )
    )
    def test_configured_callable_replaces_the_builtin(self):
        placement = self._placement(self.design_a, "IDS-1000_old_name")
        self.assertEqual(settled_name(placement), "settled:IDS-1000_old_name")

    @override_settings(
        PLUGINS_CONFIG=_naming_config(settled_name="no.such.module.fn")
    )
    def test_unimportable_callable_raises(self):
        with self.assertRaises(SettledNameError):
            settled_name(self._placement(self.design_a, "IDS-1000_x"))

    @override_settings(
        PLUGINS_CONFIG=_naming_config(
            settled_name="netbox_rack_design.tests.test_naming.not_callable_value",
        )
    )
    def test_non_callable_setting_raises(self):
        with self.assertRaises(SettledNameError):
            settled_name(self._placement(self.design_a, "IDS-1000_x"))

    @override_settings(
        PLUGINS_CONFIG=_naming_config(
            settled_name="netbox_rack_design.tests.test_naming.raising_settled_name_fn",
        )
    )
    def test_raising_callable_raises_rather_than_falling_back(self):
        placement = self._placement(self.design_a, "IDS-1000_old_name")
        with self.assertRaises(SettledNameError):
            settled_name(placement)
        name, status = settled_name_status(placement)
        self.assertIsNone(name)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["engine"], "script")
        self.assertIn("RuntimeError", status["detail"])

    @override_settings(
        PLUGINS_CONFIG=_naming_config(
            settled_name="netbox_rack_design.tests.test_naming.none_returning_settled_name_fn",
        )
    )
    def test_callable_returning_a_non_string_raises(self):
        with self.assertRaises(SettledNameError):
            settled_name(self._placement(self.design_a, "IDS-1000_x"))

    # --- config validation --------------------------------------------------

    @override_settings(PLUGINS_CONFIG={"netbox_rack_design": {"naming": "nope"}})
    def test_naming_config_must_be_a_mapping(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_naming_config()

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_soruce="cf.project"))
    def test_unknown_naming_config_key_is_rejected(self):
        # A typo must not silently disable the feature.
        with self.assertRaises(ImproperlyConfigured) as ctx:
            validate_naming_config()
        self.assertIn("prefix_soruce", str(ctx.exception))

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source=["cf.project"]))
    def test_non_string_naming_config_value_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_naming_config()

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source=["cf.project"]))
    def test_malformed_config_is_rejected_at_startup(self):
        # PluginConfig.ready() runs the same validation, so a malformed value
        # fails the boot instead of being ignored at name-generation time.
        from netbox_rack_design import RackdesignConfig

        self.assertIn(validate_naming_config, RackdesignConfig._rd_startup_checks())
        with self.assertRaises(ImproperlyConfigured):
            settled_name(self._placement(self.design_a, "IDS-1000_x"))

    @override_settings(PLUGINS_CONFIG=_naming_config())
    def test_empty_naming_config_is_valid(self):
        self.assertEqual(
            validate_naming_config(), {"prefix_source": "", "settled_name": ""}
        )

    # --- the shared source resolver ----------------------------------------

    def test_resolve_source_walks_a_design_rooted_cf_path(self):
        # planning_fields.resolve_source is the ONE resolver; it now accepts a
        # cf segment anywhere along the path, not only at the head.
        self.assertEqual(
            planning_fields.resolve_source(self.design_a, "cf.project"), "IDS-1000"
        )
        placement = self._placement(self.design_a, "x")
        self.assertEqual(
            planning_fields.resolve_source(placement, "design.cf.project"), "IDS-1000"
        )
        self.assertEqual(
            planning_fields.resolve_source(self.design_a, "site.name"), self.site.name
        )
        self.assertIsNone(planning_fields.resolve_source(self.design_a, "cf"))
        self.assertIsNone(planning_fields.resolve_source(self.design_a, "cf.nope"))
        self.assertIsNone(planning_fields.resolve_source(None, "cf.project"))

# --- chain-wide family counters (PLAN-design-chains.md Sec 3.4) -------------


class ChainFamilyCounterTestCase(TestCase):
    """A numbered family must not restart inside a child design.

    ``_next_number`` / ``_next_pdu_slot`` used to count real devices plus
    ``DesignPlacement.objects.filter(design=placement.design)`` -- this design
    only -- so a child handed out a number an ancestor had already reserved
    (Sec 3.4). The counter now spans **ancestors + self**, matching an
    ancestor's SETTLED name so the family regex still fires, and deliberately
    NOT siblings (Sec 2.1: two children of one parent are blind to each other;
    first approved wins and the other re-bases).
    """

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="AMS1", slug="ams1")
        mfr = Manufacturer.objects.create(name="Generic", slug="generic")
        cls.rack = Rack.objects.create(name="R42", site=cls.site)
        cls.sw_type = DeviceType.objects.create(
            manufacturer=mfr, model="Switch X", slug="switch-x", u_height=1,
        )
        cls.sw_role = DeviceRole.objects.create(name="Leaf Switch", slug="leaf-switch")

        # a <- b <- c <- d, every ancestor APPROVED (only an approved ancestor
        # participates in a chain), plus a sibling of b and an unrelated design.
        cls.design_a = cls._design("Network sweep IDS-1000", "IDS-1000", approved=True)
        cls.design_b = cls._design(
            "Server build IDS-2000", "IDS-2000", based_on=cls.design_a, approved=True,
        )
        cls.design_c = cls._design(
            "Storage build IDS-3000", "IDS-3000", based_on=cls.design_b, approved=True,
        )
        cls.design_d = cls._design(
            "Cabling IDS-4000", "IDS-4000", based_on=cls.design_c,
        )
        cls.design_sibling = cls._design(
            "Other child IDS-9000", "IDS-9000", based_on=cls.design_a,
        )
        cls.design_unchained = cls._design("Standalone IDS-7000", "IDS-7000")

    @classmethod
    def _design(cls, title, project, *, based_on=None, approved=False):
        return Design.objects.create(
            title=title,
            site=cls.site,
            based_on=based_on,
            status=(DesignStatusChoices.STATUS_APPROVED if approved
                    else DesignStatusChoices.STATUS_DRAFT),
            custom_field_data={"project": project},
        )

    _position = 1

    @classmethod
    def _row(cls, design, proposed_name, **extra):
        """A persisted ``add`` placement carrying a PLANNING name."""
        ChainFamilyCounterTestCase._position += 1
        kwargs = {
            "design": design,
            "kind": DesignPlacementKindChoices.KIND_ADD,
            "device_type": cls.sw_type,
            "device_role": cls.sw_role,
            "target_rack": cls.rack,
            "target_position": ChainFamilyCounterTestCase._position,
            "target_face": "front",
            "proposed_name": proposed_name,
        }
        kwargs.update(extra)
        return DesignPlacement.objects.create(**kwargs)

    def _pending(self, design):
        """The UNSAVED placement being named (the shape the preview API builds)."""
        return DesignPlacement(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.sw_type,
            device_role=self.sw_role,
            target_rack=self.rack,
            target_position=40,
            target_face="front",
        )

    # --- unchained: byte-identical to before -------------------------------

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_unchained_counter_counts_only_this_design(self):
        self._row(self.design_unchained, "ams1-sw-3")
        # Another design in the same site is invisible to an unchained counter,
        # exactly as before this change.
        self._row(self.design_a, "ams1-sw-9")
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_unchained), "ams1-sw-"),
            "4",
        )

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_unchained_counter_still_counts_real_devices_and_pending(self):
        Device.objects.create(
            name="ams1-sw-2", device_type=self.sw_type, role=self.sw_role,
            site=self.site, status="active",
        )
        placement = self._pending(self.design_unchained)
        self.assertEqual(naming_example._next_number(placement, "ams1-sw-"), "3")
        placement._rd_pending_names = ["ams1-sw-3"]
        self.assertEqual(naming_example._next_number(placement, "ams1-sw-"), "4")

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_unchained_counter_query_count_is_unchanged(self):
        self._row(self.design_unchained, "ams1-sw-3")
        placement = self._pending(self.design_unchained)
        # One Device scan + one placement scan, and NO lineage walk: a design
        # with no parent must not pay for the chain.
        with self.assertNumQueries(2):
            naming_example._next_number(placement, "ams1-sw-")

    # --- the gap: a child skips a number an ancestor reserved --------------

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_child_counter_skips_a_number_an_ancestor_reserved(self):
        self._row(self.design_a, "ams1-sw-5")
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_b), "ams1-sw-"), "6",
        )

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_ancestor_is_matched_by_its_settled_name(self):
        """The ancestor's row carries a PLANNING name, so widening the query
        alone finds nothing -- the settled name is what makes the family regex
        fire."""
        row = self._row(self.design_a, "IDS-1000_ams1-sw-5")
        # Proof the widening alone is insufficient: the persisted planning name
        # does not belong to the family the child is counting...
        family = re.compile(r"^ams1-sw-(\d+)$")
        self.assertIsNone(family.match(row.proposed_name))
        # ... while its settled name does.
        self.assertEqual(settled_name(row), "ams1-sw-5")
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_b), "ams1-sw-"), "6",
        )

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_three_deep_chain_counts_every_ancestor(self):
        self._row(self.design_a, "IDS-1000_ams1-sw-4")
        self._row(self.design_b, "IDS-2000_ams1-sw-7")
        self._row(self.design_c, "IDS-3000_ams1-sw-2")
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_d), "ams1-sw-"), "8",
        )
        # ... and each layer sees only what is BELOW it.
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_b), "ams1-sw-"), "5",
        )

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_chain_counter_query_count_at_depth_three(self):
        self._row(self.design_a, "IDS-1000_ams1-sw-4")
        # Refetched, so the walk is really paid for: setUpTestData hands back
        # instances whose based_on FK is already cached.
        placement = self._pending(Design.objects.get(pk=self.design_d.pk))
        # Device scan + one lineage hop per ancestor (baseline_chain) + ONE
        # placement scan covering self AND every ancestor -- never one query per
        # ancestor per name.
        with self.assertNumQueries(5):
            naming_example._next_number(placement, "ams1-sw-")

    # --- Sec 2.1: siblings are NOT counted ---------------------------------

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_siblings_are_not_counted(self):
        self._row(self.design_a, "ams1-sw-5")
        self._row(self.design_sibling, "ams1-sw-9")
        # Sec 2.1: two children of one parent are blind to each other by
        # design; the sibling's 9 is invisible and the collision (if any)
        # surfaces later through name_exists_in_site's non-blocking warning.
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_b), "ams1-sw-"), "6",
        )

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_a_sibling_name_the_counter_proposes_is_caught_as_a_warning(self):
        # The coherence check for the rule above: the counter may propose a name
        # a sibling already took, and that is not silent -- name_exists_in_site
        # matches every placement whose design targets the site, sibling
        # included, so it lands in the existing non-blocking collision warning.
        self._row(self.design_sibling, "ams1-sw-1")
        placement = self._pending(self.design_b)
        proposed = "ams1-sw-" + naming_example._next_number(placement, "ams1-sw-")
        self.assertEqual(proposed, "ams1-sw-1")
        self.assertTrue(name_exists_in_site(proposed, self.site))

    # --- only an APPROVED ancestor participates ----------------------------

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_unapproved_ancestor_contributes_nothing(self):
        self._row(self.design_a, "ams1-sw-5")
        Design.objects.filter(pk=self.design_a.pk).update(
            status=DesignStatusChoices.STATUS_DRAFT)
        design_b = Design.objects.get(pk=self.design_b.pk)
        with self.assertLogs("netbox_rack_design.naming", level="WARNING"):
            self.assertEqual(
                naming_example._next_number(self._pending(design_b), "ams1-sw-"), "1",
            )

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_implemented_ancestor_contributes_nothing(self):
        self._row(self.design_a, "ams1-sw-5")
        Design.objects.filter(pk=self.design_a.pk).update(
            status=DesignStatusChoices.STATUS_IMPLEMENTED)
        design_b = Design.objects.get(pk=self.design_b.pk)
        with self.assertLogs("netbox_rack_design.naming", level="WARNING"):
            self.assertEqual(
                naming_example._next_number(self._pending(design_b), "ams1-sw-"), "1",
            )

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_one_unapproved_ancestor_drops_the_WHOLE_chain(self):
        # Sec 9.2: a layer is contributed whole or not at all, and a broken
        # ancestor breaks every layer stacked on top of it -- the same rule the
        # baseline replay applies, so the numbers a child hands out cannot
        # disagree with the rack it is looking at.
        self._row(self.design_a, "ams1-sw-4")
        self._row(self.design_b, "ams1-sw-7")
        Design.objects.filter(pk=self.design_b.pk).update(
            status=DesignStatusChoices.STATUS_DRAFT)
        design_d = Design.objects.get(pk=self.design_d.pk)
        with self.assertLogs("netbox_rack_design.naming", level="WARNING"):
            self.assertEqual(
                naming_example._next_number(self._pending(design_d), "ams1-sw-"), "1",
            )

    # --- adversarial: never skip numbers forever ---------------------------

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_same_identity_in_two_layers_is_counted_once(self):
        # A adds the device; B (a later ancestor) re-plans the same identity
        # under its own planning prefix. The counter takes a MAX over matched
        # names, so the identity cannot inflate the family.
        self._row(self.design_a, "IDS-1000_ams1-sw-5")
        self._row(self.design_b, "IDS-2000_ams1-sw-5")
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_c), "ams1-sw-"), "6",
        )

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_child_row_referencing_an_ancestor_identity_does_not_skip(self):
        base = self._row(self.design_a, "IDS-1000_ams1-sw-5")
        # The child re-plans that identity via base_placement -- its own row
        # names the SAME device, so the family must still stand at 5.
        self._row(
            self.design_b, "IDS-2000_ams1-sw-5",
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device_type=None, device_role=None, base_placement=base,
        )
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_b), "ams1-sw-"), "6",
        )

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_stale_ancestor_placement_is_still_counted(self):
        # A stale row is inert for PROJECTION (it renders nothing), but its name
        # is still reserved: name_exists_in_site matches it, so ignoring it here
        # would hand the child a name that immediately warns as a collision.
        self._row(
            self.design_a, "IDS-1000_ams1-sw-5",
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device_type=None, device_role=None, target_rack=None,
            target_position=None, target_face="",
            stale=True, stale_device_name="ams1-sw-5",
        )
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_b), "ams1-sw-"), "6",
        )

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_settled_name_failure_counts_the_planning_name_and_warns(self):
        # One unresolvable ancestor row must not fail the whole naming
        # operation (the planner cannot fix an upstream design from here), and
        # must not vanish from the count either: the row still contributes its
        # planning name, and the failure is LOGGED rather than swallowed.
        design = Design.objects.create(
            title="Blank project", site=self.site,
            status=DesignStatusChoices.STATUS_APPROVED,
            custom_field_data={"project": "   "},
        )
        child = Design.objects.create(
            title="Child of blank", site=self.site, based_on=design,
            custom_field_data={"project": "IDS-8000"},
        )
        self._row(design, "ams1-sw-5")
        with self.assertLogs("netbox_rack_design.naming", level="WARNING") as logs:
            self.assertEqual(
                naming_example._next_number(self._pending(child), "ams1-sw-"), "6",
            )
        self.assertTrue(
            any("no settled name" in line and "ams1-sw-5" in line
                for line in logs.output),
            logs.output,
        )

    @override_settings(PLUGINS_CONFIG=_naming_config(
        prefix_source="cf.project",
        settled_name="netbox_rack_design.tests.test_naming.raising_settled_name_fn",
    ))
    def test_a_broken_settled_name_hook_does_not_break_name_generation(self):
        # Same rule as above for a deployment-supplied hook that raises: the
        # counter reports and carries on with the planning name rather than
        # failing every name preview in the child design.
        self._row(self.design_a, "ams1-sw-5")
        with self.assertLogs("netbox_rack_design.naming", level="WARNING"):
            self.assertEqual(
                naming_example._next_number(self._pending(self.design_b), "ams1-sw-"),
                "6",
            )

    # --- the PDU phase-slot counter, equivalently ---------------------------

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_pdu_slot_unchained_is_unchanged(self):
        self._row(self.design_unchained, "ams1-pdu-rr42-a1")
        self._row(self.design_a, "ams1-pdu-rr42-b2")
        self.assertEqual(
            naming_example._next_pdu_slot(
                self._pending(self.design_unchained), "ams1-pdu-rr42-"),
            "b1",
        )

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_pdu_slot_spans_the_chain_by_settled_name(self):
        self._row(self.design_a, "IDS-1000_ams1-pdu-rr42-a1")
        self.assertEqual(
            naming_example._next_pdu_slot(self._pending(self.design_b), "ams1-pdu-rr42-"),
            "b1",
        )

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_pdu_slot_ignores_siblings_and_unapproved_ancestors(self):
        self._row(self.design_sibling, "ams1-pdu-rr42-b2")
        self.assertEqual(
            naming_example._next_pdu_slot(self._pending(self.design_b), "ams1-pdu-rr42-"),
            "a1",
        )
        Design.objects.filter(pk=self.design_a.pk).update(
            status=DesignStatusChoices.STATUS_DRAFT)
        self._row(self.design_a, "ams1-pdu-rr42-a1")
        design_b = Design.objects.get(pk=self.design_b.pk)
        with self.assertLogs("netbox_rack_design.naming", level="WARNING"):
            self.assertEqual(
                naming_example._next_pdu_slot(self._pending(design_b), "ams1-pdu-rr42-"),
                "a1",
            )

    # --- end to end through build_name -------------------------------------

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_build_name_general_family_continues_across_the_chain(self):
        first = naming_example.build_name(self._pending(self.design_b))
        self.assertTrue(first.endswith("-1"), first)
        # The ancestor reserved that very name, under its planning prefix.
        self._row(self.design_a, f"IDS-1000_{first}")
        self.assertEqual(
            naming_example.build_name(self._pending(self.design_b)), first[:-1] + "2",
        )

    # --- the shared helper -------------------------------------------------

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_chain_placement_names_returns_planning_and_settled_names(self):
        self._row(self.design_a, "IDS-1000_ams1-sw-5")
        self._row(self.design_b, "IDS-2000_ams1-sw-6")
        self._row(self.design_sibling, "ams1-sw-99")
        from ..naming import chain_placement_names

        names = set(chain_placement_names(self._pending(self.design_c)))
        # Ancestors contribute BOTH names; the child's own rows keep their
        # planning names (unchanged semantics); a sibling contributes nothing.
        self.assertEqual(
            names,
            {"IDS-1000_ams1-sw-5", "ams1-sw-5", "IDS-2000_ams1-sw-6", "ams1-sw-6"},
        )

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_chain_placement_names_excludes_the_placement_being_named(self):
        from ..naming import chain_placement_names

        row = self._row(self.design_b, "IDS-2000_ams1-sw-6")
        self.assertNotIn(row.proposed_name, chain_placement_names(row))

    @override_settings(PLUGINS_CONFIG=_naming_config(prefix_source="cf.project"))
    def test_chain_placement_names_degrades_on_a_lineage_cycle(self):
        # A pre-existing cycle (nothing prevented one before clean() grew a
        # guard) must not loop or 500 a name preview: the chain is dropped and
        # the counter falls back to this design alone, loudly.
        Design.objects.filter(pk=self.design_a.pk).update(based_on=self.design_c)
        self._row(self.design_c, "ams1-sw-4")
        from ..naming import chain_placement_names

        design_d = Design.objects.get(pk=self.design_d.pk)
        with self.assertLogs("netbox_rack_design.naming", level="WARNING"):
            self.assertEqual(
                list(chain_placement_names(self._pending(design_d))), [])
