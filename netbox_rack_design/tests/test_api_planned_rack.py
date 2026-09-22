"""REST API tests for ``PlannedRack`` (PLAN-templates.md T1.7).

``PlannedRack`` already has the model, migrations, HTML views, GraphQL types,
filtersets and Apply support -- this is the one piece it was missing. Mirrors
``DesignPowerFeedAPITest`` (the closest existing precedent: a plugin
``NetBoxModel`` with its own list/detail API) for the CRUD suite, then adds
the round-trip coverage the task brief calls out by name: a ``DesignPlacement``
targeting a planned rack, a ``DesignPowerFeed`` bound to a planned rack, and
``Design.planned_racks``.

The last class, ``PlannedRackPkCollisionAPITest``, is the regression test D28
demands: a real ``dcim.Rack`` and a ``PlannedRack`` sharing the SAME pk value
must never let a request naming one reach the other. ``PlannedRack`` and
``dcim.Rack`` keep separate pk sequences, so this never happens by accident --
the collision has to be forced, exactly as ``test_projection_planned_rack_pk``
already does for the projection engine.
"""

from dcim.models import Location, Rack
from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase, APIViewTestCases, create_tags

from ..models import DesignPlacement, DesignPowerFeed, PlannedRack
from .utils import create_dcim_environment, make_design


class PlannedRackAPITest(APIViewTestCases.APIViewTestCase):
    """Full CRUD through the REST API -- the standard suite."""

    model = PlannedRack
    view_namespace = "plugins-api:netbox_rack_design"
    brief_fields = ["display", "id", "name", "url"]
    bulk_update_data = {"u_height": 50}

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        site = env["site"]
        cls.site = site
        cls.location = Location.objects.create(name="Location 1", slug="location-1", site=site)
        other_location = Location.objects.create(name="Location 2", slug="location-2", site=site)

        PlannedRack.objects.create(name="Planned 1", location=cls.location, u_height=42)
        PlannedRack.objects.create(name="Planned 2", location=cls.location, u_height=42)
        PlannedRack.objects.create(name="Planned 3", location=cls.location, u_height=42)

        tags = create_tags("PR-Alpha", "PR-Bravo", "PR-Charlie")

        cls.create_data = [
            {
                "name": "Planned 4", "location": cls.location.pk, "u_height": 47,
                "tags": [t.pk for t in tags],
            },
            {"name": "Planned 5", "location": cls.location.pk, "u_height": 42},
            {"name": "Planned 6", "location": other_location.pk, "u_height": 42},
        ]

    def test_realized_rack_round_trips(self):
        """A realized planned rack reads back with its real rack nested."""
        self.add_permissions(
            "netbox_rack_design.view_plannedrack", "netbox_rack_design.change_plannedrack"
        )
        planned = PlannedRack.objects.create(name="To realize", location=self.location)
        real_rack = Rack.objects.create(name="Real R1", site=self.site, location=self.location)
        url = reverse(
            "plugins-api:netbox_rack_design-api:plannedrack-detail", kwargs={"pk": planned.pk}
        )
        response = self.client.patch(
            url, {"realized_rack": real_rack.pk}, format="json", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["realized_rack"]["id"], real_rack.pk)
        self.assertTrue(response.data["is_realized"])


