"""
Full-depth device handling for the rack editor (Stage 2, slice 2f follow-up).

A full-depth device physically occupies BOTH rack faces. NetBox's own
``Rack.get_rack_units`` already returns existing full-depth devices on both
faces; these tests assert that the design PROJECTION mirrors design slots
(add / move_in / move_out_ghost / remove) to both faces too, and that the
save-layout reconcile collapses the two per-face copies the editor submits back
into a SINGLE DesignPlacement (idempotent, never duplicated).
"""

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Rack, Site
from rest_framework import status
from utilities.testing import APITestCase, TestCase

from ..choices import DesignPlacementKindChoices
from ..models import Design, DesignPlacement
from ..projection import ProjectedSlotState, project_rack


def _full_depth_type(manufacturer, model="FD Type", slug="fd-type", u_height=2):
    return DeviceType.objects.create(
        manufacturer=manufacturer,
        model=model,
        slug=slug,
        u_height=u_height,
        is_full_depth=True,
    )


def _half_depth_type(manufacturer, model="HD Type", slug="hd-type", u_height=1):
    return DeviceType.objects.create(
        manufacturer=manufacturer,
        model=model,
        slug=slug,
        u_height=u_height,
        is_full_depth=False,
    )


class FullDepthProjectionTest(TestCase):
    """``project_rack`` must mirror full-depth design slots onto both faces."""

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="Site 1", slug="site-1")
        cls.mf = Manufacturer.objects.create(name="MF 1", slug="mf-1")
        cls.role = DeviceRole.objects.create(name="Role 1", slug="role-1")
        cls.fd_type = _full_depth_type(cls.mf)
        cls.hd_type = _half_depth_type(cls.mf)
        cls.rack = Rack.objects.create(name="Rack 1", site=cls.site)
        cls.design = Design.objects.create(title="FD design", site=cls.site)

    def _fd_device(self, name, position, face="front"):
        return Device.objects.create(
            name=name,
            site=self.site,
            rack=self.rack,
            position=position,
            face=face,
            device_type=self.fd_type,
            role=self.role,
        )

    @staticmethod
    def _states_at(slots, u_position):
        return {
            s["state"] for s in slots
            if s["u_position"] is not None and float(s["u_position"]) == float(u_position)
        }

    @staticmethod
    def _slot_at(slots, u_position, state):
        """The single slot of ``state`` at ``u_position`` (or None)."""
        for s in slots:
            if (
                s["state"] == state
                and s["u_position"] is not None
                and float(s["u_position"]) == float(u_position)
            ):
                return s
        return None

    def _assert_mirrored(self, result, u_position, state):
        """
        Assert ``state`` appears at ``u_position`` on BOTH faces, with the FRONT
        (mounted) copy as the colored primary (opposite_face False) and the REAR
        copy as the passive blocked indicator (opposite_face True).
        """
        front = self._slot_at(result.front, u_position, state)
        rear = self._slot_at(result.rear, u_position, state)
        self.assertIsNotNone(front, f"missing front {state} @U{u_position}")
        self.assertIsNotNone(rear, f"missing rear {state} @U{u_position}")
        self.assertFalse(front["opposite_face"], "mounted (front) copy must be primary")
        self.assertTrue(rear["opposite_face"], "non-mounted (rear) copy must be blocked")
        # Both copies keep the same label so the name renders on the hatch.
        self.assertEqual(front["label"], rear["label"])

    def test_existing_full_depth_shows_on_both_faces(self):
        """An untouched full-depth device: front colored primary, rear blocked."""
        self._fd_device("fd-existing", position=5)
        result = project_rack(self.design, self.rack)
        self._assert_mirrored(result, 5, ProjectedSlotState.EXISTING)

    def test_full_depth_add_on_both_faces(self):
        """A full-depth ADD is projected onto both faces at the target U."""
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.fd_type,
            target_rack=self.rack,
            target_position=10,
            target_face="front",
        )
        result = project_rack(self.design, self.rack)
        self._assert_mirrored(result, 10, ProjectedSlotState.ADD)

    def test_full_depth_move_in_and_ghost_on_both_faces(self):
        """A full-depth MOVE mirrors move_in (target) AND ghost (origin); rear blocked."""
        device = self._fd_device("fd-move", position=5)
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=device,
            target_rack=self.rack,
            target_position=10,
            target_face="front",
        )
        result = project_rack(self.design, self.rack)
        # move_in at the target U (10): front primary, rear blocked.
        self._assert_mirrored(result, 10, ProjectedSlotState.MOVE_IN)
        # move_out ghost at the origin U (5): front primary, rear blocked.
        self._assert_mirrored(result, 5, ProjectedSlotState.MOVE_OUT_GHOST)

    def test_full_depth_remove_flags_both_faces(self):
        """A full-depth REMOVE flags front primary + rear blocked."""
        device = self._fd_device("fd-remove", position=5)
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=device,
        )
        result = project_rack(self.design, self.rack)
        self._assert_mirrored(result, 5, ProjectedSlotState.REMOVE)

    def test_half_depth_add_stays_on_one_face(self):
        """Regression: a HALF-depth add is NOT mirrored (front only)."""
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.hd_type,
            target_rack=self.rack,
            target_position=15,
            target_face="front",
        )
        result = project_rack(self.design, self.rack)
        front = self._slot_at(result.front, 15, ProjectedSlotState.ADD)
        self.assertIsNotNone(front)
        self.assertFalse(front["opposite_face"])  # never a blocked copy
        self.assertIsNone(self._slot_at(result.rear, 15, ProjectedSlotState.ADD))

    def test_position_less_full_depth_add_not_mirrored(self):
        """A target-less full-depth add lands once in non_racked (never face-mirrored)."""
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.fd_type,
            target_rack=self.rack,
            target_position=None,
            target_face="front",
        )
        result = project_rack(self.design, self.rack)
        add_non_racked = [s for s in result.non_racked if s["state"] == ProjectedSlotState.ADD]
        self.assertEqual(len(add_non_racked), 1)
        self.assertFalse(add_non_racked[0]["opposite_face"])

    def test_widget_payload_carries_opposite_face_flag(self):
        """_slot_to_widget exposes opposite_face so the editor JS can stay passive."""
        from ..views import _slot_to_widget

        self._fd_device("fd-widget", position=5)
        result = project_rack(self.design, self.rack)
        front = self._slot_at(result.front, 5, ProjectedSlotState.EXISTING)
        rear = self._slot_at(result.rear, 5, ProjectedSlotState.EXISTING)
        self.assertFalse(_slot_to_widget(front)["opposite_face"])
        self.assertTrue(_slot_to_widget(rear)["opposite_face"])


