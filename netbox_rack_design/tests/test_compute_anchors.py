"""Pure unit tests for ``stamping.compute_anchors`` -- no database, no Django
test case machinery. The function is documented as DB-free (see its module
docstring in ``stamping.py``), so these tests must not need one either: a
plain ``unittest.TestCase`` proves that on its own.

The core property under test is the round-trip: for a layout with no
islands, ``compute_anchors`` followed by ``compute_stamp`` back into an EMPTY
rack of the same height/starting_unit/desc_units must reproduce the original
absolute positions exactly. That is the whole point of the function -- a
template stores no absolute positions, only (anchor, order), so unless this
round-trip holds, a "save rack as template" -> "stamp template back into the
same rack" cycle would silently reshuffle devices.
"""

import unittest
from dataclasses import dataclass
from decimal import Decimal

from netbox_rack_design.stamping import compute_anchors, compute_stamp


@dataclass
class Item:
    """Duck-typed stand-in for a real device/placement -- the only shape
    ``compute_anchors``/``compute_stamp`` care about (see their docstrings).
    ``name`` is not read by either function; it just makes assertion
    failures readable.
    """

    name: str
    position: Decimal
    u_height: Decimal
    face: str = "front"
    is_full_depth: bool = False
    # Populated by ``compute_stamp`` in the round-trip helper below, not by
    # the fixtures themselves.
    anchor: str | None = None


def _dec(value):
    return value if isinstance(value, Decimal) else Decimal(str(value))


def make_item(name, position, u_height, face="front", is_full_depth=False):
    return Item(
        name=name,
        position=_dec(position),
        u_height=_dec(u_height),
        face=face,
        is_full_depth=is_full_depth,
    )


def assert_round_trips(test, items, u_height, *, starting_unit=1, desc_units=False, expected_islands=()):
    """Run ``compute_anchors`` over ``items``, then walk the result back
    through ``compute_stamp`` into a brand-new EMPTY rack of the same shape,
    and assert every non-island item lands back on its original
    ``(position, face)``.

    ``expected_islands`` is the set of item names expected to come back in
    ``islands`` -- everything else must round-trip. This lets a single
    fixture exercise "some devices round-trip, one genuine island does not"
    in one call, matching the task's own island test requirement.
    """
    anchored, islands = compute_anchors(
        items, u_height, starting_unit=starting_unit, desc_units=desc_units
    )

    island_names = {item.name for item, _reason in islands}
    test.assertEqual(island_names, set(expected_islands))
    for _item, reason in islands:
        test.assertTrue(reason)  # never a silent/empty reason

    # Build the walk-order list compute_stamp needs: anchored is already in
    # "all top entries in order, then all bottom entries in order" sequence
    # (compute_anchors's own contract), so just stamp the anchor onto a
    # lightweight copy and feed it straight through.
    walk_items = []
    for item, anchor, _order in anchored:
        walk_items.append(
            Item(
                name=item.name,
                position=item.position,
                u_height=item.u_height,
                face=item.face,
                is_full_depth=item.is_full_depth,
                anchor=anchor,
            )
        )

    placements, unplaced = compute_stamp(
        walk_items, {"front": [], "rear": []}, u_height, starting_unit=starting_unit
    )

    test.assertEqual(unplaced, [])
    by_name = {item.name: (position, face) for item, position, face in placements}
    test.assertEqual(len(by_name), len(walk_items))

    originals_by_name = {item.name: item for item in items if item.name not in island_names}
    for name, original in originals_by_name.items():
        test.assertIn(name, by_name, f"{name} did not come back out of compute_stamp at all")
        position, face = by_name[name]
        test.assertEqual(position, original.position, f"{name} position drifted")
        test.assertEqual(face, original.face, f"{name} face drifted")

    return anchored, islands