class DesignPlacementTargetPlannedRackTest(APITestCase):
    """
    Not a full CRUD suite (that's ``DesignPlacementTest`` in ``test_api.py``) --
    just the ``target_planned_rack`` round trip the task brief asks for:
    create a ``DesignPlacement`` by pk, read it back nested.
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.location = Location.objects.create(
            name="Placement Location", slug="placement-location", site=cls.site
        )
        cls.planned_rack = PlannedRack.objects.create(
            name="Greenfield 1", location=cls.location, u_height=42
        )
        cls.design = make_design(title="Placement design", site=cls.site)

    def test_create_placement_with_target_planned_rack_by_pk(self):
        self.add_permissions(
            "netbox_rack_design.add_designplacement", "netbox_rack_design.view_designplacement"
        )
        url = reverse("plugins-api:netbox_rack_design-api:designplacement-list")
        data = {
            "design": self.design.pk,
            "kind": "add",
            "device_type": self.device_type.pk,
            "target_planned_rack": self.planned_rack.pk,
            "target_position": 1,
            "target_face": "front",
            "proposed_name": "greenfield-1",
        }
        response = self.client.post(url, data, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_201_CREATED)

        placement = DesignPlacement.objects.get(pk=response.data["id"])
        self.assertEqual(placement.target_planned_rack_id, self.planned_rack.pk)
        self.assertIsNone(placement.target_rack_id)

        # Read back: target_planned_rack renders NESTED, not a bare pk.
        detail_url = reverse(
            "plugins-api:netbox_rack_design-api:designplacement-detail",
            kwargs={"pk": placement.pk},
        )
        response = self.client.get(detail_url, **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["target_planned_rack"]["id"], self.planned_rack.pk)
        self.assertEqual(response.data["target_planned_rack"]["name"], self.planned_rack.name)
        self.assertIsNone(response.data["target_rack"])


class DesignPowerFeedPlannedRackTest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.location = Location.objects.create(
            name="Feed Location", slug="feed-location", site=cls.site
        )
        cls.planned_rack = PlannedRack.objects.create(
            name="Greenfield Feed Rack", location=cls.location, u_height=42
        )
        cls.design = make_design(title="Feed design", site=cls.site)
        cls.design.planned_racks.set([cls.planned_rack])

    def test_create_feed_with_planned_rack_by_pk(self):
        self.add_permissions(
            "netbox_rack_design.add_designpowerfeed", "netbox_rack_design.view_designpowerfeed"
        )
        url = reverse("plugins-api:netbox_rack_design-api:designpowerfeed-list")
        data = {
            "design": self.design.pk,
            "planned_rack": self.planned_rack.pk,
            "name": "Greenfield Feed A",
        }
        response = self.client.post(url, data, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_201_CREATED)

        feed = DesignPowerFeed.objects.get(pk=response.data["id"])
        self.assertEqual(feed.planned_rack_id, self.planned_rack.pk)
        self.assertIsNone(feed.rack_id)

        detail_url = reverse(
            "plugins-api:netbox_rack_design-api:designpowerfeed-detail", kwargs={"pk": feed.pk}
        )
        response = self.client.get(detail_url, **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["planned_rack"]["id"], self.planned_rack.pk)
        self.assertIsNone(response.data["rack"])


class DesignPlannedRacksRoundTripTest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.location = Location.objects.create(
            name="Design Location", slug="design-location", site=cls.site
        )
        cls.planned_rack_1 = PlannedRack.objects.create(
            name="Design PR 1", location=cls.location, u_height=42
        )
        cls.planned_rack_2 = PlannedRack.objects.create(
            name="Design PR 2", location=cls.location, u_height=42
        )
        cls.design = make_design(title="Scoped planned racks", site=cls.site)

    def test_planned_racks_round_trip(self):
        self.add_permissions(
            "netbox_rack_design.change_design", "netbox_rack_design.view_design"
        )
        url = reverse(
            "plugins-api:netbox_rack_design-api:design-detail", kwargs={"pk": self.design.pk}
        )
        response = self.client.patch(
            url,
            {"planned_racks": [self.planned_rack_1.pk, self.planned_rack_2.pk]},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(
            sorted(pr["id"] for pr in response.data["planned_racks"]),
            sorted([self.planned_rack_1.pk, self.planned_rack_2.pk]),
        )
        self.design.refresh_from_db()
        self.assertEqual(
            set(self.design.planned_racks.values_list("pk", flat=True)),
            {self.planned_rack_1.pk, self.planned_rack_2.pk},
        )


class PlannedRackPkCollisionAPITest(APITestCase):
    """
    D28's landmine, at the REST layer: ``PlannedRack`` and ``dcim.Rack`` keep
    SEPARATE pk sequences, so pk 74 can exist in both tables and mean two
    different racks. Force the collision on purpose (a forced-pk
    ``PlannedRack.objects.create`` cannot pass "by luck" -- without it the two
    pks would never coincide in practice) and prove neither endpoint, nor a
    ``DesignPlacement`` naming the planned rack by that pk, can reach the
    other rack.
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.real_rack = env["racks"][0]
        cls.location = Location.objects.create(
            name="Collision Location", slug="collision-location", site=cls.site
        )

        cls.planned_rack = PlannedRack(
            name="Colliding planned rack", location=cls.location, u_height=10,
        )
        cls.planned_rack.pk = cls.real_rack.pk
        cls.planned_rack.save(force_insert=True)
        assert cls.planned_rack.pk == cls.real_rack.pk, (
            "setup did not actually force the pk collision this test needs"
        )

        cls.design = make_design(title="Collision design", site=cls.site)

    def test_planned_rack_detail_returns_planned_rack_not_real_rack(self):
        self.add_permissions("netbox_rack_design.view_plannedrack")
        url = reverse(
            "plugins-api:netbox_rack_design-api:plannedrack-detail",
            kwargs={"pk": self.planned_rack.pk},
        )
        response = self.client.get(url, **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], self.planned_rack.name)
        self.assertNotEqual(response.data["name"], self.real_rack.name)

    def test_real_rack_detail_unaffected_by_same_pk_planned_rack(self):
        self.add_permissions("dcim.view_rack")
        url = reverse("dcim-api:rack-detail", kwargs={"pk": self.real_rack.pk})
        response = self.client.get(url, **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], self.real_rack.name)
        self.assertNotEqual(response.data["name"], self.planned_rack.name)

    def test_placement_naming_planned_rack_by_colliding_pk_targets_planned_rack(self):
        """
        A placement created with ``target_planned_rack=<colliding pk>`` must
        resolve to the PLANNED rack -- never silently accepted as (or confused
        with) the real rack that happens to share the same integer pk.
        """
        self.add_permissions(
            "netbox_rack_design.add_designplacement", "netbox_rack_design.view_designplacement"
        )
        url = reverse("plugins-api:netbox_rack_design-api:designplacement-list")
        data = {
            "design": self.design.pk,
            "kind": "add",
            "device_type": self.device_type.pk,
            "target_planned_rack": self.planned_rack.pk,
            "target_position": 20,
            "target_face": "front",
            "proposed_name": "collision-add",
        }
        response = self.client.post(url, data, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_201_CREATED)

        placement = DesignPlacement.objects.get(pk=response.data["id"])
        self.assertEqual(placement.target_planned_rack_id, self.planned_rack.pk)
        self.assertIsNone(placement.target_rack_id)

        detail_url = reverse(
            "plugins-api:netbox_rack_design-api:designplacement-detail",
            kwargs={"pk": placement.pk},
        )
        response = self.client.get(detail_url, **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["target_planned_rack"]["id"], self.planned_rack.pk)
        self.assertEqual(response.data["target_planned_rack"]["name"], self.planned_rack.name)
        self.assertIsNone(response.data["target_rack"])