class FullDepthSaveLayoutTest(APITestCase):
    """The save-layout reconcile must collapse two per-face copies into ONE placement."""

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="Site 1", slug="site-1")
        cls.mf = Manufacturer.objects.create(name="MF 1", slug="mf-1")
        cls.role = DeviceRole.objects.create(name="Role 1", slug="role-1")
        cls.fd_type = _full_depth_type(cls.mf)
        cls.rack = Rack.objects.create(name="Rack 1", site=cls.site)
        cls.design = Design.objects.create(title="FD layout", site=cls.site)
        cls.device = Device.objects.create(
            name="fd-1",
            site=cls.site,
            rack=cls.rack,
            position=5,
            face="front",
            device_type=cls.fd_type,
            role=cls.role,
        )

    def _url(self):
        from django.urls import reverse

        return reverse(
            "plugins-api:netbox_rack_design-api:design-save-layout",
            kwargs={"pk": self.design.pk},
        )

    def _grant_all(self):
        self.add_permissions(
            "netbox_rack_design.change_design",
            "netbox_rack_design.add_designplacement",
            "netbox_rack_design.change_designplacement",
            "netbox_rack_design.delete_designplacement",
        )

    def _post(self, racks):
        return self.client.post(
            self._url(),
            {"design_id": self.design.pk, "racks": racks},
            format="json",
            **self.header,
        )

    def test_full_depth_noop_on_both_faces_returns_304(self):
        """Submitting the full-depth device as existing on BOTH faces is a no-op."""
        self._grant_all()
        response = self._post([
            {
                "rack_id": self.rack.pk,
                "front": [{
                    "kind": "existing", "device_id": self.device.pk,
                    "device_type_id": self.fd_type.pk, "u_position": 5, "face": "front",
                }],
                "rear": [{
                    "kind": "existing", "device_id": self.device.pk,
                    "device_type_id": self.fd_type.pk, "u_position": 5, "face": "rear",
                }],
            },
        ])
        self.assertHttpStatus(response, status.HTTP_304_NOT_MODIFIED)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    def test_full_depth_move_on_both_faces_yields_single_placement(self):
        """Moving a full-depth device (submitted on both faces) → exactly ONE placement."""
        self._grant_all()
        response = self._post([
            {
                "rack_id": self.rack.pk,
                "front": [{
                    "kind": "move", "device_id": self.device.pk,
                    "device_type_id": self.fd_type.pk, "u_position": 10, "face": "front",
                }],
                "rear": [{
                    "kind": "move", "device_id": self.device.pk,
                    "device_type_id": self.fd_type.pk, "u_position": 10, "face": "rear",
                }],
            },
        ])
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placements = DesignPlacement.objects.filter(design=self.design)
        self.assertEqual(placements.count(), 1)
        placement = placements.first()
        self.assertEqual(placement.kind, DesignPlacementKindChoices.KIND_MOVE)
        self.assertEqual(placement.device_id, self.device.pk)
        self.assertEqual(float(placement.target_position), 10.0)
        # Full-depth placement face is normalised to "" (face is meaningless).
        self.assertEqual(placement.target_face, "")
        # Re-POSTing the same move is idempotent (single placement, 304).
        again = self._post([
            {
                "rack_id": self.rack.pk,
                "front": [{
                    "kind": "move", "device_id": self.device.pk,
                    "device_type_id": self.fd_type.pk, "u_position": 10, "face": "front",
                }],
                "rear": [{
                    "kind": "move", "device_id": self.device.pk,
                    "device_type_id": self.fd_type.pk, "u_position": 10, "face": "rear",
                }],
            },
        ])
        self.assertHttpStatus(again, status.HTTP_304_NOT_MODIFIED)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 1)

    def test_full_depth_move_onto_occupied_opposite_face_returns_400(self):
        """A full-depth device needs BOTH faces free. Moving it to a U whose
        opposite face is occupied by another (un-moved) device must be rejected —
        the same conflict the editor now surfaces live on the tile before save."""
        self._grant_all()
        # A half-depth device sits on the REAR at U10; that U's rear is occupied.
        hd_type = _half_depth_type(self.mf)
        Device.objects.create(
            name="rear-blocker", site=self.site, rack=self.rack,
            position=10, face="rear", device_type=hd_type, role=self.role,
        )
        response = self._post([
            {
                "rack_id": self.rack.pk,
                "front": [{
                    "kind": "move", "device_id": self.device.pk,
                    "device_type_id": self.fd_type.pk, "u_position": 10, "face": "front",
                }],
                "rear": [{
                    "kind": "move", "device_id": self.device.pk,
                    "device_type_id": self.fd_type.pk, "u_position": 10, "face": "rear",
                }],
            },
        ])
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("errors", response.data)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    def test_full_depth_remove_on_both_faces_yields_single_placement(self):
        """Removing a full-depth device (submitted on both faces) → exactly ONE placement."""
        self._grant_all()
        response = self._post([
            {
                "rack_id": self.rack.pk,
                "front": [{
                    "kind": "remove", "device_id": self.device.pk,
                    "device_type_id": self.fd_type.pk,
                }],
                "rear": [{
                    "kind": "remove", "device_id": self.device.pk,
                    "device_type_id": self.fd_type.pk,
                }],
            },
        ])
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placements = DesignPlacement.objects.filter(design=self.design)
        self.assertEqual(placements.count(), 1)
        self.assertEqual(placements.first().kind, DesignPlacementKindChoices.KIND_REMOVE)
        # Real device untouched.
        self.device.refresh_from_db()
        self.assertEqual(self.device.rack_id, self.rack.pk)
