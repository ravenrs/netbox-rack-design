"""REST API tests for NetBox Rack Design (subclassing NetBox's standard suite)."""

from dcim.models import Device
from django.urls import reverse
from rest_framework import status
from users.models import Token, User
from utilities.testing import APITestCase, APIViewTestCases, create_tags

from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignGroup, DesignPlacement, FavoriteDeviceType
from .utils import create_dcim_environment


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
    bulk_update_data = {
        "summary": "Bulk-updated summary",
        "status": DesignStatusChoices.STATUS_REJECTED,
    }

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        site = env["site"]

        Design.objects.create(title="Design 1", site=site)
        Design.objects.create(title="Design 2", site=site)
        Design.objects.create(title="Design 3", site=site)

        tags = create_tags("Alpha", "Bravo", "Charlie")

        cls.create_data = [
            {
                "title": "Design 4",
                "site": site.pk,
                "status": DesignStatusChoices.STATUS_DRAFT,
                "tags": [t.pk for t in tags],
            },
            {
                "title": "Design 5",
                "site": site.pk,
                "status": DesignStatusChoices.STATUS_DRAFT,
            },
            {
                "title": "Design 6",
                "site": site.pk,
                "status": DesignStatusChoices.STATUS_DRAFT,
            },
        ]


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
        self.header_b = {"HTTP_AUTHORIZATION": f"Token {self.token_b.key}"}

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
