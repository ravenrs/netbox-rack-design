"""
Unit tests for ``netbox_rack_design.stamping.compute_stamp`` -- the pure
template-placement algorithm (PLAN-templates.md Phase 3, D1/D12/D15/D16/D17).

Deliberately a plain ``unittest.TestCase``, not a NetBox/Django test mixin:
the function under test takes no database, no HTTP and no Django models, so
proving it right must not need a DB either. This is the handoff contract for
every other stamping task (T3.2/T3.3) -- if these fail, nothing downstream can
be trusted.
"""

import unittest
from dataclasses import dataclass
from decimal import Decimal

# ``stamping`` does not exist yet -- this import is expected to fail until
# T3.1's implementation step. That failure IS the "confirm it fails" gate.
from netbox_rack_design.stamping import compute_stamp


@dataclass
class Item:
    """A minimal stand-in for a ``TemplatePlacement``-shaped object. The
    function under test only reads these four attributes (duck-typed), so
    any object -- ORM instance included -- with this shape works."""

    name: str
    u_height: object  # Decimal or float/int; may be half-U
    face: str = "front"
    anchor: str = "top"
    is_full_depth: bool = False


class ComputeStampTests(unittest.TestCase):
    # --- the worked example from PLAN-templates.md Sec 3 -------------------

    def test_worked_example_from_plan(self):
        items = [
            Item("patch panel", 1, anchor="top"),
            Item("organizer", 1, anchor="top"),
            Item("switch", 1, anchor="top"),
            Item("organizer", 1, anchor="top"),
            Item("switch", 1, anchor="top"),
            Item("PDU shelf", 1, anchor="bottom"),
            Item("UPS", 1, anchor="bottom"),
        ]
        # U46 already taken on the front face.
        occupied = {"front": [(46, 1)], "rear": []}

        placements, unplaced = compute_stamp(items, occupied, 47)

        self.assertEqual(unplaced, [])
        expected = [
            ("patch panel", 47),
            ("organizer", 45),
            ("switch", 44),
            ("organizer", 43),
            ("switch", 42),
            ("PDU shelf", 1),
            ("UPS", 2),
        ]
        got = [(item.name, position) for item, position, face in placements]
        self.assertEqual(got, expected)

    # --- rule 1: full-depth blocks/is-blocked-by both faces -----------------

    def test_full_depth_occupies_and_blocks_both_faces(self):
        # A full-depth item placed first must reserve U47 on BOTH faces, so a
        # later front-face item anchored top must skip U47 and land on U46.
        items = [
            Item("full-depth switch", 1, anchor="top", is_full_depth=True),
            Item("front-only device", 1, anchor="top", face="front"),
        ]
        occupied = {"front": [], "rear": []}

        placements, unplaced = compute_stamp(items, occupied, 47)

        self.assertEqual(unplaced, [])
        positions = {item.name: position for item, position, face in placements}
        self.assertEqual(positions["full-depth switch"], 47)
        self.assertEqual(positions["front-only device"], 46)

    def test_full_depth_is_blocked_by_existing_occupant_on_either_face(self):
        # Only the rear face is pre-occupied at U47; a full-depth item must
        # still be refused U47 because it needs both faces free.
        items = [Item("full-depth switch", 1, anchor="top", is_full_depth=True)]
        occupied = {"front": [], "rear": [(47, 1)]}

        placements, unplaced = compute_stamp(items, occupied, 47)

        self.assertEqual(unplaced, [])
        self.assertEqual(placements[0][1], 46)

    # --- rule 2: non-full-depth items only interact with their own face -----

    def test_non_full_depth_front_and_rear_are_independent(self):
        items = [
            Item("front device", 1, anchor="top", face="front"),
            Item("rear device", 1, anchor="top", face="rear"),
        ]
        occupied = {"front": [], "rear": []}

        placements, unplaced = compute_stamp(items, occupied, 47)

        self.assertEqual(unplaced, [])
        positions = {(item.name, face): position for item, position, face in placements}
        # Both land at U47 -- they are on different faces so neither blocks
        # the other.
        self.assertEqual(positions[("front device", "front")], 47)
        self.assertEqual(positions[("rear device", "rear")], 47)

    # --- rule 3: half-unit positions, mirroring _u_interval_overlap ---------

    def test_half_unit_positions_are_respected(self):
        # A 0.5U-tall occupant at U10.0..U10.5 (front) leaves U10.5..U11.0
        # free on the same face for another half-U item.
        items = [Item("half-u organizer", Decimal("0.5"), anchor="bottom")]
        occupied = {"front": [(Decimal("10"), Decimal("0.5"))], "rear": []}

        placements, unplaced = compute_stamp(
            items, occupied, 47, starting_unit=1
        )

        # Scanning from the bottom (U1) upward, the first free 0.5U slot is
        # U1.0, well below the U10 occupant -- this just proves half-unit
        # arithmetic doesn't crash/misalign, exercised more precisely below.
        self.assertEqual(unplaced, [])
        self.assertEqual(placements[0][1], Decimal("1"))

    def test_half_unit_occupant_blocks_only_its_own_half(self):
        # Occupant at U10.0-U10.5 (front). An item anchored top scanning
        # downward from U11 in a tiny 11U rack: U11 is 1U tall and free, so
        # it should land there -- U10 is untouched by this scan. This checks
        # that overlap uses the same half-open interval test as
        # _u_interval_overlap (start_a < end_b and start_b < end_a).
        items = [Item("half-u item", Decimal("0.5"), anchor="top")]
        occupied = {"front": [(Decimal("10"), Decimal("0.5"))], "rear": []}

        placements, unplaced = compute_stamp(items, occupied, 11)

        self.assertEqual(unplaced, [])
        # Highest legal 0.5U slot from the top in an 11U rack is U11.0 (since
        # 11.0 + 0.5 = 11.5 <= 12, the rack's exclusive top edge for starting
        # at unit 1... but per rule 4 below, a device may not hang off the
        # top: the top edge is u_height+starting_unit = 12, so 11.5 is fine.
        # Simpler: just confirm it did not collide with the occupant.
        pos = placements[0][1]
        self.assertFalse(
            pos <= Decimal("10.5") and pos + Decimal("0.5") > Decimal("10")
        )

    def test_half_unit_exact_overlap_is_rejected(self):
        # Occupant U10.0-U10.5 (front). An item that would land exactly at
        # U10.0-U10.5 must be rejected and pushed to the next free half-slot.
        items = [
            Item("filler", Decimal("36.5"), anchor="top"),  # fills U10.5..U47
            Item("half-u item", Decimal("0.5"), anchor="top"),
        ]
        occupied = {"front": [(Decimal("10"), Decimal("0.5"))], "rear": []}

        placements, unplaced = compute_stamp(items, occupied, 47)

        self.assertEqual(unplaced, [])
        by_name = {item.name: position for item, position, face in placements}
        # filler (36.5U, anchored top) takes the highest legal start,
        # U11.5..U48 (exclusive), because U11.5 does not overlap the
        # pre-existing U10.0-U10.5 occupant. The half-u item, scanning from
        # the top, must then skip the filler and land in the next free
        # half-slot immediately below it: U11.0-U11.5 (U10.5-U11.0 is free
        # too, but U11.0 is nearer the top anchor and is scanned first).
        self.assertEqual(by_name["filler"], Decimal("11.5"))
        self.assertEqual(by_name["half-u item"], Decimal("11.0"))

    # --- rule 4: an item anchored top must not hang off the rack top -------

    def test_top_anchored_multi_u_does_not_hang_off_top(self):
        items = [Item("2u switch", 2, anchor="top")]
        occupied = {"front": [], "rear": []}

        placements, unplaced = compute_stamp(items, occupied, 47)

        self.assertEqual(unplaced, [])
        # Highest legal position for a 2U item in a 47U rack is 46 (occupying
        # U46-U47), not 47 (which would occupy U47-U48, off the rack).
        self.assertEqual(placements[0][1], 46)

    def test_item_taller_than_rack_is_unplaced(self):
        items = [Item("too tall", 48, anchor="top")]
        occupied = {"front": [], "rear": []}

        placements, unplaced = compute_stamp(items, occupied, 47)

        self.assertEqual(placements, [])
        self.assertEqual(len(unplaced), 1)
        self.assertEqual(unplaced[0][0].name, "too tall")

    # --- rule 5: earlier placements occupy space for later ones -------------

    def test_earlier_placements_block_later_ones_in_same_call(self):
        items = [
            Item("first", 47, anchor="top"),  # fills the whole rack
            Item("second", 1, anchor="top"),
        ]
        occupied = {"front": [], "rear": []}

        placements, unplaced = compute_stamp(items, occupied, 47)

        self.assertEqual(len(placements), 1)
        self.assertEqual(placements[0][0].name, "first")
        self.assertEqual(len(unplaced), 1)
        self.assertEqual(unplaced[0][0].name, "second")

    # --- rule 6: starting_unit above 1 -- units below it don't exist --------

    def test_starting_unit_above_one_bounds_the_scan(self):
        # A rack whose starting_unit is 5 and u_height 10 spans U5..U14.
        items = [
            Item("bottom item", 1, anchor="bottom"),
            Item("top item", 1, anchor="top"),
        ]
        occupied = {"front": [], "rear": []}

        placements, unplaced = compute_stamp(
            items, occupied, 10, starting_unit=5
        )

        self.assertEqual(unplaced, [])
        by_name = {item.name: position for item, position, face in placements}
        self.assertEqual(by_name["bottom item"], 5)
        self.assertEqual(by_name["top item"], 14)

    # --- rule 7: deterministic / stable ordering -----------------------------

    def test_deterministic_across_repeated_calls(self):
        items = [
            Item("a", 1, anchor="top"),
            Item("b", 1, anchor="top"),
            Item("c", 1, anchor="bottom"),
        ]
        occupied = {"front": [], "rear": []}

        first = compute_stamp(items, occupied, 47)
        second = compute_stamp(items, occupied, 47)

        first_positions = [(p[0].name, p[1], p[2]) for p in first[0]]
        second_positions = [(p[0].name, p[1], p[2]) for p in second[0]]
        self.assertEqual(first_positions, second_positions)
        self.assertEqual(first[1], second[1])

    # --- 0U items (a PDU/tray device) go to the tray, never a rack unit -----
    #
    # BUG (2026-09-23): a 0U ``u_height`` item was being walked through
    # ``_find_slot`` like any other item and landed at a real position
    # (typically the top of the scan), colliding with other 0U items that
    # all "fit" at the same unit. A 0U device type belongs in the rack's
    # NON-RACKED TRAY (``DesignPlacement.target_position = None``, no
    # face -- see ``_RackSlotTarget``/save-layout's "other" bucket), not in
    # a numbered unit.

    def test_zero_height_item_lands_in_tray_not_a_unit(self):
        items = [Item("PDU-2B", 0, anchor="bottom")]
        occupied = {"front": [], "rear": []}

        placements, unplaced = compute_stamp(items, occupied, 47)

        self.assertEqual(unplaced, [])
        self.assertEqual(len(placements), 1)
        item, position, face = placements[0]
        self.assertIsNone(position)
        self.assertEqual(face, "")

    def test_two_zero_height_items_do_not_collide(self):
        # This is the exact bug signature reported live: two 0U PDU template
        # placements (anchor="bottom", order 0/1) both landed at position
        # 1.0/"front" and collided. Neither should ever get a real position.
        items = [
            Item("PDU-2B a", 0, anchor="bottom"),
            Item("PDU-2B b", 0, anchor="bottom"),
        ]
        occupied = {"front": [], "rear": []}

        placements, unplaced = compute_stamp(items, occupied, 47)

        self.assertEqual(unplaced, [])
        self.assertEqual(len(placements), 2)
        for _item, position, face in placements:
            self.assertIsNone(position)
            self.assertEqual(face, "")

    def test_zero_height_item_does_not_consume_a_unit(self):
        # A 0U item and a 1U item anchored top must not fight over U47 -- the
        # 0U item never enters the unit-packing walk at all.
        items = [
            Item("PDU-2B", 0, anchor="top"),
            Item("1U switch", 1, anchor="top"),
        ]
        occupied = {"front": [], "rear": []}

        placements, unplaced = compute_stamp(items, occupied, 47)

        self.assertEqual(unplaced, [])
        by_name = {item.name: (position, face) for item, position, face in placements}
        self.assertIsNone(by_name["PDU-2B"][0])
        self.assertEqual(by_name["1U switch"][0], 47)

    def test_all_zero_height_items_in_a_tiny_rack_does_not_crash(self):
        # Every item in the template is 0U -- the unit-packing walk never
        # runs at all. Must not divide by zero or otherwise crash on an
        # effectively-empty rack.
        items = [
            Item("PDU-2B a", 0, anchor="top"),
            Item("PDU-2B b", 0, anchor="bottom"),
        ]
        occupied = {"front": [], "rear": []}

        placements, unplaced = compute_stamp(items, occupied, 1, starting_unit=1)

        self.assertEqual(unplaced, [])
        self.assertEqual(len(placements), 2)
        for _item, position, face in placements:
            self.assertIsNone(position)
            self.assertEqual(face, "")

    # --- D12: an unplaced item does not shift the others --------------------

    def test_unplaced_item_does_not_shift_subsequent_items(self):
        items = [
            Item("blocker", 47, anchor="top"),  # fills the rack, unplaced? no it fits
        ]
        # Rebuild a scenario: item 1 cannot fit (too tall), item 2 after it
        # must still land in its own first-free slot as if item 1 never
        # existed.
        items = [
            Item("impossible", 48, anchor="top"),
            Item("normal", 1, anchor="top"),
        ]
        occupied = {"front": [], "rear": []}

        placements, unplaced = compute_stamp(items, occupied, 47)

        self.assertEqual(len(unplaced), 1)
        self.assertEqual(unplaced[0][0].name, "impossible")
        self.assertEqual(len(placements), 1)
        self.assertEqual(placements[0][0].name, "normal")
        self.assertEqual(placements[0][1], 47)


if __name__ == "__main__":
    unittest.main()
