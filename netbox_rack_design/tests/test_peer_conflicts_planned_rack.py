"""
Peer conflicts across a SHARED planned rack (PLAN-templates.md D6/D28, T1.4c).

A ``PlannedRack`` is a shared object exactly like a real ``dcim.Rack`` (D6):
two different draft designs can both scope it, and the existing peer-conflict
machinery (``_peer_placements``/``_peer_conflicts``/``peer_placements_for_design``
in ``projection.py``) is supposed to tell each planner about the other's claim
-- "no new projection path" per D6's own wording. Before this task a guard
(``if rackinfo.is_planned(rack): return []``) made that entirely invisible for
a planned rack: two designs claiming the very same planned rack's units simply
never saw each other.

Peer conflicts are pure REPORTING (PLAN-templates.md D12 / projection.py's own
peer-conflicts section docstring): ``_peer_conflicts`` iterates only over
slots THIS design's own placements already occupy, flags them
``peer_conflict`` and appends a ``peer_slot_claim`` entry. It never marks an
empty row illegal and never blocks a placement -- that must stay true for
planned racks too, so several tests below assert the "non-blocking" half
explicitly, not just the "reported" half.

D28's same-pk landmine applies here with full force: ``PlannedRack`` and
``dcim.Rack`` keep SEPARATE pk sequences, so a naive fix that filters
``design__racks=rack`` OR routes a peer comparison through a bare
``peer.target_rack_id`` (rather than ``rackinfo.placement_targets_rack``)
would make a planned rack report the peers of whatever unrelated real rack
happens to share its pk. ``PlannedRackPeerConflictPkCollisionTestCase`` below
forces that collision on purpose, following the same pattern as
``test_projection_planned_rack_pk.py``: without the forced collision the test
would pass "by luck" and prove nothing.
"""

from dcim.models import Location
from django.test import TestCase, override_settings

from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignPlacement, PlannedRack
from ..projection import peer_placements_for_design, project_rack
from .utils import create_dcim_environment


def _peer_plugins_config(**overrides):
    cfg = {"peer_conflicts_enabled": True}
    cfg.update(overrides)
    return {"netbox_rack_design": cfg}


class PeerConflictPlannedRackTestCase(TestCase):
    """Two draft designs sharing one ``PlannedRack``, overlapping placements."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.location = Location.objects.create(
            name="Peer Loc", slug="peer-loc", site=cls.site,
        )
        cls.planned_rack = PlannedRack.objects.create(
            name="Shared Planned Rack", location=cls.location, u_height=10,
        )

    def _design(self, title, *, status=DesignStatusChoices.STATUS_DRAFT):
        design = Design.objects.create(title=title, site=self.site)
        if status != DesignStatusChoices.STATUS_DRAFT:
            design.status = status
            design.save()
        return design

    def _scope(self, design):
        design.planned_racks.add(self.planned_rack)
        return design

    def _add(self, design, position, *, name):
        return DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_planned_rack=self.planned_rack,
            target_position=position,
            target_face="front",
            proposed_name=name,
        )

    def _by_kind(self, conflicts, kind):
        return [c for c in conflicts if c["kind"] == kind]

    def _at(self, slots, position):
        return [s for s in slots if s["u_position"] is not None and int(s["u_position"]) == position]

    def test_two_drafts_overlapping_a_unit_on_shared_planned_rack_report_each_other(self):
        a = self._design("Planned Peer A")
        self._scope(a)
        self._add(a, 3, name="a-dev")

        b = self._design("Planned Peer B")
        self._scope(b)
        self._add(b, 3, name="b-dev")

        result = project_rack(a, self.planned_rack)
        claims = self._by_kind(result.conflicts, "peer_slot_claim")
        self.assertEqual(len(claims), 1, result.conflicts)
        self.assertEqual(claims[0]["source_design"], b)
        self.assertIn("U3", claims[0]["detail"])

        at3 = self._at(result.front, 3)
        self.assertEqual(len(at3), 1, at3)
        self.assertTrue(at3[0]["peer_conflict"])
        self.assertEqual(at3[0]["peer_design_id"], b.pk)
        self.assertEqual(at3[0]["peer_design_title"], str(b))

        # Symmetric: B's own projection reports A right back.
        result_b = project_rack(b, self.planned_rack)
        claims_b = self._by_kind(result_b.conflicts, "peer_slot_claim")
        self.assertEqual(len(claims_b), 1, result_b.conflicts)
        self.assertEqual(claims_b[0]["source_design"], a)

    def test_peer_conflict_is_pure_reporting_not_blocking(self):
        """A slot that a peer claims still renders/ADDs normally -- flagged,
        never marked illegal, and the placement is never refused."""
        a = self._design("Planned Peer C")
        self._scope(a)
        self._add(a, 5, name="c-dev")

        b = self._design("Planned Peer D")
        self._scope(b)
        self._add(b, 5, name="d-dev")

        result = project_rack(a, self.planned_rack)
        at5 = self._at(result.front, 5)
        self.assertEqual(len(at5), 1, at5)
        # Flagged...
        self.assertTrue(at5[0]["peer_conflict"])
        # ...but not illegal/blocked.
        self.assertFalse(at5[0]["conflict"], at5[0])
        self.assertIsNone(at5[0].get("conflict_reason"))

    def test_peer_placements_for_design_includes_planned_rack_peers(self):
        a = self._design("Planned Peer E")
        self._scope(a)
        placement_a = self._add(a, 7, name="e-dev")

        b = self._design("Planned Peer F")
        self._scope(b)
        placement_b = self._add(b, 7, name="f-dev")

        peers = peer_placements_for_design(a)
        self.assertIn(placement_b, peers)
        self.assertNotIn(placement_a, peers)

    def test_non_draft_peer_is_excluded(self):
        a = self._design("Planned Peer G")
        self._scope(a)
        self._add(a, 2, name="g-dev")

        approved = self._design("Planned Peer H", status=DesignStatusChoices.STATUS_APPROVED)
        self._scope(approved)
        self._add(approved, 2, name="h-dev")
        # Approved is not implemented, so it IS a peer (severity differs, but
        # it still reports) -- this is the sibling case to prove the exclusion
        # below is about IMPLEMENTED specifically, matching the real-rack rule.
        result = project_rack(a, self.planned_rack)
        claims = self._by_kind(result.conflicts, "peer_slot_claim")
        self.assertEqual(len(claims), 1, result.conflicts)
        self.assertEqual(claims[0]["source_design"], approved)

        implemented = self._design("Planned Peer I", status=DesignStatusChoices.STATUS_IMPLEMENTED)
        self._scope(implemented)
        self._add(implemented, 2, name="i-dev")

        result2 = project_rack(a, self.planned_rack)
        claims2 = self._by_kind(result2.conflicts, "peer_slot_claim")
        designs_reporting = {c["source_design"] for c in claims2}
        self.assertNotIn(implemented, designs_reporting)

    @override_settings(PLUGINS_CONFIG=_peer_plugins_config(peer_conflicts_enabled=False))
    def test_flag_off_suppresses_planned_rack_peers_too(self):
        a = self._design("Planned Peer J")
        self._scope(a)
        self._add(a, 4, name="j-dev")

        b = self._design("Planned Peer K")
        self._scope(b)
        self._add(b, 4, name="k-dev")

        result = project_rack(a, self.planned_rack)
        self.assertEqual(result.conflicts, [])
        at4 = self._at(result.front, 4)
        self.assertFalse(at4[0]["peer_conflict"])

        self.assertEqual(peer_placements_for_design(a), [])


class PeerConflictRealRackRegressionTestCase(TestCase):
    """The pre-existing real-rack peer behaviour must stay identical."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]

    def test_two_drafts_overlapping_a_unit_on_real_rack_report_each_other(self):
        a = Design.objects.create(title="Real Peer A", site=self.site)
        a.racks.add(self.racks[1])
        DesignPlacement.objects.create(
            design=a, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_rack=self.racks[1],
            target_position=9, target_face="front", proposed_name="a-real",
        )

        b = Design.objects.create(title="Real Peer B", site=self.site)
        b.racks.add(self.racks[1])
        DesignPlacement.objects.create(
            design=b, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_rack=self.racks[1],
            target_position=9, target_face="front", proposed_name="b-real",
        )

        result = project_rack(a, self.racks[1])
        claims = [c for c in result.conflicts if c["kind"] == "peer_slot_claim"]
        self.assertEqual(len(claims), 1, result.conflicts)
        self.assertEqual(claims[0]["source_design"], b)

        peers = peer_placements_for_design(a)
        self.assertEqual(len(peers), 1)


