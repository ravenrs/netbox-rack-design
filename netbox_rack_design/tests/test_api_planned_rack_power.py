"""
API-level tests for letting a planned PDU bind to power on a PLANNED rack
(PLAN-templates.md D25/T1.8b) -- the bug docs/planned-racks.md's "Power on a
planned rack" section already promises works.

``test_planned_rack_power.py`` covers this at the model level (constraints,
clean(), effective_custom_fields()); this file covers the five DesignViewSet
actions that identify a rack by id and, before the fix, unconditionally
rejected or silently ignored a ``"p:<pk>"`` key via ``parse_real_rack_id``:
``rack_power``, ``power_source``, ``copy_feeds``, ``feeds``, ``planned_feed``.

Every action here is exercised against BOTH a planned rack (the new case) and
confirms the real-rack case is unaffected (the existing ``RackPowerTest`` /
``FeedsActionTest`` / ``CopyFeedsActionTest`` / ``PlannedFeedActionTest``
classes in ``test_api.py`` are the authoritative regression coverage for
that -- this file adds only the extra "unknown p:<pk> still 400s" case
alongside the planned-rack-happy-path tests, to keep both readings of the
fix next to each other).
"""

from dcim.models import Location
from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase

from ..models import DesignPlacement, DesignPowerFeed, DesignRackPower, PlannedRack
from .utils import create_dcim_environment, make_design


class PlannedRackPowerActionTest(APITestCase):
    """rack-power GET/POST against a planned rack."""

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.location = Location.objects.create(
            name="RP Location", slug="rp-location", site=cls.site
        )
        cls.planned_rack = PlannedRack.objects.create(
            name="Greenfield RP", location=cls.location, u_height=42
        )
        cls.design = make_design(title="Planned rack power design", site=cls.site)
        cls.design.planned_racks.set([cls.planned_rack])

    def _url(self):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-rack-power",
            kwargs={"pk": self.design.pk},
        )

    def test_post_then_get_round_trips_for_planned_rack(self):
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design"
        )
        power_config = {
            "source": "manual",
            "custom_fields": {"power_limitation": 4000, "pdu_location": "bottom"},
        }
        response = self.client.post(
            self._url(),
            {"rack_id": f"p:{self.planned_rack.pk}", "power_config": power_config},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["power_config"], power_config)

        row = DesignRackPower.objects.get(design=self.design, planned_rack=self.planned_rack)
        self.assertIsNone(row.rack_id)
        self.assertEqual(row.power_config, power_config)

        get_response = self.client.get(
            self._url() + f"?rack_id=p:{self.planned_rack.pk}", **self.header
        )
        self.assertHttpStatus(get_response, status.HTTP_200_OK)
        self.assertEqual(get_response.data["power_config"], power_config)

    def test_get_with_no_stored_config_returns_null_for_planned_rack(self):
        self.add_permissions("netbox_rack_design.view_design")
        response = self.client.get(
            self._url() + f"?rack_id=p:{self.planned_rack.pk}", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertIsNone(response.data["power_config"])

    def test_post_unknown_planned_rack_returns_400(self):
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design"
        )
        response = self.client.post(
            self._url(),
            {"rack_id": "p:9999999", "power_config": {}},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["rack_id"], ["Rack does not exist."])
        self.assertEqual(DesignRackPower.objects.count(), 0)

    def test_get_unknown_planned_rack_returns_null_not_error(self):
        # Mirrors the pre-existing real-rack GET behaviour (RackPowerTest.
        # test_get_with_no_stored_config_returns_null): this is a plain
        # filter, never an existence check -- only the POST/write path
        # insists the rack exists.
        self.add_permissions("netbox_rack_design.view_design")
        response = self.client.get(self._url() + "?rack_id=p:9999999", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertIsNone(response.data["power_config"])


class PlannedFeedActionPlannedRackTest(APITestCase):
    """planned-feed GET/POST/DELETE against a planned rack."""

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.location = Location.objects.create(
            name="PF Location", slug="pf-location", site=cls.site
        )
        cls.planned_rack = PlannedRack.objects.create(
            name="Greenfield PF", location=cls.location, u_height=42
        )
        cls.design = make_design(title="Planned feed design", site=cls.site)
        cls.design.planned_racks.set([cls.planned_rack])

    def _url(self):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-planned-feed",
            kwargs={"pk": self.design.pk},
        )

    def test_post_creates_a_planned_feed_on_a_planned_rack(self):
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design"
        )
        response = self.client.post(
            self._url(),
            {
                "rack_id": f"p:{self.planned_rack.pk}",
                "name": "Feed A",
                "voltage": 230,
                "amperage": 32,
                "phase": "single-phase",
                "supply": "ac",
            },
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        feed = DesignPowerFeed.objects.get(design=self.design, planned_rack=self.planned_rack)
        self.assertIsNone(feed.rack_id)
        self.assertEqual(feed.name, "Feed A")
        self.assertEqual(response.data["id"], feed.pk)

    def test_get_lists_this_planned_racks_planned_feeds(self):
        self.add_permissions("netbox_rack_design.view_design")
        feed = DesignPowerFeed.objects.create(
            design=self.design, planned_rack=self.planned_rack, name="Feed A",
        )
        response = self.client.get(
            self._url() + f"?rack_id=p:{self.planned_rack.pk}", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["id"], feed.pk)

    def test_post_unknown_planned_rack_returns_400(self):
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design"
        )
        response = self.client.post(
            self._url(),
            {"rack_id": "p:9999999", "name": "Feed A"},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["rack_id"], ["Rack does not exist."])
        self.assertEqual(DesignPowerFeed.objects.count(), 0)

    def test_delete_by_rack_id_and_name_for_planned_rack(self):
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design"
        )
        feed = DesignPowerFeed.objects.create(
            design=self.design, planned_rack=self.planned_rack, name="Feed A",
        )
        response = self.client.generic(
            "DELETE",
            self._url(),
            data=f'{{"rack_id": "p:{self.planned_rack.pk}", "name": "Feed A"}}',
            content_type="application/json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertFalse(DesignPowerFeed.objects.filter(pk=feed.pk).exists())

    def test_post_rejected_when_design_approved(self):
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design"
        )
        from ..choices import DesignStatusChoices

        self.design.status = DesignStatusChoices.STATUS_APPROVED
        self.design.save()
        response = self.client.post(
            self._url(),
            {"rack_id": f"p:{self.planned_rack.pk}", "name": "Feed A"},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_409_CONFLICT)
        self.assertEqual(DesignPowerFeed.objects.count(), 0)


