"""
Tests for ``projection.project_rack``'s non-racked tray (spec §9.1/§9.2).

Covers the 0.9.0 headline behaviour: real DCIM devices associated with a rack
but not mounted at a U (``Device.rack == rack and Device.position is None``)
must be projected as ``existing`` slots in ``non_racked`` -- exactly like a
racked existing device, just without a U/face -- and a rack with none must
project an empty tray. Design-touched tray devices (moved/removed) must NOT
double up with the plain existing pass; they get their own design-aware slot.
"""

from django.test import TestCase
from utilities.testing import create_test_device

from ..choices import DesignPlacementKindChoices
from ..models import Design, DesignPlacement
from ..projection import ProjectedSlotState, project_rack
from .utils import create_dcim_environment


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