class PlannedRackPeerConflictPkCollisionTestCase(TestCase):
    """D28: force a ``PlannedRack``/``dcim.Rack`` pk collision and prove peer
    conflicts never cross the streams in either direction."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.real_rack = env["racks"][0]
        cls.device_type = env["device_type"]

        cls.location = Location.objects.create(
            name="Collision Loc", slug="collision-loc", site=cls.site,
        )
        cls.planned_rack = PlannedRack(
            name="Colliding Planned Rack", location=cls.location, u_height=6,
        )
        cls.planned_rack.pk = cls.real_rack.pk
        cls.planned_rack.save(force_insert=True)
        assert cls.planned_rack.pk == cls.real_rack.pk, (
            "setup did not actually force the pk collision this test needs"
        )

        # A design that scopes the REAL rack (same pk as the planned one) and
        # claims U4 there.
        cls.real_design = Design.objects.create(title="Real Rack Claimant", site=cls.site)
        cls.real_design.racks.add(cls.real_rack)
        DesignPlacement.objects.create(
            design=cls.real_design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type, target_rack=cls.real_rack,
            target_position=4, target_face="front", proposed_name="real-claim",
        )

        # A design that scopes the PLANNED rack (same pk) and claims the SAME
        # unit number, U4, there.
        cls.planned_design = Design.objects.create(title="Planned Rack Claimant", site=cls.site)
        cls.planned_design.planned_racks.add(cls.planned_rack)
        DesignPlacement.objects.create(
            design=cls.planned_design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type, target_planned_rack=cls.planned_rack,
            target_position=4, target_face="front", proposed_name="planned-claim",
        )

    def test_planned_rack_projection_does_not_see_real_racks_peers(self):
        result = project_rack(self.planned_design, self.planned_rack)
        claims = [c for c in result.conflicts if c["kind"] == "peer_slot_claim"]
        self.assertEqual(claims, [], result.conflicts)
        peers = peer_placements_for_design(self.planned_design)
        self.assertEqual(peers, [])

    def test_real_rack_projection_does_not_see_planned_racks_peers(self):
        result = project_rack(self.real_design, self.real_rack)
        claims = [c for c in result.conflicts if c["kind"] == "peer_slot_claim"]
        self.assertEqual(claims, [], result.conflicts)
        peers = peer_placements_for_design(self.real_design)
        self.assertEqual(peers, [])