class ComputeAnchorsRoundTripTests(unittest.TestCase):
    def test_worked_tor_five_items_at_top_of_47u(self):
        # PLAN-templates.md section 3's worked example: 5x 1U items stacked
        # flush against the physical top of a 47U rack, positions 47..43.
        items = [
            make_item("patch-panel", 47, 1),
            make_item("organizer-1", 46, 1),
            make_item("switch-1", 45, 1),
            make_item("organizer-2", 44, 1),
            make_item("switch-2", 43, 1),
        ]
        anchored, islands = assert_round_trips(self, items, 47)
        self.assertEqual(islands, [])
        self.assertEqual([(item.name, anchor, order) for item, anchor, order in anchored], [
            ("patch-panel", "top", 0),
            ("organizer-1", "top", 1),
            ("switch-1", "top", 2),
            ("organizer-2", "top", 3),
            ("switch-2", "top", 4),
        ])

    def test_top_group_and_bottom_group_same_rack(self):
        items = [
            make_item("top-a", 47, 1),
            make_item("top-b", 45, 2),  # occupies 45-46, flush below top-a
            make_item("bottom-a", 1, 1),
            make_item("bottom-b", 2, 3),
        ]
        anchored, islands = assert_round_trips(self, items, 47)
        self.assertEqual(islands, [])
        anchors = {item.name: (anchor, order) for item, anchor, order in anchored}
        self.assertEqual(anchors["top-a"], ("top", 0))
        self.assertEqual(anchors["top-b"], ("top", 1))
        self.assertEqual(anchors["bottom-a"], ("bottom", 0))
        self.assertEqual(anchors["bottom-b"], ("bottom", 1))

    def test_full_depth_item_among_half_depth_ones(self):
        # A full-depth item sits directly below two front-only items and
        # blocks the rear at that same span; nothing else is on the rear.
        # It should round-trip via the front chain's contiguity.
        items = [
            make_item("front-1", 47, 1, face="front"),
            make_item("front-2", 46, 1, face="front"),
            make_item("chassis", 44, 2, face="front", is_full_depth=True),
        ]
        assert_round_trips(self, items, 47)

    def test_half_unit_positions(self):
        items = [
            # 47.5-48.0 is the top half of U47 -- flush against the physical
            # top edge (48.0); 47.0-47.5 sits directly below it.
            make_item("shelf-top", Decimal("47.5"), Decimal("0.5")),
            make_item("shelf-below", Decimal("47.0"), Decimal("0.5")),
            make_item("organizer-bottom", Decimal("1.0"), Decimal("0.5")),
        ]
        assert_round_trips(self, items, 47)

    def test_desc_units_not_mirrored(self):
        # Same relative shape as test_worked_tor..., but in a desc_units=True
        # rack (U1 numbered at the physical top). If the maths were done
        # backwards, this would come out identical to the ascending case
        # (position 47 at physical top) instead of mirrored (position 1 at
        # physical top). Assert it is NOT the ascending-case answer.
        items = [
            make_item("d-top-1", 1, 1),
            make_item("d-top-2", 2, 1),
        ]
        anchored, islands = assert_round_trips(self, items, 47, desc_units=True)
        self.assertEqual(islands, [])
        anchors = {item.name: (anchor, order) for item, anchor, order in anchored}
        # Position 1 is the physical top when desc_units=True. But
        # compute_stamp itself has no idea what desc_units means -- its
        # anchor="top" always scans from the numerically HIGHEST unit. The
        # only way to get compute_stamp to put these back at the numerically
        # LOW positions 1/2 (where they physically started) is to label them
        # anchor="bottom" (compute_stamp's bottom scan starts at the
        # numerically low end and walks up). A naive implementation that
        # forgot to invert this label would hand back anchor="top" here,
        # which round-trips to positions 47/46 instead of 1/2 -- exactly the
        # "silently mirrors the whole layout" bug this test exists to catch.
        # (assert_round_trips above already proves the POSITIONS come back
        # correct; this asserts the mechanism -- the label -- is the reason
        # why, not an accident.)
        self.assertEqual(anchors["d-top-1"], ("bottom", 0))
        self.assertEqual(anchors["d-top-2"], ("bottom", 1))

    def test_desc_units_true_full_layout_round_trips(self):
        # A fuller desc_units=True layout: a top run (physical top = U1) and
        # a bottom run (physical bottom = U47), verifying the mirrored
        # maths end to end, not just the classification of one pair.
        items = [
            make_item("top-1", 1, 1),
            make_item("top-2", 2, 2),
            make_item("bottom-1", 47, 1),
            make_item("bottom-2", 45, 2),
        ]
        assert_round_trips(self, items, 47, desc_units=True)

    def test_starting_unit_greater_than_one(self):
        # A rack (or a sub-frame) that does not start counting at 1 -- the
        # physical top/bottom formulas must use starting_unit, not a bare 1.
        items = [
            make_item("top-1", 20, 1),
            make_item("bottom-1", 10, 1),
        ]
        assert_round_trips(self, items, 11, starting_unit=10)

    def test_front_and_rear_populated_independently(self):
        items = [
            make_item("front-top", 47, 1, face="front"),
            make_item("front-bottom", 1, 1, face="front"),
            make_item("rear-top", 46, 2, face="rear"),  # occupies 46-47
            make_item("rear-bottom", 1, 3, face="rear"),
        ]
        anchored, islands = assert_round_trips(self, items, 47)
        self.assertEqual(islands, [])
        anchors = {item.name: (anchor, order) for item, anchor, order in anchored}
        self.assertEqual(anchors["front-top"][0], "top")
        self.assertEqual(anchors["rear-top"][0], "top")
        self.assertEqual(anchors["front-bottom"][0], "bottom")
        self.assertEqual(anchors["rear-bottom"][0], "bottom")

    def test_genuine_island_reported_rest_round_trips(self):
        # top-1/top-2 form a contiguous run at the physical top; bottom-1
        # forms one at the physical bottom; island-item sits alone in the
        # middle with dead air on both sides -- it cannot be represented.
        items = [
            make_item("top-1", 47, 1),
            make_item("top-2", 46, 1),
            make_item("island-item", 20, 1),
            make_item("bottom-1", 1, 1),
        ]
        anchored, islands = assert_round_trips(
            self, items, 47, expected_islands=("island-item",)
        )
        self.assertEqual(len(islands), 1)
        island_item, reason = islands[0]
        self.assertEqual(island_item.name, "island-item")
        self.assertIn("dead air", reason)
        # The island must not have consumed an anchor/order slot.
        names_in_anchored = [item.name for item, _a, _o in anchored]
        self.assertNotIn("island-item", names_in_anchored)

    def test_empty_rack_no_items(self):
        anchored, islands = compute_anchors([], 47)
        self.assertEqual(anchored, [])
        self.assertEqual(islands, [])


if __name__ == "__main__":
    unittest.main()
