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

from decimal import Decimal

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from users.models import User
from utilities.testing import create_test_device

from .. import apply
from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignPlacement
from ..projection import (
    ProjectedSlotState,
    chassis_in_scope,
    has_chassis_in_scope,
    project_chassis,
    project_rack,
)
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
    ``display_label`` is the placement's ``proposed_name`` when one exists
    (a rename), and a "<design title>-<real name>" decoration produced at
    RENDER time when it does not (a keep-name move stores nothing). The
    identity ``label`` itself is UNCHANGED either way (it anchors ghost
    pairing, harnesses, and the read-model); only the display layer shows the
    decorated/assigned name. Ghost (origin) slots keep the device's real name,
    undecorated, as their display -- the origin marker names what is
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

    def test_keep_name_move_stores_nothing_and_display_is_decorated(self):
        # A move that does not rename stores an empty proposed_name -- there is
        # nothing to write down -- and the "<design title>-<real name>"
        # decoration is produced HERE, at render time, never stored.
        placement = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[0],
            target_position=10,
            target_face="front",
        )
        self.assertEqual(placement.proposed_name, "")
        result = project_rack(self.design, self.racks[0])
        move_ins = [s for s in result.front if s["state"] == ProjectedSlotState.MOVE_IN]
        self.assertEqual(move_ins[0]["label"], self.devices[0].name)
        self.assertEqual(
            move_ins[0]["display_label"], f"{self.design.title}-{self.devices[0].name}"
        )


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

    def test_a_removed_blade_still_renders_in_its_bay(self):
        """REGRESSION (user 2026-08-26): "Save throws away what I flagged for
        removal".

        It never did -- the removal was stored and then rendered as NOTHING. A
        removal takes no target (the model forbids one), so matching a chassis's
        blades solely on target_bay/parent_placement could not find it, and the
        blade reappeared looking untouched on the next load.
        """
        from dcim.choices import SubdeviceRoleChoices
        from dcim.models import Device, DeviceBay, DeviceRole, DeviceType

        self.design.racks.add(self.racks[0])
        role = DeviceRole.objects.first()
        chassis_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer, model="RM-Chassis",
            slug="rm-chassis", u_height=2,
            subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        )
        blade_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer, model="RM-Blade",
            slug="rm-blade", u_height=0,
            subdevice_role=SubdeviceRoleChoices.ROLE_CHILD,
        )
        chassis = Device.objects.create(
            name="RM-Chassis-1", site=self.site, rack=self.racks[0], position=12,
            face="front", device_type=chassis_type, role=role,
        )
        blade = Device.objects.create(
            name="RM-Blade-1", site=self.site, rack=self.racks[0], position=None,
            device_type=blade_type, role=role,
        )
        DeviceBay.objects.create(device=chassis, name="b1", installed_device=blade)
        DeviceBay.objects.create(device=chassis, name="b2")
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=blade,
        )

        entry = next(e for e in chassis_in_scope(self.design) if e["device"] == chassis)
        column = project_chassis(self.design, entry)
        slot = next(s for s in column["slots"] if s["name"] == "b1")
        self.assertEqual(slot["state"], ProjectedSlotState.REMOVE)
        self.assertEqual(slot["label"], "RM-Blade-1")
        # A vacating blade does not count against the chassis's capacity.
        self.assertEqual(column["used"], 0)

    def test_bayless_parent_device_is_not_a_chassis(self):
        """``subdevice_role=parent`` alone does NOT make a chassis: the role is
        widely set on plain servers (one 4475-device instance had 2306 such
        devices with no bay at all), and each would draw an empty 0/0 column in
        the chassis layer that nothing can ever be dropped into. A device earns
        a column by HAVING A BAY (user 2026-08-26)."""
        from dcim.choices import SubdeviceRoleChoices
        from dcim.models import Device, DeviceBay, DeviceRole, DeviceType

        self.design.racks.add(self.racks[0])
        role = DeviceRole.objects.first()
        parent_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer, model="Server-sff8",
            slug="server-sff8", u_height=1,
            subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        )
        Device.objects.create(
            name="Bayless-1", site=self.site, rack=self.racks[0], position=30,
            face="front", device_type=parent_type, role=role,
        )
        self.assertFalse(has_chassis_in_scope(self.design))
        self.assertEqual(chassis_in_scope(self.design), [])

        # Give it one bay and it becomes a chassis, listed exactly once.
        real = Device.objects.get(name="Bayless-1")
        DeviceBay.objects.create(device=real, name="Bay 1")
        DeviceBay.objects.create(device=real, name="Bay 2")
        self.assertTrue(has_chassis_in_scope(self.design))
        entries = chassis_in_scope(self.design)
        self.assertEqual([e["key"] for e in entries], [f"dev-{real.pk}"])

    def test_planned_chassis_without_bay_templates_is_not_a_chassis(self):
        """Same rule for a PLANNED chassis, read off its type: with no
        DeviceBayTemplate no bay will exist once the design is applied, so there
        is nothing to plan into."""
        from dcim.choices import SubdeviceRoleChoices
        from dcim.models import DeviceBayTemplate, DeviceType

        self.design.racks.add(self.racks[0])
        parent_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer, model="Planned-sff10",
            slug="planned-sff10", u_height=1,
            subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        )
        placement = DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=parent_type, target_rack=self.racks[0],
            target_position=35, target_face="front",
        )
        self.assertFalse(has_chassis_in_scope(self.design))
        self.assertEqual(chassis_in_scope(self.design), [])

        DeviceBayTemplate.objects.create(device_type=parent_type, name="Bay 1")
        self.assertTrue(has_chassis_in_scope(self.design))
        entries = chassis_in_scope(self.design)
        self.assertEqual([e["key"] for e in entries], [f"pl-{placement.pk}"])

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