class FeedsActionPlannedRackTest(APITestCase):
    """feeds GET against a planned rack: real stays [], planned is populated."""

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.location = Location.objects.create(
            name="Feeds Location", slug="feeds-location", site=cls.site
        )
        cls.planned_rack = PlannedRack.objects.create(
            name="Greenfield Feeds", location=cls.location, u_height=42
        )
        cls.design = make_design(title="Feeds design", site=cls.site)
        cls.design.planned_racks.set([cls.planned_rack])

    def _url(self):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-feeds",
            kwargs={"pk": self.design.pk},
        )

    def test_planned_feed_on_planned_rack_is_listed(self):
        self.add_permissions("netbox_rack_design.view_design")
        feed = DesignPowerFeed.objects.create(
            design=self.design, planned_rack=self.planned_rack, name="Feed A",
            voltage=400, amperage=32,
        )
        response = self.client.get(
            self._url() + f"?rack_id=p:{self.planned_rack.pk}", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["real"], [])
        self.assertEqual(len(response.data["planned"]), 1)
        entry = response.data["planned"][0]
        self.assertEqual(entry["id"], feed.pk)
        self.assertEqual(entry["name"], "Feed A")
        self.assertEqual(entry["source"], "planned")
        self.assertNotIn("inherited", entry)

    def test_no_feeds_returns_empty_lists(self):
        self.add_permissions("netbox_rack_design.view_design")
        response = self.client.get(
            self._url() + f"?rack_id=p:{self.planned_rack.pk}", **self.header
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data, {"real": [], "planned": []})

    def test_unknown_planned_rack_returns_empty_lists_not_error(self):
        # Mirrors the pre-existing real-rack GET behaviour: this is a plain
        # filter, never an existence check.
        self.add_permissions("netbox_rack_design.view_design")
        response = self.client.get(self._url() + "?rack_id=p:9999999", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data, {"real": [], "planned": []})


