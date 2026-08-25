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


class DisplayLabelProjectionTestCase(TestCase):
    """Tile label = ASSIGNED name (user ruling 2026-07-10): a slot's visible
    ``display_label`` is the placement's proposed_name when one exists,
    falling back to the identity label. The identity ``label`` itself is
    UNCHANGED (it anchors ghost pairing, harnesses, and the read-model);
    only the display layer shows the new name. Ghost (origin) slots keep the
    device's real name as their display -- the origin marker names what is
    physically there today."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]
        cls.device_type = env["device_type"]
        cls.design = Design.objects.create(title="Rename plan", site=cls.site)

    def test_renamed_move_in_display_label_is_proposed_name(self):
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],  # "Device 1" @ U1
            target_rack=self.racks[0],
            target_position=10,
            target_face="front",
            proposed_name="renamed-node-42",
        )
        result = project_rack(self.design, self.racks[0])
        move_ins = [s for s in result.front if s["state"] == ProjectedSlotState.MOVE_IN]
        self.assertEqual(len(move_ins), 1)
        # Identity label unchanged; display shows the assigned name.
        self.assertEqual(move_ins[0]["label"], self.devices[0].name)
        self.assertEqual(move_ins[0]["display_label"], "renamed-node-42")
        # The origin ghost keeps the physical device's name as its display.
        ghosts = [s for s in result.front if s["state"] == ProjectedSlotState.MOVE_OUT_GHOST]
        self.assertEqual(len(ghosts), 1)
        self.assertEqual(ghosts[0]["display_label"], self.devices[0].name)

    def test_unnamed_move_display_label_falls_back_to_device_name(self):
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[0],
            target_position=10,
            target_face="front",
        )
        result = project_rack(self.design, self.racks[0])
        move_ins = [s for s in result.front if s["state"] == ProjectedSlotState.MOVE_IN]
        self.assertEqual(move_ins[0]["display_label"], self.devices[0].name)


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

    def test_blade_in_a_chassis_bay_is_not_a_tray_slot(self):
        """A child device installed in a chassis DeviceBay keeps ``rack`` set and
        ``position`` NULL (core sets both: dcim/models/devices.py -- a child type
        may carry neither a position nor a face). It is therefore caught by the
        plain ``position__isnull=True`` tray query, and used to render beside the
        rack's real 0U accessories as though it were loose hardware. It is not:
        it lives inside its parent's bay and must be excluded from the tray."""
        from dcim.choices import SubdeviceRoleChoices
        from dcim.models import Device, DeviceBay, DeviceRole, DeviceType

        role = DeviceRole.objects.first()
        chassis_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer,
            model="Chassis-4Bay",
            slug="chassis-4bay",
            u_height=4,
            subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        )
        blade_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer,
            model="Blade",
            slug="blade",
            u_height=0,
            subdevice_role=SubdeviceRoleChoices.ROLE_CHILD,
        )
        chassis = Device.objects.create(
            name="Chassis-1", site=self.site, rack=self.racks[0], position=10,
            face="front", device_type=chassis_type, role=role,
        )
        blade = Device.objects.create(
            name="Blade-1", site=self.site, rack=self.racks[0], position=None,
            device_type=blade_type, role=role,
        )
        DeviceBay.objects.create(device=chassis, name="Bay 1", installed_device=blade)
        blade.refresh_from_db()
        self.assertIsNotNone(blade.rack_id, "core keeps the child device on the rack")
        self.assertIsNone(blade.position, "core forbids a position on a child device")

        result = project_rack(self.design, self.racks[0])
        tray_labels = {slot["label"] for slot in result.non_racked}
        self.assertNotIn("Blade-1", tray_labels)
        # the chassis itself is a normal racked device and is unaffected
        self.assertIn("Chassis-1", {slot["label"] for slot in result.front})

    def test_chassis_slot_carries_its_bays(self):
        """A parent device's slot exposes its DeviceBays so the editor and the
        read-only elevation can render the chassis as a container: every bay,
        occupied or empty, in core's own ordering."""
        from dcim.choices import SubdeviceRoleChoices
        from dcim.models import Device, DeviceBay, DeviceRole, DeviceType

        role = DeviceRole.objects.first()
        chassis_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer, model="Chassis-2Bay",
            slug="chassis-2bay", u_height=2,
            subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        )
        blade_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer, model="Blade-B",
            slug="blade-b", u_height=0,
            subdevice_role=SubdeviceRoleChoices.ROLE_CHILD,
        )
        chassis = Device.objects.create(
            name="Chassis-B", site=self.site, rack=self.racks[0], position=20,
            face="front", device_type=chassis_type, role=role,
        )
        blade = Device.objects.create(
            name="Blade-B1", site=self.site, rack=self.racks[0], position=None,
            device_type=blade_type, role=role,
        )
        DeviceBay.objects.create(device=chassis, name="Bay 1", installed_device=blade)
        DeviceBay.objects.create(device=chassis, name="Bay 2")

        result = project_rack(self.design, self.racks[0])
        slot = next(s for s in result.front if s["label"] == "Chassis-B")
        bays = slot["bays"]
        self.assertEqual([b["name"] for b in bays], ["Bay 1", "Bay 2"])
        self.assertTrue(bays[0]["occupied"])
        self.assertEqual(bays[0]["device"], blade)
        self.assertEqual(bays[0]["label"], "Blade-B1")
        self.assertEqual(bays[0]["device_type"], blade_type)
        self.assertFalse(bays[1]["occupied"])
        self.assertIsNone(bays[1]["device"])

    def test_non_parent_device_slot_has_no_bays(self):
        """An ordinary device carries an empty bay list -- the key always exists
        so consumers never have to guard on its presence."""
        from dcim.models import Device, DeviceRole

        Device.objects.create(
            name="Plain-1", site=self.site, rack=self.racks[0], position=30,
            face="front", device_type=self.device_type,
            role=DeviceRole.objects.first(),
        )
        result = project_rack(self.design, self.racks[0])
        slot = next(s for s in result.front if s["label"] == "Plain-1")
        self.assertEqual(slot["bays"], [])

    def test_planned_blade_shows_in_its_real_chassis_bay(self):
        """A blade planned into a REAL chassis bay renders inside that chassis's
        strip, in the bay it targets -- not as a loose tray slot."""
        from dcim.choices import SubdeviceRoleChoices
        from dcim.models import Device, DeviceBayTemplate, DeviceRole, DeviceType

        from ..choices import DesignPlacementKindChoices
        from ..models import DesignPlacement

        role = DeviceRole.objects.first()
        chassis_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer, model="Chassis-P",
            slug="chassis-p", u_height=2,
            subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        )
        DeviceBayTemplate.objects.create(device_type=chassis_type, name="b1")
        blade_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer, model="Blade-P",
            slug="blade-p", u_height=0,
            subdevice_role=SubdeviceRoleChoices.ROLE_CHILD,
        )
        chassis = Device.objects.create(
            name="Chassis-P1", site=self.site, rack=self.racks[0], position=8,
            face="front", device_type=chassis_type, role=role,
        )
        bay = chassis.devicebays.get(name="b1")
        placement = DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=blade_type, target_rack=self.racks[0],
            target_bay=bay, target_bay_name="b1", proposed_name="new-blade-1",
        )

        result = project_rack(self.design, self.racks[0])
        self.assertNotIn("new-blade-1", {s["label"] for s in result.non_racked})
        slot = next(s for s in result.front if s["label"] == "Chassis-P1")
        entry = next(b for b in slot["bays"] if b["name"] == "b1")
        self.assertEqual(entry["state"], ProjectedSlotState.ADD)
        self.assertEqual(entry["label"], "new-blade-1")
        self.assertEqual(entry["placement"], placement)
        self.assertTrue(entry["occupied"])

    def test_planned_blade_shows_in_a_planned_chassis(self):
        """A blade planned into a chassis that is itself an 'add' renders in the
        planned chassis's own strip, built from the type's bay templates."""
        from dcim.choices import SubdeviceRoleChoices
        from dcim.models import DeviceBayTemplate, DeviceType

        from ..choices import DesignPlacementKindChoices
        from ..models import DesignPlacement

        chassis_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer, model="Chassis-Q",
            slug="chassis-q", u_height=2,
            subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        )
        DeviceBayTemplate.objects.create(device_type=chassis_type, name="q1")
        DeviceBayTemplate.objects.create(device_type=chassis_type, name="q2")
        blade_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer, model="Blade-Q",
            slug="blade-q", u_height=0,
            subdevice_role=SubdeviceRoleChoices.ROLE_CHILD,
        )
        chassis_p = DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=chassis_type, target_rack=self.racks[0],
            target_position=6, target_face="front", proposed_name="new-chassis",
        )
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=blade_type, target_rack=self.racks[0],
            parent_placement=chassis_p, target_bay_name="q1",
            proposed_name="new-blade-q1",
        )

        result = project_rack(self.design, self.racks[0])
        slot = next(s for s in result.front if s["label"] == "new-chassis")
        self.assertEqual([b["name"] for b in slot["bays"]], ["q1", "q2"])
        filled = next(b for b in slot["bays"] if b["name"] == "q1")
        self.assertEqual(filled["label"], "new-blade-q1")
        self.assertEqual(filled["state"], ProjectedSlotState.ADD)
        self.assertTrue(filled["occupied"])
        self.assertFalse(next(b for b in slot["bays"] if b["name"] == "q2")["occupied"])

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