class StalePlacementProjectionTestCase(TestCase):
    """A placement whose real device was deleted (``stale=True``, ``device`` now
    null) must be invisible to projection: it renders neither a ghost/move_in
    slot nor a remove slot, and project_rack must not raise on it."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]  # Device 1 @ Rack1/U1/front, Device 2 @ Rack1/U2/front
        cls.design = Design.objects.create(title="Stale plan", site=cls.site)

    def test_stale_move_and_remove_disappear_from_projection(self):
        move = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[1],
            target_position=5,
        )
        remove = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.devices[1],
        )

        # Before deletion: the move's ghost/move_in and the remove's slot are
        # present in the elevation.
        before_source = project_rack(self.design, self.racks[0])
        ghosts = [s for s in before_source.front if s["state"] == ProjectedSlotState.MOVE_OUT_GHOST]
        self.assertEqual(len(ghosts), 1)
        removes = [s for s in before_source.front if s["state"] == ProjectedSlotState.REMOVE]
        self.assertEqual(len(removes), 1)
        before_target = project_rack(self.design, self.racks[1])
        move_ins = [s for s in before_target.front if s["state"] == ProjectedSlotState.MOVE_IN]
        self.assertEqual(len(move_ins), 1)

        device1_pk, device2_pk = self.devices[0].pk, self.devices[1].pk
        self.devices[0].delete()
        self.devices[1].delete()
        move.refresh_from_db()
        remove.refresh_from_db()
        self.assertTrue(move.stale)
        self.assertTrue(remove.stale)

        # After deletion: project_rack must not raise, and the stale rows
        # produce no slots at all in either rack.
        after_source = project_rack(self.design, self.racks[0])
        after_ghosts = [
            s for s in after_source.front if s["state"] == ProjectedSlotState.MOVE_OUT_GHOST
        ]
        after_removes = [s for s in after_source.front if s["state"] == ProjectedSlotState.REMOVE]
        self.assertEqual(after_ghosts, [])
        self.assertEqual(after_removes, [])
        after_target = project_rack(self.design, self.racks[1])
        after_move_ins = [
            s for s in after_target.front if s["state"] == ProjectedSlotState.MOVE_IN
        ]
        self.assertEqual(after_move_ins, [])

        # Sanity: the deleted devices are really gone, not just unlinked.
        from dcim.models import Device

        self.assertFalse(Device.objects.filter(pk__in=[device1_pk, device2_pk]).exists())


# ---------------------------------------------------------------------------
# Layered projection across a design chain (PLAN-design-chains.md G1 / §9.2)
# ---------------------------------------------------------------------------


class ChainProjectionTestCase(TestCase):
    """``project_rack`` replays an approved ancestor's placements as BASELINE.

    PLAN-design-chains.md §9.2: a parent contributes its layer whole or not at
    all. An ancestor ``add`` occupies its target U, a ``move`` vacates the
    source AND occupies the target, a ``remove`` frees the U -- and from the
    child's point of view all of that has already happened, so it renders as
    part of the world (``existing`` + ``inherited``), never as this design's
    own ``add`` / ``move_in`` / ``remove`` proposal.
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]  # Device 1 @ Rack1/U1/front, Device 2 @ U2/front
        cls.device_type = env["device_type"]

    # --- helpers -----------------------------------------------------------

    def _design(self, title, *, based_on=None):
        return Design.objects.create(title=title, site=self.site, based_on=based_on)

    def _approve(self, design):
        """Approve a design AFTER its placements exist.

        An approved design is frozen (``DesignPlacement.clean()`` rejects
        writes), so every fixture here builds the layer first and approves
        second -- which is also the real lifecycle.
        """
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()
        return design

    def _add(self, design, position, *, name="", rack=None, device_type=None, face="front"):
        return DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=device_type or self.device_type,
            target_rack=rack or self.racks[0],
            target_position=position,
            target_face=face,
            proposed_name=name,
        )

    def _at(self, slots, position):
        return [s for s in slots if s["u_position"] is not None and int(s["u_position"]) == position]

    def _states(self, slots):
        return sorted(s["state"] for s in slots)

    # --- an ancestor add is baseline, not a planned add ---------------------

    def test_ancestor_add_appears_as_baseline_not_as_a_planned_add(self):
        a = self._design("Network sweep IDS-1000")
        upstream = self._add(a, 10, name="srv-a")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        result = project_rack(b, self.racks[0])

        at10 = self._at(result.front, 10)
        self.assertEqual(len(at10), 1, at10)
        slot = at10[0]
        self.assertEqual(slot["state"], ProjectedSlotState.EXISTING,
                         "an ancestor's add has already happened from the child's point "
                         "of view: it is part of the world, not a planned add")
        self.assertTrue(slot["inherited"])
        self.assertEqual(slot["source_design_id"], a.pk)
        self.assertEqual(slot["placement"], upstream)
        self.assertEqual(slot["device_type"], self.device_type)
        self.assertIsNone(slot["device"])
        # Nothing anywhere renders it as this design's own proposal.
        self.assertEqual(
            [s for s in result.front if s["state"] == ProjectedSlotState.ADD], [])

    def test_ancestor_move_vacates_the_source_and_occupies_the_target(self):
        a = self._design("Network sweep IDS-1000")
        DesignPlacement.objects.create(
            design=a,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],  # real, at U1
            target_rack=self.racks[0],
            target_position=10,
            target_face="front",
        )
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        result = project_rack(b, self.racks[0])

        self.assertEqual(self._at(result.front, 1), [],
                         "the ancestor's move vacated U1; it must be free for the child")
        at10 = self._at(result.front, 10)
        self.assertEqual(len(at10), 1, at10)
        self.assertEqual(at10[0]["state"], ProjectedSlotState.EXISTING)
        self.assertTrue(at10[0]["inherited"])
        self.assertEqual(at10[0]["device"], self.devices[0])
        # No proposal-flavoured slots: the child did not propose this.
        self.assertEqual(
            [s for s in result.front
             if s["state"] in (ProjectedSlotState.MOVE_IN,
                               ProjectedSlotState.MOVE_OUT_GHOST)], [])

    def test_ancestor_remove_frees_the_unit(self):
        a = self._design("Network sweep IDS-1000")
        DesignPlacement.objects.create(
            design=a,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.devices[1],  # real, at U2
        )
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        result = project_rack(b, self.racks[0])

        self.assertEqual(self._at(result.front, 2), [],
                         "the ancestor removed the device at U2; the U is free")
        self.assertEqual(
            [s for s in result.front if s["state"] == ProjectedSlotState.REMOVE], [])
        # Device 1 is untouched and still projects normally.
        at1 = self._at(result.front, 1)
        self.assertEqual(len(at1), 1)
        self.assertFalse(at1[0]["inherited"])

    def test_ancestor_move_out_of_the_rack_vacates_without_occupying(self):
        a = self._design("Network sweep IDS-1000")
        DesignPlacement.objects.create(
            design=a,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[1],  # a DIFFERENT rack
            target_position=5,
            target_face="front",
        )
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        source = project_rack(b, self.racks[0])
        self.assertEqual(self._at(source.front, 1), [])
        target = project_rack(b, self.racks[1])
        at5 = self._at(target.front, 5)
        self.assertEqual(len(at5), 1, at5)
        self.assertTrue(at5[0]["inherited"])
        self.assertEqual(at5[0]["device"], self.devices[0])

    # --- chain ORDER -------------------------------------------------------

    def test_three_deep_chain_composes_oldest_first(self):
        """A adds at U10, B relocates it to U20, C to U30 -- D sees U30 only.

        The ORDER is load-bearing: replaying oldest-LAST would apply A's add
        after C's move and leave the device at U10 (or, worse, at two Us at
        once). This is the case that distinguishes the two orders.
        """
        a = self._design("Network sweep IDS-1000")
        upstream = self._add(a, 10, name="IDS-1000_srv-01")
        self._approve(a)

        b = self._design("Server build IDS-2000", based_on=a)
        DesignPlacement.objects.create(
            design=b,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=upstream,
            target_rack=self.racks[0],
            target_position=20,
            target_face="front",
        )
        self._approve(b)

        c = self._design("Storage build IDS-3000", based_on=b)
        DesignPlacement.objects.create(
            design=c,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=upstream,
            target_rack=self.racks[0],
            target_position=30,
            target_face="front",
        )
        self._approve(c)

        d = self._design("Final build IDS-4000", based_on=c)
        result = project_rack(d, self.racks[0])

        self.assertEqual(self._at(result.front, 10), [],
                         "U10 is where A put it; B and C moved it away")
        self.assertEqual(self._at(result.front, 20), [],
                         "U20 is where B put it; C moved it away")
        at30 = self._at(result.front, 30)
        self.assertEqual(len(at30), 1, at30)
        self.assertEqual(at30[0]["state"], ProjectedSlotState.EXISTING)
        self.assertTrue(at30[0]["inherited"])
        self.assertEqual(at30[0]["source_design_id"], c.pk,
                         "provenance is the ancestor that LAST touched the identity")
        # Exactly one slot for that identity across the whole elevation.
        inherited = [s for s in result.front + result.rear + result.non_racked
                     if s["inherited"]]
        self.assertEqual(len(inherited), 1, inherited)
        self.assertEqual(result.conflicts, [])

    def test_chain_order_ancestor_remove_after_add_leaves_nothing(self):
        """A adds at U10, B removes that planned identity -- C sees an empty U10.

        Replayed in the wrong order the remove would find nothing to remove and
        the add would then occupy U10: a believable rack that is simply false.
        """
        a = self._design("Network sweep IDS-1000")
        upstream = self._add(a, 10, name="doomed")
        self._approve(a)

        b = self._design("Server build IDS-2000", based_on=a)
        DesignPlacement.objects.create(
            design=b,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            base_placement=upstream,
        )
        self._approve(b)

        c = self._design("Storage build IDS-3000", based_on=b)
        result = project_rack(c, self.racks[0])

        self.assertEqual(self._at(result.front, 10), [])
        self.assertEqual([s for s in result.front if s["inherited"]], [])

    # --- names on inherited (ancestor-owned) slots (§3.2 R1) ----------------

    def test_inherited_add_renders_under_its_own_stored_name(self):
        # An ancestor's own 'add' has no real device: its proposed_name IS the
        # identity, plain, with no per-design decoration (only the design
        # CURRENTLY being projected decorates a keep-name move).
        a = self._design("Network sweep IDS-1234")
        self._add(a, 10, name="srv-01")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        slot = self._at(project_rack(b, self.racks[0]).front, 10)[0]
        self.assertEqual(slot["display_label"], "srv-01")
        self.assertEqual(slot["label"], "srv-01",
                         "an ancestor-planned identity has no real device name, so its "
                         "own stored name IS the stable identity in the child's world")

    def test_inherited_moved_real_device_keeps_its_real_name_as_identity(self):
        a = self._design("Network sweep IDS-1234")
        DesignPlacement.objects.create(
            design=a,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[0],
            target_position=10,
            target_face="front",
            proposed_name="renamed-01",
        )
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        slot = self._at(project_rack(b, self.racks[0]).front, 10)[0]
        self.assertEqual(slot["label"], self.devices[0].name,
                         "a REAL device's identity stays its real name (2026-07-10 ruling)")
        self.assertEqual(slot["display_label"], "renamed-01",
                         "the visible name is what the ancestor renamed it to, verbatim -- "
                         "no per-design decoration on an ancestor's own move")

    def test_inherited_kept_name_move_renders_plain_with_no_ancestor_decoration(self):
        # The ancestor's move KEPT the device's name: from the child's point of
        # view that move already happened, so the slot renders plain -- only
        # the design CURRENTLY being projected decorates a keep-name move.
        a = self._design("Network sweep IDS-1234")
        DesignPlacement.objects.create(
            design=a,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[0],
            target_position=10,
            target_face="front",
        )
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        slot = self._at(project_rack(b, self.racks[0]).front, 10)[0]
        self.assertEqual(slot["label"], self.devices[0].name)
        self.assertEqual(slot["display_label"], self.devices[0].name)

    # --- a child acting on an ancestor-planned identity (base_placement) ---

    def test_child_move_of_base_placement_identity_vacates_the_ancestor_target(self):
        a = self._design("Network sweep IDS-1000")
        upstream = self._add(a, 10, name="srv-01")
        self._approve(a)

        b = self._design("Server build IDS-2000", based_on=a)
        own = DesignPlacement.objects.create(
            design=b,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=upstream,
            target_rack=self.racks[0],
            target_position=20,
            target_face="front",
            proposed_name="IDS-2000_srv-01",
        )

        result = project_rack(b, self.racks[0])

        at10 = self._at(result.front, 10)
        self.assertEqual(len(at10), 1, at10)
        self.assertEqual(at10[0]["state"], ProjectedSlotState.MOVE_OUT_GHOST,
                         "THIS design proposes the move, so U10 is a ghost it vacates "
                         "-- not a plain inherited EXISTING slot")
        # The ghost still carries full provenance (defect fix, PLAN-design-
        # chains.md §8.4): U10 is where the ANCESTOR's add put the identity,
        # so the tile that depicts vacating it is ancestor-caused and gets the
        # "from an ancestor design" frame, even though its `state` stays the
        # proposal flavour MOVE_OUT_GHOST rather than EXISTING.
        self.assertTrue(at10[0]["inherited"])
        self.assertEqual(at10[0]["source_design_id"], a.pk)
        self.assertEqual(at10[0]["placement"], own)
        at20 = self._at(result.front, 20)
        self.assertEqual(len(at20), 1, at20)
        self.assertEqual(at20[0]["state"], ProjectedSlotState.MOVE_IN)
        self.assertEqual(at20[0]["label"], "srv-01")
        self.assertEqual(at20[0]["display_label"], "IDS-2000_srv-01")
        # The move_in is THIS design's own act, so it must not itself claim
        # `inherited` -- but it still names where the identity came from.
        self.assertFalse(at20[0]["inherited"])
        self.assertEqual(at20[0]["source_design_id"], a.pk)
        # The identity appears exactly twice (ghost + move_in) and nowhere else.
        self.assertEqual(len(self._at(result.front, 10) + self._at(result.front, 20)), 2)
        self.assertEqual([s for s in result.front if s["inherited"]], [at10[0]])

    def test_child_remove_of_base_placement_identity_flags_the_ancestor_slot(self):
        a = self._design("Network sweep IDS-1000")
        upstream = self._add(a, 10, name="srv-01")
        self._approve(a)

        b = self._design("Server build IDS-2000", based_on=a)
        own = DesignPlacement.objects.create(
            design=b,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            base_placement=upstream,
        )

        result = project_rack(b, self.racks[0])
        at10 = self._at(result.front, 10)
        self.assertEqual(len(at10), 1, at10)
        self.assertEqual(at10[0]["state"], ProjectedSlotState.REMOVE)
        self.assertEqual(at10[0]["placement"], own)
        self.assertFalse(at10[0]["inherited"])

    # --- move-slot provenance & the no-op-move ghost guard (defect fix) ----

    def test_child_move_of_ancestor_moved_device_to_a_different_cell(self):
        """A moved it U1->U10; B moves it further, U10->U20 (a DIFFERENT cell).

        The ghost at U10 depicts where the ANCESTOR's layer left the device,
        so it is flagged fully ``inherited`` with the ancestor's
        ``source_design_id``. The move_in at U20 is THIS design's own act, so
        it must NOT claim ``inherited`` -- but it still carries the ancestor's
        ``source_design_id`` so the hover card can name where the identity
        came from.
        """
        a = self._design("Network sweep IDS-1000")
        DesignPlacement.objects.create(
            design=a,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[0],
            target_position=10,
            target_face="front",
        )
        self._approve(a)

        b = self._design("Server build IDS-2000", based_on=a)
        own = DesignPlacement.objects.create(
            design=b,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[0],
            target_position=20,
            target_face="front",
        )

        result = project_rack(b, self.racks[0])

        at10 = self._at(result.front, 10)
        self.assertEqual(len(at10), 1, at10)
        self.assertEqual(at10[0]["state"], ProjectedSlotState.MOVE_OUT_GHOST)
        self.assertTrue(at10[0]["inherited"],
                         "the ghost depicts where the ANCESTOR left the device")
        self.assertEqual(at10[0]["source_design_id"], a.pk)

        at20 = self._at(result.front, 20)
        self.assertEqual(len(at20), 1, at20)
        self.assertEqual(at20[0]["state"], ProjectedSlotState.MOVE_IN)
        self.assertEqual(at20[0]["placement"], own)
        self.assertFalse(at20[0]["inherited"],
                          "the move_in is THIS design's own act, not the ancestor's")
        self.assertEqual(at20[0]["source_design_id"], a.pk,
                          "provenance still names where the identity came from")

    def test_child_move_onto_the_exact_cell_the_ancestor_left_it_in_suppresses_the_ghost(self):
        """A moved it to U10; B's move ALSO targets U10 -- vacates nothing.

        Same rack + position + face as the ancestor left it in: the move is a
        no-op location-wise, so drawing a ghost there would stack it directly
        under the move_in tile at the same rect. Only the move_in is emitted.
        """
        a = self._design("Network sweep IDS-1000")
        DesignPlacement.objects.create(
            design=a,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[0],
            target_position=10,
            target_face="front",
        )
        self._approve(a)

        b = self._design("Server build IDS-2000", based_on=a)
        own = DesignPlacement.objects.create(
            design=b,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[0],
            target_position=10,
            target_face="front",
            proposed_name="IDS-2000_renamed",
        )

        result = project_rack(b, self.racks[0])

        at10 = self._at(result.front, 10)
        self.assertEqual(len(at10), 1, at10)
        self.assertEqual(
            [s for s in result.front if s["state"] == ProjectedSlotState.MOVE_OUT_GHOST],
            [], "the move vacates nothing at U10, so no ghost belongs there")
        self.assertEqual(at10[0]["state"], ProjectedSlotState.MOVE_IN)
        self.assertEqual(at10[0]["placement"], own)
        # Positions must be compared numerically: Decimal("10") == 10 must not
        # defeat the guard.
        self.assertEqual(Decimal(str(own.target_position)), Decimal("10"))

    def test_child_move_onto_the_exact_cell_suppresses_both_full_depth_mirror_faces(self):
        """The same no-op-move guard, for a full-depth device: front AND rear.

        The ``_append`` full-depth mirror copies a slot per face, so
        suppressing the source ghost slot must remove BOTH its mirrored
        copies -- asserted directly rather than assumed.
        """
        fd_type = DeviceType.objects.create(
            manufacturer=Manufacturer.objects.create(name="FD MF", slug="fd-mf-provenance"),
            model="FD Type Provenance", slug="fd-type-provenance",
            u_height=2, is_full_depth=True,
        )
        fd_device = Device.objects.create(
            name="fd-provenance", site=self.site, rack=self.racks[0],
            position=5, face="front", device_type=fd_type,
            role=DeviceRole.objects.create(name="FD Role", slug="fd-role-provenance"),
        )
        a = self._design("Network sweep IDS-1000")
        DesignPlacement.objects.create(
            design=a,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=fd_device,
            target_rack=self.racks[0],
            target_position=15,
            target_face="front",
        )
        self._approve(a)

        b = self._design("Server build IDS-2000", based_on=a)
        DesignPlacement.objects.create(
            design=b,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=fd_device,
            target_rack=self.racks[0],
            target_position=15,
            target_face="front",
        )

        result = project_rack(b, self.racks[0])

        ghosts_front = [s for s in result.front if s["state"] == ProjectedSlotState.MOVE_OUT_GHOST]
        ghosts_rear = [s for s in result.rear if s["state"] == ProjectedSlotState.MOVE_OUT_GHOST]
        self.assertEqual(ghosts_front, [], "front mirror of the suppressed ghost must be absent")
        self.assertEqual(ghosts_rear, [], "rear mirror of the suppressed ghost must be absent")
        move_ins_front = [s for s in result.front if s["state"] == ProjectedSlotState.MOVE_IN]
        move_ins_rear = [s for s in result.rear if s["state"] == ProjectedSlotState.MOVE_IN]
        self.assertEqual(len(move_ins_front), 1, move_ins_front)
        self.assertEqual(len(move_ins_rear), 1, move_ins_rear)

    def test_plain_move_with_no_ancestor_is_unchanged(self):
        """No ``based_on``: ghost and move_in both present, neither inherited."""
        design = self._design("Standalone build")
        own = DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[0],
            target_position=10,
            target_face="front",
        )

        result = project_rack(design, self.racks[0])

        at1 = self._at(result.front, 1)
        self.assertEqual(len(at1), 1, at1)
        self.assertEqual(at1[0]["state"], ProjectedSlotState.MOVE_OUT_GHOST)
        self.assertFalse(at1[0]["inherited"])
        self.assertIsNone(at1[0]["source_design_id"])

        at10 = self._at(result.front, 10)
        self.assertEqual(len(at10), 1, at10)
        self.assertEqual(at10[0]["state"], ProjectedSlotState.MOVE_IN)
        self.assertEqual(at10[0]["placement"], own)
        self.assertFalse(at10[0]["inherited"])
        self.assertIsNone(at10[0]["source_design_id"])

    def test_ancestor_planned_identity_the_child_does_not_touch_is_still_inherited(self):
        """Regression guard: an ancestor's own move, untouched by the child,
        must still come through ``baseline.emit()`` as ``inherited=True`` --
        this fix must not leak into the plain baseline (non-moves_removes)
        path."""
        a = self._design("Network sweep IDS-1000")
        DesignPlacement.objects.create(
            design=a,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[0],
            target_position=10,
            target_face="front",
        )
        self._approve(a)

        b = self._design("Server build IDS-2000", based_on=a)
        # An unrelated action in b, touching a DIFFERENT identity, so the
        # ancestor-moved device at U10 goes through emit() untouched.
        DesignPlacement.objects.create(
            design=b,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[1],
            target_rack=self.racks[0],
            target_position=30,
            target_face="front",
        )

        result = project_rack(b, self.racks[0])

        at10 = self._at(result.front, 10)
        self.assertEqual(len(at10), 1, at10)
        self.assertEqual(at10[0]["state"], ProjectedSlotState.EXISTING)
        self.assertTrue(at10[0]["inherited"])
        self.assertEqual(at10[0]["source_design_id"], a.pk)

    # --- §9.2 refusal ------------------------------------------------------

    def test_implemented_ancestor_refuses_the_chain(self):
        a = self._design("Network sweep IDS-1000")
        self._add(a, 10, name="srv-a")
        self._approve(a)
        Design.objects.filter(pk=a.pk).update(
            status=DesignStatusChoices.STATUS_IMPLEMENTED)
        b = self._design("Server build IDS-2000",
                         based_on=Design.objects.get(pk=a.pk))

        result = project_rack(b, self.racks[0])

        self.assertEqual(self._at(result.front, 10), [],
                         "a plausible wrong rack is worse than no rack: the chain "
                         "refuses to project rather than double-counting an applied "
                         "ancestor (§9.5)")
        self.assertEqual([s for s in result.front if s["inherited"]], [])
        self.assertEqual(len(result.conflicts), 1, result.conflicts)
        entry = result.conflicts[0]
        self.assertEqual(entry["kind"], "ancestor_implemented")
        self.assertEqual(entry["severity"], "error")
        self.assertEqual(entry["source_design"].pk, a.pk)
        self.assertIn("re-base", entry["detail"].lower())
        # Reality is still projected: the refusal drops the CHAIN, not the rack.
        self.assertEqual(len(self._at(result.front, 1)), 1)

    def test_draft_ancestor_refuses_the_chain(self):
        # Approval is what makes a design derivable and frozen (§2.2); a draft
        # parent's placements are still moving, so inheriting them would render
        # a world that can change under the child with nothing to say so.
        a = self._design("Network sweep IDS-1000")
        self._add(a, 10, name="srv-a")
        b = self._design("Server build IDS-2000", based_on=a)

        result = project_rack(b, self.racks[0])

        self.assertEqual(self._at(result.front, 10), [])
        self.assertEqual([s for s in result.front if s["inherited"]], [])
        self.assertEqual([c["kind"] for c in result.conflicts],
                         ["ancestor_not_approved"])
        self.assertEqual(result.conflicts[0]["source_design"], a)

    def test_implemented_grandparent_refuses_the_whole_chain(self):
        a = self._design("Network sweep IDS-1000")
        self._add(a, 10, name="srv-a")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)
        self._add(b, 11, name="srv-b")
        self._approve(b)
        Design.objects.filter(pk=a.pk).update(
            status=DesignStatusChoices.STATUS_IMPLEMENTED)
        c = self._design("Storage build IDS-3000", based_on=Design.objects.get(pk=b.pk))

        result = project_rack(c, self.racks[0])
        self.assertEqual([s for s in result.front if s["inherited"]], [],
                         "a layer is contributed WHOLE or not at all, and one broken "
                         "ancestor breaks the stack it sits under")
        self.assertEqual(self._at(result.front, 11), [])
        self.assertEqual([c_["kind"] for c_ in result.conflicts],
                         ["ancestor_implemented"])

    def test_lineage_cycle_surfaces_as_a_conflict_not_an_exception(self):
        a = self._design("Network sweep IDS-1000")
        b = self._design("Server build IDS-2000", based_on=a)
        # Force a cycle past clean() the way test_models does.
        Design.objects.filter(pk=a.pk).update(based_on=b)
        b = Design.objects.get(pk=b.pk)

        result = project_rack(b, self.racks[0])  # must not raise, must not loop
        self.assertEqual([c["kind"] for c in result.conflicts], ["chain_broken"])
        self.assertEqual(result.conflicts[0]["severity"], "error")
        self.assertEqual([s for s in result.front if s["inherited"]], [])
        # Reality still renders.
        self.assertEqual(len(self._at(result.front, 1)), 1)

    # --- structural invariants --------------------------------------------

    def test_nothing_is_double_counted_anywhere(self):
        a = self._design("Network sweep IDS-1000")
        self._add(a, 10, name="srv-a")
        DesignPlacement.objects.create(
            design=a,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],  # U1 -> U11
            target_rack=self.racks[0],
            target_position=11,
            target_face="front",
        )
        DesignPlacement.objects.create(
            design=a,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.devices[1],  # U2 gone
        )
        self._approve(a)

        b = self._design("Server build IDS-2000", based_on=a)
        self._add(b, 20, name="own-add")

        result = project_rack(b, self.racks[0])

        keys = [(s["u_position"], s["face"], s["label"]) for s in result.front]
        self.assertEqual(len(keys), len(set(keys)), keys)
        for device, expected in ((self.devices[0], 1), (self.devices[1], 0)):
            hits = [s for s in result.front + result.rear + result.non_racked
                    if s["device"] == device]
            self.assertEqual(len(hits), expected, f"{device}: {hits}")
        placements = [s["placement"] for s in result.front if s["placement"] is not None]
        self.assertEqual(len(placements), len({p.pk for p in placements}))
        self.assertEqual(result.conflicts, [])

    def test_design_with_no_parent_projects_exactly_as_before(self):
        """The regression guard that matters most: no ``based_on``, no change."""
        design = self._design("Standalone IDS-9000")
        self._add(design, 10, name="new-1")
        DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=self.racks[0],
            target_position=11,
            target_face="front",
        )
        DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.devices[1],
        )

        result = project_rack(design, self.racks[0])

        self.assertEqual(result.conflicts, [])
        self.assertEqual(
            sorted((int(s["u_position"]), s["state"], s["label"]) for s in result.front),
            [
                (1, ProjectedSlotState.MOVE_OUT_GHOST, self.devices[0].name),
                (2, ProjectedSlotState.REMOVE, self.devices[1].name),
                (10, ProjectedSlotState.ADD, "new-1"),
                (11, ProjectedSlotState.MOVE_IN, self.devices[0].name),
            ],
        )
        for slot in result.front + result.rear + result.non_racked:
            self.assertFalse(slot["inherited"], slot)
            self.assertIsNone(slot["source_design_id"], slot)
            self.assertFalse(slot["conflict"], slot)

    def test_inherited_full_depth_add_mirrors_onto_both_faces(self):
        from dcim.models import DeviceType

        fd_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer,
            model="FD Chain", slug="fd-chain", u_height=2, is_full_depth=True,
        )
        a = self._design("Network sweep IDS-1000")
        self._add(a, 20, name="fd-1", device_type=fd_type, face="rear")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        result = project_rack(b, self.racks[0])
        front = self._at(result.front, 20)
        rear = self._at(result.rear, 20)
        self.assertEqual(len(front), 1, front)
        self.assertEqual(len(rear), 1, rear)
        self.assertTrue(front[0]["inherited"] and rear[0]["inherited"])
        self.assertTrue(front[0]["opposite_face"],
                        "the mounted face is rear, so the front copy is the passive "
                        "blocked mirror")
        self.assertFalse(rear[0]["opposite_face"])

    def test_inherited_position_less_add_lands_in_the_tray(self):
        a = self._design("Network sweep IDS-1000")
        DesignPlacement.objects.create(
            design=a,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[0],
            target_position=None,
            proposed_name="vertical-pdu-1",
        )
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        result = project_rack(b, self.racks[0])
        tray = [s for s in result.non_racked if s["inherited"]]
        self.assertEqual(len(tray), 1, result.non_racked)
        self.assertEqual(tray[0]["state"], ProjectedSlotState.EXISTING)
        self.assertEqual(tray[0]["label"], "vertical-pdu-1")
        self.assertEqual(tray[0]["source_design_id"], a.pk)

    def test_ancestor_blade_placements_never_become_rack_slots(self):
        # A blade lives inside a chassis strip, never at a U. An ancestor's
        # blade must not leak into the child's rack faces or tray (the same
        # rule the single-layer pass already enforces for this design's own).
        from dcim.models import Device, DeviceBay, DeviceType

        parent_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer,
            model="Chassis Chain", slug="chassis-chain", u_height=4,
            is_full_depth=False, subdevice_role="parent",
        )
        child_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer,
            model="Blade Chain", slug="blade-chain", u_height=1,
            is_full_depth=False, subdevice_role="child",
        )
        chassis = Device.objects.create(
            name="Chassis Chain 1", site=self.site, rack=self.racks[0],
            position=30, face="front", device_type=parent_type,
            role=self.devices[0].role, status="active",
        )
        bay = DeviceBay.objects.create(device=chassis, name="slot1")

        a = self._design("Network sweep IDS-1000")
        DesignPlacement.objects.create(
            design=a,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=child_type,
            target_rack=self.racks[0],
            target_bay=bay,
            proposed_name="blade-1",
        )
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        result = project_rack(b, self.racks[0])
        labels = [s["label"] for s in result.front + result.rear + result.non_racked]
        self.assertNotIn("blade-1", labels, labels)


