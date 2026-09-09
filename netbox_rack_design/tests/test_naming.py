"""
Tests for the naming-convention engine (``netbox_rack_design.naming``).

Covers the three modes (sequence / template / script), the dotted-path template
context for both an *add* (placement-backed proxy) and a *move/remove* (real
dcim.Device), safe traversal of missing/blank attributes, ordinal ordering, and
the read-only collision check (which must perform NO dcim writes).
"""

import re
from unittest.mock import patch

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings

from .. import naming_example
from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignPlacement
from ..naming import (
    chain_placement_names,
    effective_name,
    generate_name,
    name_exists_in_site,
    peer_name_claims,
    pending_names,
    placement_ordinal,
    validate_naming_config,
)
from .utils import create_dcim_environment


def sample_naming_fn(placement):
    """Module-level callable used to exercise ``script`` mode (must be importable)."""
    return f"script:{placement.pk}"


not_callable_value = "I am a string, not a function"


def family_counter_naming_fn(placement):
    """A naming script of the shape a real deployment uses: the next free
    number in a family, counting persisted siblings AND the names already
    handed out in this session (``pending_names``).

    Exists to pin that a BATCH caller feeds each generated name back in as
    pending -- a script like this has no other way to avoid handing the same
    number to two placements it is asked about in one go.
    """
    taken = set(chain_placement_names(placement)) | set(pending_names(placement))
    n = 1
    while f"fam-{n}" in taken:
        n += 1
    return f"fam-{n}"


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

    # --- a device name is stored ONLY when the plan changes it --------------

    def test_keep_name_move_stores_empty_proposed_name(self):
        # self.p_move (setUpTestData) never sets proposed_name: a move that
        # keeps the device's own name writes nothing. Any "<design
        # title>-<name>" decoration a planner sees is produced at render time
        # (projection.py), never stored here.
        self.assertEqual(self.p_move.proposed_name, "")

    def test_chain_placement_names_includes_a_keep_name_move_under_its_real_name(self):
        # self.p_move is a keep-name move of self.devices[0]; with no
        # proposed_name to report, its EFFECTIVE name -- what
        # chain_placement_names must contribute to a family counter -- is the
        # device's own real name. Before this rule this row contributed an
        # empty string instead, invisible to any family regex.
        names = chain_placement_names(self.p_add)
        self.assertIn(self.devices[0].name, names)
        self.assertNotIn("", names)

    def test_chain_placement_names_issues_one_query_for_the_rows(self):
        # No lineage to walk (this design has no based_on) and no per-row
        # query: reading each row's device is select_related, not a query per
        # placement.
        with self.assertNumQueries(1):
            list(chain_placement_names(self.p_add))