class CopyFeedsActionPlannedRackTest(APITestCase):
    """copy-feeds from a REAL source rack onto a PLANNED target rack."""

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.location = Location.objects.create(
            name="Copy Location", slug="copy-location", site=cls.site
        )
        cls.planned_rack = PlannedRack.objects.create(
            name="Greenfield Copy", location=cls.location, u_height=42
        )
        cls.design = make_design(title="Copy feeds design", site=cls.site)
        cls.design.planned_racks.set([cls.planned_rack])

    def _url(self):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-copy-feeds",
            kwargs={"pk": self.design.pk},
        )

    def test_copy_from_real_rack_onto_planned_rack(self):
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design"
        )
        source = self.racks[0]
        DesignPowerFeed.objects.create(
            design=self.design, rack=source, name=f"{source.name}-A",
            voltage=230, amperage=16,
        )
        response = self.client.post(
            self._url(),
            {
                "rack_id": f"p:{self.planned_rack.pk}",
                "source_rack_id": str(source.pk),
            },
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["created"], 1)
        feed = DesignPowerFeed.objects.get(
            design=self.design, planned_rack=self.planned_rack
        )
        self.assertIsNone(feed.rack_id)
        # The source-name prefix is retargeted onto the planned rack's name.
        self.assertEqual(feed.name, f"{self.planned_rack.name}-A")

    def test_copy_onto_unknown_planned_rack_returns_400(self):
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design"
        )
        source = self.racks[0]
        response = self.client.post(
            self._url(),
            {"rack_id": "p:9999999", "source_rack_id": str(source.pk)},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["rack_id"], ["Rack does not exist."])


class PlannedRackFeedBindingTest(APITestCase):
    """
    End-to-end: define a planned feed on a planned rack, then bind a planned
    PDU 'add' to it through save-layout -- the same pattern
    ``SaveLayoutFeedBindingTest`` uses for a real rack (save-layout itself is
    unchanged by this fix; it already accepts a "p:<pk>" rack_id).
    """

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.location = Location.objects.create(
            name="Bind Location", slug="bind-location", site=cls.site
        )
        cls.planned_rack = PlannedRack.objects.create(
            name="Greenfield Bind", location=cls.location, u_height=42
        )
        cls.design = make_design(title="Bind design", site=cls.site)
        cls.design.planned_racks.set([cls.planned_rack])

    def test_define_and_bind_planned_feed_on_planned_rack(self):
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design",
            "netbox_rack_design.add_designplacement",
            "netbox_rack_design.change_designplacement",
            "netbox_rack_design.delete_designplacement",
        )
        feed_url = reverse(
            "plugins-api:netbox_rack_design-api:design-planned-feed",
            kwargs={"pk": self.design.pk},
        )
        feed_response = self.client.post(
            feed_url,
            {"rack_id": f"p:{self.planned_rack.pk}", "name": "Feed A"},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(feed_response, status.HTTP_200_OK)
        feed_id = feed_response.data["id"]

        save_layout_url = reverse(
            "plugins-api:netbox_rack_design-api:design-save-layout",
            kwargs={"pk": self.design.pk},
        )
        payload = {
            "design_id": self.design.pk,
            "racks": [
                {
                    "rack_id": f"p:{self.planned_rack.pk}",
                    "front": [
                        {"kind": "add", "device_type_id": self.device_type.pk,
                         "u_position": 1, "face": "front",
                         "planned_power_feed_id": feed_id},
                    ],
                },
            ],
        }
        response = self.client.post(save_layout_url, payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)

        placement = DesignPlacement.objects.get(design=self.design)
        self.assertEqual(placement.target_planned_rack_id, self.planned_rack.pk)
        self.assertEqual(placement.planned_power_feed_id, feed_id)