# ---------------------------------------------------------------------------
# The BAY layer across a design chain (PLAN-design-chains.md G1, bays)
# ---------------------------------------------------------------------------


class ChainBayProjectionTestCase(TestCase):
    """An approved ancestor's BLADE placements are baseline inside a chassis.

    A blade is never a rack slot (core forbids a child device a position or a
    face), so an ancestor's bay-targeted placements cannot ride the rack replay
    -- they need a parallel one keyed on the SAME identity
    (``_identity_key``) and named by the SAME ``_resolve_names``, or bay
    identities and names drift from rack ones.

    Consumed by three single-layer surfaces, all of which must see it:
    ``_overlay_planned_blades`` (the bay strips on a rack elevation),
    ``chassis_in_scope`` (which chassis exist at all) and ``project_chassis``
    (one chassis as a column of bays).
    """

    @classmethod
    def setUpTestData(cls):
        from dcim.choices import SubdeviceRoleChoices
        from dcim.models import Device, DeviceBayTemplate, DeviceType

        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]
        cls.device_type = env["device_type"]
        cls.role = cls.devices[0].role

        cls.chassis_type = DeviceType.objects.create(
            manufacturer=cls.device_type.manufacturer,
            model="Chain-Chassis", slug="chain-chassis", u_height=2,
            subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        )
        for name in ("c1", "c2", "c3"):
            DeviceBayTemplate.objects.create(device_type=cls.chassis_type, name=name)
        cls.blade_type = DeviceType.objects.create(
            manufacturer=cls.device_type.manufacturer,
            model="Chain-Blade", slug="chain-blade", u_height=0,
            subdevice_role=SubdeviceRoleChoices.ROLE_CHILD,
        )
        # A REAL chassis with real bays, standing in rack 0 at U30.
        cls.chassis = Device.objects.create(
            name="Chain-Chassis-1", site=cls.site, rack=cls.racks[0], position=30,
            face="front", device_type=cls.chassis_type, role=cls.role, status="active",
        )
        cls.bays = {bay.name: bay for bay in cls.chassis.devicebays.all()}

    # --- helpers -----------------------------------------------------------

    def _design(self, title, *, based_on=None, scoped=True):
        design = Design.objects.create(title=title, site=self.site, based_on=based_on)
        if scoped:
            design.racks.add(self.racks[0])
        return design

    def _approve(self, design):
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()
        return design

    def _blade_add(self, design, *, bay=None, parent=None, bay_name="", name=""):
        return DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.blade_type,
            target_rack=self.racks[0],
            target_bay=bay,
            target_bay_name=bay.name if bay is not None else bay_name,
            parent_placement=parent,
            proposed_name=name,
        )

    def _chassis_add(self, design, position, *, name=""):
        return DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.chassis_type,
            target_rack=self.racks[0],
            target_position=position,
            target_face="front",
            proposed_name=name,
        )

    def _base_parent_blade(self, design, chassis_placement, bay_name, *, name=""):
        """A blade this design plans into a chassis an ANCESTOR planned."""
        return DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.blade_type,
            target_rack=self.racks[0],
            base_parent_placement=chassis_placement,
            target_bay_name=bay_name,
            proposed_name=name,
        )

    def _real_blade(self, bay, name):
        from dcim.models import Device

        blade = Device.objects.create(
            name=name, site=self.site, rack=self.racks[0], position=None,
            device_type=self.blade_type, role=self.role, status="active",
        )
        bay.installed_device = blade
        bay.save()
        return blade

    def _strip(self, result, label):
        """The bay strip of the chassis rendered as ``label`` on the front face."""
        slot = next(s for s in result.front if s["label"] == label)
        return {entry["name"]: entry for entry in slot["bays"]}

    def _column(self, design, key):
        entry = next(e for e in chassis_in_scope(design) if e["key"] == key)
        return project_chassis(design, entry)

    def _bay(self, column, name):
        return next(s for s in column["slots"] if s["name"] == name)

    # --- an ancestor's blade in a REAL chassis ------------------------------

    def test_ancestor_blade_in_a_real_chassis_is_baseline_in_the_rack_strip(self):
        a = self._design("Network sweep IDS-1000")
        upstream = self._blade_add(a, bay=self.bays["c1"], name="blade-a")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        strip = self._strip(project_rack(b, self.racks[0]), "Chain-Chassis-1")
        entry = strip["c1"]
        self.assertEqual(entry["state"], ProjectedSlotState.EXISTING,
                         "an ancestor's blade has already happened from the child's "
                         "point of view: it is part of the world, not a planned add")
        self.assertTrue(entry["occupied"])
        self.assertTrue(entry["inherited"])
        self.assertEqual(entry["source_design_id"], a.pk)
        self.assertEqual(entry["placement"], upstream)
        self.assertEqual(entry["device_type"], self.blade_type)
        self.assertEqual(entry["label"], "blade-a")
        # The other bays are untouched, and nothing leaked to a rack face/tray.
        self.assertFalse(strip["c2"]["occupied"])
        self.assertNotIn(
            "blade-a",
            [s["label"] for s in project_rack(b, self.racks[0]).non_racked],
        )

    def test_ancestor_blade_in_a_real_chassis_is_baseline_in_the_chassis_column(self):
        a = self._design("Network sweep IDS-1000")
        upstream = self._blade_add(a, bay=self.bays["c1"], name="blade-a")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        column = self._column(b, f"dev-{self.chassis.pk}")
        bay = self._bay(column, "c1")
        self.assertEqual(bay["state"], ProjectedSlotState.EXISTING)
        self.assertTrue(bay["inherited"])
        self.assertEqual(bay["source_design_id"], a.pk)
        self.assertEqual(bay["placement"], upstream)
        self.assertEqual(bay["label"], "blade-a")
        self.assertEqual(column["used"], 1, column["slots"])
        self.assertEqual(column["conflicts"], [])

    # --- an ancestor's blade in a chassis the SAME ancestor planned ---------

    def test_ancestor_blade_in_an_ancestor_planned_chassis_is_baseline(self):
        a = self._design("Network sweep IDS-1000")
        chassis_p = self._chassis_add(a, 12, name="planned-chassis")
        upstream = self._blade_add(a, parent=chassis_p, bay_name="c2", name="blade-p")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        result = project_rack(b, self.racks[0])
        chassis_slots = [s for s in result.front if s["label"] == "planned-chassis"]
        self.assertEqual(len(chassis_slots), 1, "the chassis must not be double-counted")
        chassis_slot = chassis_slots[0]
        self.assertTrue(chassis_slot["inherited"])
        self.assertEqual(chassis_slot["source_design_id"], a.pk)
        # _attach_planned_chassis_bays must give an INHERITED planned chassis its
        # DeviceBayTemplate bays, exactly as it does for this design's own add.
        strip = {e["name"]: e for e in chassis_slot["bays"]}
        self.assertEqual(sorted(strip), ["c1", "c2", "c3"])
        entry = strip["c2"]
        self.assertEqual(entry["state"], ProjectedSlotState.EXISTING)
        self.assertTrue(entry["inherited"])
        self.assertEqual(entry["placement"], upstream)
        self.assertEqual(entry["label"], "blade-p")

        column = self._column(b, f"pl-{chassis_p.pk}")
        self.assertEqual([s["name"] for s in column["slots"]], ["c1", "c2", "c3"])
        self.assertEqual(self._bay(column, "c2")["label"], "blade-p")
        self.assertTrue(self._bay(column, "c2")["inherited"])

    def test_inherited_planned_chassis_is_listed_in_chassis_in_scope(self):
        a = self._design("Network sweep IDS-1000")
        chassis_p = self._chassis_add(a, 12, name="planned-chassis")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        entries = chassis_in_scope(b)
        keys = [e["key"] for e in entries]
        self.assertEqual(keys.count(f"pl-{chassis_p.pk}"), 1, keys)
        self.assertIn(f"dev-{self.chassis.pk}", keys)
        row = next(e for e in entries if e["key"] == f"pl-{chassis_p.pk}")
        self.assertEqual(row["bay_names"], ["c1", "c2", "c3"])
        self.assertEqual(row["placement"], chassis_p)
        self.assertEqual(row["rack"], self.racks[0])
        self.assertTrue(row["inherited"])
        self.assertEqual(row["source_design_id"], a.pk)
        self.assertTrue(has_chassis_in_scope(b))

    def test_inherited_moved_real_chassis_keeps_its_real_bays(self):
        # _attach_bays works off (device, device_type) and must therefore give an
        # inherited MOVED real chassis its dcim.DeviceBays.
        blade = self._real_blade(self.bays["c3"], "real-blade-3")
        a = self._design("Network sweep IDS-1000")
        DesignPlacement.objects.create(
            design=a,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.chassis,
            target_rack=self.racks[0],
            target_position=40,
            target_face="front",
        )
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        result = project_rack(b, self.racks[0])
        slot = next(s for s in result.front
                    if s["label"] == "Chain-Chassis-1" and s["u_position"] == 40)
        self.assertTrue(slot["inherited"])
        strip = {e["name"]: e for e in slot["bays"]}
        self.assertEqual(sorted(strip), ["c1", "c2", "c3"])
        self.assertEqual(strip["c3"]["device"], blade)
        self.assertEqual(strip["c3"]["state"], ProjectedSlotState.EXISTING)
        self.assertFalse(strip["c3"]["inherited"],
                         "the blade itself is plain reality -- only the chassis moved")

    # --- the child acting on an inherited blade -----------------------------

    def test_child_move_of_an_inherited_blade_frees_the_ancestor_bay(self):
        a = self._design("Network sweep IDS-1000")
        upstream = self._blade_add(a, bay=self.bays["c1"], name="blade-a")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)
        own = DesignPlacement.objects.create(
            design=b,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=upstream,
            target_rack=self.racks[0],
            target_bay=self.bays["c2"],
            target_bay_name="c2",
        )

        strip = self._strip(project_rack(b, self.racks[0]), "Chain-Chassis-1")
        self.assertFalse(strip["c1"]["occupied"],
                         "the ancestor's bay is freed: THIS design moved that blade out")
        self.assertIsNone(strip["c1"]["state"])
        self.assertFalse(strip["c1"]["inherited"])
        self.assertEqual(strip["c2"]["state"], ProjectedSlotState.MOVE_IN)
        self.assertEqual(strip["c2"]["placement"], own)
        self.assertFalse(strip["c2"]["inherited"])

        column = self._column(b, f"dev-{self.chassis.pk}")
        self.assertIsNone(self._bay(column, "c1")["state"])
        self.assertEqual(self._bay(column, "c2")["state"], ProjectedSlotState.MOVE_IN)
        self.assertEqual(column["used"], 1, column["slots"])

    def test_child_remove_of_an_inherited_blade_flags_its_bay(self):
        a = self._design("Network sweep IDS-1000")
        upstream = self._blade_add(a, bay=self.bays["c1"], name="blade-a")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)
        own = DesignPlacement.objects.create(
            design=b,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            base_placement=upstream,
        )

        strip = self._strip(project_rack(b, self.racks[0]), "Chain-Chassis-1")
        self.assertEqual(strip["c1"]["state"], ProjectedSlotState.REMOVE)
        self.assertEqual(strip["c1"]["placement"], own)
        self.assertFalse(strip["c1"]["occupied"])
        self.assertFalse(strip["c1"]["inherited"],
                         "THIS design proposes the removal, so the bay is its own tile")
        self.assertEqual(strip["c1"]["label"], "blade-a")

        column = self._column(b, f"dev-{self.chassis.pk}")
        self.assertEqual(self._bay(column, "c1")["state"], ProjectedSlotState.REMOVE)
        self.assertEqual(self._bay(column, "c1")["placement"], own)

    def test_ancestor_remove_of_an_inherited_blade_leaves_the_bay_empty(self):
        a = self._design("Network sweep IDS-1000")
        upstream = self._blade_add(a, bay=self.bays["c1"], name="blade-a")
        self._approve(a)
        b = self._design("Middle IDS-1500", based_on=a)
        DesignPlacement.objects.create(
            design=b,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            base_placement=upstream,
        )
        self._approve(b)
        c = self._design("Server build IDS-2000", based_on=b)

        strip = self._strip(project_rack(c, self.racks[0]), "Chain-Chassis-1")
        self.assertFalse(strip["c1"]["occupied"], strip["c1"])
        self.assertIsNone(strip["c1"]["state"])
        self.assertIsNone(strip["c1"]["placement"])

    def test_ancestor_move_of_a_real_blade_frees_its_real_bay(self):
        blade = self._real_blade(self.bays["c1"], "real-blade-1")
        a = self._design("Network sweep IDS-1000")
        DesignPlacement.objects.create(
            design=a,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=blade,
            target_rack=self.racks[0],
            target_bay=self.bays["c3"],
            target_bay_name="c3",
        )
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        strip = self._strip(project_rack(b, self.racks[0]), "Chain-Chassis-1")
        self.assertFalse(strip["c1"]["occupied"],
                         "reality still installs the blade in c1, but the ancestor "
                         "already moved it: that part of reality is no longer true")
        self.assertIsNone(strip["c1"]["device"])
        self.assertTrue(strip["c3"]["occupied"])
        self.assertEqual(strip["c3"]["device"], blade)
        self.assertTrue(strip["c3"]["inherited"])
        self.assertEqual(strip["c3"]["source_design_id"], a.pk)
        # And the blade is nowhere in the tray either.
        result = project_rack(b, self.racks[0])
        self.assertNotIn("real-blade-1", [s["label"] for s in result.non_racked])

        column = self._column(b, f"dev-{self.chassis.pk}")
        self.assertIsNone(self._bay(column, "c1")["state"])
        self.assertEqual(self._bay(column, "c3")["device"], blade)
        self.assertTrue(self._bay(column, "c3")["inherited"])

    # --- conflicts ----------------------------------------------------------

    def test_child_claiming_an_inherited_bay_is_a_conflict_not_a_block(self):
        a = self._design("Network sweep IDS-1000")
        self._blade_add(a, bay=self.bays["c1"], name="blade-a")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)
        own = self._blade_add(b, bay=self.bays["c1"], name="blade-b")

        result = project_rack(b, self.racks[0])
        entry = self._strip(result, "Chain-Chassis-1")["c1"]
        self.assertEqual(entry["state"], ProjectedSlotState.ADD,
                         "this design's own proposal still renders -- an upstream "
                         "conflict is not the hard-collision path (§8.2)")
        self.assertEqual(entry["placement"], own)
        self.assertTrue(entry["conflict"])
        self.assertTrue(entry["conflict_reason"])
        rows = [c for c in result.conflicts if c["kind"] == "bay_occupied"]
        self.assertEqual(len(rows), 1, result.conflicts)
        self.assertEqual(rows[0]["source_design"], a)
        self.assertEqual(rows[0]["placement"], own)
        self.assertTrue(rows[0]["detail"])

        column = self._column(b, f"dev-{self.chassis.pk}")
        self.assertTrue(self._bay(column, "c1")["conflict"])
        self.assertEqual(
            [c["kind"] for c in column["conflicts"] if c["kind"] == "bay_occupied"],
            ["bay_occupied"],
        )

    def test_chassis_an_ancestor_removed_is_not_in_scope(self):
        a = self._design("Network sweep IDS-1000")
        DesignPlacement.objects.create(
            design=a,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.chassis,
        )
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        keys = [e["key"] for e in chassis_in_scope(b)]
        self.assertNotIn(f"dev-{self.chassis.pk}", keys,
                         "the ancestor decommissioned it: offering its bays would be "
                         "a column nothing can ever be built into")

    # --- names on an inherited blade -----------------------------------------

    def test_inherited_blade_renders_under_its_own_stored_name(self):
        a = self._design("Network sweep IDS-1234")
        self._blade_add(a, bay=self.bays["c1"], name="blade-01")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)

        entry = self._strip(project_rack(b, self.racks[0]), "Chain-Chassis-1")["c1"]
        self.assertEqual(entry["label"], "blade-01",
                         "an inherited bay renders under its own stored name, by the same "
                         "_resolve_names the rack layer uses (§3.2 R1)")
        column = self._column(b, f"dev-{self.chassis.pk}")
        self.assertEqual(self._bay(column, "c1")["label"], "blade-01")

    def test_a_columns_conflicts_are_its_own(self):
        # The chassis layer shares ONE replay across its columns, so a bay
        # conflict must not be reported by every other chassis on the page.
        a = self._design("Network sweep IDS-1000")
        self._blade_add(a, bay=self.bays["c1"], name="blade-a")
        chassis_p = self._chassis_add(a, 12, name="planned-chassis")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)
        self._blade_add(b, bay=self.bays["c1"], name="blade-b")

        entries = chassis_in_scope(b)
        columns = {e["key"]: project_chassis(b, e) for e in entries}
        self.assertEqual(
            [c["kind"] for c in columns[f"dev-{self.chassis.pk}"]["conflicts"]],
            ["bay_occupied"],
        )
        self.assertEqual(columns[f"pl-{chassis_p.pk}"]["conflicts"], [])

    # --- structural invariants ----------------------------------------------

    def test_refused_chain_inherits_no_bays(self):
        a = self._design("Network sweep IDS-1000")
        self._blade_add(a, bay=self.bays["c1"], name="blade-a")
        a.status = DesignStatusChoices.STATUS_IMPLEMENTED
        a.save()
        b = self._design("Server build IDS-2000", based_on=a)

        result = project_rack(b, self.racks[0])
        strip = self._strip(result, "Chain-Chassis-1")
        self.assertFalse(strip["c1"]["occupied"], strip["c1"])
        self.assertIn("ancestor_implemented", [c["kind"] for c in result.conflicts])

    def test_unchained_design_bay_view_is_unchanged(self):
        blade = self._real_blade(self.bays["c1"], "real-blade-1")
        design = self._design("Plain plan")
        own_chassis = self._chassis_add(design, 12, name="own-chassis")
        own_blade = self._blade_add(design, parent=own_chassis, bay_name="c1",
                                    name="own-blade")

        result = project_rack(design, self.racks[0])
        real = self._strip(result, "Chain-Chassis-1")
        self.assertEqual(real["c1"]["device"], blade)
        self.assertEqual(real["c1"]["state"], ProjectedSlotState.EXISTING)
        self.assertFalse(real["c1"]["inherited"])
        self.assertFalse(real["c1"]["conflict"])
        planned = self._strip(result, "own-chassis")
        self.assertEqual(planned["c1"]["placement"], own_blade)
        self.assertEqual(planned["c1"]["state"], ProjectedSlotState.ADD)
        self.assertFalse(planned["c1"]["inherited"])
        self.assertEqual(result.conflicts, [])
        for column in (self._column(design, f"dev-{self.chassis.pk}"),
                       self._column(design, f"pl-{own_chassis.pk}")):
            self.assertEqual(column["conflicts"], [])
            self.assertFalse(any(s["inherited"] for s in column["slots"]))

    def test_child_blade_into_an_ancestor_planned_chassis_in_the_rack_strip(self):
        """A child may plan a blade into a chassis an ANCESTOR planned
        (``base_parent_placement``, the phase-3 gap). It is the CHILD's own
        proposal -- an ``add``, not inherited -- sitting in a bay of an
        inherited chassis."""
        a = self._design("Network sweep IDS-1000")
        chassis_p = self._chassis_add(a, 12, name="planned-chassis")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)
        own = self._base_parent_blade(b, chassis_p, "c1", name="blade-b")

        result = project_rack(b, self.racks[0])
        strip = self._strip(result, "planned-chassis")
        entry = strip["c1"]
        self.assertEqual(entry["state"], ProjectedSlotState.ADD)
        self.assertTrue(entry["occupied"])
        self.assertFalse(entry["inherited"],
                         "the BLADE is this design's own proposal; only the "
                         "chassis around it is inherited")
        self.assertIsNone(entry["source_design_id"])
        self.assertEqual(entry["placement"], own)
        self.assertEqual(entry["device_type"], self.blade_type)
        self.assertEqual(entry["label"], "blade-b")
        self.assertFalse(entry["conflict"])
        self.assertFalse(strip["c2"]["occupied"])
        # A blade is never a rack slot: it must not leak to a face or the tray.
        self.assertNotIn(
            "blade-b",
            [s["label"] for s in result.front + result.rear + result.non_racked],
        )

    def test_child_blade_into_an_ancestor_planned_chassis_in_the_chassis_column(self):
        a = self._design("Network sweep IDS-1000")
        chassis_p = self._chassis_add(a, 12, name="planned-chassis")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)
        own = self._base_parent_blade(b, chassis_p, "c1", name="blade-b")

        column = self._column(b, f"pl-{chassis_p.pk}")
        bay = self._bay(column, "c1")
        self.assertEqual(bay["state"], ProjectedSlotState.ADD)
        self.assertFalse(bay["inherited"])
        self.assertEqual(bay["placement"], own)
        self.assertEqual(bay["label"], "blade-b")
        self.assertEqual(bay["device_type"], self.blade_type)
        self.assertEqual(column["used"], 1, column["slots"])
        self.assertEqual(column["conflicts"], [])

    def test_child_blade_resolves_its_parent_through_the_identity_seam(self):
        """The chassis is planned by A and RE-PLANNED (moved and renamed) by B;
        the child C addresses it through A's originating add. Resolution goes
        through ``_identity_key``, so the later layer's row is still the same
        parent."""
        a = self._design("Network sweep IDS-1000")
        chassis_p = self._chassis_add(a, 12, name="planned-chassis")
        self._approve(a)
        b = self._design("Relocation IDS-2000", based_on=a)
        DesignPlacement.objects.create(
            design=b,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=chassis_p,
            target_rack=self.racks[0],
            target_position=18,
            target_face="front",
            proposed_name="renamed-chassis",
        )
        self._approve(b)
        c = self._design("Server build IDS-3000", based_on=b)
        own = self._base_parent_blade(c, chassis_p, "c2", name="blade-c")

        result = project_rack(c, self.racks[0])
        strip = self._strip(result, "renamed-chassis")
        self.assertEqual(strip["c2"]["state"], ProjectedSlotState.ADD)
        self.assertEqual(strip["c2"]["placement"], own)

        # ...and the column, whose key is the IDENTITY (A's add), not B's row.
        column = self._column(c, f"pl-{chassis_p.pk}")
        self.assertEqual(self._bay(column, "c2")["placement"], own)
        self.assertEqual(self._bay(column, "c2")["state"], ProjectedSlotState.ADD)

    def test_child_blade_claiming_an_inherited_bay_conflicts(self):
        """§8.5.3: a warning that still RENDERS, never a block."""
        a = self._design("Network sweep IDS-1000")
        chassis_p = self._chassis_add(a, 12, name="planned-chassis")
        self._blade_add(a, parent=chassis_p, bay_name="c1", name="blade-a")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)
        own = self._base_parent_blade(b, chassis_p, "c1", name="blade-b")

        column = self._column(b, f"pl-{chassis_p.pk}")
        bay = self._bay(column, "c1")
        self.assertEqual(bay["state"], ProjectedSlotState.ADD,
                         "the child's own blade still renders")
        self.assertEqual(bay["placement"], own)
        self.assertTrue(bay["conflict"])
        self.assertIn("blade-a", bay["conflict_reason"])
        kinds = [(c["kind"], c["severity"]) for c in column["conflicts"]]
        self.assertIn(("bay_occupied", "warning"), kinds)
        occupied = next(c for c in column["conflicts"] if c["kind"] == "bay_occupied")
        self.assertEqual(occupied["placement"], own)
        self.assertEqual(occupied["source_design"], a)

        # Same flag on the rack strip.
        result = project_rack(b, self.racks[0])
        strip = self._strip(result, "planned-chassis")
        self.assertTrue(strip["c1"]["conflict"])
        self.assertEqual(strip["c1"]["placement"], own)
        self.assertIn("bay_occupied", [c["kind"] for c in result.conflicts])

    def test_a_refused_chain_renders_none_of_the_cross_design_bay(self):
        """A refusal drops the CHAIN, not the rack: the inherited chassis is not
        there to plan into, so neither it nor the child's blade may render."""
        a = self._design("Network sweep IDS-1000")
        chassis_p = self._chassis_add(a, 12, name="planned-chassis")
        self._approve(a)
        b = self._design("Server build IDS-2000", based_on=a)
        self._base_parent_blade(b, chassis_p, "c1", name="blade-b")
        # Un-approve the ancestor: its layer is refused whole (§9.2).
        a.status = DesignStatusChoices.STATUS_DRAFT
        a.save()

        result = project_rack(b, self.racks[0])
        labels = [s["label"] for s in result.front + result.rear + result.non_racked]
        self.assertNotIn("planned-chassis", labels)
        self.assertNotIn("blade-b", labels)
        self.assertEqual(
            [c["kind"] for c in result.conflicts], ["ancestor_not_approved"]
        )
        keys = [e["key"] for e in chassis_in_scope(b)]
        self.assertNotIn(f"pl-{chassis_p.pk}", keys)

    def test_an_unchained_designs_planned_chassis_bay_is_unchanged(self):
        """Regression guard for routing the planned-parent match through the
        identity seam: a design with no ancestors must resolve its OWN planned
        chassis exactly as before."""
        solo = self._design("Solo IDS-4000")
        chassis_p = self._chassis_add(solo, 12, name="solo-chassis")
        own = self._blade_add(solo, parent=chassis_p, bay_name="c3", name="solo-blade")

        strip = self._strip(project_rack(solo, self.racks[0]), "solo-chassis")
        self.assertEqual(strip["c3"]["state"], ProjectedSlotState.ADD)
        self.assertEqual(strip["c3"]["placement"], own)
        self.assertFalse(strip["c3"]["inherited"])
        column = self._column(solo, f"pl-{chassis_p.pk}")
        self.assertEqual(self._bay(column, "c3")["placement"], own)
        self.assertEqual(column["conflicts"], [])