class PeerNameClaimsTestCase(TestCase):
    """PLAN-peer-conflicts.md phase 1: :func:`peer_name_claims`, the
    peer-aware sibling of :func:`name_exists_in_site`. Unlike that function
    (which only answers True/False and deliberately does not exclude
    lineage/version siblings -- ``preview_name`` depends on exactly that),
    this one names WHICH of an already-filtered peer list claims the same
    effective name, and does no query of its own -- the caller
    (``projection.py``) has already fetched the peer placements once per
    rack."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.devices = env["devices"]  # Device 1 @ U1, Device 2 @ U2
        cls.design = Design.objects.create(title="Mine", site=cls.site)
        cls.peer_a = Design.objects.create(title="Peer A", site=cls.site)
        cls.peer_b = Design.objects.create(title="Peer B", site=cls.site)

    def _add(self, design, name):
        return DesignPlacement.objects.create(
            design=design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, proposed_name=name,
        )

    def _move(self, design, device, *, name=""):
        return DesignPlacement.objects.create(
            design=design, kind=DesignPlacementKindChoices.KIND_MOVE,
            device=device, proposed_name=name,
        )

    def test_effective_name_is_proposed_name_or_the_real_device_s_name(self):
        add = self._add(self.design, "planned-1")
        keep_move = self._move(self.design, self.devices[0])
        rename_move = self._move(self.design, self.devices[1], name="renamed-2")
        self.assertEqual(effective_name(add), "planned-1")
        self.assertEqual(effective_name(keep_move), self.devices[0].name)
        self.assertEqual(effective_name(rename_move), "renamed-2")

    def test_effective_name_is_none_for_an_unnamed_add_or_a_remove(self):
        unnamed_add = self._add(self.design, "")
        remove = DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.devices[0],
        )
        self.assertIsNone(effective_name(unnamed_add))
        self.assertIsNone(effective_name(remove))

    def test_peer_name_claims_matches_the_effective_name(self):
        mine = self._add(self.design, "dup-name")
        peer_hit = self._add(self.peer_a, "dup-name")
        peer_miss = self._add(self.peer_b, "no-collision")

        matches = peer_name_claims(mine, [peer_hit, peer_miss])
        self.assertEqual(matches, [peer_hit])

    def test_peer_name_claims_matches_a_keep_name_move_by_the_real_device_name(self):
        mine = self._move(self.design, self.devices[0])  # keep-name: real name
        peer_hit = self._add(self.peer_a, self.devices[0].name)
        self.assertEqual(peer_name_claims(mine, [peer_hit]), [peer_hit])

    def test_peer_name_claims_empty_when_this_placement_names_nothing(self):
        unnamed_add = self._add(self.design, "")
        peer = self._add(self.peer_a, "whatever")
        self.assertEqual(peer_name_claims(unnamed_add, [peer]), [])

    def test_peer_name_claims_does_no_query(self):
        mine = self._add(self.design, "dup-name")
        peer_hit = self._add(self.peer_a, "dup-name")
        with self.assertNumQueries(0):
            peer_name_claims(mine, [peer_hit])


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


# --- chain-wide family counters (PLAN-design-chains.md Sec 3.4) -------------


class ChainFamilyCounterTestCase(TestCase):
    """A numbered family must not restart inside a child design.

    ``_next_number`` / ``_next_pdu_slot`` used to count real devices plus
    ``DesignPlacement.objects.filter(design=placement.design)`` -- this design
    only -- so a child handed out a number an ancestor had already reserved
    (Sec 3.4). The counter now spans **ancestors + self**, matching an
    ancestor's row under its own EFFECTIVE name (there is no planning prefix
    to strip any more: a name is stored only when a plan actually changes it),
    and deliberately NOT siblings (Sec 2.1: two children of one parent are
    blind to each other; first approved wins and the other re-bases).
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

    def test_unchained_counter_counts_only_this_design(self):
        self._row(self.design_unchained, "ams1-sw-3")
        # Another design in the same site is invisible to an unchained counter,
        # exactly as before this change.
        self._row(self.design_a, "ams1-sw-9")
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_unchained), "ams1-sw-"),
            "4",
        )

    def test_unchained_counter_still_counts_real_devices_and_pending(self):
        Device.objects.create(
            name="ams1-sw-2", device_type=self.sw_type, role=self.sw_role,
            site=self.site, status="active",
        )
        placement = self._pending(self.design_unchained)
        self.assertEqual(naming_example._next_number(placement, "ams1-sw-"), "3")
        placement._rd_pending_names = ["ams1-sw-3"]
        self.assertEqual(naming_example._next_number(placement, "ams1-sw-"), "4")

    def test_unchained_counter_query_count_is_unchanged(self):
        self._row(self.design_unchained, "ams1-sw-3")
        placement = self._pending(self.design_unchained)
        # One Device scan + one placement scan, and NO lineage walk: a design
        # with no parent must not pay for the chain.
        with self.assertNumQueries(2):
            naming_example._next_number(placement, "ams1-sw-")

    # --- the gap: a child skips a number an ancestor reserved --------------

    def test_child_counter_skips_a_number_an_ancestor_reserved(self):
        self._row(self.design_a, "ams1-sw-5")
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_b), "ams1-sw-"), "6",
        )

    def test_ancestor_row_is_matched_by_its_own_stored_name(self):
        # An ancestor's row carries its EFFECTIVE name directly (there is no
        # planning prefix to strip any more), so the family regex fires on it
        # exactly as it would on any other row in the count.
        row = self._row(self.design_a, "ams1-sw-5")
        family = re.compile(r"^ams1-sw-(\d+)$")
        self.assertIsNotNone(family.match(row.proposed_name))
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_b), "ams1-sw-"), "6",
        )

    def test_three_deep_chain_counts_every_ancestor(self):
        self._row(self.design_a, "ams1-sw-4")
        self._row(self.design_b, "ams1-sw-7")
        self._row(self.design_c, "ams1-sw-9")
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_d), "ams1-sw-"), "10",
        )
        # ... and an ancestor layer does not see a DESCENDANT's reservation:
        # design_b sees its own row and design_a's, but not design_c's (a
        # design c is based on, not the other way around).
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_b), "ams1-sw-"), "8",
        )

    def test_chain_counter_query_count_at_depth_three(self):
        self._row(self.design_a, "ams1-sw-4")
        # Refetched, so the walk is really paid for: setUpTestData hands back
        # instances whose based_on FK is already cached.
        placement = self._pending(Design.objects.get(pk=self.design_d.pk))
        # Device scan + one lineage hop per ancestor (baseline_chain) + ONE
        # placement scan covering self AND every ancestor -- never one query per
        # ancestor per name.
        with self.assertNumQueries(5):
            naming_example._next_number(placement, "ams1-sw-")

    # --- Sec 2.1: siblings are NOT counted ---------------------------------

    def test_siblings_are_not_counted(self):
        self._row(self.design_a, "ams1-sw-5")
        self._row(self.design_sibling, "ams1-sw-9")
        # Sec 2.1: two children of one parent are blind to each other by
        # design; the sibling's 9 is invisible and the collision (if any)
        # surfaces later through name_exists_in_site's non-blocking warning.
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_b), "ams1-sw-"), "6",
        )

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

    def test_unapproved_ancestor_contributes_nothing(self):
        self._row(self.design_a, "ams1-sw-5")
        Design.objects.filter(pk=self.design_a.pk).update(
            status=DesignStatusChoices.STATUS_DRAFT)
        design_b = Design.objects.get(pk=self.design_b.pk)
        with self.assertLogs("netbox_rack_design.naming", level="WARNING"):
            self.assertEqual(
                naming_example._next_number(self._pending(design_b), "ams1-sw-"), "1",
            )

    def test_implemented_ancestor_contributes_nothing(self):
        self._row(self.design_a, "ams1-sw-5")
        Design.objects.filter(pk=self.design_a.pk).update(
            status=DesignStatusChoices.STATUS_IMPLEMENTED)
        design_b = Design.objects.get(pk=self.design_b.pk)
        with self.assertLogs("netbox_rack_design.naming", level="WARNING"):
            self.assertEqual(
                naming_example._next_number(self._pending(design_b), "ams1-sw-"), "1",
            )

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

    def test_same_name_in_two_layers_is_counted_once(self):
        # A adds "ams1-sw-5"; B's own row (independently) carries the same
        # literal name. The counter takes a MAX over matched names, so a
        # duplicate cannot inflate the family.
        self._row(self.design_a, "ams1-sw-5")
        self._row(self.design_b, "ams1-sw-5")
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_c), "ams1-sw-"), "6",
        )

    def test_child_row_referencing_an_ancestor_identity_does_not_skip(self):
        base = self._row(self.design_a, "ams1-sw-5")
        # The child re-plans that identity via base_placement, keeping its
        # name (no rename of its own) -- the ancestor's OWN row already
        # reserves "ams1-sw-5", so the family must still stand at 5 regardless
        # of what the child's row itself contributes.
        self._row(
            self.design_b, "",
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device_type=None, device_role=None, base_placement=base,
        )
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_b), "ams1-sw-"), "6",
        )

    def test_stale_ancestor_placement_is_still_counted(self):
        # A stale row is inert for PROJECTION (it renders nothing), but its name
        # is still reserved: name_exists_in_site matches it, so ignoring it here
        # would hand the child a name that immediately warns as a collision.
        self._row(
            self.design_a, "ams1-sw-5",
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device_type=None, device_role=None, target_rack=None,
            target_position=None, target_face="",
            stale=True, stale_device_name="ams1-sw-5",
        )
        self.assertEqual(
            naming_example._next_number(self._pending(self.design_b), "ams1-sw-"), "6",
        )

    # --- the PDU phase-slot counter, equivalently ---------------------------

    def test_pdu_slot_unchained_is_unchanged(self):
        self._row(self.design_unchained, "ams1-pdu-rr42-a1")
        self._row(self.design_a, "ams1-pdu-rr42-b2")
        self.assertEqual(
            naming_example._next_pdu_slot(
                self._pending(self.design_unchained), "ams1-pdu-rr42-"),
            "b1",
        )

    def test_pdu_slot_spans_the_chain(self):
        self._row(self.design_a, "ams1-pdu-rr42-a1")
        self.assertEqual(
            naming_example._next_pdu_slot(self._pending(self.design_b), "ams1-pdu-rr42-"),
            "b1",
        )

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

    def test_build_name_general_family_continues_across_the_chain(self):
        first = naming_example.build_name(self._pending(self.design_b))
        self.assertTrue(first.endswith("-1"), first)
        # The ancestor reserved that very name -- stored verbatim, no prefix.
        self._row(self.design_a, first)
        self.assertEqual(
            naming_example.build_name(self._pending(self.design_b)), first[:-1] + "2",
        )

    # --- the shared helper -------------------------------------------------

    def test_chain_placement_names_returns_one_effective_name_per_row(self):
        self._row(self.design_a, "ams1-sw-5")
        self._row(self.design_b, "ams1-sw-6")
        self._row(self.design_sibling, "ams1-sw-99")

        names = set(chain_placement_names(self._pending(self.design_c)))
        # Ancestors contribute their own stored name; the child's own rows do
        # too; a sibling contributes nothing.
        self.assertEqual(names, {"ams1-sw-5", "ams1-sw-6"})

    def test_chain_placement_names_excludes_the_placement_being_named(self):
        row = self._row(self.design_b, "ams1-sw-6")
        self.assertNotIn(row.proposed_name, chain_placement_names(row))

    def test_chain_placement_names_degrades_on_a_lineage_cycle(self):
        # A pre-existing cycle (nothing prevented one before clean() grew a
        # guard) must not loop or 500 a name preview: the chain is dropped and
        # the counter falls back to this design alone, loudly.
        Design.objects.filter(pk=self.design_a.pk).update(based_on=self.design_c)
        self._row(self.design_c, "ams1-sw-4")

        design_d = Design.objects.get(pk=self.design_d.pk)
        with self.assertLogs("netbox_rack_design.naming", level="WARNING"):
            self.assertEqual(
                list(chain_placement_names(self._pending(design_d))), [])


