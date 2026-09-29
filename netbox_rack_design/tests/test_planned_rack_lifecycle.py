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

from core.models import ObjectType
from dcim.models import Location, Rack
from django.contrib.messages import get_messages
from django.test import TestCase as DjangoTestCase
from django.urls import reverse
from rest_framework import status
from users.models import User
from utilities.testing import APITestCase, TestCase

from ..choices import DesignPlacementKindChoices
from ..models import (
    Design,
    DesignPlacement,
    DesignPowerFeed,
    DesignRackPower,
    PlannedRack,
)
from .utils import create_dcim_environment, make_design

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
        design = make_design(title="Scoping design", site=self.site)
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
        design = make_design(title="Placement design", site=self.site)
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
        design = make_design(title="Feed design", site=self.site)
        DesignPowerFeed.objects.create(design=design, planned_rack=pr, name="Feed A")
        self.assertEqual(list(pr.referencing_designs()), [design])
        self.assertFalse(pr.is_orphan)

    def test_rack_power_only_planned_rack_is_referenced(self):
        pr = PlannedRack.objects.create(name="Power only", location=self.location, u_height=10)
        design = make_design(title="Power design", site=self.site)
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
        design = make_design(title="API referencing design", site=self.site)
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
        design = make_design(title="API realized ref design", site=self.site)
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
        design = make_design(title="HTML referencing design", site=self.site)
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


class PlannedRackDetailReferencingDesignsTest(TestCase):
    """The detail page names the designs standing in the way of a delete.

    Deleting a planned rack is refused while any design still plans across
    it, and the refusal says to remove it from each design's planning scope
    first -- but the page never said WHICH designs, so a planner had to go
    hunting (user report 2026-09-22). Each one is listed with a link to its
    editor, where the Racks panel's remove control lives.
    """

    user_permissions = (
        "netbox_rack_design.view_plannedrack",
        "netbox_rack_design.view_design",
    )

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.location = Location.objects.create(
            name="Detail Location", slug="detail-location", site=cls.site
        )

    def _url(self, pr):
        return reverse("plugins:netbox_rack_design:plannedrack", kwargs={"pk": pr.pk})

    def test_referencing_designs_are_listed_with_editor_links(self):
        pr = PlannedRack.objects.create(name="Held", location=self.location, u_height=10)
        design = make_design(title="Holding design", site=self.site)
        design.planned_racks.add(pr)

        response = self.client.get(self._url(pr))
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn("Holding design", content)
        self.assertIn(
            reverse("plugins:netbox_rack_design:design_editor_default",
                    kwargs={"pk": design.pk}),
            content,
            "each design links to its editor, where the rack can be detached")

    def test_a_design_the_user_cannot_view_is_not_disclosed(self):
        """The list is restricted, like every other object list.

        The delete REFUSAL names the design regardless -- an obstacle with
        no name is not actionable -- but this panel is an ordinary listing
        and obeys ordinary object permissions.
        """
        pr = PlannedRack.objects.create(name="Hidden", location=self.location, u_height=10)
        design = make_design(title="Invisible design", site=self.site)
        design.planned_racks.add(pr)

        # A SEPARATE user who may view planned racks but no design: NetBox
        # grants through ObjectPermission, so clearing `user_permissions` on
        # the suite's own user would not model this.
        from django.test import Client as DjangoClient
        from users.models import ObjectPermission

        other = User.objects.create_user(username="planned-rack-only")
        perm = ObjectPermission.objects.create(name="pr-only", actions=["view"])
        perm.object_types.set([ObjectType.objects.get_for_model(PlannedRack)])
        perm.users.add(other)
        client = DjangoClient()
        client.force_login(other)

        response = client.get(self._url(pr))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Invisible design", response.content.decode())

    def test_orphan_says_it_can_be_deleted(self):
        pr = PlannedRack.objects.create(name="Free", location=self.location, u_height=10)
        response = self.client.get(self._url(pr))
        self.assertHttpStatus(response, 200)
        self.assertIn("referencing_designs", response.context)
        self.assertEqual(list(response.context["referencing_designs"]), [])