class ApplyMarkerProjectionTestCase(TestCase):
    """Apply-projection phase (PLAN §A4): once ``apply.run()`` has materialized
    a placement as a real ``dcim.Device``, projection must draw exactly ONE
    tile for it (the plan's own tile, flagged ``applied``) rather than that
    tile plus the DCIM copy apply just created -- the same defect shape as
    the 0.27.1 ghost/move_in overlap, now for apply. In any OTHER design
    whose scope includes the rack, the applied device renders as an ordinary
    existing device but flagged ``reserved_by_design_id``/
    ``reserved_by_design_title`` (falling back to the ``DesignApply`` row's
    name snapshot once the creating design is deleted).
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]  # Device 1 @ Rack1/U1/front, Device 2 @ U2/front
        cls.device_type = env["device_type"]
        cls.device_role = env["device_role"]
        cls.superuser = User.objects.create_superuser(username="apply-marker-super")

    def _design(self, title, *, based_on=None):
        return Design.objects.create(title=title, site=self.site, based_on=based_on)

    def _approve(self, design):
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()
        return design

    def _add(self, design, position, *, name="new-srv", rack=None, device_type=None, face="front"):
        return DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=device_type or self.device_type,
            target_rack=rack or self.racks[0],
            target_position=position,
            target_face=face,
            proposed_name=name,
            device_role=self.device_role,
        )

    def _move(self, design, device, position, *, rack=None, face="front"):
        return DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=device,
            target_rack=rack or self.racks[0],
            target_position=position,
            target_face=face,
        )

    def _at(self, slots, position):
        return [s for s in slots if s["u_position"] is not None and int(s["u_position"]) == position]

    def test_own_design_add_draws_exactly_one_tile_flagged_applied(self):
        design = self._design("Apply IDS-1000")
        self._add(design, 10, name="new-srv")
        self._approve(design)
        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)

        projected = project_rack(design, self.racks[0])
        at10 = self._at(projected.front, 10)
        self.assertEqual(len(at10), 1, at10)
        self.assertEqual(at10[0]["state"], ProjectedSlotState.ADD)
        self.assertTrue(at10[0]["applied"])
        self.assertIsNone(at10[0]["reserved_by_design_id"])

    def test_own_design_move_draws_exactly_one_tile_flagged_applied(self):
        design = self._design("Apply IDS-2000")
        self._move(design, self.devices[0], 10)
        self._approve(design)
        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)

        projected = project_rack(design, self.racks[0])
        at10 = self._at(projected.front, 10)
        self.assertEqual(len(at10), 1, at10)
        self.assertEqual(at10[0]["state"], ProjectedSlotState.MOVE_IN)
        self.assertTrue(at10[0]["applied"])
        self.assertIsNone(at10[0]["reserved_by_design_id"])
        # Device 1's OWN ghost at its original U1 is untouched by apply
        # (the real device a move acts on is left completely alone).
        at1 = self._at(projected.front, 1)
        self.assertEqual(len(at1), 1, at1)
        self.assertEqual(at1[0]["state"], ProjectedSlotState.MOVE_OUT_GHOST)
        self.assertFalse(at1[0]["applied"])

    def test_other_design_sees_the_applied_device_reserved_by_the_creating_design(self):
        creator = self._design("Apply IDS-3000")
        self._add(creator, 10, name="new-srv")
        self._approve(creator)
        result = apply.run(creator, self.superuser)
        self.assertTrue(result.ok, result.problems)

        other = self._design("Other IDS-4000")
        projected = project_rack(other, self.racks[0])
        at10 = self._at(projected.front, 10)
        self.assertEqual(len(at10), 1, at10)
        self.assertEqual(at10[0]["state"], ProjectedSlotState.EXISTING)
        self.assertFalse(at10[0]["applied"])
        self.assertEqual(at10[0]["reserved_by_design_id"], creator.pk)
        self.assertEqual(at10[0]["reserved_by_design_title"], str(creator))

    def test_reservation_survives_the_creating_design_being_deleted(self):
        creator = self._design("Apply IDS-5000")
        self._add(creator, 10, name="new-srv")
        self._approve(creator)
        result = apply.run(creator, self.superuser)
        self.assertTrue(result.ok, result.problems)
        creator.refresh_from_db()
        creator_title = str(creator)
        creator.delete()

        other = self._design("Other IDS-6000")
        projected = project_rack(other, self.racks[0])
        at10 = self._at(projected.front, 10)
        self.assertEqual(len(at10), 1, at10)
        self.assertIsNone(at10[0]["reserved_by_design_id"])
        self.assertEqual(at10[0]["reserved_by_design_title"], creator_title)

    def test_device_not_created_by_any_apply_is_unaffected(self):
        other = self._design("Other IDS-7000")
        projected = project_rack(other, self.racks[0])
        at1 = self._at(projected.front, 1)  # Device 1, plain real device
        self.assertEqual(len(at1), 1, at1)
        self.assertFalse(at1[0]["applied"])
        self.assertIsNone(at1[0]["reserved_by_design_id"])
        self.assertEqual(at1[0]["reserved_by_design_title"], "")

    def test_full_depth_mirror_carries_the_applied_flag(self):
        fd_type = DeviceType.objects.create(
            manufacturer=Manufacturer.objects.create(name="FD MF Apply", slug="fd-mf-apply"),
            model="FD Type Apply", slug="fd-type-apply",
            u_height=2, is_full_depth=True,
        )
        design = self._design("Apply IDS-8000")
        self._add(design, 15, name="fd-new-srv", device_type=fd_type)
        self._approve(design)
        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)

        projected = project_rack(design, self.racks[0])
        front15 = self._at(projected.front, 15)
        rear15 = self._at(projected.rear, 15)
        self.assertEqual(len(front15), 1, front15)
        self.assertEqual(len(rear15), 1, rear15)
        self.assertTrue(front15[0]["applied"])
        self.assertTrue(rear15[0]["applied"], "the _append mirror must carry the flag too")

    def test_query_count_does_not_grow_with_the_number_of_applied_devices(self):
        design = self._design("Apply IDS-9000")
        self._add(design, 10, name="new-srv-a")
        self._approve(design)
        result = apply.run(design, self.superuser)
        self.assertTrue(result.ok, result.problems)

        # Warm up any caches unrelated to the count we're proving.
        project_rack(design, self.racks[0])
        with CaptureQueriesContext(connection) as before:
            project_rack(design, self.racks[0])
        baseline_queries = len(before.captured_queries)

        # A pile of MORE applied devices, in the SAME rack being projected
        # (each its own design, so ``design`` is one query but device rows
        # returned by it are many) must not change the query count -- proving
        # the apply-marker lookup is one query regardless of row count.
        for i in range(15):
            bulk_design = self._design(f"Bulk apply IDS-{9100 + i}")
            self._add(bulk_design, 20 + i, name=f"bulk-{i}")
            self._approve(bulk_design)
            bulk_result = apply.run(bulk_design, self.superuser)
            self.assertTrue(bulk_result.ok, bulk_result.problems)

        with CaptureQueriesContext(connection) as after:
            project_rack(design, self.racks[0])
        self.assertEqual(
            len(after.captured_queries), baseline_queries,
            "query count must not scale with the number of applied devices",
        )


def _peer_plugins_config(**overrides):
    cfg = {"peer_conflicts_enabled": True}
    cfg.update(overrides)
    return {"netbox_rack_design": cfg}


class PeerConflictProjectionTestCase(TestCase):
    """PLAN-peer-conflicts.md phase 1: detection only, in the projection.

    Two designs that are not in the same chain are blind to each other while
    planning (PLAN-design-chains.md §2.1). ``_peer_conflicts`` reports the
    three P3 kinds through the same ``ProjectedElevation.conflicts`` channel
    the chain producers already use, and flags the colliding slots
    ``peer_conflict``/``peer_design_id``/``peer_design_title``.
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]  # Rack 1: Device1@U1, Device2@U2. Rack 2: empty.
        cls.devices = env["devices"]
        cls.device_type = env["device_type"]

    # --- helpers -------------------------------------------------------

    def _design(self, title, *, based_on=None, root=None, version=1):
        return Design.objects.create(
            title=title, site=self.site, based_on=based_on, root=root, version=version,
        )

    def _approve(self, design):
        design.status = DesignStatusChoices.STATUS_APPROVED
        design.save()
        return design

    def _implement(self, design):
        design.status = DesignStatusChoices.STATUS_IMPLEMENTED
        design.save()
        return design

    def _scope(self, design, rack):
        design.racks.add(rack)
        return design

    def _add(self, design, position, *, name, rack=None, face="front", device_type=None):
        return DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=device_type or self.device_type,
            target_rack=rack or self.racks[1],
            target_position=position,
            target_face=face,
            proposed_name=name,
        )

    def _move(self, design, device, position, *, rack=None, face="front", name=""):
        return DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=device,
            target_rack=rack or self.racks[1],
            target_position=position,
            target_face=face,
            proposed_name=name,
        )

    def _kinds(self, conflicts):
        return sorted(c["kind"] for c in conflicts)

    def _by_kind(self, conflicts, kind):
        return [c for c in conflicts if c["kind"] == kind]

    def _at(self, slots, position):
        return [s for s in slots if s["u_position"] is not None and int(s["u_position"]) == position]

    # --- P3: the three kinds ---------------------------------------------

    def test_two_peer_designs_overlapping_a_unit_reports_one_slot_claim(self):
        a = self._design("Peer A IDS-1000")
        self._scope(a, self.racks[1])
        self._add(a, 10, name="a-srv")

        b = self._design("Peer B IDS-2000")
        self._scope(b, self.racks[1])
        self._add(b, 10, name="b-srv")

        result = project_rack(a, self.racks[1])
        claims = self._by_kind(result.conflicts, "peer_slot_claim")
        self.assertEqual(len(claims), 1, result.conflicts)
        self.assertEqual(claims[0]["source_design"], b)
        self.assertIn("U10", claims[0]["detail"])

        at10 = self._at(result.front, 10)
        self.assertEqual(len(at10), 1, at10)
        self.assertTrue(at10[0]["peer_conflict"])
        self.assertEqual(at10[0]["peer_design_id"], b.pk)
        # The entry points at the tile BY IDENTITY, the join _conflict()
        # documents -- so the panel can highlight it (phase 2).
        self.assertIs(claims[0]["slot"], at10[0])
        self.assertEqual(at10[0]["peer_design_title"], str(b))

        # Symmetric (P6): B's own projection reports A right back.
        result_b = project_rack(b, self.racks[1])
        claims_b = self._by_kind(result_b.conflicts, "peer_slot_claim")
        self.assertEqual(len(claims_b), 1, result_b.conflicts)
        self.assertEqual(claims_b[0]["source_design"], a)

    def test_two_peer_designs_with_the_same_planned_name_reports_one_name_claim(self):
        a = self._design("Peer A IDS-3000")
        self._scope(a, self.racks[1])
        self._add(a, 10, name="dup-name")

        b = self._design("Peer B IDS-4000")
        self._scope(b, self.racks[1])
        self._add(b, 11, name="dup-name")

        result = project_rack(a, self.racks[1])
        claims = self._by_kind(result.conflicts, "peer_name_claim")
        self.assertEqual(len(claims), 1, result.conflicts)
        self.assertEqual(claims[0]["source_design"], b)
        self.assertIn("dup-name", claims[0]["detail"])
        # Not also reported as a slot claim -- different units.
        self.assertEqual(self._by_kind(result.conflicts, "peer_slot_claim"), [])

    def test_two_peer_designs_moving_the_same_device_reports_one_device_claim(self):
        a = self._design("Peer A IDS-5000")
        self._scope(a, self.racks[1])
        self._move(a, self.devices[0], 10)

        b = self._design("Peer B IDS-6000")
        self._scope(b, self.racks[1])
        self._move(b, self.devices[0], 11)

        result = project_rack(a, self.racks[1])
        claims = self._by_kind(result.conflicts, "peer_device_claim")
        self.assertEqual(len(claims), 1, result.conflicts)
        self.assertEqual(claims[0]["source_design"], b)
        self.assertIn(str(self.devices[0]), claims[0]["detail"])
        # Not also reported as a slot claim -- different target units.
        self.assertEqual(self._by_kind(result.conflicts, "peer_slot_claim"), [])

    def test_peer_removal_is_not_a_case(self):
        """A peer planning to REMOVE the device at a unit is not a peer
        conflict (P3): the real device still stands there, and the ordinary
        occupancy rule already stops this design from adding onto it."""
        a = self._design("Peer A IDS-6500")
        self._scope(a, self.racks[0])
        DesignPlacement.objects.create(
            design=a, kind=DesignPlacementKindChoices.KIND_REMOVE, device=self.devices[1],  # U2
        )
        b = self._design("Peer B IDS-6600")
        self._scope(b, self.racks[0])
        # Same unit the peer is removing -- must NOT be flagged as a
        # peer_slot_claim (the real device at U2 is what stops this add, via
        # the ordinary occupancy rule, not a peer conflict).
        self._add(b, 2, name="unrelated", rack=self.racks[0])

        result = project_rack(b, self.racks[0])
        self.assertEqual(self._by_kind(result.conflicts, "peer_slot_claim"), [])

    def test_a_peer_landing_on_untouched_reality_is_not_this_designs_conflict(self):
        """A peer dropping a device onto a REAL device this design never
        touches is the peer's own collision, not this design's.

        Without this the panel would fill with entries the reader cannot act
        on: every design that merely scopes a busy rack would inherit every
        other design's collisions with reality.
        """
        a = self._design("Peer A IDS-6700")
        self._scope(a, self.racks[0])
        self._add(a, 20, name="a-srv", rack=self.racks[0])  # nowhere near U1

        b = self._design("Peer B IDS-6800")
        self._scope(b, self.racks[0])
        self._add(b, 1, name="b-srv", rack=self.racks[0])  # onto real Device 1

        result = project_rack(a, self.racks[0])
        self.assertEqual(self._by_kind(result.conflicts, "peer_slot_claim"), [])
        self.assertEqual(
            [s["u_position"] for s in result.front if s["peer_conflict"]], [],
        )

    def test_a_peer_over_an_inherited_slot_is_reported(self):
        """An INHERITED claim is this design's claim, so a peer over it is a
        real conflict -- the other side of the reality carve-out above."""
        ancestor = self._design("Ancestor IDS-6900")
        self._scope(ancestor, self.racks[1])
        self._add(ancestor, 10, name="anc-srv")
        self._approve(ancestor)

        child = self._design("Child IDS-6910", based_on=ancestor)
        self._scope(child, self.racks[1])  # plans nothing of its own

        peer = self._design("Peer IDS-6920")
        self._scope(peer, self.racks[1])
        self._add(peer, 10, name="peer-srv")

        result = project_rack(child, self.racks[1])
        claims = self._by_kind(result.conflicts, "peer_slot_claim")
        self.assertEqual(len(claims), 1, result.conflicts)
        self.assertEqual(claims[0]["source_design"], peer)
        at10 = self._at(result.front, 10)
        self.assertTrue(at10[0]["inherited"])
        self.assertTrue(at10[0]["peer_conflict"])

    def test_a_whole_unit_reads_as_U10_not_U10_0(self):
        """The detail line is read by a human; ``target_position`` is a
        Decimal, so it stringifies as ``10.0`` unless formatted."""
        a = self._design("Peer A IDS-6930")
        self._scope(a, self.racks[1])
        self._add(a, 10, name="a-srv")
        b = self._design("Peer B IDS-6940")
        self._scope(b, self.racks[1])
        self._add(b, 10, name="b-srv")

        detail = self._by_kind(project_rack(a, self.racks[1]).conflicts, "peer_slot_claim")[0]["detail"]
        self.assertIn("U10 ", detail)
        self.assertNotIn("U10.0", detail)

    # --- lineage / version exclusions (P2) --------------------------------

    def test_ancestor_claim_is_not_reported(self):
        ancestor = self._design("Ancestor IDS-7000")
        self._scope(ancestor, self.racks[1])
        self._add(ancestor, 10, name="anc-srv")
        self._approve(ancestor)

        # Same unit as the ancestor's -- absent the lineage exclusion this
        # would be flagged a peer_slot_claim; instead it is inheritance.
        child = self._design("Child IDS-7100", based_on=ancestor)
        self._scope(child, self.racks[1])
        self._add(child, 10, name="child-srv")

        result = project_rack(child, self.racks[1])
        self.assertEqual(result.conflicts, [])

    def test_descendant_claim_is_not_reported(self):
        parent = self._design("Parent IDS-7200")
        self._scope(parent, self.racks[1])
        self._add(parent, 10, name="parent-srv")
        self._approve(parent)

        # based_on does not itself require the parent to stay approved; the
        # child claims the SAME unit, which -- absent the descendant
        # exclusion -- would be a peer_slot_claim.
        child = self._design("Child IDS-7300", based_on=parent)
        self._scope(child, self.racks[1])
        self._add(child, 10, name="child-srv")

        # Projecting the PARENT: the child is a descendant, not a peer, even
        # though it also scopes and claims the same unit in the same rack.
        result = project_rack(parent, self.racks[1])
        self.assertEqual(result.conflicts, [])

    def test_another_version_of_the_same_plan_is_not_reported(self):
        root = self._design("Plan root IDS-7400")
        self._scope(root, self.racks[1])
        self._add(root, 10, name="v1-srv")

        v2 = self._design("Plan root IDS-7400", root=root, version=2)
        self._scope(v2, self.racks[1])
        self._add(v2, 10, name="v2-srv")

        result = project_rack(root, self.racks[1])
        self.assertEqual(result.conflicts, [])

    def test_implemented_peer_is_not_reported(self):
        peer = self._design("Implemented peer IDS-7500")
        self._scope(peer, self.racks[1])
        self._add(peer, 10, name="impl-srv")
        self._approve(peer)
        self._implement(peer)

        other = self._design("Other IDS-7600")
        self._scope(other, self.racks[1])
        self._add(other, 10, name="other-srv")

        result = project_rack(other, self.racks[1])
        self.assertEqual(result.conflicts, [])

    # --- P1: severity -----------------------------------------------------

    def test_approved_peer_is_error_and_draft_peer_is_warning(self):
        approved_peer = self._design("Approved peer IDS-7700")
        self._scope(approved_peer, self.racks[1])
        self._add(approved_peer, 10, name="appr-srv")
        self._approve(approved_peer)

        draft_peer = self._design("Draft peer IDS-7800")
        self._scope(draft_peer, self.racks[1])
        self._add(draft_peer, 11, name="draft-srv")

        mine = self._design("Mine IDS-7900")
        self._scope(mine, self.racks[1])
        self._add(mine, 10, name="mine-a")
        self._add(mine, 11, name="mine-b")

        result = project_rack(mine, self.racks[1])
        claims = self._by_kind(result.conflicts, "peer_slot_claim")
        by_design = {c["source_design"].pk: c["severity"] for c in claims}
        self.assertEqual(by_design[approved_peer.pk], "error")
        self.assertEqual(by_design[draft_peer.pk], "warning")

    # --- P16: named even when the peer is not viewable --------------------

    def test_peer_is_named_even_though_nothing_here_checks_visibility(self):
        """Detection never scopes the peer query by NetBox object permissions
        (P16): the projection is a plain read, so the peer's title is always
        in the conflict/slot, regardless of who is looking. Enforcing "the
        user cannot view design C" is a view-layer concern (later phases);
        this pins that detection itself does not filter it out."""
        hidden = self._design("Hidden design IDS-8000")
        self._scope(hidden, self.racks[1])
        self._add(hidden, 10, name="hidden-srv")

        mine = self._design("Mine IDS-8100")
        self._scope(mine, self.racks[1])
        self._add(mine, 10, name="mine-srv")

        result = project_rack(mine, self.racks[1])
        claims = self._by_kind(result.conflicts, "peer_slot_claim")
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["source_design"].title, "Hidden design IDS-8000")
        self.assertIn("Hidden design IDS-8000", claims[0]["detail"])
        at10 = self._at(result.front, 10)
        self.assertEqual(at10[0]["peer_design_title"], str(hidden))

    # --- full-depth mirror -------------------------------------------------

    def test_full_depth_mirror_carries_peer_conflict_flags(self):
        fd_type = DeviceType.objects.create(
            manufacturer=self.device_type.manufacturer, model="FD Type Peer",
            slug="fd-type-peer", u_height=2, is_full_depth=True,
        )
        a = self._design("Peer A IDS-8200")
        self._scope(a, self.racks[1])
        self._add(a, 15, name="a-fd", device_type=fd_type)

        b = self._design("Peer B IDS-8300")
        self._scope(b, self.racks[1])
        self._add(b, 15, name="b-fd", device_type=fd_type)

        result = project_rack(a, self.racks[1])
        front15 = self._at(result.front, 15)
        rear15 = self._at(result.rear, 15)
        self.assertEqual(len(front15), 1, front15)
        self.assertEqual(len(rear15), 1, rear15)
        self.assertTrue(front15[0]["peer_conflict"])
        self.assertTrue(rear15[0]["peer_conflict"], "the _append mirror must carry the flag too")
        self.assertEqual(front15[0]["peer_design_id"], b.pk)
        self.assertEqual(rear15[0]["peer_design_id"], b.pk)

    # --- P13: the config flag ------------------------------------------

    @override_settings(PLUGINS_CONFIG=_peer_plugins_config(peer_conflicts_enabled=False))
    def test_flag_off_produces_no_conflicts_and_no_extra_query(self):
        a = self._design("Peer A IDS-8400")
        self._scope(a, self.racks[1])
        self._add(a, 10, name="a-srv")

        b = self._design("Peer B IDS-8500")
        self._scope(b, self.racks[1])
        self._add(b, 10, name="b-srv")

        with CaptureQueriesContext(connection) as off:
            result = project_rack(a, self.racks[1])
        self.assertEqual(result.conflicts, [])

        with override_settings(PLUGINS_CONFIG=_peer_plugins_config(peer_conflicts_enabled=True)):
            with CaptureQueriesContext(connection) as on:
                project_rack(a, self.racks[1])
        self.assertLess(
            len(off.captured_queries), len(on.captured_queries),
            "turning the flag off must skip the peer query entirely, not just "
            "discard its result",
        )

    # --- performance --------------------------------------------------------

    def test_query_count_does_not_grow_with_the_number_of_peers(self):
        mine = self._design("Mine IDS-8600")
        self._scope(mine, self.racks[1])
        self._add(mine, 10, name="mine-srv")

        # Warm up caches unrelated to the count being proven.
        project_rack(mine, self.racks[1])
        with CaptureQueriesContext(connection) as before:
            project_rack(mine, self.racks[1])
        baseline_queries = len(before.captured_queries)

        for i in range(15):
            peer = self._design(f"Bulk peer IDS-{8700 + i}")
            self._scope(peer, self.racks[1])
            self._add(peer, 10, name=f"bulk-{i}", rack=self.racks[1])

        with CaptureQueriesContext(connection) as after:
            project_rack(mine, self.racks[1])
        self.assertEqual(
            len(after.captured_queries), baseline_queries,
            "query count must not scale with the number of peer designs",
        )
