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


class CreatePlannedRackActionTest(APITestCase):
    """
    POST .../designs/<pk>/create-planned-rack/ -- the editor's "Create rack"
    dialog. Covers the two features layered onto the existing single-rack
    action: NetBox-style name-pattern expansion (``R[1-4]`` -> 4 racks) and
    optional copy-feeds-on-create (seed each new rack's supply from an
    existing rack in the same design, the same clone ``DesignViewSet.
    copy_feeds`` performs for the rack-power dialog's "Copy from rack").
    """

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.location = Location.objects.create(
            name="Create Location", slug="create-location", site=cls.site
        )
        cls.design = make_design(title="Create planned rack design", site=cls.site)

    def _url(self):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-create-planned-rack",
            kwargs={"pk": self.design.pk},
        )

    def test_plain_name_creates_one_rack_old_response_shape(self):
        """A name with no brackets: one rack, the same response shape as
        before this feature -- exactly ``rack_key``/``planned_rack_id``/
        ``planned_rack_ids``, no ``planned_racks`` list."""
        self.add_permissions("netbox_rack_design.add_design", "netbox_rack_design.change_design")
        response = self.client.post(
            self._url(),
            {"name": "R101", "u_height": 42, "location_id": self.location.pk},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertEqual(
            sorted(response.data.keys()),
            ["planned_rack_id", "planned_rack_ids", "rack_key"],
        )
        planned = PlannedRack.objects.get(name="R101", location=self.location)
        self.assertEqual(response.data["planned_rack_id"], planned.pk)
        self.assertEqual(response.data["rack_key"], f"p:{planned.pk}")
        self.assertEqual(response.data["planned_rack_ids"], [planned.pk])
        self.assertEqual(PlannedRack.objects.filter(location=self.location).count(), 1)

    def test_pattern_name_creates_multiple_racks_in_order(self):
        self.add_permissions("netbox_rack_design.add_design", "netbox_rack_design.change_design")
        response = self.client.post(
            self._url(),
            {"name": "R[1-4]", "u_height": 42, "location_id": self.location.pk},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertIn("planned_racks", response.data)
        names = [entry["name"] for entry in response.data["planned_racks"]]
        self.assertEqual(names, ["R1", "R2", "R3", "R4"])
        for entry in response.data["planned_racks"]:
            self.assertIn("rack_key", entry)
            self.assertIn("planned_rack_id", entry)
        self.assertEqual(
            set(
                PlannedRack.objects.filter(location=self.location)
                .values_list("name", flat=True)
            ),
            {"R1", "R2", "R3", "R4"},
        )
        self.design.refresh_from_db()
        self.assertEqual(self.design.planned_racks.count(), 4)

    def test_malformed_pattern_returns_400_not_500(self):
        self.add_permissions("netbox_rack_design.add_design", "netbox_rack_design.change_design")
        response = self.client.post(
            self._url(),
            {"name": "R[9-8]", "u_height": 42, "location_id": self.location.pk},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(PlannedRack.objects.filter(location=self.location).count(), 0)

    def test_collision_anywhere_in_expansion_creates_nothing(self):
        """One of the expanded names already exists in this location -- the
        whole batch is refused, and NONE of the others are created either."""
        self.add_permissions("netbox_rack_design.add_design", "netbox_rack_design.change_design")
        PlannedRack.objects.create(name="R3", location=self.location, u_height=42)
        response = self.client.post(
            self._url(),
            {"name": "R[1-4]", "u_height": 42, "location_id": self.location.pk},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("R3", str(response.data))
        # Only the pre-existing R3 survives -- R1/R2/R4 were never created.
        self.assertEqual(
            set(
                PlannedRack.objects.filter(location=self.location)
                .values_list("name", flat=True)
            ),
            {"R3"},
        )

    def test_copy_feeds_on_create_gives_each_rack_its_own_feeds(self):
        self.add_permissions("netbox_rack_design.add_design", "netbox_rack_design.change_design")
        source = self.racks[0]
        DesignPowerFeed.objects.create(
            design=self.design, rack=source, name=f"{source.name}-A",
            voltage=230, amperage=16,
        )
        response = self.client.post(
            self._url(),
            {
                "name": "R[1-2]",
                "u_height": 42,
                "location_id": self.location.pk,
                "copy_feeds_from_rack_id": str(source.pk),
            },
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        r1 = PlannedRack.objects.get(name="R1", location=self.location)
        r2 = PlannedRack.objects.get(name="R2", location=self.location)
        feed1 = DesignPowerFeed.objects.get(design=self.design, planned_rack=r1)
        feed2 = DesignPowerFeed.objects.get(design=self.design, planned_rack=r2)
        # Each rack's copied feed is named for ITSELF, not the source or its
        # sibling (mirrors copy_feeds's own name-retargeting).
        self.assertEqual(feed1.name, "R1-A")
        self.assertEqual(feed2.name, "R2-A")

    def test_copy_feeds_omitted_creates_no_feeds(self):
        self.add_permissions("netbox_rack_design.add_design", "netbox_rack_design.change_design")
        response = self.client.post(
            self._url(),
            {"name": "R900", "u_height": 42, "location_id": self.location.pk},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertEqual(DesignPowerFeed.objects.filter(design=self.design).count(), 0)


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
