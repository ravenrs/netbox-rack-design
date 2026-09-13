"""
T1.9 -- deletion rules for a ``PlannedRack`` (PLAN-templates.md sec 1 "Still
open" / decision index D3-D7, D25, D28).

``PlannedRack`` is a SHARED object (D6): several designs, possibly owned by
different people, can reference the same row through ``Design.planned_racks``
and the CASCADE FKs on ``DesignPlacement.target_planned_rack``,
``DesignPowerFeed.planned_rack`` and ``DesignRackPower.planned_rack`` (D25).
Nothing previously stopped one being deleted out from under a design still
planning into it, or stopped a REALIZED one (D7 -- the row that survives
Apply forever) from being deleted at all.

Covers the four decisions from the task brief:

1. Deleting a PlannedRack that designs still reference -> refused outright
   (409 / redirect+message), naming the referencing designs.
2. Deleting a REALIZED PlannedRack -> refused unconditionally (D7's row
   "survives forever"), independent of whether anything still references it.
3. Orphans (nothing references it) -> deletion allowed; cleanup is MANUAL,
   surfaced via ``PlannedRack.orphaned()`` / the filterset's ``orphan`` filter
   and ``matches_existing_rack`` for the "worse than dead weight" case.
4. Deleting a Design must not delete a PlannedRack other designs still use,
   and must not delete the PlannedRack row at all (only the M2M scope + the
   deleting design's own placements/feeds/rack-power cascade away).
"""

from dcim.models import Location, Rack
from django.contrib.messages import get_messages
from django.test import TestCase as DjangoTestCase
from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase, TestCase

from ..choices import DesignPlacementKindChoices
from ..models import (
    Design,
    DesignPlacement,
    DesignPowerFeed,
    DesignRackPower,
    PlannedRack,
)
from .utils import create_dcim_environment

# ---------------------------------------------------------------------------
# Model-level: referencing_designs / is_orphan / orphaned() / matches_existing_rack
# ---------------------------------------------------------------------------


class PlannedRackReferencingDesignsTest(DjangoTestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.location = Location.objects.create(
            name="Ref Location", slug="ref-location", site=cls.site
        )

    def test_orphan_planned_rack_has_no_referencing_designs(self):
        pr = PlannedRack.objects.create(name="Orphan", location=self.location, u_height=10)
        self.assertEqual(list(pr.referencing_designs()), [])
        self.assertTrue(pr.is_orphan)
        self.assertIn(pr.pk, PlannedRack.orphaned().values_list("pk", flat=True))

    def test_scoped_only_planned_rack_is_referenced(self):
        pr = PlannedRack.objects.create(name="Scoped only", location=self.location, u_height=10)
        design = Design.objects.create(title="Scoping design", site=self.site)
        design.planned_racks.add(pr)
        self.assertEqual(list(pr.referencing_designs()), [design])
        self.assertFalse(pr.is_orphan)
        self.assertNotIn(pr.pk, PlannedRack.orphaned().values_list("pk", flat=True))

    def test_placement_only_planned_rack_is_referenced_even_without_scope(self):
        # clean() does not enforce that a placement's target_planned_rack is
        # inside the design's declared M2M scope -- referencing_designs() must
        # not rely on scoped_designs alone, or this placement's design would
        # be an undetected loss.
        pr = PlannedRack.objects.create(name="Placement only", location=self.location, u_height=10)
        design = Design.objects.create(title="Placement design", site=self.site)
        DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_planned_rack=pr,
            target_position=1,
        )
        self.assertEqual(list(pr.referencing_designs()), [design])
        self.assertFalse(pr.is_orphan)

    def test_feed_only_planned_rack_is_referenced(self):
        pr = PlannedRack.objects.create(name="Feed only", location=self.location, u_height=10)
        design = Design.objects.create(title="Feed design", site=self.site)
        DesignPowerFeed.objects.create(design=design, planned_rack=pr, name="Feed A")
        self.assertEqual(list(pr.referencing_designs()), [design])
        self.assertFalse(pr.is_orphan)

    def test_rack_power_only_planned_rack_is_referenced(self):
        pr = PlannedRack.objects.create(name="Power only", location=self.location, u_height=10)
        design = Design.objects.create(title="Power design", site=self.site)
        DesignRackPower.objects.create(design=design, planned_rack=pr)
        self.assertEqual(list(pr.referencing_designs()), [design])
        self.assertFalse(pr.is_orphan)

    def test_matches_existing_rack_true_when_shadowed_and_unrealized(self):
        pr = PlannedRack.objects.create(name="Shadowed", location=self.location, u_height=10)
        Rack.objects.create(name="Shadowed", site=self.site, location=self.location)
        self.assertTrue(pr.matches_existing_rack)

    def test_matches_existing_rack_false_when_no_such_rack(self):
        pr = PlannedRack.objects.create(name="No shadow", location=self.location, u_height=10)
        self.assertFalse(pr.matches_existing_rack)

    def test_matches_existing_rack_false_once_realized(self):
        # Realization already names the one real rack this row means; a
        # DIFFERENT same-named rack appearing later is not this row's concern.
        real = Rack.objects.create(name="Realized target", site=self.site, location=self.location)
        pr = PlannedRack.objects.create(
            name="Realized shadow", location=self.location, u_height=10, realized_rack=real,
        )
        Rack.objects.create(name="Realized shadow", site=self.site, location=self.location)
        self.assertFalse(pr.matches_existing_rack)


