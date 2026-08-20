"""REST API tests for NetBox Rack Design (subclassing NetBox's standard suite)."""

from dcim.choices import PowerFeedPhaseChoices
from dcim.models import (
    Cable,
    Device,
    DeviceRole,
    DeviceType,
    Manufacturer,
    PowerFeed,
    PowerOutlet,
    PowerPanel,
    PowerPort,
    Rack,
    Site,
)
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from users.models import Token, User
from utilities.testing import (
    APITestCase,
    APIViewTestCases,
    create_tags,
    create_test_device,
)

from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import (
    Design,
    DesignGroup,
    DesignPlacement,
    DesignPowerFeed,
    DesignRackPower,
    FavoriteDeviceType,
    HiddenDesignRack,
)
from .utils import api_token_header, create_dcim_environment


class DesignGroupTest(APIViewTestCases.APIViewTestCase):
    model = DesignGroup
    view_namespace = "plugins-api:netbox_rack_design"
    brief_fields = ["display", "id", "name", "url"]
    bulk_update_data = {
        "description": "New description",
    }

    @classmethod
    def setUpTestData(cls):
        parent = DesignGroup.objects.create(name="Parent")
        DesignGroup.objects.create(name="Group 1", parent=parent)
        DesignGroup.objects.create(name="Group 2")
        DesignGroup.objects.create(name="Group 3")

        tags = create_tags("Alpha", "Bravo", "Charlie")

        cls.create_data = [
            {"name": "Group 4", "parent": parent.pk, "tags": [t.pk for t in tags]},
            {"name": "Group 5", "description": "Fifth"},
            {"name": "Group 6"},
        ]


class DesignTest(APIViewTestCases.APIViewTestCase):
    model = Design
    view_namespace = "plugins-api:netbox_rack_design"
    brief_fields = ["display", "id", "status", "title", "url", "version"]

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        site = env["site"]
        cls.site = site
        cls.racks = env["racks"]

        Design.objects.create(title="Design 1", site=site)
        Design.objects.create(title="Design 2", site=site)
        Design.objects.create(title="Design 3", site=site)

        tags = create_tags("Alpha", "Bravo", "Charlie")

        # bulk_update_data must differ from setUpTestData; assigning the M2M by
        # id exercises rack write on PATCH for all three existing objects.
        cls.bulk_update_data = {
            "summary": "Bulk-updated summary",
            "status": DesignStatusChoices.STATUS_REJECTED,
            "racks": [cls.racks[0].pk],
        }

        cls.create_data = [
            {
                "title": "Design 4",
                "site": site.pk,
                "status": DesignStatusChoices.STATUS_DRAFT,
                "racks": [r.pk for r in cls.racks],
                "tags": [t.pk for t in tags],
            },
            {
                "title": "Design 5",
                "site": site.pk,
                "status": DesignStatusChoices.STATUS_DRAFT,
                "racks": [cls.racks[0].pk],
            },
            {
                "title": "Design 6",
                "site": site.pk,
                "status": DesignStatusChoices.STATUS_DRAFT,
            },
        ]

    def test_get_design_returns_racks(self):
        """A serialized Design exposes its scoped racks as brief Rack reprs."""
        self.add_permissions("netbox_rack_design.view_design")
        design = Design.objects.create(title="Scoped", site=self.site)
        design.racks.add(*self.racks)

        url = reverse("plugins-api:netbox_rack_design-api:design-detail", args=[design.pk])
        response = self.client.get(url, **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        returned = {r["id"] for r in response.data["racks"]}
        self.assertEqual(returned, {r.pk for r in self.racks})

    def test_set_racks_by_id_on_create(self):
        """POST can assign racks by id, writing only the Design M2M through-rows."""
        self.add_permissions(
            "netbox_rack_design.add_design", "netbox_rack_design.view_design"
        )
        data = {
            "title": "Created with racks",
            "site": self.site.pk,
            "status": DesignStatusChoices.STATUS_DRAFT,
            "racks": [r.pk for r in self.racks],
        }
        url = reverse("plugins-api:netbox_rack_design-api:design-list")
        response = self.client.post(url, data, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        design = Design.objects.get(pk=response.data["id"])
        self.assertEqual(set(design.racks.all()), set(self.racks))


class DesignPlacementTest(APIViewTestCases.APIViewTestCase):
    model = DesignPlacement
    view_namespace = "plugins-api:netbox_rack_design"
    brief_fields = ["display", "id", "kind", "url"]
    bulk_update_data = {
        "proposed_name": "renamed-node",
    }

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        site = env["site"]
        device_type = env["device_type"]
        rack = env["racks"][1]  # empty rack, free U slots

        design = Design.objects.create(title="Design 1", site=site)

        DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=device_type,
            target_rack=rack,
            target_position=1,
        )
        DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=device_type,
            target_rack=rack,
            target_position=2,
        )
        DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=device_type,
            target_rack=rack,
            target_position=3,
        )

        tags = create_tags("Alpha", "Bravo", "Charlie")

        cls.create_data = [
            {
                "design": design.pk,
                "kind": DesignPlacementKindChoices.KIND_ADD,
                "device_type": device_type.pk,
                "target_rack": rack.pk,
                "target_position": 10.0,
                "tags": [t.pk for t in tags],
            },
            {
                "design": design.pk,
                "kind": DesignPlacementKindChoices.KIND_ADD,
                "device_type": device_type.pk,
                "target_rack": rack.pk,
                "target_position": 11.0,
            },
            {
                "design": design.pk,
                "kind": DesignPlacementKindChoices.KIND_ADD,
                "device_type": device_type.pk,
                "target_rack": rack.pk,
                "target_position": 12.0,
            },
        ]

    def test_proposed_name_round_trips(self):
        """proposed_name is writable on create and returned on read."""
        self.add_permissions(
            "netbox_rack_design.add_designplacement",
            "netbox_rack_design.view_designplacement",
        )
        data = dict(self.create_data[0])
        data["proposed_name"] = "preview-rt-node"
        url = reverse("plugins-api:netbox_rack_design-api:designplacement-list")
        response = self.client.post(url, data, format="json", **self.header)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["proposed_name"], "preview-rt-node")
        placement = DesignPlacement.objects.get(pk=response.data["id"])
        self.assertEqual(placement.proposed_name, "preview-rt-node")


