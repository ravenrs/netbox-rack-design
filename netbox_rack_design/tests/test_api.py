"""REST API tests for NetBox Rack Design (subclassing NetBox's standard suite)."""

from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase, APIViewTestCases, create_tags

from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignGroup, DesignPlacement
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