# ---------------------------------------------------------------------------
# REST API: PlannedRackViewSet.perform_destroy
# ---------------------------------------------------------------------------


class PlannedRackAPIDeleteGuardTest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.location = Location.objects.create(
            name="API Delete Location", slug="api-delete-location", site=cls.site
        )

    def _delete(self, pr):
        url = reverse(
            "plugins-api:netbox_rack_design-api:plannedrack-detail", kwargs={"pk": pr.pk}
        )
        return self.client.delete(url, **self.header)

    def test_delete_orphan_succeeds(self):
        self.add_permissions("netbox_rack_design.delete_plannedrack")
        pr = PlannedRack.objects.create(name="API orphan", location=self.location, u_height=10)
        response = self._delete(pr)
        self.assertHttpStatus(response, status.HTTP_204_NO_CONTENT)
        self.assertFalse(PlannedRack.objects.filter(pk=pr.pk).exists())

    def test_delete_still_referenced_returns_409_not_500(self):
        self.add_permissions("netbox_rack_design.delete_plannedrack")
        pr = PlannedRack.objects.create(name="API referenced", location=self.location, u_height=10)
        design = Design.objects.create(title="API referencing design", site=self.site)
        design.planned_racks.add(pr)
        response = self._delete(pr)
        self.assertHttpStatus(response, status.HTTP_409_CONFLICT)
        self.assertIn("API referencing design", str(response.data))
        self.assertTrue(PlannedRack.objects.filter(pk=pr.pk).exists())

    def test_delete_realized_returns_409_even_if_unreferenced(self):
        self.add_permissions("netbox_rack_design.delete_plannedrack")
        real = Rack.objects.create(name="API realized target", site=self.site, location=self.location)
        pr = PlannedRack.objects.create(
            name="API realized", location=self.location, u_height=10, realized_rack=real,
        )
        response = self._delete(pr)
        self.assertHttpStatus(response, status.HTTP_409_CONFLICT)
        self.assertIn("realized", str(response.data).lower())
        self.assertTrue(PlannedRack.objects.filter(pk=pr.pk).exists())

    def test_delete_realized_and_referenced_reports_realized_reason(self):
        # Realization is checked first and is unconditional (D7) -- it must
        # win even when the row is ALSO still referenced.
        self.add_permissions("netbox_rack_design.delete_plannedrack")
        real = Rack.objects.create(
            name="API realized+ref target", site=self.site, location=self.location
        )
        pr = PlannedRack.objects.create(
            name="API realized+ref", location=self.location, u_height=10, realized_rack=real,
        )
        design = Design.objects.create(title="API realized ref design", site=self.site)
        design.planned_racks.add(pr)
        response = self._delete(pr)
        self.assertHttpStatus(response, status.HTTP_409_CONFLICT)
        self.assertIn("realized", str(response.data).lower())


# ---------------------------------------------------------------------------
# HTML: PlannedRackDeleteView
# ---------------------------------------------------------------------------