class SaveLayoutTest(APITestCase):
    """Tests for the DesignViewSet save-layout action (Stage 2, increment 2a)."""

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]  # Device 1 @ Rack1/U1/front, Device 2 @ Rack1/U2/front
        cls.device_type = env["device_type"]
        cls.device_role = env["device_role"]
        cls.tenant = env["tenant"]
        cls.design = Design.objects.create(title="Layout design", site=cls.site)

    def _url(self, design):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-save-layout",
            kwargs={"pk": design.pk},
        )

    def _grant_all(self):
        self.add_permissions(
            "netbox_rack_design.change_design",
            "netbox_rack_design.add_designplacement",
            "netbox_rack_design.change_designplacement",
            "netbox_rack_design.delete_designplacement",
        )

    def _payload(self, racks):
        return {"design_id": self.design.pk, "racks": racks}

    def test_move_persists_one_placement_and_leaves_device(self):
        """Moving an existing device persists ONE move placement; real Device unchanged."""
        self._grant_all()
        device = self.devices[0]
        rack = self.racks[0]
        # Move Device 1 from U1 to U10 (free) on the front face.
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "move", "device_id": device.pk, "u_position": 10, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)

        placements = DesignPlacement.objects.filter(design=self.design)
        self.assertEqual(placements.count(), 1)
        placement = placements.first()
        self.assertEqual(placement.kind, DesignPlacementKindChoices.KIND_MOVE)
        self.assertEqual(placement.device_id, device.pk)
        self.assertEqual(float(placement.target_position), 10.0)
        self.assertEqual(placement.target_rack_id, rack.pk)

        # Real device is untouched.
        device.refresh_from_db()
        self.assertEqual(float(device.position), 1.0)
        self.assertEqual(device.rack_id, rack.pk)

    def test_collision_returns_400_and_persists_nothing(self):
        """Moving a device onto an occupied unit → 400, no placements persisted."""
        self._grant_all()
        device = self.devices[0]  # at U1
        rack = self.racks[0]
        # U2 is occupied by Device 2 → collision.
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "move", "device_id": device.pk, "u_position": 2, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("errors", response.data)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    def test_swap_two_devices_succeeds(self):
        """Two devices swapping slots in one submit is valid: each vacates the
        slot the other moves into, so the projected layout has no collision.

        Regression: collision was validated against the PHYSICAL rack (excluding
        only the device being moved), so a swap 400'd because each target still
        looked occupied by the other real device.
        """
        self._grant_all()
        rack = self.racks[0]
        d1, d2 = self.devices[0], self.devices[1]  # U1, U2 (both front, half-depth)
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "move", "device_id": d1.pk, "u_position": 2, "face": "front"},
                    {"kind": "move", "device_id": d2.pk, "u_position": 1, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placements = {p.device_id: p for p in DesignPlacement.objects.filter(design=self.design)}
        self.assertEqual(len(placements), 2)
        self.assertEqual(float(placements[d1.pk].target_position), 2.0)
        self.assertEqual(float(placements[d2.pk].target_position), 1.0)
        # Real devices are never mutated.
        d1.refresh_from_db()
        d2.refresh_from_db()
        self.assertEqual((float(d1.position), float(d2.position)), (1.0, 2.0))

    def test_move_into_slot_vacated_by_another_move_succeeds(self):
        """Moving a device into a U that another moved-away device vacated is
        valid (the vacating move need not be a mutual swap)."""
        self._grant_all()
        rack = self.racks[0]
        d1, d2 = self.devices[0], self.devices[1]  # U1, U2
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    # d2 leaves U2 for a free U; d1 moves into the vacated U2.
                    {"kind": "move", "device_id": d2.pk, "u_position": 5, "face": "front"},
                    {"kind": "move", "device_id": d1.pk, "u_position": 2, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placements = {p.device_id: float(p.target_position)
                      for p in DesignPlacement.objects.filter(design=self.design)}
        self.assertEqual(placements, {d1.pk: 2.0, d2.pk: 5.0})

    def test_move_into_slot_vacated_by_remove_succeeds(self):
        """Removing a device frees its slot for another device to move in."""
        self._grant_all()
        rack = self.racks[0]
        d1, d2 = self.devices[0], self.devices[1]  # U1, U2
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "remove", "device_id": d2.pk},
                    {"kind": "move", "device_id": d1.pk, "u_position": 2, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        by_kind = {p.kind: p for p in DesignPlacement.objects.filter(design=self.design)}
        self.assertEqual(float(by_kind[DesignPlacementKindChoices.KIND_MOVE].target_position), 2.0)
        self.assertEqual(by_kind[DesignPlacementKindChoices.KIND_REMOVE].device_id, d2.pk)

    # --- 0.9.0: non-racked tray save contract (spec §9.5) ------------------

    def test_dismount_to_tray_persists_move_with_no_position(self):
        """U -> tray (dismount): a 'move' item in the 'other' bucket persists a
        move placement with target_rack set and target_position=None."""
        self._grant_all()
        rack = self.racks[0]
        device = self.devices[0]  # real: Rack1/U1/front
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "other": [
                    {"kind": "move", "device_id": device.pk},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placements = DesignPlacement.objects.filter(design=self.design)
        self.assertEqual(placements.count(), 1)
        placement = placements.first()
        self.assertEqual(placement.kind, DesignPlacementKindChoices.KIND_MOVE)
        self.assertEqual(placement.device_id, device.pk)
        self.assertEqual(placement.target_rack_id, rack.pk)
        self.assertIsNone(placement.target_position)
        self.assertEqual(placement.target_face, "")
        # Real device is never mutated.
        device.refresh_from_db()
        self.assertEqual(float(device.position), 1.0)

    def test_mount_from_tray_persists_move_with_position(self):
        """Tray -> U (mount): a real position-less device moved onto a U
        persists a move placement with target_position set."""
        self._grant_all()
        rack = self.racks[0]
        pdu = create_test_device(
            "PDU-Mount", site=self.site, rack=rack, position=None, face="",
        )
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "move", "device_id": pdu.pk, "u_position": 10, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placements = DesignPlacement.objects.filter(design=self.design)
        self.assertEqual(placements.count(), 1)
        placement = placements.first()
        self.assertEqual(placement.kind, DesignPlacementKindChoices.KIND_MOVE)
        self.assertEqual(placement.device_id, pdu.pk)
        self.assertEqual(float(placement.target_position), 10.0)
        self.assertEqual(placement.target_face, "front")
        pdu.refresh_from_db()
        self.assertIsNone(pdu.position)

    def test_tray_to_tray_reassociation_persists_new_rack_no_position(self):
        """Tray -> tray (cross-rack reassociation): a real position-less device
        moved to another rack's tray persists a move placement with the new
        rack and no position."""
        self._grant_all()
        origin_rack, other_rack = self.racks[0], self.racks[1]
        pdu = create_test_device(
            "PDU-Reassoc", site=self.site, rack=origin_rack, position=None, face="",
        )
        payload = self._payload([
            {
                "rack_id": other_rack.pk,
                "other": [
                    {"kind": "move", "device_id": pdu.pk},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placements = DesignPlacement.objects.filter(design=self.design)
        self.assertEqual(placements.count(), 1)
        placement = placements.first()
        self.assertEqual(placement.kind, DesignPlacementKindChoices.KIND_MOVE)
        self.assertEqual(placement.device_id, pdu.pk)
        self.assertEqual(placement.target_rack_id, other_rack.pk)
        self.assertIsNone(placement.target_position)
        pdu.refresh_from_db()
        self.assertEqual(pdu.rack_id, origin_rack.pk)  # real device untouched

    def test_tray_device_resubmitted_as_existing_is_idempotent_noop(self):
        """A real position-less device resubmitted unchanged in the 'other'
        bucket as 'existing' is a no-op (304), regardless of its real face --
        a tray target carries no face (spec §9.5)."""
        self._grant_all()
        rack = self.racks[0]
        pdu = create_test_device(
            "PDU-Noop", site=self.site, rack=rack, position=None, face="rear",
        )
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "other": [
                    {"kind": "existing", "device_id": pdu.pk},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_304_NOT_MODIFIED)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    def test_palette_add_into_tray_persists_placement_with_no_position(self):
        """Palette -> tray (spec §9.3): a brand-new catalog add with no
        u_position persists an add placement with target_position=None."""
        self._grant_all()
        rack = self.racks[0]
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "other": [
                    {"kind": "add", "device_type_id": self.device_type.pk},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placements = DesignPlacement.objects.filter(design=self.design)
        self.assertEqual(placements.count(), 1)
        placement = placements.first()
        self.assertEqual(placement.kind, DesignPlacementKindChoices.KIND_ADD)
        self.assertIsNone(placement.device_id)
        self.assertEqual(placement.target_rack_id, rack.pk)
        self.assertIsNone(placement.target_position)

    def test_move_onto_unmoved_device_still_returns_400(self):
        """The projected-layout relaxation must NOT let a device move onto a slot
        held by a device that stays put — that is still a real collision."""
        self._grant_all()
        rack = self.racks[0]
        d1, d2 = self.devices[0], self.devices[1]  # U1, U2
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "move", "device_id": d1.pk, "u_position": 2, "face": "front"},
                    # d2 stays at its real U2 (submitted as existing, not moved).
                    {"kind": "existing", "device_id": d2.pk, "u_position": 2, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    def test_noop_payload_returns_304(self):
        """Everything submitted as existing at real positions → 304, no changes."""
        self._grant_all()
        rack = self.racks[0]
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "existing", "device_id": self.devices[0].pk, "u_position": 1, "face": "front"},
                    {"kind": "existing", "device_id": self.devices[1].pk, "u_position": 2, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_304_NOT_MODIFIED)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    def test_remove_persists_remove_placement(self):
        """A remove item persists a remove placement."""
        self._grant_all()
        device = self.devices[0]
        rack = self.racks[0]
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "remove", "device_id": device.pk},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placements = DesignPlacement.objects.filter(design=self.design)
        self.assertEqual(placements.count(), 1)
        placement = placements.first()
        self.assertEqual(placement.kind, DesignPlacementKindChoices.KIND_REMOVE)
        self.assertEqual(placement.device_id, device.pk)
        self.assertIsNone(placement.target_rack_id)
        # Real device untouched.
        device.refresh_from_db()
        self.assertEqual(device.rack_id, rack.pk)

    def test_missing_change_perm_returns_403(self):
        """A user lacking change permission → 403."""
        # No permissions granted at all.
        rack = self.racks[0]
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "move", "device_id": self.devices[0].pk, "u_position": 10, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    # --- increment 2b-1: brand-new catalog adds ----------------------------

    def test_brand_new_add_creates_one_placement_no_device(self):
        """A brand-new catalog add → 200, ONE KIND_ADD placement; no Device created."""
        self._grant_all()
        rack = self.racks[0]
        device_count_before = Device.objects.count()
        # U10 is free on the front face.
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 10, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)

        placements = DesignPlacement.objects.filter(design=self.design)
        self.assertEqual(placements.count(), 1)
        placement = placements.first()
        self.assertEqual(placement.kind, DesignPlacementKindChoices.KIND_ADD)
        self.assertEqual(placement.device_type_id, self.device_type.pk)
        self.assertEqual(placement.target_rack_id, rack.pk)
        self.assertEqual(float(placement.target_position), 10.0)
        self.assertEqual(placement.target_face, "front")
        self.assertIsNone(placement.device_id)

        # No real dcim.Device was created.
        self.assertEqual(Device.objects.count(), device_count_before)

    def test_brand_new_add_on_occupied_unit_returns_400(self):
        """A brand-new add onto an occupied U → 400 with an error; nothing persisted."""
        self._grant_all()
        rack = self.racks[0]
        # U2 is occupied by Device 2 → collision.
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 2, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("errors", response.data)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    def test_brand_new_add_coexists_with_reposition_and_move(self):
        """A brand-new add, an existing-add reposition, and a move coexist (no cross-deletion)."""
        self._grant_all()
        rack = self.racks[0]
        # An existing add placement to be repositioned (currently U5/front).
        existing_add = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=rack,
            target_position=5,
            target_face="front",
        )
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    # brand-new add at U10
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 10, "face": "front"},
                    # reposition the existing add from U5 -> U11
                    {"kind": "add", "placement_id": existing_add.pk,
                     "u_position": 11, "face": "front"},
                    # move a real device from U1 -> U12
                    {"kind": "move", "device_id": self.devices[0].pk,
                     "u_position": 12, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)

        placements = DesignPlacement.objects.filter(design=self.design)
        # The reposition reused existing_add (no extra row); a new add + a move.
        self.assertEqual(placements.count(), 3)

        # Existing add survived and was repositioned to U11.
        existing_add.refresh_from_db()
        self.assertEqual(float(existing_add.target_position), 11.0)

        adds = placements.filter(kind=DesignPlacementKindChoices.KIND_ADD)
        self.assertEqual(adds.count(), 2)
        new_add = adds.exclude(pk=existing_add.pk).first()
        self.assertEqual(float(new_add.target_position), 10.0)

        move = placements.filter(kind=DesignPlacementKindChoices.KIND_MOVE).first()
        self.assertIsNotNone(move)
        self.assertEqual(move.device_id, self.devices[0].pk)
        self.assertEqual(float(move.target_position), 12.0)

    # --- increment 2b-3b: add carries a device role + tenant ----------------

    def test_brand_new_add_persists_role_and_tenant(self):
        """A brand-new add with device_role_id + tenant_id persists them."""
        self._grant_all()
        rack = self.racks[0]
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "device_role_id": self.device_role.pk,
                     "tenant_id": self.tenant.pk,
                     "u_position": 10, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)

        placement = DesignPlacement.objects.get(design=self.design)
        self.assertEqual(placement.kind, DesignPlacementKindChoices.KIND_ADD)
        self.assertEqual(placement.device_role_id, self.device_role.pk)
        self.assertEqual(placement.tenant_id, self.tenant.pk)

    def test_brand_new_add_without_role_or_tenant_persists_nulls(self):
        """A brand-new add omitting role/tenant persists them as NULL."""
        self._grant_all()
        rack = self.racks[0]
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 10, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)

        placement = DesignPlacement.objects.get(design=self.design)
        self.assertIsNone(placement.device_role_id)
        self.assertIsNone(placement.tenant_id)

    def test_brand_new_add_with_bad_role_returns_400(self):
        """A brand-new add with a non-existent device_role_id → 400, nothing persisted."""
        self._grant_all()
        rack = self.racks[0]
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "device_role_id": 999999,
                     "u_position": 10, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("errors", response.data)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    def test_brand_new_add_with_bad_tenant_returns_400(self):
        """A brand-new add with a non-existent tenant_id → 400, nothing persisted."""
        self._grant_all()
        rack = self.racks[0]
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "tenant_id": 999999,
                     "u_position": 10, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("errors", response.data)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    def test_reposition_existing_add_sets_role_and_tenant(self):
        """Repositioning an add can also set role/tenant when sent."""
        self._grant_all()
        rack = self.racks[0]
        existing_add = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=rack,
            target_position=5,
            target_face="front",
        )
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "placement_id": existing_add.pk,
                     "device_role_id": self.device_role.pk,
                     "tenant_id": self.tenant.pk,
                     "u_position": 9, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        existing_add.refresh_from_db()
        self.assertEqual(float(existing_add.target_position), 9.0)
        self.assertEqual(existing_add.device_role_id, self.device_role.pk)
        self.assertEqual(existing_add.tenant_id, self.tenant.pk)

    # --- regression: existing-add reposition / cancel (2a) ------------------

    def test_reposition_existing_add_updates_in_place(self):
        """An add item with placement_id repositions the existing add (no new row)."""
        self._grant_all()
        rack = self.racks[0]
        existing_add = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=rack,
            target_position=5,
            target_face="front",
        )
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "placement_id": existing_add.pk,
                     "u_position": 9, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 1)
        existing_add.refresh_from_db()
        self.assertEqual(float(existing_add.target_position), 9.0)

    def test_cancel_existing_add_deletes_it(self):
        """An add item with cancel=true deletes the existing add placement."""
        self._grant_all()
        rack = self.racks[0]
        existing_add = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=rack,
            target_position=5,
            target_face="front",
        )
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "placement_id": existing_add.pk,
                     "u_position": 5, "face": "front", "cancel": True},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertFalse(
            DesignPlacement.objects.filter(pk=existing_add.pk).exists()
        )

    def test_unmentioned_add_is_not_deleted(self):
        """No-data-loss: an existing add not mentioned in the payload survives."""
        self._grant_all()
        rack = self.racks[0]
        existing_add = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=rack,
            target_position=5,
            target_face="front",
        )
        # Submit only a move of a real device; the add is never mentioned.
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "move", "device_id": self.devices[0].pk,
                     "u_position": 10, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        # The unmentioned add must NOT be deleted.
        self.assertTrue(
            DesignPlacement.objects.filter(pk=existing_add.pk).exists()
        )

    # --- Phase B: PDU power_config rides the item payload -------------------

    def test_brand_new_add_persists_power_config(self):
        """A brand-new add carrying power_config persists it onto the placement
        (the frontend only sends it for a PDU add, but the reconcile does not
        gate on role -- it just stores whatever the item carries)."""
        self._grant_all()
        rack = self.racks[0]
        power_config = {
            "source": "manual",
            "custom_fields": {"pdu_scheme": "2x1PH2Banks"},
            "feed": {"voltage": 230, "amperage": 32, "phase": 1, "supply": "ac"},
        }
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 10, "face": "front",
                     "power_config": power_config},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placement = DesignPlacement.objects.get(design=self.design)
        self.assertEqual(placement.power_config, power_config)

    def test_reposition_existing_add_sets_power_config(self):
        """Repositioning an existing add can also set power_config when sent."""
        self._grant_all()
        rack = self.racks[0]
        existing_add = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=rack,
            target_position=5,
            target_face="front",
        )
        power_config = {"source": "manual", "custom_fields": {}, "feed": {
            "voltage": 230, "amperage": 16, "phase": 1, "supply": "ac",
        }}
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "placement_id": existing_add.pk,
                     "u_position": 9, "face": "front",
                     "power_config": power_config},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        existing_add.refresh_from_db()
        self.assertEqual(existing_add.power_config, power_config)

    def test_add_without_power_config_leaves_it_null(self):
        """An 'add' item that omits power_config persists it as NULL (default)."""
        self._grant_all()
        rack = self.racks[0]
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 10, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placement = DesignPlacement.objects.get(design=self.design)
        self.assertIsNone(placement.power_config)

    # --- slice 2d: multi-rack save round-trip (the conservative-guard contract)

    def _multi_rack_payload(self, rack_a, rack_b, add_position=5):
        """A two-rack payload: edit rack A (move + remove) AND add into rack B."""
        return self._payload([
            {
                "rack_id": rack_a.pk,
                "front": [
                    # Move Device 1 (rack A / U1) to a free unit in rack A.
                    {"kind": "move", "device_id": self.devices[0].pk,
                     "u_position": 10, "face": "front"},
                    # Flag Device 2 (rack A / U2) for removal.
                    {"kind": "remove", "device_id": self.devices[1].pk},
                ],
            },
            {
                "rack_id": rack_b.pk,
                "front": [
                    # Brand-new catalog add into the (empty) rack B.
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": add_position, "face": "front"},
                ],
            },
        ])

    def test_multi_rack_save_reconciles_both_racks_in_one_call(self):
        """One save-layout POST spanning TWO racks reconciles both at once."""
        self._grant_all()
        rack_a = self.racks[0]
        rack_b = self.racks[1]
        response = self.client.post(
            self._url(self.design),
            self._multi_rack_payload(rack_a, rack_b),
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)

        placements = DesignPlacement.objects.filter(design=self.design)
        self.assertEqual(placements.count(), 3)

        # Rack A: the move landed in rack A at U10.
        move = placements.get(kind=DesignPlacementKindChoices.KIND_MOVE)
        self.assertEqual(move.device_id, self.devices[0].pk)
        self.assertEqual(move.target_rack_id, rack_a.pk)
        self.assertEqual(float(move.target_position), 10.0)
        # Rack A: the remove flags Device 2 (no destination rack).
        remove = placements.get(kind=DesignPlacementKindChoices.KIND_REMOVE)
        self.assertEqual(remove.device_id, self.devices[1].pk)
        self.assertIsNone(remove.target_rack_id)
        # Rack B: the add targets rack B (no real device created).
        add = placements.get(kind=DesignPlacementKindChoices.KIND_ADD)
        self.assertEqual(add.target_rack_id, rack_b.pk)
        self.assertEqual(float(add.target_position), 5.0)
        self.assertIsNone(add.device_id)

        # Real devices are never mutated.
        self.devices[0].refresh_from_db()
        self.assertEqual(self.devices[0].rack_id, rack_a.pk)
        self.assertEqual(float(self.devices[0].position), 1.0)

    def test_multi_rack_save_is_idempotent_on_resubmit(self):
        """Re-POSTing the same two-rack layout makes no duplicate / spurious change."""
        self._grant_all()
        rack_a = self.racks[0]
        rack_b = self.racks[1]

        first = self.client.post(
            self._url(self.design),
            self._multi_rack_payload(rack_a, rack_b),
            format="json",
            **self.header,
        )
        self.assertHttpStatus(first, status.HTTP_200_OK)
        after_first = set(
            DesignPlacement.objects.filter(design=self.design).values_list("pk", flat=True)
        )
        self.assertEqual(len(after_first), 3)
        add = DesignPlacement.objects.get(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD
        )

        # Reload-style resubmit: the editor now knows the add's placement_id, and
        # the move/remove re-assert the same intent. Nothing actually changed, so
        # the reconcile must report 304 and leave the exact same rows in place.
        resubmit = self._payload([
            {
                "rack_id": rack_a.pk,
                "front": [
                    {"kind": "move", "device_id": self.devices[0].pk,
                     "u_position": 10, "face": "front"},
                    {"kind": "remove", "device_id": self.devices[1].pk},
                ],
            },
            {
                "rack_id": rack_b.pk,
                "front": [
                    {"kind": "add", "placement_id": add.pk,
                     "u_position": 5, "face": "front"},
                ],
            },
        ])
        second = self.client.post(
            self._url(self.design), resubmit, format="json", **self.header
        )
        self.assertHttpStatus(second, status.HTTP_304_NOT_MODIFIED)
        after_second = set(
            DesignPlacement.objects.filter(design=self.design).values_list("pk", flat=True)
        )
        # No duplicates created; the identical row set survives.
        self.assertEqual(after_first, after_second)

    def test_saving_rack_a_only_does_not_disturb_rack_b(self):
        """A save scoped to rack A leaves rack B's existing placements untouched."""
        self._grant_all()
        rack_a = self.racks[0]
        rack_b = self.racks[1]

        # A real device living in rack B, with a move placement keeping it in
        # rack B (a move/remove row is exactly the data-loss-prone kind the
        # conservative guard protects).
        device_b = create_test_device("Device B", site=self.site, rack=rack_b, position=3, face="front")
        move_b = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=device_b,
            target_rack=rack_b,
            target_position=20,
            target_face="front",
        )
        before = (move_b.target_rack_id, float(move_b.target_position), move_b.target_face)

        # Submit ONLY rack A (move Device 1). Rack B is never mentioned.
        payload = self._payload([
            {
                "rack_id": rack_a.pk,
                "front": [
                    {"kind": "move", "device_id": self.devices[0].pk,
                     "u_position": 10, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)

        # Rack A reconciled.
        self.assertTrue(
            DesignPlacement.objects.filter(
                design=self.design,
                kind=DesignPlacementKindChoices.KIND_MOVE,
                device_id=self.devices[0].pk,
                target_rack_id=rack_a.pk,
            ).exists()
        )
        # Rack B's placement is completely untouched (not deleted, not modified).
        move_b.refresh_from_db()
        self.assertEqual(
            (move_b.target_rack_id, float(move_b.target_position), move_b.target_face),
            before,
        )


def _plugins_config(**overrides):
    """Build a PLUGINS_CONFIG dict for the plugin with the given naming overrides."""
    cfg = {
        "naming_mode": "sequence",
        "naming_template": "{design.name}-{n}",
        "naming_script": "",
    }
    cfg.update(overrides)
    return {"netbox_rack_design": cfg}


@override_settings(PLUGINS_CONFIG=_plugins_config(distribution_mode="builtin"))
class RecomputeDistributionTest(APITestCase):
    """
    Tests for the DesignViewSet recompute-distribution action.

    The endpoint re-runs the server distribution engine over an UNSAVED editor
    layout so the editor's per-bank chips can refresh LIVE (like the always-live
    power bar) instead of only on Save. It is read-only: it applies the posted
    layout through the save-layout reconciliation inside a rolled-back
    transaction, so the engine sees the live edit but NOTHING is persisted
    (docs/pdu-distribution-spec.md).
    """

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="Dist Site", slug="dist-site")
        cls.mfr = Manufacturer.objects.create(name="Mfr D", slug="mfr-d")
        cls.rack = Rack.objects.create(name="Rack D", site=cls.site, u_height=10)

        panel = PowerPanel.objects.create(site=cls.site, name="Panel D")
        # 230 V x 32 A single-phase -> 7360 W feed; split across 2 banks -> 3680 W.
        cls.feed = PowerFeed.objects.create(
            power_panel=panel, name="Feed D", voltage=230, amperage=32,
            phase=PowerFeedPhaseChoices.PHASE_SINGLE,
        )

        # A real PDU cabled to the feed, with outlets in bank 1 (1/1) and bank 2
        # (2/1, 2/2). Bank id = the first segment of the outlet port name.
        pdu_role = DeviceRole.objects.create(name="PDU", slug="pdu")
        pdu_type = DeviceType.objects.create(
            manufacturer=cls.mfr, model="PDU D", slug="pdu-d", u_height=0,
        )
        cls.pdu = Device.objects.create(
            name="pdu-d-1", device_type=pdu_type, site=cls.site, rack=cls.rack,
            role=pdu_role, status="active",
        )
        outlets = {
            nm: PowerOutlet.objects.create(device=cls.pdu, name=nm)
            for nm in ("1/1", "2/1", "2/2")
        }
        pdu_input = PowerPort.objects.create(device=cls.pdu, name="Input")
        Cable(a_terminations=[pdu_input], b_terminations=[cls.feed]).save()

        # Two single-PSU 1000 W consumers, BOTH cabled to bank 2 (outlets 2/1, 2/2).
        cons_type = DeviceType.objects.create(
            manufacturer=cls.mfr, model="Srv D", slug="srv-d", u_height=1,
            is_full_depth=False,
        )
        cons_role = DeviceRole.objects.create(name="Server", slug="server")
        cls.consumers = {}
        for name, pos, outlet_name in (("cons-a", 1, "2/1"), ("cons-b", 2, "2/2")):
            dev = Device.objects.create(
                name=name, device_type=cons_type, site=cls.site, rack=cls.rack,
                role=cons_role, status="active", position=pos, face="front",
            )
            psu = PowerPort.objects.create(device=dev, name="PSU1", allocated_draw=1000)
            Cable(a_terminations=[psu], b_terminations=[outlets[outlet_name]]).save()
            cls.consumers[name] = dev

        # A SECOND rack with its own feed + PDU: the destination of a cross-rack
        # move, whose banks must pick up the moved device's draw.
        cls.rack2 = Rack.objects.create(name="Rack D2", site=cls.site, u_height=10)
        cls.feed2 = PowerFeed.objects.create(
            power_panel=panel, name="Feed D2", voltage=230, amperage=32,
            phase=PowerFeedPhaseChoices.PHASE_SINGLE,
        )
        cls.pdu2 = Device.objects.create(
            name="pdu-d-2", device_type=pdu_type, site=cls.site, rack=cls.rack2,
            role=pdu_role, status="active",
        )
        for nm in ("1/1", "2/1"):
            PowerOutlet.objects.create(device=cls.pdu2, name=nm)
        pdu2_input = PowerPort.objects.create(device=cls.pdu2, name="Input")
        Cable(a_terminations=[pdu2_input], b_terminations=[cls.feed2]).save()

        cls.design = Design.objects.create(title="Dist design", site=cls.site)

    def _url(self):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-recompute-distribution",
            kwargs={"pk": self.design.pk},
        )

    def _bank2_load(self, data):
        bank = data["distributions"][str(self.rack.pk)]["pdus"]["pdu-d-1"]["banks"]["2"]
        return bank["allocated_power"] + bank["planned_power"]

    def _existing(self, dev, pos):
        return {"kind": "existing", "device_id": dev.pk, "u_position": pos, "face": "front"}

    def test_recompute_reflects_removal_without_persisting(self):
        self.add_permissions("netbox_rack_design.view_design")
        a, b = self.consumers["cons-a"], self.consumers["cons-b"]

        # Baseline: no edits -> bank 2 carries BOTH consumers (2 x 1000 = 2000 W).
        base = self.client.post(
            self._url(),
            {"design_id": self.design.pk, "racks": [
                {"rack_id": self.rack.pk, "front": [self._existing(a, 1), self._existing(b, 2)]},
            ]},
            format="json", **self.header,
        )
        self.assertHttpStatus(base, status.HTTP_200_OK)
        self.assertEqual(self._bank2_load(base.data), 2000)

        # Flag cons-b for removal -> bank 2 drops to 1000 W, with no Save.
        resp = self.client.post(
            self._url(),
            {"design_id": self.design.pk, "racks": [
                {"rack_id": self.rack.pk, "front": [
                    self._existing(a, 1),
                    {"kind": "remove", "device_id": b.pk, "face": "front"},
                ]},
            ]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        self.assertEqual(self._bank2_load(resp.data), 1000)

        # The whole point: NOTHING was persisted -- it is a pure preview.
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    def test_recompute_follows_a_device_moved_to_another_rack(self):
        """User bug 2026-08-18: dragging devices from one rack to another made
        their draw vanish -- it left the source rack's banks and never landed in
        the destination's. A real device keeps its cabling to the SOURCE rack's
        PDU until the design is implemented, so the destination charged it to a
        PDU that is not in its own topology and dropped it. The draw must follow
        the device across racks, live, with no Save.
        """
        self.add_permissions("netbox_rack_design.view_design")
        a, b = self.consumers["cons-a"], self.consumers["cons-b"]

        payload = {"design_id": self.design.pk, "racks": [
            {"rack_id": self.rack.pk, "front": [self._existing(a, 1)]},
            {"rack_id": self.rack2.pk, "front": [
                # cons-b lands here; the source rack simply stops listing it.
                {"kind": "move", "device_id": b.pk, "u_position": 1, "face": "front"},
            ]},
        ]}

        resp = self.client.post(self._url(), payload, format="json", **self.header)
        self.assertHttpStatus(resp, status.HTTP_200_OK)

        # Source drops it...
        self.assertEqual(self._bank2_load(resp.data), 1000)
        # ...and the DESTINATION rack picks up its full 1000 W.
        dest = resp.data["distributions"][str(self.rack2.pk)]["pdus"]["pdu-d-2"]
        moved = [
            d["name"]
            for bank in dest["banks"].values()
            for d in bank["devices"]
        ]
        self.assertIn("cons-b", moved)
        self.assertEqual(
            sum(bk["allocated_power"] + bk["planned_power"] for bk in dest["banks"].values()),
            1000,
        )
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    def test_recompute_returns_live_rack_power_summary(self):
        """The response carries the rack-level power summary too, so the editor's
        BAR is live: its capacity comes from the rack's feeds (planned included),
        maths the browser must not duplicate."""
        self.add_permissions("netbox_rack_design.view_design")
        body = {"design_id": self.design.pk,
                "racks": [{"rack_id": self.rack.pk, "front": []}]}
        base = self.client.post(self._url(), body, format="json", **self.header)
        self.assertHttpStatus(base, status.HTTP_200_OK)
        power = base.data["power"][str(self.rack.pk)]
        self.assertIn("capacity_w", power)
        self.assertIn("draw_w", power)
        # No "distribution" key: the per-bank blob rides the other half of the
        # response, and duplicating it would double every payload.
        self.assertNotIn("distribution", power)
        before = power["capacity_w"]

        # A planned feed on this rack raises the capacity WITHOUT a save: the
        # editor reads exactly this to move the bar's denominator.
        DesignPowerFeed.objects.create(
            design=self.design, rack=self.rack, name="Extra", voltage=230,
            amperage=32, phase=PowerFeedPhaseChoices.PHASE_SINGLE,
        )
        after = self.client.post(self._url(), body, format="json", **self.header)
        self.assertHttpStatus(after, status.HTTP_200_OK)
        self.assertGreater(after.data["power"][str(self.rack.pk)]["capacity_w"], before)

    def test_recompute_requires_view_permission(self):
        # No permission granted -> 403 (POST maps to view_design for this action).
        resp = self.client.post(
            self._url(),
            {"design_id": self.design.pk, "racks": []},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_403_FORBIDDEN)


class PreviewNameTest(APITestCase):
    """
    Tests for the DesignViewSet preview-name action (Phase 2).

    The endpoint computes the would-be name for a PROSPECTIVE placement without
    persisting anything: no DesignPlacement is saved and no dcim object mutated.
    """

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]
        cls.device_type = env["device_type"]
        cls.device_role = env["device_role"]
        cls.tenant = env["tenant"]
        cls.design = Design.objects.create(title="DC-Preview", site=cls.site)

    def _url(self, design=None):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-preview-name",
            kwargs={"pk": (design or self.design).pk},
        )

    @override_settings(PLUGINS_CONFIG=_plugins_config(naming_mode="sequence"))
    def test_preview_add_returns_sequence_name(self):
        """An 'add' preview returns the sequence-mode '<title>-<n>' name."""
        self.add_permissions("netbox_rack_design.view_design")
        body = {"kind": "add", "device_type": self.device_type.pk, "index": 1}
        response = self.client.post(self._url(), body, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "DC-Preview-1")
        self.assertFalse(response.data["exists_in_site"])

    @override_settings(
        PLUGINS_CONFIG=_plugins_config(
            naming_mode="template", naming_template="{device.site.name}-{n}"
        )
    )
    def test_preview_template_mode_resolves_dotted_path(self):
        """Template mode resolves a dotted path over the placement context."""
        self.add_permissions("netbox_rack_design.view_design")
        body = {"kind": "add", "device_type": self.device_type.pk, "index": 3}
        response = self.client.post(self._url(), body, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        # {device.site.name} for an 'add' resolves to the design's site name.
        self.assertEqual(response.data["name"], "Site 1-3")

    @override_settings(PLUGINS_CONFIG=_plugins_config(naming_mode="sequence"))
    def test_pending_names_prevent_same_session_duplicates(self):
        """User bug 2026-07-10: two palette adds in one session both got the
        same generated name -- the preview API computed against the DB only,
        so unsaved in-editor siblings were invisible. The editor now sends
        `pending_names`; the engine must return a DIFFERENT, consecutive name
        when the naive same-index second request carries the first's name."""
        self.add_permissions("netbox_rack_design.view_design")
        body1 = {"kind": "add", "device_type": self.device_type.pk, "index": 5}
        r1 = self.client.post(self._url(), body1, format="json", **self.header)
        self.assertHttpStatus(r1, status.HTTP_200_OK)
        name1 = r1.data["name"]
        self.assertEqual(name1, "DC-Preview-5")

        body2 = {
            "kind": "add", "device_type": self.device_type.pk, "index": 5,
            "pending_names": [name1],
        }
        r2 = self.client.post(self._url(), body2, format="json", **self.header)
        self.assertHttpStatus(r2, status.HTTP_200_OK)
        self.assertNotEqual(
            r2.data["name"], name1,
            "the second same-family preview must not repeat an unsaved "
            "sibling's name")
        self.assertEqual(r2.data["name"], "DC-Preview-6")

    @override_settings(PLUGINS_CONFIG=_plugins_config(naming_mode="sequence"))
    def test_exists_in_site_true_for_real_device(self):
        """exists_in_site flips true when a real device already uses the name."""
        self.add_permissions("netbox_rack_design.view_design")
        create_test_device("DC-Preview-1", site=self.site)
        body = {"kind": "add", "device_type": self.device_type.pk, "index": 1}
        response = self.client.post(self._url(), body, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "DC-Preview-1")
        self.assertTrue(response.data["exists_in_site"])

    @override_settings(PLUGINS_CONFIG=_plugins_config(naming_mode="sequence"))
    def test_exists_in_site_true_for_other_placement(self):
        """exists_in_site flips true when another placement uses the name in-site."""
        self.add_permissions("netbox_rack_design.view_design")
        DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.racks[1],
            target_position=1,
            proposed_name="DC-Preview-9",
        )
        body = {"kind": "add", "device_type": self.device_type.pk, "index": 9}
        response = self.client.post(self._url(), body, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], "DC-Preview-9")
        self.assertTrue(response.data["exists_in_site"])

    @override_settings(PLUGINS_CONFIG=_plugins_config(naming_mode="sequence"))
    def test_preview_writes_nothing(self):
        """The endpoint persists no placement and creates no dcim Device."""
        self.add_permissions("netbox_rack_design.view_design")
        placements_before = DesignPlacement.objects.count()
        devices_before = Device.objects.count()
        body = {
            "kind": "add",
            "device_type": self.device_type.pk,
            "device_role": self.device_role.pk,
            "tenant": self.tenant.pk,
            "target_rack": self.racks[1].pk,
            "target_position": 5,
            "target_face": "front",
            "index": 1,
        }
        response = self.client.post(self._url(), body, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(DesignPlacement.objects.count(), placements_before)
        self.assertEqual(Device.objects.count(), devices_before)

    def test_bad_device_type_returns_400(self):
        """An unknown device_type PK → 400 with a clear message; nothing written."""
        self.add_permissions("netbox_rack_design.view_design")
        body = {"kind": "add", "device_type": 9999999}
        response = self.client.post(self._url(), body, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("device_type", response.data)

    def test_preview_without_view_permission_denied(self):
        """A user lacking view_design → 403."""
        body = {"kind": "add", "device_type": self.device_type.pk}
        response = self.client.post(self._url(), body, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)


class DesignRackScopeTest(APITestCase):
    """
    Tests for the DesignViewSet add-rack / remove-rack scope actions (Phase A).

    Adding enforces the same-site rule and object permissions; removing only
    detaches from design.racks and never deletes the rack or its placements.
    """

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]
        cls.device_type = env["device_type"]
        cls.design = Design.objects.create(title="Scope design", site=cls.site)

        # A rack in a DIFFERENT site -- adding it must be rejected.
        cls.other_site = Site.objects.create(name="Site 2", slug="site-2")
        cls.foreign_rack = Rack.objects.create(name="Foreign Rack", site=cls.other_site)

    def _add_url(self, design):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-add-rack",
            kwargs={"pk": design.pk},
        )

    def _remove_url(self, design):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-remove-rack",
            kwargs={"pk": design.pk},
        )

    def test_add_rack_same_site_succeeds(self):
        """A same-site rack is added to the scope; the updated scope is returned."""
        self.add_permissions("netbox_rack_design.change_design")
        rack = self.racks[0]
        response = self.client.post(
            self._add_url(self.design), {"rack_id": rack.pk}, format="json", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["rack_ids"], [rack.pk])
        self.assertIn(rack, self.design.racks.all())

    def test_add_rack_is_idempotent(self):
        """Re-adding a rack already in scope is a no-op (still one through-row)."""
        self.add_permissions("netbox_rack_design.change_design")
        rack = self.racks[0]
        self.design.racks.add(rack)
        response = self.client.post(
            self._add_url(self.design), {"rack_id": rack.pk}, format="json", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(self.design.racks.count(), 1)

    def test_add_rack_cross_site_rejected(self):
        """A rack from another site is rejected (same-site rule), scope unchanged."""
        self.add_permissions("netbox_rack_design.change_design")
        response = self.client.post(
            self._add_url(self.design),
            {"rack_id": self.foreign_rack.pk},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn(self.foreign_rack, self.design.racks.all())

    def test_add_rack_nonexistent_rejected(self):
        """A non-existent rack_id → 400."""
        self.add_permissions("netbox_rack_design.change_design")
        response = self.client.post(
            self._add_url(self.design), {"rack_id": 9999999}, format="json", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_add_rack_without_change_permission_denied(self):
        """A user lacking change_design → 403, scope unchanged."""
        rack = self.racks[0]
        response = self.client.post(
            self._add_url(self.design), {"rack_id": rack.pk}, format="json", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.design.racks.count(), 0)

    def test_remove_rack_zero_affected_detaches_immediately(self):
        """A rack with no placements targeting it detaches without confirmation."""
        self.add_permissions("netbox_rack_design.change_design")
        rack = self.racks[0]
        self.design.racks.add(rack)
        response = self.client.post(
            self._remove_url(self.design), {"rack_id": rack.pk}, format="json", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["deleted_count"], 0)
        self.assertEqual(response.data["rack_ids"], [])
        self.assertNotIn(rack, self.design.racks.all())
        self.assertTrue(Rack.objects.filter(pk=rack.pk).exists())

    def test_remove_rack_with_affected_requires_confirmation(self):
        """Affected placements + no confirm → 409, nothing deleted or detached."""
        self.add_permissions("netbox_rack_design.change_design")
        rack = self.racks[0]
        self.design.racks.add(rack)
        placement = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=rack,
            target_position=10,
        )
        response = self.client.post(
            self._remove_url(self.design), {"rack_id": rack.pk}, format="json", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_409_CONFLICT)
        self.assertTrue(response.data["requires_confirmation"])
        self.assertEqual(response.data["affected_count"], 1)
        self.assertEqual(response.data["affected"][0]["placement_id"], placement.pk)
        # Nothing was deleted or detached.
        self.assertIn(rack, self.design.racks.all())
        self.assertTrue(DesignPlacement.objects.filter(pk=placement.pk).exists())

    def test_remove_rack_confirmed_deletes_target_placements_only(self):
        """confirm=true deletes target_rack==R placements; unrelated ones survive."""
        self.add_permissions("netbox_rack_design.change_design")
        rack = self.racks[0]
        other_rack = self.racks[1]
        self.design.racks.set([rack, other_rack])

        # Affected: an add into R and a move into R.
        add_into_r = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=rack,
            target_position=10,
        )
        move_into_r = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=self.devices[0],
            target_rack=rack,
            target_position=11,
        )
        # Unrelated: a remove-kind placement for a device in R (target_rack is
        # NULL, destination is not R) and an add targeting a different rack.
        remove_in_r = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=self.devices[1],
        )
        add_into_other = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=other_rack,
            target_position=5,
        )

        response = self.client.post(
            self._remove_url(self.design),
            {"rack_id": rack.pk, "confirm": True},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["deleted_count"], 2)
        self.assertEqual(response.data["rack_ids"], [other_rack.pk])

        # Target-rack==R placements are gone.
        self.assertFalse(DesignPlacement.objects.filter(pk=add_into_r.pk).exists())
        self.assertFalse(DesignPlacement.objects.filter(pk=move_into_r.pk).exists())
        # Unrelated placements survive untouched.
        self.assertTrue(DesignPlacement.objects.filter(pk=remove_in_r.pk).exists())
        self.assertTrue(DesignPlacement.objects.filter(pk=add_into_other.pk).exists())
        # Rack detached; real devices/racks untouched.
        self.assertNotIn(rack, self.design.racks.all())
        self.assertTrue(Rack.objects.filter(pk=rack.pk).exists())
        self.devices[0].refresh_from_db()
        self.assertEqual(self.devices[0].rack_id, self.racks[0].pk)

    def test_remove_rack_without_change_permission_denied(self):
        """A user lacking change_design → 403; scope unchanged."""
        rack = self.racks[0]
        self.design.racks.add(rack)
        response = self.client.post(
            self._remove_url(self.design), {"rack_id": rack.pk}, format="json", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertIn(rack, self.design.racks.all())


class HiddenDesignRackTest(APITestCase):
    """
    Tests for the user-scoped per-design rack visibility endpoint (Phase A).

    We store HIDDEN rows, so the core properties are: hide/show toggling,
    show-all clearing, and strict per-user isolation.
    """

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.design = Design.objects.create(title="Visibility design", site=cls.site)
        cls.design.racks.set(cls.racks)

    def setUp(self):
        super().setUp()  # builds self.user / self.token / self.header
        self.user_b = User.objects.create_user(username="user_b")
        self.token_b = Token.objects.create(user=self.user_b)
        self.header_b = api_token_header(self.token_b)

    def _list_url(self):
        return reverse("plugins-api:netbox_rack_design-api:hiddendesignrack-list")

    def _toggle_url(self):
        return reverse("plugins-api:netbox_rack_design-api:hiddendesignrack-toggle")

    def _show_all_url(self):
        return reverse("plugins-api:netbox_rack_design-api:hiddendesignrack-show-all")

    def test_toggle_hides_then_shows(self):
        """First toggle hides a rack (creates a row); second shows it (removes it)."""
        body = {"design_id": self.design.pk, "rack_id": self.racks[0].pk}

        response = self.client.post(self._toggle_url(), body, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertTrue(response.data["hidden"])
        self.assertEqual(response.data["hidden_rack_ids"], [self.racks[0].pk])
        self.assertTrue(
            HiddenDesignRack.objects.filter(
                user=self.user, design=self.design, rack=self.racks[0]
            ).exists()
        )

        response = self.client.post(self._toggle_url(), body, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertFalse(response.data["hidden"])
        self.assertEqual(response.data["hidden_rack_ids"], [])
        self.assertFalse(
            HiddenDesignRack.objects.filter(
                user=self.user, design=self.design, rack=self.racks[0]
            ).exists()
        )

    def test_list_returns_only_current_users_hidden_racks(self):
        """GET ?design_id= returns ONLY the requesting user's hidden rack ids."""
        HiddenDesignRack.objects.create(
            user=self.user, design=self.design, rack=self.racks[0]
        )
        HiddenDesignRack.objects.create(
            user=self.user_b, design=self.design, rack=self.racks[1]
        )

        response = self.client.get(
            self._list_url(), {"design_id": self.design.pk}, **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["hidden_rack_ids"], [self.racks[0].pk])

        response = self.client.get(
            self._list_url(), {"design_id": self.design.pk}, **self.header_b
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["hidden_rack_ids"], [self.racks[1].pk])

    def test_list_requires_design_id(self):
        """GET without ?design_id → 400."""
        response = self.client.get(self._list_url(), **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_show_all_clears_only_current_users_rows(self):
        """show-all clears the requesting user's hidden rows but not user B's."""
        HiddenDesignRack.objects.create(
            user=self.user, design=self.design, rack=self.racks[0]
        )
        HiddenDesignRack.objects.create(
            user=self.user, design=self.design, rack=self.racks[1]
        )
        b_row = HiddenDesignRack.objects.create(
            user=self.user_b, design=self.design, rack=self.racks[0]
        )

        response = self.client.post(
            self._show_all_url(), {"design_id": self.design.pk}, format="json", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["hidden_rack_ids"], [])
        self.assertFalse(
            HiddenDesignRack.objects.filter(user=self.user, design=self.design).exists()
        )
        # User B's row survives.
        self.assertTrue(HiddenDesignRack.objects.filter(pk=b_row.pk).exists())

    def test_toggle_as_user_a_never_affects_user_b(self):
        """User B's hidden state is untouched when user A toggles the same rack."""
        b_row = HiddenDesignRack.objects.create(
            user=self.user_b, design=self.design, rack=self.racks[0]
        )
        body = {"design_id": self.design.pk, "rack_id": self.racks[0].pk}
        response = self.client.post(self._toggle_url(), body, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertTrue(response.data["hidden"])

        # A separate row was created for user A; user B's row survives.
        self.assertTrue(HiddenDesignRack.objects.filter(pk=b_row.pk).exists())
        self.assertEqual(
            HiddenDesignRack.objects.filter(
                design=self.design, rack=self.racks[0]
            ).count(),
            2,
        )

    def test_toggle_bad_design_or_rack_rejected(self):
        """A non-existent design_id or rack_id → 400, no row created."""
        response = self.client.post(
            self._toggle_url(),
            {"design_id": 9999999, "rack_id": self.racks[0].pk},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        response = self.client.post(
            self._toggle_url(),
            {"design_id": self.design.pk, "rack_id": 9999999},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(HiddenDesignRack.objects.filter(user=self.user).count(), 0)

    def test_unauthenticated_is_rejected(self):
        """No token → 401/403 on list, toggle, and show-all."""
        for response in (
            self.client.get(self._list_url(), {"design_id": self.design.pk}),
            self.client.post(
                self._toggle_url(),
                {"design_id": self.design.pk, "rack_id": self.racks[0].pk},
                format="json",
            ),
            self.client.post(
                self._show_all_url(), {"design_id": self.design.pk}, format="json"
            ),
        ):
            self.assertIn(
                response.status_code,
                (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
            )


class FavoriteDeviceTypeTest(APITestCase):
    """
    Tests for the user-scoped favorite-device-types endpoint (increment 2c-1).

    The core property under test is per-user isolation: a user only ever sees
    and mutates their own favorites; the client never passes a user.
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.device_type = env["device_type"]
        # A second device type so user B can star something distinct.
        from dcim.models import DeviceType

        cls.other_device_type = DeviceType.objects.create(
            manufacturer=env["manufacturer"],
            model="Device Type 2",
            slug="device-type-2",
            u_height=1,
        )

    def setUp(self):
        super().setUp()  # builds self.user / self.token / self.header
        # A second authenticated user (user B) with their own token/header.
        self.user_b = User.objects.create_user(username="user_b")
        self.token_b = Token.objects.create(user=self.user_b)
        self.header_b = api_token_header(self.token_b)

    def _list_url(self):
        return reverse(
            "plugins-api:netbox_rack_design-api:favoritedevicetype-list"
        )

    def _toggle_url(self):
        return reverse(
            "plugins-api:netbox_rack_design-api:favoritedevicetype-toggle"
        )

    def test_toggle_stars_then_unstars(self):
        """First toggle stars (creates a row); second unstars (removes it)."""
        body = {"device_type_id": self.device_type.pk}

        response = self.client.post(self._toggle_url(), body, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["device_type_id"], self.device_type.pk)
        self.assertTrue(response.data["favorite"])
        self.assertTrue(
            FavoriteDeviceType.objects.filter(
                user=self.user, device_type=self.device_type
            ).exists()
        )

        response = self.client.post(self._toggle_url(), body, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertFalse(response.data["favorite"])
        self.assertFalse(
            FavoriteDeviceType.objects.filter(
                user=self.user, device_type=self.device_type
            ).exists()
        )

    def test_list_returns_only_current_users_favorites(self):
        """GET returns ONLY the requesting user's device-type ids."""
        FavoriteDeviceType.objects.create(user=self.user, device_type=self.device_type)
        FavoriteDeviceType.objects.create(
            user=self.user_b, device_type=self.other_device_type
        )

        # User A sees only their own.
        response = self.client.get(self._list_url(), **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["device_type_ids"], [self.device_type.pk])

        # User B sees only their own.
        response = self.client.get(self._list_url(), **self.header_b)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["device_type_ids"], [self.other_device_type.pk])

    def test_toggle_as_user_a_never_affects_user_b(self):
        """User B's favorites are untouched when user A toggles."""
        b_fav = FavoriteDeviceType.objects.create(
            user=self.user_b, device_type=self.device_type
        )
        # User A stars the same device type.
        body = {"device_type_id": self.device_type.pk}
        response = self.client.post(self._toggle_url(), body, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertTrue(response.data["favorite"])

        # A separate row was created for user A; user B's row survives.
        self.assertTrue(FavoriteDeviceType.objects.filter(pk=b_fav.pk).exists())
        self.assertEqual(
            FavoriteDeviceType.objects.filter(device_type=self.device_type).count(), 2
        )

        # User A unstars; user B's row STILL survives.
        response = self.client.post(self._toggle_url(), body, format="json", **self.header)
        self.assertFalse(response.data["favorite"])
        self.assertTrue(FavoriteDeviceType.objects.filter(pk=b_fav.pk).exists())
        self.assertFalse(
            FavoriteDeviceType.objects.filter(
                user=self.user, device_type=self.device_type
            ).exists()
        )

    def test_unauthenticated_is_rejected(self):
        """No token → 401/403 on both list and toggle."""
        response = self.client.get(self._list_url())
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
        response = self.client.post(
            self._toggle_url(),
            {"device_type_id": self.device_type.pk},
            format="json",
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_invalid_device_type_id_is_rejected(self):
        """A device_type_id that doesn't resolve → 400, no row created."""
        body = {"device_type_id": 9999999}
        response = self.client.post(self._toggle_url(), body, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(FavoriteDeviceType.objects.filter(user=self.user).count(), 0)

    def test_double_star_does_not_duplicate_row(self):
        """Pre-existing star + a star toggle reaching get_or_create stays unique."""
        FavoriteDeviceType.objects.create(user=self.user, device_type=self.device_type)
        # get_or_create must not raise the unique constraint nor add a 2nd row;
        # because the row already exists, the toggle unstars it.
        body = {"device_type_id": self.device_type.pk}
        response = self.client.post(self._toggle_url(), body, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertFalse(response.data["favorite"])
        self.assertEqual(
            FavoriteDeviceType.objects.filter(
                user=self.user, device_type=self.device_type
            ).count(),
            0,
        )


class DeviceTypePowerTest(APITestCase):
    """
    Tests for the device-type-power endpoint (palette-add-live): the catalog
    palette fetches per-type projected draw here so a freshly dropped catalog
    add shows the SAME draw the projection will compute after Save + reload.
    """

    @classmethod
    def setUpTestData(cls):
        from dcim.models import DeviceType, Manufacturer, PowerPortTemplate

        mfr = Manufacturer.objects.create(name="DTP Mfr", slug="dtp-mfr")

        # Type WITH power data (200 W allocated on one PSU template).
        cls.dt_known = DeviceType.objects.create(
            manufacturer=mfr, model="DTP-Known", slug="dtp-known",
            u_height=1, is_full_depth=False)
        PowerPortTemplate.objects.create(
            device_type=cls.dt_known, name="PSU1",
            allocated_draw=200, maximum_draw=250)

        # Type WITH power ports defined but NO draw values -> unknown.
        cls.dt_unknown = DeviceType.objects.create(
            manufacturer=mfr, model="DTP-Unknown", slug="dtp-unknown",
            u_height=1, is_full_depth=False)
        PowerPortTemplate.objects.create(
            device_type=cls.dt_unknown, name="PSU1")

        # Type with NO power ports at all -> passive (known 0).
        cls.dt_passive = DeviceType.objects.create(
            manufacturer=mfr, model="DTP-Passive", slug="dtp-passive",
            u_height=1, is_full_depth=False)

    def _url(self):
        return reverse(
            "plugins-api:netbox_rack_design-api:devicetypepower-list"
        )

    def test_known_type_returns_draw_and_ports(self):
        """A type with a drawn PSU template reports draw_w, draw_known, ports."""
        response = self.client.get(
            self._url() + f"?id={self.dt_known.pk}", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        info = response.data["results"][str(self.dt_known.pk)]
        self.assertEqual(info["draw_w"], 200.0)
        self.assertTrue(info["draw_known"])
        self.assertEqual(len(info["power_ports"]), 1)
        self.assertEqual(info["power_ports"][0]["name"], "PSU1")
        self.assertEqual(info["power_ports"][0]["draw"], 200)
        # A bare type template has no cabling -> connected is None.
        self.assertIsNone(info["power_ports"][0]["connected"])

    def test_unknown_type_reports_not_known(self):
        """Power ports with no draw value -> draw_known False (a powered gap)."""
        response = self.client.get(
            self._url() + f"?id={self.dt_unknown.pk}", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        info = response.data["results"][str(self.dt_unknown.pk)]
        self.assertEqual(info["draw_w"], 0.0)
        self.assertFalse(info["draw_known"])

    def test_passive_type_is_known_zero(self):
        """No power ports at all -> passive: 0 W, known (not the unknown hatch)."""
        response = self.client.get(
            self._url() + f"?id={self.dt_passive.pk}", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        info = response.data["results"][str(self.dt_passive.pk)]
        self.assertEqual(info["draw_w"], 0.0)
        self.assertTrue(info["draw_known"])
        self.assertEqual(info["power_ports"], [])

    def test_excluded_role_reports_known_zero(self):
        """With a role in power_exclude_roles (a PDU) the type reports a KNOWN 0 W
        -- the same figure _project_power gives the saved slot. Without this, a
        PDU whose inlet template carries no draw would paint as the unknown hatch
        while its saved twin reads like passive gear."""
        from dcim.models import DeviceRole

        role = DeviceRole.objects.create(name="DTP PDU", slug="pdu")
        response = self.client.get(
            self._url() + f"?id={self.dt_unknown.pk}&role_id={role.pk}", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        info = response.data["results"][str(self.dt_unknown.pk)]
        self.assertEqual(info["draw_w"], 0.0)
        self.assertTrue(info["draw_known"])
        # The per-PSU detail is still reported, exactly as _project_power does
        # for an excluded slot.
        self.assertEqual([p["name"] for p in info["power_ports"]], ["PSU1"])

    def test_consumer_role_leaves_the_draw_alone(self):
        """A role that is NOT excluded changes nothing: the type's own draw wins."""
        from dcim.models import DeviceRole

        role = DeviceRole.objects.create(name="DTP Server", slug="server")
        response = self.client.get(
            self._url() + f"?id={self.dt_known.pk}&role_id={role.pk}", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        info = response.data["results"][str(self.dt_known.pk)]
        self.assertEqual(info["draw_w"], 200.0)
        self.assertTrue(info["draw_known"])

    def test_unresolvable_role_id_is_ignored(self):
        """A bogus/blank role_id must not error -- it just means "no role yet"."""
        for raw in ("9999999", "abc", ""):
            response = self.client.get(
                self._url() + f"?id={self.dt_unknown.pk}&role_id={raw}", **self.header)
            self.assertHttpStatus(response, status.HTTP_200_OK)
            info = response.data["results"][str(self.dt_unknown.pk)]
            self.assertFalse(info["draw_known"], raw)

    def test_batch_ids_and_unknown_id_omitted(self):
        """Multiple ids resolve together; a non-existent id is simply absent."""
        url = (self._url()
               + f"?id={self.dt_known.pk}&id={self.dt_passive.pk}&id=9999999")
        response = self.client.get(url, **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertIn(str(self.dt_known.pk), results)
        self.assertIn(str(self.dt_passive.pk), results)
        self.assertNotIn("9999999", results)

    def test_no_ids_returns_empty(self):
        """No id params -> empty result map, still 200 (never errors)."""
        response = self.client.get(self._url(), **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["results"], {})

    def test_requires_authentication(self):
        """The endpoint is authenticated-only (no anonymous reads)."""
        response = self.client.get(self._url() + f"?id={self.dt_known.pk}")
        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


class RackPowerTest(APITestCase):
    """
    Tests for the DesignViewSet rack-power action (Phase B): upsert/read the
    per-design rack power custom-field override (DesignRackPower). The rack
    is persistent design data, so POST saves immediately (no layout Save).
    """

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.design = Design.objects.create(title="Power design", site=cls.site)

    def _url(self, design=None):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-rack-power",
            kwargs={"pk": (design or self.design).pk},
        )

    def test_post_then_get_round_trips(self):
        """POST upserts the rack's power_config; a later GET returns it."""
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design"
        )
        rack = self.racks[0]
        power_config = {
            "source": "manual",
            "custom_fields": {"power_limitation": 5000, "pdu_location": "top"},
        }
        response = self.client.post(
            self._url(),
            {"rack_id": rack.pk, "power_config": power_config},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["power_config"], power_config)
        self.assertEqual(
            DesignRackPower.objects.filter(design=self.design, rack=rack).count(), 1
        )

        get_response = self.client.get(
            self._url() + f"?rack_id={rack.pk}", **self.header
        )
        self.assertHttpStatus(get_response, status.HTTP_200_OK)
        self.assertEqual(get_response.data["power_config"], power_config)

    def test_post_upserts_not_duplicates(self):
        """A second POST for the same (design, rack) updates the same row."""
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design"
        )
        rack = self.racks[0]
        self.client.post(
            self._url(),
            {"rack_id": rack.pk, "power_config": {"custom_fields": {"a": 1}}},
            format="json",
            **self.header,
        )
        response = self.client.post(
            self._url(),
            {"rack_id": rack.pk, "power_config": {"custom_fields": {"a": 2}}},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(
            DesignRackPower.objects.filter(design=self.design, rack=rack).count(), 1
        )
        rack_power = DesignRackPower.objects.get(design=self.design, rack=rack)
        self.assertEqual(rack_power.power_config, {"custom_fields": {"a": 2}})

    def test_get_with_no_stored_config_returns_null(self):
        """GET for a rack with no stored DesignRackPower returns power_config=null."""
        self.add_permissions("netbox_rack_design.view_design")
        rack = self.racks[1]
        response = self.client.get(self._url() + f"?rack_id={rack.pk}", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertIsNone(response.data["power_config"])

    def test_get_missing_rack_id_returns_400(self):
        """GET without ?rack_id= -> 400."""
        self.add_permissions("netbox_rack_design.view_design")
        response = self.client.get(self._url(), **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_post_nonexistent_rack_returns_400(self):
        """POST with a non-existent rack_id -> 400, nothing persisted."""
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design"
        )
        response = self.client.post(
            self._url(),
            {"rack_id": 9999999, "power_config": {}},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(DesignRackPower.objects.count(), 0)

    def test_post_without_change_permission_denied(self):
        """A user lacking change_design -> 403 on POST, nothing persisted."""
        self.add_permissions("netbox_rack_design.view_design")
        rack = self.racks[0]
        response = self.client.post(
            self._url(),
            {"rack_id": rack.pk, "power_config": {"custom_fields": {}}},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertEqual(DesignRackPower.objects.count(), 0)


class PowerSourceTest(APITestCase):
    """
    Tests for the DesignViewSet power-source action: a read-only lookup for the
    "copy from rack" mode of the rack power dialog (kind=rack only -- planned
    PDUs bind to feeds rather than copying another PDU's electricals). Performs
    no writes.
    """

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.design = Design.objects.create(title="Source design", site=cls.site)

    def _url(self, design=None):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-power-source",
            kwargs={"pk": (design or self.design).pk},
        )

    def test_kind_rack_returns_rack_custom_fields(self):
        """kind=rack returns the source rack's custom fields dict."""
        self.add_permissions("netbox_rack_design.view_design")
        rack = self.racks[0]
        response = self.client.get(
            self._url() + f"?rack_id={rack.pk}&kind=rack", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["custom_fields"], dict(rack.cf))

    def test_kind_rack_returns_source_rack_feeds(self):
        """kind=rack also returns the source rack's REAL feeds, so the copy-from-
        rack row can preview (and then clone) the supply, not just the cf."""
        self.add_permissions("netbox_rack_design.view_design")
        rack = self.racks[0]
        panel = PowerPanel.objects.create(site=self.site, name="PS Panel")
        PowerFeed.objects.create(
            power_panel=panel, rack=rack, name=f"{rack.name}-A", voltage=230,
            amperage=32, phase=PowerFeedPhaseChoices.PHASE_SINGLE,
        )
        response = self.client.get(
            self._url() + f"?rack_id={rack.pk}&kind=rack", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        feeds = response.data["feeds"]
        self.assertEqual(len(feeds), 1, feeds)
        self.assertEqual(feeds[0]["name"], f"{rack.name}-A")
        self.assertEqual(feeds[0]["amperage"], 32)
        self.assertEqual(feeds[0]["source"], "real")

    def test_kind_rack_falls_back_to_planned_feeds(self):
        """A source rack with no real feeds reports the design's PLANNED feeds for
        it, so a rack planned earlier in the same design can be cloned too."""
        self.add_permissions("netbox_rack_design.view_design")
        rack = self.racks[0]
        DesignPowerFeed.objects.create(
            design=self.design, rack=rack, name="Planned A", voltage=400,
            amperage=16, phase=PowerFeedPhaseChoices.PHASE_3PHASE,
        )
        response = self.client.get(
            self._url() + f"?rack_id={rack.pk}&kind=rack", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        feeds = response.data["feeds"]
        self.assertEqual([f["name"] for f in feeds], ["Planned A"])
        self.assertEqual(feeds[0]["source"], "planned")

    def test_kind_pdu_now_rejected(self):
        """kind=pdu was removed with the feed-binding redesign -> 400."""
        self.add_permissions("netbox_rack_design.view_design")
        rack = self.racks[0]
        response = self.client.get(
            self._url() + f"?rack_id={rack.pk}&kind=pdu&feed=a1", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_invalid_kind_returns_400(self):
        """An unrecognised kind -> 400."""
        self.add_permissions("netbox_rack_design.view_design")
        rack = self.racks[0]
        response = self.client.get(
            self._url() + f"?rack_id={rack.pk}&kind=bogus", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_missing_rack_returns_400(self):
        """A non-existent rack_id -> 400."""
        self.add_permissions("netbox_rack_design.view_design")
        response = self.client.get(
            self._url() + "?rack_id=9999999&kind=rack", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_without_view_permission_denied(self):
        """A user lacking view_design -> 403."""
        rack = self.racks[0]
        response = self.client.get(
            self._url() + f"?rack_id={rack.pk}&kind=rack", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)


# ---------------------------------------------------------------------------
# Phase C: the feed model + binding (docs/pdu-distribution-spec.md §6/§8)
# ---------------------------------------------------------------------------


class FeedsActionTest(APITestCase):
    """
    Tests for the DesignViewSet feeds action: the rack's real PowerFeeds plus
    this design's planned DesignPowerFeeds, in the uniform feed shape, for the
    bind-to-feed picker. Read-only.
    """

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.design = Design.objects.create(title="Feeds design", site=cls.site)

    def _url(self, design=None):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-feeds",
            kwargs={"pk": (design or self.design).pk},
        )

    def test_returns_real_and_planned_feeds_in_uniform_shape(self):
        """Both a real PowerFeed and a DesignPowerFeed come back with the same
        {id, name, voltage, amperage, phase, supply, source} shape."""
        self.add_permissions("netbox_rack_design.view_design")
        rack = self.racks[0]

        power_panel = PowerPanel.objects.create(site=self.site, name="Panel 1")
        real_feed = PowerFeed.objects.create(
            power_panel=power_panel, rack=rack, name="Real Feed A",
            voltage=230, amperage=32, phase="single-phase", supply="ac",
        )
        planned_feed = DesignPowerFeed.objects.create(
            design=self.design, rack=rack, name="Feed B",
            voltage=400, amperage=63, phase="three-phase", supply="ac",
        )

        response = self.client.get(
            self._url() + f"?rack_id={rack.pk}", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(len(response.data["real"]), 1)
        self.assertEqual(len(response.data["planned"]), 1)
        self.assertEqual(
            response.data["real"][0],
            {
                "id": real_feed.pk, "name": "Real Feed A", "voltage": 230,
                "amperage": 32, "phase": "single-phase", "supply": "ac",
                "source": "real",
            },
        )
        self.assertEqual(
            response.data["planned"][0],
            {
                "id": planned_feed.pk, "name": "Feed B", "voltage": 400,
                "amperage": 63, "phase": "three-phase", "supply": "ac",
                "source": "planned",
            },
        )

    def test_planned_feeds_scoped_to_this_design(self):
        """A DesignPowerFeed belonging to another design is not returned."""
        self.add_permissions("netbox_rack_design.view_design")
        rack = self.racks[0]
        other_design = Design.objects.create(title="Other design", site=self.site)
        DesignPowerFeed.objects.create(
            design=other_design, rack=rack, name="Feed X",
        )
        response = self.client.get(
            self._url() + f"?rack_id={rack.pk}", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["planned"], [])

    def test_missing_rack_id_returns_400(self):
        """GET without ?rack_id= -> 400."""
        self.add_permissions("netbox_rack_design.view_design")
        response = self.client.get(self._url(), **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_without_view_permission_denied(self):
        """A user lacking view_design -> 403."""
        rack = self.racks[0]
        response = self.client.get(
            self._url() + f"?rack_id={rack.pk}", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)


class CopyFeedsActionTest(APITestCase):
    """
    Tests for the DesignViewSet copy-feeds action: clone a source rack's feeds
    onto a target rack as PLANNED feeds -- the half of "copy from rack" that
    carries the supply itself. Writes only DesignPowerFeed rows.
    """

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.source = cls.racks[0]
        cls.target = cls.racks[1]
        cls.design = Design.objects.create(title="Copy feeds design", site=cls.site)
        panel = PowerPanel.objects.create(site=cls.site, name="CF Panel")
        for suffix, amps in (("A", 32), ("B", 16)):
            PowerFeed.objects.create(
                power_panel=panel, rack=cls.source,
                name=f"{cls.source.name}-{suffix}", voltage=230, amperage=amps,
                phase=PowerFeedPhaseChoices.PHASE_SINGLE,
            )

    def _url(self, design=None):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-copy-feeds",
            kwargs={"pk": (design or self.design).pk},
        )

    def _post(self, **body):
        return self.client.post(self._url(), body, format="json", **self.header)

    def _grant(self):
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design"
        )

    def test_copies_real_feeds_renamed_for_the_target_rack(self):
        """Each real feed becomes a planned feed on the target, with the source
        rack-name prefix swapped for the target's (R1-A -> R2-A)."""
        self._grant()
        response = self._post(rack_id=self.target.pk, source_rack_id=self.source.pk)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["created"], 2, response.data)
        self.assertEqual(response.data["updated"], 0, response.data)
        planned = DesignPowerFeed.objects.filter(
            design=self.design, rack=self.target).order_by("name")
        self.assertEqual(
            [f.name for f in planned],
            [f"{self.target.name}-A", f"{self.target.name}-B"],
        )
        # Electricals ride along per feed (32 A and 16 A, not one flattened value).
        self.assertEqual([f.amperage for f in planned], [32, 16])
        self.assertEqual([f.voltage for f in planned], [230, 230])
        # The source rack is untouched: no planned feeds were created for it.
        self.assertFalse(
            DesignPowerFeed.objects.filter(design=self.design, rack=self.source).exists()
        )

    def test_repeat_copy_updates_instead_of_duplicating(self):
        """Upsert by (design, rack, name): a second copy after the source's
        electricals changed updates the same rows."""
        self._grant()
        self.assertHttpStatus(
            self._post(rack_id=self.target.pk, source_rack_id=self.source.pk),
            status.HTTP_200_OK,
        )
        feed = PowerFeed.objects.get(rack=self.source, name=f"{self.source.name}-A")
        feed.amperage = 63
        feed.save()
        response = self._post(rack_id=self.target.pk, source_rack_id=self.source.pk)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["created"], 0, response.data)
        self.assertEqual(response.data["updated"], 2, response.data)
        self.assertEqual(
            DesignPowerFeed.objects.filter(design=self.design, rack=self.target).count(), 2
        )
        self.assertEqual(
            DesignPowerFeed.objects.get(
                design=self.design, rack=self.target, name=f"{self.target.name}-A"
            ).amperage,
            63,
        )

    def test_non_rack_named_feed_keeps_its_name(self):
        """A feed not named after its rack is copied verbatim."""
        self._grant()
        PowerFeed.objects.filter(rack=self.source).delete()
        panel = PowerPanel.objects.get(name="CF Panel")
        PowerFeed.objects.create(
            power_panel=panel, rack=self.source, name="Utility A", voltage=230,
            amperage=32, phase=PowerFeedPhaseChoices.PHASE_SINGLE,
        )
        self.assertHttpStatus(
            self._post(rack_id=self.target.pk, source_rack_id=self.source.pk),
            status.HTTP_200_OK,
        )
        self.assertEqual(
            [f.name for f in DesignPowerFeed.objects.filter(
                design=self.design, rack=self.target)],
            ["Utility A"],
        )

    def test_source_without_real_feeds_copies_its_planned_feeds(self):
        """A source rack planned earlier in the same design can be cloned."""
        self._grant()
        PowerFeed.objects.filter(rack=self.source).delete()
        DesignPowerFeed.objects.create(
            design=self.design, rack=self.source, name=f"{self.source.name}-A",
            voltage=400, amperage=16, phase=PowerFeedPhaseChoices.PHASE_3PHASE,
        )
        response = self._post(rack_id=self.target.pk, source_rack_id=self.source.pk)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        copied = DesignPowerFeed.objects.get(design=self.design, rack=self.target)
        self.assertEqual(copied.name, f"{self.target.name}-A")
        self.assertEqual(copied.voltage, 400)
        self.assertEqual(copied.phase, PowerFeedPhaseChoices.PHASE_3PHASE)

    def test_source_with_no_feeds_is_a_no_op(self):
        """Nothing to copy -> 200 with an empty list, no rows written."""
        self._grant()
        PowerFeed.objects.filter(rack=self.source).delete()
        response = self._post(rack_id=self.target.pk, source_rack_id=self.source.pk)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["feeds"], [])
        self.assertEqual(
            DesignPowerFeed.objects.filter(design=self.design).count(), 0
        )

    def test_same_rack_rejected(self):
        """Copying a rack onto itself is a 400, not a self-duplicating no-op."""
        self._grant()
        response = self._post(rack_id=self.source.pk, source_rack_id=self.source.pk)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_rack_outside_the_designs_site_rejected(self):
        """Same-site rule, mirroring add-rack / rack-power / planned-feed."""
        self._grant()
        other_site = Site.objects.create(name="CF Other", slug="cf-other")
        stray = Rack.objects.create(name="CF Stray", site=other_site, u_height=10)
        response = self._post(rack_id=stray.pk, source_rack_id=self.source.pk)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_missing_source_rack_rejected(self):
        self._grant()
        response = self._post(rack_id=self.target.pk, source_rack_id=9999999)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_requires_change_permission(self):
        """View-only users cannot copy: this action writes."""
        self.add_permissions("netbox_rack_design.view_design")
        response = self._post(rack_id=self.target.pk, source_rack_id=self.source.pk)
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertEqual(DesignPowerFeed.objects.count(), 0)


class PlannedFeedActionTest(APITestCase):
    """
    Tests for the DesignViewSet planned-feed action: upsert/list this design's
    DesignPowerFeed rows (the greenfield "define planned feed" dialog).
    """

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.design = Design.objects.create(title="Planned feed design", site=cls.site)

    def _url(self, design=None):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-planned-feed",
            kwargs={"pk": (design or self.design).pk},
        )

    def test_post_creates_a_planned_feed(self):
        """POST creates a new DesignPowerFeed and returns its serialized shape."""
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design"
        )
        rack = self.racks[0]
        response = self.client.post(
            self._url(),
            {
                "rack_id": rack.pk, "name": "Feed A",
                "voltage": 230, "amperage": 16,
                "phase": "single-phase", "supply": "ac",
            },
            format="json", **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        feed = DesignPowerFeed.objects.get(design=self.design, rack=rack, name="Feed A")
        self.assertEqual(response.data["id"], feed.pk)
        self.assertEqual(feed.voltage, 230)
        self.assertEqual(feed.amperage, 16)
        self.assertEqual(feed.phase, "single-phase")

    def test_second_post_same_rack_and_name_updates_not_duplicates(self):
        """A second POST for the same (rack, name) updates the same row."""
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design"
        )
        rack = self.racks[0]
        self.client.post(
            self._url(),
            {"rack_id": rack.pk, "name": "Feed A", "voltage": 230, "amperage": 16},
            format="json", **self.header,
        )
        response = self.client.post(
            self._url(),
            {"rack_id": rack.pk, "name": "Feed A", "voltage": 400, "amperage": 32},
            format="json", **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(
            DesignPowerFeed.objects.filter(
                design=self.design, rack=rack, name="Feed A"
            ).count(),
            1,
        )
        feed = DesignPowerFeed.objects.get(design=self.design, rack=rack, name="Feed A")
        self.assertEqual(feed.voltage, 400)
        self.assertEqual(feed.amperage, 32)

    def test_get_lists_this_racks_planned_feeds(self):
        """GET ?rack_id= lists only that rack's planned feeds for this design."""
        self.add_permissions("netbox_rack_design.view_design")
        rack, other_rack = self.racks[0], self.racks[1]
        DesignPowerFeed.objects.create(design=self.design, rack=rack, name="Feed A")
        DesignPowerFeed.objects.create(design=self.design, rack=rack, name="Feed B")
        DesignPowerFeed.objects.create(design=self.design, rack=other_rack, name="Feed C")

        response = self.client.get(
            self._url() + f"?rack_id={rack.pk}", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        names = {row["name"] for row in response.data}
        self.assertEqual(names, {"Feed A", "Feed B"})

    def test_post_cross_site_rack_rejected(self):
        """A rack outside the design's site -> 400, nothing persisted."""
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design"
        )
        other_site = Site.objects.create(name="Other site", slug="other-site")
        other_rack = Rack.objects.create(name="Other rack", site=other_site)
        response = self.client.post(
            self._url(),
            {"rack_id": other_rack.pk, "name": "Feed A"},
            format="json", **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(DesignPowerFeed.objects.filter(rack=other_rack).exists())

    def test_post_nonexistent_rack_returns_400(self):
        """POST with a non-existent rack_id -> 400."""
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design"
        )
        response = self.client.post(
            self._url(),
            {"rack_id": 9999999, "name": "Feed A"},
            format="json", **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_post_without_change_permission_denied(self):
        """A user lacking change_design -> 403, nothing persisted."""
        self.add_permissions("netbox_rack_design.view_design")
        rack = self.racks[0]
        response = self.client.post(
            self._url(),
            {"rack_id": rack.pk, "name": "Feed A"},
            format="json", **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertFalse(DesignPowerFeed.objects.filter(design=self.design).exists())


class SaveLayoutFeedBindingTest(APITestCase):
    """
    Tests for save-layout persisting a PDU add's power-feed binding
    (docs/pdu-distribution-spec.md §6.2/§8): real_power_feed_id /
    planned_power_feed_id ride the item payload onto the placement.
    """

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]
        cls.design = Design.objects.create(title="Feed binding design", site=cls.site)

        power_panel = PowerPanel.objects.create(site=cls.site, name="Panel 1")
        cls.real_feed = PowerFeed.objects.create(
            power_panel=power_panel, rack=cls.racks[0], name="Real Feed A",
        )
        cls.planned_feed = DesignPowerFeed.objects.create(
            design=cls.design, rack=cls.racks[0], name="Planned Feed B",
        )

    def _url(self, design=None):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-save-layout",
            kwargs={"pk": (design or self.design).pk},
        )

    def _grant_all(self):
        self.add_permissions(
            "netbox_rack_design.change_design",
            "netbox_rack_design.add_designplacement",
            "netbox_rack_design.change_designplacement",
            "netbox_rack_design.delete_designplacement",
        )

    def _payload(self, racks):
        return {"design_id": self.design.pk, "racks": racks}

    def test_add_persists_real_power_feed_binding(self):
        """A PDU add carrying real_power_feed_id persists it onto the placement."""
        self._grant_all()
        rack = self.racks[0]
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 10, "face": "front",
                     "real_power_feed_id": self.real_feed.pk},
                ],
            },
        ])
        response = self.client.post(self._url(), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placement = DesignPlacement.objects.get(design=self.design)
        self.assertEqual(placement.real_power_feed_id, self.real_feed.pk)
        self.assertIsNone(placement.planned_power_feed_id)

    def test_add_persists_power_source_device(self):
        """A PDU add carrying power_source_device_id persists the cf-source FK."""
        self._grant_all()
        source_pdu = create_test_device(
            "src-pdu-a1", site=self.site, rack=self.racks[1], position=None, face="",
        )
        rack = self.racks[0]
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 10, "face": "front",
                     "power_source_device_id": source_pdu.pk},
                ],
            },
        ])
        response = self.client.post(self._url(), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placement = DesignPlacement.objects.get(design=self.design)
        self.assertEqual(placement.power_source_device_id, source_pdu.pk)

    def test_add_with_nonexistent_source_device_skipped_gracefully(self):
        """A stale power_source_device_id is skipped, not a hard error."""
        self._grant_all()
        rack = self.racks[0]
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 10, "face": "front",
                     "power_source_device_id": 9999999},
                ],
            },
        ])
        response = self.client.post(self._url(), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placement = DesignPlacement.objects.get(design=self.design)
        self.assertIsNone(placement.power_source_device_id)

    def test_add_persists_planned_power_feed_binding(self):
        """A PDU add carrying planned_power_feed_id persists it onto the placement."""
        self._grant_all()
        rack = self.racks[0]
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 10, "face": "front",
                     "planned_power_feed_id": self.planned_feed.pk},
                ],
            },
        ])
        response = self.client.post(self._url(), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placement = DesignPlacement.objects.get(design=self.design)
        self.assertIsNone(placement.real_power_feed_id)
        self.assertEqual(placement.planned_power_feed_id, self.planned_feed.pk)

    def test_add_with_both_feed_ids_rejected_and_persists_neither(self):
        """An item carrying BOTH ids is rejected: 400, no placement persisted."""
        self._grant_all()
        rack = self.racks[0]
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 10, "face": "front",
                     "real_power_feed_id": self.real_feed.pk,
                     "planned_power_feed_id": self.planned_feed.pk},
                ],
            },
        ])
        response = self.client.post(self._url(), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    def test_reposition_existing_add_updates_binding(self):
        """Repositioning an existing add can also (re)bind it to a feed."""
        self._grant_all()
        rack = self.racks[0]
        existing_add = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=rack,
            target_position=5,
            target_face="front",
        )
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "placement_id": existing_add.pk,
                     "u_position": 9, "face": "front",
                     "real_power_feed_id": self.real_feed.pk},
                ],
            },
        ])
        response = self.client.post(self._url(), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        existing_add.refresh_from_db()
        self.assertEqual(existing_add.real_power_feed_id, self.real_feed.pk)

    def test_add_without_feed_ids_leaves_binding_null(self):
        """An 'add' item that omits both feed keys persists no binding (default)."""
        self._grant_all()
        rack = self.racks[0]
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 10, "face": "front"},
                ],
            },
        ])
        response = self.client.post(self._url(), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placement = DesignPlacement.objects.get(design=self.design)
        self.assertIsNone(placement.real_power_feed_id)
        self.assertIsNone(placement.planned_power_feed_id)

    def test_nonexistent_real_feed_id_skipped_gracefully(self):
        """A stale/non-existent real_power_feed_id is skipped, not a hard error."""
        self._grant_all()
        rack = self.racks[0]
        payload = self._payload([
            {
                "rack_id": rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 10, "face": "front",
                     "real_power_feed_id": 9999999},
                ],
            },
        ])
        response = self.client.post(self._url(), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placement = DesignPlacement.objects.get(design=self.design)
        self.assertIsNone(placement.real_power_feed_id)