# --- the `naming` config sub-dict itself (independent of settled names) ----


class NamingConfigValidationTestCase(TestCase):
    """``naming_config()``/``validate_naming_config()`` validate the ``naming``
    config sub-dict's SHAPE, independent of whatever options (if any) are
    currently defined under it. ``DEFAULT_NAMING_OPTIONS`` is currently ``{}``
    (see naming.py's module docstring) -- no option is live today -- but the
    validation machinery itself is still wired into
    ``RackdesignConfig._rd_startup_checks()`` and must keep working so a
    malformed value fails the boot loudly rather than being silently ignored.
    """

    @override_settings(PLUGINS_CONFIG={"netbox_rack_design": {"naming": "nope"}})
    def test_naming_config_must_be_a_mapping(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_naming_config()

    @override_settings(
        PLUGINS_CONFIG={"netbox_rack_design": {"naming": {"anything": "x"}}}
    )
    def test_unknown_naming_config_key_is_rejected(self):
        # DEFAULT_NAMING_OPTIONS is {} -- no option is defined yet, so EVERY
        # key is unknown. That is the honest statement of today's behaviour,
        # and it is exactly what will fail loudly later if an option is added
        # to naming.py without also being added to DEFAULT_NAMING_OPTIONS.
        with self.assertRaises(ImproperlyConfigured) as ctx:
            validate_naming_config()
        self.assertIn("anything", str(ctx.exception))

    def test_non_string_naming_config_value_is_rejected(self):
        # With DEFAULT_NAMING_OPTIONS empty there is no KNOWN key today to
        # reach this branch through -- every key is rejected as unknown before
        # its value is ever type-checked (see the test above). The type check
        # itself still exists, ready for whenever an option is added, so it is
        # exercised here directly against a patched-in option.
        with patch("netbox_rack_design.naming.DEFAULT_NAMING_OPTIONS", {"example": ""}):
            with override_settings(
                PLUGINS_CONFIG={
                    "netbox_rack_design": {"naming": {"example": ["not a string"]}}
                }
            ):
                with self.assertRaises(ImproperlyConfigured):
                    validate_naming_config()

    @override_settings(PLUGINS_CONFIG={"netbox_rack_design": {"naming": {}}})
    def test_empty_naming_config_is_valid(self):
        self.assertEqual(validate_naming_config(), {})

    def test_startup_checks_include_naming_config_validation(self):
        # PluginConfig.ready() runs _rd_startup_checks() in order, so a
        # malformed 'naming' value fails the boot instead of surfacing much
        # later as a naming-time error.
        from netbox_rack_design import RackdesignConfig

        self.assertIn(validate_naming_config, RackdesignConfig._rd_startup_checks())