class PlannedRackHTMLDeleteGuardTest(TestCase):
    user_permissions = (
        "netbox_rack_design.view_plannedrack",
        "netbox_rack_design.delete_plannedrack",
    )

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.location = Location.objects.create(
            name="HTML Delete Location", slug="html-delete-location", site=cls.site
        )

    def _delete_url(self, pr):
        return reverse("plugins:netbox_rack_design:plannedrack_delete", kwargs={"pk": pr.pk})

    def test_delete_orphan_succeeds(self):
        pr = PlannedRack.objects.create(name="HTML orphan", location=self.location, u_height=10)
        response = self.client.post(self._delete_url(pr), {"confirm": "true"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PlannedRack.objects.filter(pk=pr.pk).exists())

    def test_delete_still_referenced_rejected_with_message(self):
        pr = PlannedRack.objects.create(name="HTML referenced", location=self.location, u_height=10)
        design = Design.objects.create(title="HTML referencing design", site=self.site)
        design.planned_racks.add(pr)
        response = self.client.post(self._delete_url(pr), {"confirm": "true"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(PlannedRack.objects.filter(pk=pr.pk).exists())
        shown = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("HTML referencing design" in m for m in shown))

    def test_delete_realized_rejected_with_message(self):
        real = Rack.objects.create(
            name="HTML realized target", site=self.site, location=self.location
        )
        pr = PlannedRack.objects.create(
            name="HTML realized", location=self.location, u_height=10, realized_rack=real,
        )
        response = self.client.post(self._delete_url(pr), {"confirm": "true"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(PlannedRack.objects.filter(pk=pr.pk).exists())
        shown = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("realized" in m.lower() for m in shown))


# ---------------------------------------------------------------------------
# Deleting a Design must not delete a shared PlannedRack (decision 4)
# ---------------------------------------------------------------------------


class DesignDeletionLeavesPlannedRackTest(DjangoTestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.location = Location.objects.create(
            name="Design Delete Location", slug="design-delete-location", site=cls.site
        )

    def test_deleting_one_of_two_referencing_designs_leaves_planned_rack_and_other_design(self):
        pr = PlannedRack.objects.create(name="Shared PR", location=self.location, u_height=10)
        design_a = Design.objects.create(title="Design A", site=self.site)
        design_b = Design.objects.create(title="Design B", site=self.site)
        design_a.planned_racks.add(pr)
        design_b.planned_racks.add(pr)

        design_a.delete()

        pr.refresh_from_db()
        self.assertTrue(PlannedRack.objects.filter(pk=pr.pk).exists())
        self.assertTrue(Design.objects.filter(pk=design_b.pk).exists())
        self.assertEqual(list(pr.referencing_designs()), [design_b])

    def test_deleting_the_only_referencing_design_leaves_planned_rack_as_an_orphan(self):
        # The PlannedRack row itself is NOT deleted by a Design delete -- only
        # the M2M through-row (and, if any, this design's own placements/
        # feeds/rack-power CASCADE away with the design, per their own FKs).
        # The row becomes an ORPHAN, which decision 3 leaves for manual
        # cleanup rather than deleting it automatically.
        pr = PlannedRack.objects.create(name="Solo PR", location=self.location, u_height=10)
        design = Design.objects.create(title="Solo design", site=self.site)
        design.planned_racks.add(pr)

        design.delete()

        pr.refresh_from_db()
        self.assertTrue(PlannedRack.objects.filter(pk=pr.pk).exists())
        self.assertTrue(pr.is_orphan)
        self.assertIn(pr.pk, PlannedRack.orphaned().values_list("pk", flat=True))

    def test_deleting_design_cascades_its_own_placement_targeting_planned_rack(self):
        # This mirrors target_rack's existing (deliberate) CASCADE behaviour --
        # the placement's DESTINATION FK, not its history FK -- so a design
        # delete legitimately takes its own placements with it. The shared
        # PlannedRack row itself still survives.
        pr = PlannedRack.objects.create(name="Placement CASCADE PR", location=self.location, u_height=10)
        design = Design.objects.create(title="Placement cascade design", site=self.site)
        placement = DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_planned_rack=pr,
            target_position=1,
        )
        design.delete()
        self.assertFalse(DesignPlacement.objects.filter(pk=placement.pk).exists())
        self.assertTrue(PlannedRack.objects.filter(pk=pr.pk).exists())


# ---------------------------------------------------------------------------
# Manual cleanup surface: the ``orphan`` filter (decision 3)
# ---------------------------------------------------------------------------


class PlannedRackOrphanFilterTest(APITestCase):
    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.location = Location.objects.create(
            name="Filter Location", slug="filter-location", site=cls.site
        )
        cls.orphan = PlannedRack.objects.create(
            name="Filter orphan", location=cls.location, u_height=10
        )
        cls.referenced = PlannedRack.objects.create(
            name="Filter referenced", location=cls.location, u_height=10
        )
        cls.design = Design.objects.create(title="Filter design", site=cls.site)
        cls.design.planned_racks.add(cls.referenced)

    def test_orphan_true_returns_only_unreferenced(self):
        self.add_permissions("netbox_rack_design.view_plannedrack")
        url = reverse("plugins-api:netbox_rack_design-api:plannedrack-list")
        response = self.client.get(url, {"orphan": "true"}, **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        ids = {row["id"] for row in response.data["results"]}
        self.assertIn(self.orphan.pk, ids)
        self.assertNotIn(self.referenced.pk, ids)

    def test_orphan_false_returns_only_referenced(self):
        self.add_permissions("netbox_rack_design.view_plannedrack")
        url = reverse("plugins-api:netbox_rack_design-api:plannedrack-list")
        response = self.client.get(url, {"orphan": "false"}, **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        ids = {row["id"] for row in response.data["results"]}
        self.assertIn(self.referenced.pk, ids)
        self.assertNotIn(self.orphan.pk, ids)