class PlannedRackBulkDeleteGuardTest(TestCase):
    """The list view's "Delete Selected" must exist, and obey the same guards.

    The list renders NetBox's bulk buttons for any table with a checkbox
    column, and the form's action is ``get_viewname(model, "bulk_delete")``
    -- which resolves to ``None`` when no such view is registered, so the
    POST lands on ``/planned-racks/None`` and 404s (user report
    2026-09-22). Registering it is only half the fix: a realized or
    still-referenced planned rack must survive a bulk delete exactly as it
    survives the single one (D7 / T1.9).
    """

    user_permissions = (
        "netbox_rack_design.view_plannedrack",
        "netbox_rack_design.delete_plannedrack",
    )

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.location = Location.objects.create(
            name="Bulk Delete Location", slug="bulk-delete-location", site=cls.site
        )

    def _bulk_delete_url(self):
        return reverse("plugins:netbox_rack_design:plannedrack_bulk_delete")

    def _post(self, pks):
        return self.client.post(
            self._bulk_delete_url(),
            {"pk": [str(pk) for pk in pks], "_confirm": "true", "confirm": "true"},
        )

    def test_bulk_delete_orphans_succeeds(self):
        a = PlannedRack.objects.create(name="Bulk A", location=self.location, u_height=10)
        b = PlannedRack.objects.create(name="Bulk B", location=self.location, u_height=10)
        response = self._post([a.pk, b.pk])
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PlannedRack.objects.filter(pk__in=[a.pk, b.pk]).exists())

    def test_bulk_delete_referenced_rejected_and_nothing_is_deleted(self):
        orphan = PlannedRack.objects.create(name="Bulk orphan", location=self.location, u_height=10)
        held = PlannedRack.objects.create(name="Bulk held", location=self.location, u_height=10)
        design = make_design(title="Bulk referencing design", site=self.site)
        design.planned_racks.add(held)
        response = self._post([orphan.pk, held.pk])
        self.assertEqual(response.status_code, 302)
        self.assertTrue(PlannedRack.objects.filter(pk=held.pk).exists())
        self.assertTrue(PlannedRack.objects.filter(pk=orphan.pk).exists(),
                        "a refused batch deletes nothing -- same as the Design bulk guard")
        shown = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("Bulk referencing design" in m for m in shown))

    def test_bulk_delete_realized_rejected(self):
        real = Rack.objects.create(
            name="Bulk realized target", site=self.site, location=self.location
        )
        pr = PlannedRack.objects.create(
            name="Bulk realized", location=self.location, u_height=10, realized_rack=real,
        )
        response = self._post([pr.pk])
        self.assertEqual(response.status_code, 302)
        self.assertTrue(PlannedRack.objects.filter(pk=pr.pk).exists())
        shown = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("realized" in m.lower() for m in shown))


class TemplateBulkViewUrlsTest(TestCase):
    """Every list that shows bulk buttons must have the views behind them."""

    user_permissions = ("netbox_rack_design.view_template",)

    def test_bulk_urls_resolve(self):
        for viewname in (
            "plugins:netbox_rack_design:plannedrack_bulk_delete",
            "plugins:netbox_rack_design:plannedrack_bulk_edit",
            "plugins:netbox_rack_design:template_bulk_delete",
            "plugins:netbox_rack_design:templategroup_bulk_delete",
            "plugins:netbox_rack_design:templateplacement_bulk_delete",
            # The Import button on each list resolves through bulk_import --
            # missing, it rendered href="None" and 404'd exactly as the bulk
            # delete did.
            "plugins:netbox_rack_design:plannedrack_bulk_import",
            "plugins:netbox_rack_design:template_bulk_import",
            "plugins:netbox_rack_design:templategroup_bulk_import",
            "plugins:netbox_rack_design:templateplacement_bulk_import",
        ):
            with self.subTest(viewname=viewname):
                self.assertTrue(reverse(viewname))


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
        design_a = make_design(title="Design A", site=self.site)
        design_b = make_design(title="Design B", site=self.site)
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
        design = make_design(title="Solo design", site=self.site)
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
        design = make_design(title="Placement cascade design", site=self.site)
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
        cls.design = make_design(title="Filter design", site=cls.site)
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


class PlannedRackReferencedMessageTest(DjangoTestCase):
    """The delete refusal reads as a sentence for one design and for several.

    It said "Q3 Expansion (v1) still plan across it" when one design held
    the rack (found recording Part 9).
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.location = Location.objects.create(
            name="Msg Location", slug="msg-location", site=cls.site
        )

    def test_one_design_plans_and_loses_its_placements(self):
        from ..api.views import _planned_rack_referenced_rest_message
        from ..views import _planned_rack_referenced_message

        pr = PlannedRack.objects.create(name="One", location=self.location, u_height=10)
        make_design(title="Alone", site=self.site).planned_racks.add(pr)
        for message in (_planned_rack_referenced_message(pr),
                        _planned_rack_referenced_rest_message(pr)):
            self.assertIn("still plans across it", message)
            self.assertIn("lose its placements", message)

    def test_several_designs_plan_and_lose_their_placements(self):
        from ..views import _planned_rack_referenced_message

        pr = PlannedRack.objects.create(name="Two", location=self.location, u_height=10)
        make_design(title="First", site=self.site).planned_racks.add(pr)
        make_design(title="Second", site=self.site).planned_racks.add(pr)
        message = _planned_rack_referenced_message(pr)
        self.assertIn("still plan across it", message)
        self.assertIn("lose their placements", message)
