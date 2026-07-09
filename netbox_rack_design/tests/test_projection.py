"""
Tests for ``projection.project_rack``'s non-racked tray (spec §9.1/§9.2) and
the server-side displacement marking (spec §3/§4.3, parity ruling 2026-07-09).

Covers the 0.9.0 tray behaviour: real DCIM devices associated with a rack
but not mounted at a U (``Device.rack == rack and Device.position is None``)
must be projected as ``existing`` slots in ``non_racked`` -- exactly like a
racked existing device, just without a U/face -- and a rack with none must
project an empty tray. Design-touched tray devices (moved/removed) must NOT
double up with the plain existing pass; they get their own design-aware slot.

Also covers displaced-slot marking: a vacating slot whose rows are occupied
by a live planned slot must come back ``displaced`` with ``displaced_by``, so
the read-only elevation (and the editor's on-load render) can apply the
stripe treatment without re-deriving the knowledge client-side.
"""

from django.test import TestCase
from utilities.testing import create_test_device

from ..choices import DesignPlacementKindChoices
from ..models import Design, DesignPlacement
from ..projection import ProjectedSlotState, project_rack
from .utils import create_dcim_environment


class DisplacedProjectionTestCase(TestCase):
    """Server-side displacement marking (spec §4.3 / §3 stripe): a vacating
    slot (move_out_ghost or remove) whose rows are occupied by a live planned
    slot (add/move_in) at the same rack+face rows is marked ``displaced``
    with ``displaced_by`` naming the occupant."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]  # Device 1 @ Rack1/U1/front, Device 2 @ U2/front
        cls.device_type = env["device_type"]
        cls.design = Design.objects.create(title="Displace plan", site=cls.site)

    def test_ghost_overlapped_by_add_is_marked_displaced(self):
        # Device 1 moves U1 -> U10; a new add lands on the vacated U1.
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[0],
            target_position=10,
            target_face="front",
        )
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[0],
            target_position=1,
            target_face="front",
            proposed_name="NEW-in-vacated-slot",
        )
        result = project_rack(self.design, self.racks[0])
        ghosts = [s for s in result.front if s["state"] == ProjectedSlotState.MOVE_OUT_GHOST]
        self.assertEqual(len(ghosts), 1, ghosts)
        self.assertTrue(
            ghosts[0]["displaced"],
            f"ghost overlapped by an add at the same rows must be displaced: {ghosts[0]}")
        self.assertEqual(ghosts[0]["displaced_by"], "NEW-in-vacated-slot")
        # The occupying add itself is NOT displaced.
        adds = [s for s in result.front if s["state"] == ProjectedSlotState.ADD]
        self.assertEqual(len(adds), 1)
        self.assertFalse(adds[0]["displaced"])

    def test_remove_overlapped_by_move_in_is_marked_displaced(self):
        # Device 2 (U2) is flagged for removal; Device 1 moves onto U2.
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.devices[1],
        )
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[0],
            target_position=2,
            target_face="front",
        )
        result = project_rack(self.design, self.racks[0])
        removes = [s for s in result.front if s["state"] == ProjectedSlotState.REMOVE]
        self.assertEqual(len(removes), 1, removes)
        self.assertTrue(removes[0]["displaced"], removes[0])
        self.assertEqual(removes[0]["displaced_by"], self.devices[0].name)

    def test_unoccupied_ghost_is_not_displaced(self):
        # A plain move with nothing landing on the vacated rows: no marking.
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[0],
            target_position=10,
            target_face="front",
        )
        result = project_rack(self.design, self.racks[0])
        ghosts = [s for s in result.front if s["state"] == ProjectedSlotState.MOVE_OUT_GHOST]
        self.assertEqual(len(ghosts), 1)
        self.assertFalse(ghosts[0]["displaced"])
        self.assertIsNone(ghosts[0]["displaced_by"])

    def test_devices_own_move_never_displaces_its_own_ghost(self):
        # A device's own move_in must never mark its own origin ghost as
        # displaced (same placement -- spec §4.2: a device's own footprint
        # never blocks/displaces itself). Strongest case: a FULL-DEPTH device
        # whose ghost and move_in copies land on BOTH faces at overlapping
        # rows (a 1U shift), so every face list has a same-placement overlap.
        from dcim.models import Device, DeviceType

        fd_type = DeviceType.objects.create(
            manufacturer=self.devices[0].device_type.manufacturer,
            model="FD Self", slug="fd-self", u_height=2, is_full_depth=True,
        )
        fd_dev = Device.objects.create(
            name="FD Self Device", site=self.site, rack=self.racks[0],
            position=20, face="front", device_type=fd_type,
            role=self.devices[0].role,
        )
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=fd_dev,          # U20-21 -> U21-22: overlaps its own ghost
            target_rack=self.racks[0],
            target_position=21,
            target_face="front",
        )
        result = project_rack(self.design, self.racks[0])
        for face in (result.front, result.rear):
            ghosts = [s for s in face
                      if s["state"] == ProjectedSlotState.MOVE_OUT_GHOST
                      and s["label"] == "FD Self Device"]
            self.assertEqual(len(ghosts), 1, ghosts)
            self.assertFalse(
                ghosts[0]["displaced"],
                f"a device's own move_in must not displace its own ghost: {ghosts[0]}")


class TrayProjectionTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]
        cls.design = Design.objects.create(title="Tray plan", site=cls.site)

    def test_real_tray_device_appears_as_existing(self):
        """A real position-less device (e.g. a vertical PDU) shows up in
        non_racked as an 'existing' slot, unchanged, device set."""
        pdu = create_test_device(
            "PDU-A1",
            site=self.site,
            rack=self.racks[0],
            position=None,
            face="rear",
        )
        result = project_rack(self.design, self.racks[0])
        labels = {slot["label"]: slot for slot in result.non_racked}
        self.assertIn("PDU-A1", labels)
        slot = labels["PDU-A1"]
        self.assertEqual(slot["state"], ProjectedSlotState.EXISTING)
        self.assertIsNone(slot["u_position"])
        self.assertEqual(slot["device"], pdu)
        self.assertEqual(slot["device_type"], self.device_type)
        self.assertIsNone(slot["placement"])
        # A tray slot's face is always "" (spec §9.2) -- the device's real
        # face (here 'rear') carries no layout meaning off-rack.
        self.assertEqual(slot["face"], "")

    def test_rack_without_tray_devices_has_empty_non_racked(self):
        """A rack with zero position-less devices and no design placements
        projects an empty tray (the negative case)."""
        result = project_rack(self.design, self.racks[1])
        self.assertEqual(result.non_racked, [])

    def test_moved_tray_device_uses_placement_projection_not_existing(self):
        """A tray device the design MOVES (e.g. onto a U) is excluded from the
        plain existing pass -- it must not double up -- and instead renders
        via the normal move projection (ghost at its tray origin, move_in at
        the target)."""
        pdu = create_test_device(
            "PDU-B1",
            site=self.site,
            rack=self.racks[0],
            position=None,
            face="rear",
        )
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=pdu,
            target_rack=self.racks[0],
            target_position=20,
            target_face="front",
        )
        result = project_rack(self.design, self.racks[0])
        # No plain 'existing' PDU-B1 entry left in non_racked.
        existing_labels = [
            s["label"] for s in result.non_racked if s["state"] == ProjectedSlotState.EXISTING
        ]
        self.assertNotIn("PDU-B1", existing_labels)
        # A move_in slot lands on the front face at U20.
        move_in = [s for s in result.front if s["state"] == ProjectedSlotState.MOVE_IN]
        self.assertEqual(len(move_in), 1)
        self.assertEqual(move_in[0]["device"], pdu)
        self.assertEqual(float(move_in[0]["u_position"]), 20.0)

    def test_removed_tray_device_excluded_from_plain_existing(self):
        """A tray device flagged for removal is excluded from the plain
        existing pass; it gets its own 'remove' slot instead."""
        pdu = create_test_device(
            "PDU-C1",
            site=self.site,
            rack=self.racks[0],
            position=None,
            face="",
        )
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=pdu,
        )
        result = project_rack(self.design, self.racks[0])
        existing_labels = [
            s["label"] for s in result.non_racked if s["state"] == ProjectedSlotState.EXISTING
        ]
        self.assertNotIn("PDU-C1", existing_labels)
        remove_slots = [s for s in result.non_racked if s["state"] == ProjectedSlotState.REMOVE]
        self.assertEqual(len(remove_slots), 1)
        self.assertEqual(remove_slots[0]["device"], pdu)
