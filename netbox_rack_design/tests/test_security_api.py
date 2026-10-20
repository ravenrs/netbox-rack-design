"""
Security regression tests for the REST API (1.2.2).

Each test drives the ATTACK (an object-constrained ObjectPermission, a read-only
token, a cross-site rack id, malformed input) and asserts the API refuses it or
degrades to a 4xx -- and never 500s. Where NetBox core shows a related object's
name to someone who cannot open it (a nested parent, a ProtectedError message),
the tests assert the same: lists and pickers are restricted, names are not.
"""

from unittest import mock

from core.models import ObjectChange, ObjectType
from dcim.models import PowerFeed, PowerPanel, Rack, Site
from django.db import IntegrityError
from django.urls import reverse
from rest_framework import status
from users.models import ObjectPermission
from utilities.exceptions import PermissionsViolation
from utilities.testing import APITestCase, create_test_device

from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import (
    Design,
    DesignPlacement,
    DesignPowerFeed,
    DesignRackPower,
    PlannedRack,
    Template,
)
from .utils import create_dcim_environment, make_design

ADD = DesignPlacementKindChoices.KIND_ADD
APPROVED = DesignStatusChoices.STATUS_APPROVED
NS = "plugins-api:netbox_rack_design-api"


class ApiSecurityBase(APITestCase):
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
        cls.other_site = Site.objects.create(name="Other site", slug="other-site")
        cls.other_rack = Rack.objects.create(name="Other rack", site=cls.other_site)
        cls.other_device = create_test_device(
            "Other dev", site=cls.other_site, rack=cls.other_rack, position=1, face="front",
        )

    def grant(self, model, actions, constraints=None):
        permission = ObjectPermission(
            name=f"sec-api-{model.__name__}-{'-'.join(actions)}-{ObjectPermission.objects.count()}",
            actions=list(actions), constraints=constraints,
        )
        permission.save()
        permission.users.add(self.user)
        permission.object_types.add(ObjectType.objects.get_for_model(model))
        return permission

    def url(self, name, *args):
        return reverse(f"{NS}:{name}", args=args)

    def design(self, title, *, status=None, **kw):
        design = make_design(title=title, site=self.site, **kw)
        if status:
            design.status = status
            design.save()
        return design

    def post(self, url, body=None, **kw):
        return self.client.post(url, body or {}, format="json", **self.header, **kw)

    def _spy_clear_events(self):
        """``(seen, patcher)``: ``seen`` gets the size of the request's event queue
        each time the views clear it, so a test proves events really were queued
        by the rolled-back writes (a vacuous "nothing flushed" proves nothing)."""
        from core.signals import clear_events
        from netbox.context import events_queue

        seen = []

        class Spy:
            @staticmethod
            def send(sender, **kwargs):
                seen.append(len(events_queue.get()))
                return clear_events.send(sender=sender, **kwargs)

        return seen, mock.patch("netbox_rack_design.api.views.clear_events", Spy())

    def readonly_token(self):
        self.token.write_enabled = False
        self.token.save()


# --- 1. root / version are server-set (sequence stays writable) ------------------------------

class ServerSetLineageFieldsTest(ApiSecurityBase):

    def test_post_cannot_graft_into_another_version_group(self):
        self.add_permissions("netbox_rack_design.add_design", "netbox_rack_design.view_design")
        victim = self.design("Victim root")
        response = self.post(self.url("design-list"), {
            "title": "Graft", "sites": [self.site.pk], "root": victim.pk,
            "version": 7, "sequence": 3,
        })
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        created = Design.objects.get(title="Graft")
        self.assertIsNone(created.root_id)
        self.assertEqual(created.version, 1)
        # ``sequence`` is only an execution-order hint, editable in the HTML form
        # too: it stays writable over REST (it carries no graft risk).
        self.assertEqual(created.sequence, 3)

    def test_patch_cannot_change_root_or_version_but_sequence_stays_writable(self):
        self.add_permissions("netbox_rack_design.change_design", "netbox_rack_design.view_design")
        victim = self.design("Victim root")
        mine = self.design("Mine")
        response = self.client.patch(
            self.url("design-detail", mine.pk),
            {"root": victim.pk, "version": 9, "sequence": 999}, format="json", **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        mine.refresh_from_db()
        self.assertIsNone(mine.root_id)
        self.assertEqual(mine.version, 1)
        self.assertEqual(mine.sequence, 999)


# --- 2. chain / feeds: lists are restricted, the ancestor stack is not ------------

class ChainParityTest(ApiSecurityBase):
    """Like core: a restricted LIST (children) drops what the caller may not view;
    a chain of parents shows every name, as core's breadcrumbs do for a parent
    region the reader cannot open."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        make = lambda title, **kw: make_design(title=title, site=cls.site, **kw)  # noqa: E731
        cls.a = make("Secret A", status=APPROVED)
        cls.b = make("Secret B", status=APPROVED, based_on=cls.a)
        cls.c = make("Visible C", status=APPROVED, based_on=cls.b)
        cls.d = make("Visible D", based_on=cls.c)
        cls.hidden_child = make("Secret Child", based_on=cls.d)
        cls.visible_child = make("Visible Child", based_on=cls.d)

    def setUp(self):
        super().setUp()
        visible = [self.c.pk, self.d.pk, self.visible_child.pk]
        self.grant(Design, ["view"], {"pk__in": visible})

    def _chain(self, design=None):
        return self.client.get(self.url("design-chain", (design or self.d).pk), **self.header)

    def test_every_ancestor_is_listed_by_name(self):
        response = self._chain()
        self.assertHttpStatus(response, status.HTTP_200_OK)
        ancestors = response.data["ancestors"]
        self.assertEqual([a["id"] for a in ancestors], [self.a.pk, self.b.pk, self.c.pk])
        self.assertEqual([a["title"] for a in ancestors], ["Secret A", "Secret B", "Visible C"])

    def test_hidden_children_are_dropped(self):
        response = self._chain()
        self.assertEqual([c["id"] for c in response.data["children"]], [self.visible_child.pk])
        self.assertNotIn("Secret Child", response.content.decode())

    def test_the_refusal_names_the_ancestor_at_fault(self):
        Design.objects.filter(pk=self.b.pk).update(status=DesignStatusChoices.STATUS_DRAFT)
        refusal = self._chain().data["refusal"]
        self.assertEqual(refusal["kind"], "ancestor_not_approved")
        self.assertIn("Secret B", refusal["detail"])
        self.assertEqual(refusal["source_design"]["id"], self.b.pk)

    def test_the_refusal_kind_follows_the_ancestors_real_status(self):
        Design.objects.filter(pk=self.b.pk).update(status=DesignStatusChoices.STATUS_IMPLEMENTED)
        self.assertEqual(self._chain().data["refusal"]["kind"], "ancestor_implemented")

    def test_feeds_name_the_ancestor_that_owns_an_inherited_feed(self):
        DesignPowerFeed.objects.create(design=self.a, rack=self.racks[0], name="Feed A")
        response = self.client.get(
            self.url("design-feeds", self.d.pk) + f"?rack_id={self.racks[0].pk}", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        inherited = [f for f in response.data["planned"] if f.get("inherited")]
        self.assertEqual(len(inherited), 1)
        self.assertEqual(inherited[0]["design_id"], self.a.pk)
        self.assertIn("Secret A", inherited[0]["design_name"])

    def test_conflicts_name_the_ancestor_at_fault(self):
        Design.objects.filter(pk=self.b.pk).update(status=DesignStatusChoices.STATUS_DRAFT)
        self.d.racks.add(self.racks[0])
        response = self.client.get(self.url("design-conflicts", self.d.pk), **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        refusals = [c for c in response.data["conflicts"] if c["kind"] == "ancestor_not_approved"]
        self.assertTrue(refusals)
        self.assertEqual(refusals[0]["source_design_id"], self.b.pk)
        self.assertIn("Secret B", refusals[0]["source_design_name"])
        self.assertNotIn("source_design_restricted", refusals[0])


class ConflictsNameTheSourceTest(ApiSecurityBase):
    """An occupancy conflict names the ancestor at fault, viewable or not."""

    def test_unit_occupied_names_the_ancestor(self):
        grand = self.design("Secret grand", status=APPROVED)
        DesignPlacement.objects.create(
            design=grand, kind=ADD, device_type=self.device_type, target_rack=self.racks[0],
            target_position=10, target_face="front", proposed_name="up-srv")
        parent = make_design(title="Visible parent", site=self.site, based_on=grand, status=APPROVED)
        child = make_design(title="Visible child", site=self.site, based_on=parent)
        child.racks.add(self.racks[0])
        DesignPlacement.objects.create(
            design=child, kind=ADD, device_type=self.device_type, target_rack=self.racks[0],
            target_position=10, target_face="front", proposed_name="srv-mine")
        self.grant(Design, ["view"], {"pk__in": [parent.pk, child.pk]})
        response = self.client.get(self.url("design-conflicts", child.pk), **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        rows = [c for c in response.data["conflicts"] if c["kind"] == "unit_occupied"]
        self.assertEqual(len(rows), 1, response.data["conflicts"])
        self.assertEqual(rows[0]["source_design_id"], grand.pk)
        self.assertIn("Secret grand", rows[0]["source_design_name"])


class WritableLinkTest(ApiSecurityBase):
    """A writable link to a design (or one of its rows) resolves like core's
    WritableNestedSerializer: any existing object, whether or not the caller may
    view it (``PATCH device {"tenant": <pk of a tenant they cannot view>}`` works
    in core, and shows the tenant back). An unknown pk is still refused."""

    def setUp(self):
        super().setUp()
        self.hidden = self.design("Secret H", status=APPROVED)
        self.visible = self.design("Visible V", status=APPROVED)
        self.mine = self.design("Mine")
        self.grant(Design, ["view"], {"pk__in": [self.visible.pk, self.mine.pk]})

    def _patch(self, name, pk, body):
        return self.client.patch(self.url(name, pk), body, format="json", **self.header)

    def test_design_based_on(self):
        self.grant(Design, ["change"])
        missing = self._patch("design-detail", self.mine.pk, {"based_on": 10**9})
        self.assertHttpStatus(missing, status.HTTP_400_BAD_REQUEST)
        ok = self._patch("design-detail", self.mine.pk, {"based_on": self.hidden.pk})
        self.assertHttpStatus(ok, status.HTTP_200_OK)
        self.assertEqual(ok.data["based_on"]["id"], self.hidden.pk)
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.based_on_id, self.hidden.pk)

    def test_design_depends_on(self):
        self.grant(Design, ["add"])
        body = {"title": "New", "sites": [self.site.pk]}
        missing = self.post(self.url("design-list"), {**body, "depends_on": [10**9]})
        self.assertHttpStatus(missing, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Design.objects.filter(title="New").exists())
        ok = self.post(self.url("design-list"), {**body, "depends_on": [self.hidden.pk]})
        self.assertHttpStatus(ok, status.HTTP_201_CREATED)

    def test_placement_base_placement_may_be_a_row_of_an_ancestor_the_caller_cannot_view(self):
        self.grant(DesignPlacement, ["add", "view"])
        hidden_add = DesignPlacement.objects.create(
            design=self.hidden, kind=ADD, device_type=self.device_type,
            target_rack=self.racks[0], target_position=20, target_face="front")
        Design.objects.filter(pk=self.mine.pk).update(based_on=self.hidden)
        self.mine.racks.add(self.racks[0])
        move = {"design": self.mine.pk, "kind": "move", "target_rack": self.racks[0].pk,
                "target_position": 30, "target_face": "front"}
        ok = self.post(self.url("designplacement-list"), {**move, "base_placement": hidden_add.pk})
        self.assertHttpStatus(ok, status.HTTP_201_CREATED)
        missing = self.post(self.url("designplacement-list"), {**move, "base_placement": 10**9})
        self.assertHttpStatus(missing, status.HTTP_400_BAD_REQUEST)
        self.assertIn("base_placement", missing.data)

    def test_planned_feed_design(self):
        self.grant(DesignPowerFeed, ["add", "view"])
        feed = {"rack": self.racks[0].pk, "name": "Feed", "voltage": 230, "amperage": 16}
        missing = self.post(self.url("designpowerfeed-list"), {**feed, "design": 10**9})
        self.assertHttpStatus(missing, status.HTTP_400_BAD_REQUEST)
        ok = self.post(self.url("designpowerfeed-list"), {**feed, "design": self.hidden.pk})
        # The design is approved, hence frozen: that, not visibility, is what refuses it.
        self.assertHttpStatus(ok, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("does not exist", str(ok.data).lower())
        ok = self.post(self.url("designpowerfeed-list"), {**feed, "design": self.mine.pk})
        self.assertHttpStatus(ok, status.HTTP_201_CREATED)


class DesignCreateSiteChecksTest(ApiSecurityBase):
    """``POST /designs/`` has no pk yet, so ``Design.clean`` used to skip every
    check that reads the sites: a rack, a planned rack or a parent from another
    site went straight in."""

    def setUp(self):
        super().setUp()
        from dcim.models import Location

        self.grant(Design, ["add", "view"])
        self.far = Location.objects.create(name="Far", slug="far", site=self.other_site)
        self.near = Location.objects.create(name="Near", slug="near", site=self.site)

    def create(self, **extra):
        return self.post(self.url("design-list"), {
            "title": "Created", "sites": [self.site.pk], **extra})

    def test_a_rack_of_another_site_is_refused(self):
        response = self.create(racks=[self.other_rack.pk])
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("racks", response.data)
        self.assertFalse(Design.objects.filter(title="Created").exists())

    def test_a_planned_rack_of_another_site_is_refused(self):
        foreign = PlannedRack.objects.create(name="PR-far", u_height=42, location=self.far)
        response = self.create(planned_racks=[foreign.pk])
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("planned_racks", response.data)
        self.assertFalse(Design.objects.filter(title="Created").exists())

    def test_a_parent_sharing_no_site_is_refused(self):
        parent = make_design(title="Far parent", site=self.other_site, status=APPROVED)
        response = self.create(based_on=parent.pk)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("based_on", response.data)
        self.assertFalse(Design.objects.filter(title="Created").exists())

    def test_in_site_scope_and_a_parent_sharing_a_site_are_created(self):
        near = PlannedRack.objects.create(name="PR-near", u_height=42, location=self.near)
        parent = make_design(title="Near parent", sites=[self.site, self.other_site], status=APPROVED)
        response = self.create(
            racks=[self.racks[0].pk], planned_racks=[near.pk], based_on=parent.pk)
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        created = Design.objects.get(title="Created")
        self.assertEqual(list(created.racks.all()), [self.racks[0]])
        self.assertEqual(created.based_on_id, parent.pk)


class DeriveTest(ApiSecurityBase):
    """Derive: an approved design is clonable by every add_design holder (view or
    not); the new row must still satisfy the holder's add constraints, judged on
    the finished design (sites and racks copied)."""

    def test_an_approved_source_is_clonable_without_view(self):
        src = self.design("Secret source", status=APPROVED)
        self.grant(Design, ["add"])
        response = self.post(self.url("design-derive", src.pk), {"title": "Child"})
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertEqual(Design.objects.get(title="Child").based_on_id, src.pk)

    def test_a_draft_source_is_refused_and_named(self):
        draft = self.design("Secret draft")
        self.grant(Design, ["add"])
        response = self.post(self.url("design-derive", draft.pk))
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Secret draft", response.content.decode())

    def test_a_sites_constrained_add_permission_succeeds(self):
        src = self.design("Src", status=APPROVED)
        self.grant(Design, ["add"], {"sites__id": self.site.pk})
        response = self.post(self.url("design-derive", src.pk), {"title": "Child"})
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertEqual(list(Design.objects.get(title="Child").sites.all()), [self.site])

    def test_a_row_outside_the_add_constraint_is_rolled_back(self):
        src = self.design("Src", status=APPROVED)
        self.grant(Design, ["add"], {"title__in": ["Src", "Allowed"]})
        seen, spy = self._spy_clear_events()
        with spy:
            response = self.post(self.url("design-derive", src.pk), {"title": "Not allowed"})
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Design.objects.filter(title="Not allowed").exists())
        # the rolled-back create DID queue an event, and it was dropped
        self.assertTrue(seen and max(seen) > 0, seen)
        self.assertHttpStatus(
            self.post(self.url("design-derive", src.pk), {"title": "Allowed"}),
            status.HTTP_201_CREATED)

    def test_new_version_checks_the_add_constraint_on_the_clone(self):
        src = self.design("Src")
        self.grant(Design, ["add"], {"title__in": ["Src", "Allowed"]})
        self.grant(Design, ["view"], {"pk": src.pk})
        seen, spy = self._spy_clear_events()
        with spy:
            response = self.post(self.url("design-new-version", src.pk), {"title": "Not allowed"})
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Design.objects.filter(root=src).count(), 0)
        self.assertEqual(Design.objects.filter(title="Not allowed").count(), 0)
        self.assertTrue(seen and max(seen) > 0, seen)
        self.assertHttpStatus(
            self.post(self.url("design-new-version", src.pk), {"title": "Allowed"}),
            status.HTTP_201_CREATED)

    def test_rebase_checks_the_change_constraint_after_the_move(self):
        parent = self.design("Parent", status=APPROVED)
        new_parent = self.design("New parent", status=APPROVED)
        mine = make_design(title="Mine", site=self.site, based_on=parent)
        self.grant(Design, ["view"], {"pk__in": [parent.pk, new_parent.pk, mine.pk]})
        self.grant(Design, ["change"], {"based_on_id": parent.pk})
        seen, spy = self._spy_clear_events()
        with spy:
            response = self.post(self.url("design-rebase", mine.pk), {"based_on": new_parent.pk})
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        mine.refresh_from_db()
        self.assertEqual(mine.based_on_id, parent.pk)
        self.assertTrue(seen and max(seen) > 0, seen)


class CreatePlannedRackWriteCheckTest(ApiSecurityBase):

    def setUp(self):
        super().setUp()
        from dcim.models import Location

        self.add_permissions("netbox_rack_design.add_design", "netbox_rack_design.change_design",
                             "netbox_rack_design.view_design")
        self.d = self.design("Planning")
        self.location = Location.objects.create(
            name="Far hall", slug="far-hall", site=self.other_site)
        self.body = {"name": "PR[1-2]", "u_height": 42, "location_id": self.location.pk}

    def test_racks_outside_the_add_constraint_are_not_created_and_scope_is_untouched(self):
        self.grant(PlannedRack, ["add"], {"location__site__slug": self.site.slug})
        response = self.post(self.url("design-create-planned-rack", self.d.pk), self.body)
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertFalse(PlannedRack.objects.exists())
        self.assertEqual(list(self.d.sites.all()), [self.site])

    def test_a_user_without_add_plannedrack_gets_the_plain_permission_text(self):
        response = self.post(self.url("design-create-planned-rack", self.d.pk), self.body)
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertIn("do not have permission to add planned racks", str(response.data["detail"]))
        self.assertNotIn("constraints", str(response.data["detail"]))
        self.assertFalse(PlannedRack.objects.exists())

    def test_the_constraint_text_is_only_for_a_constraint_failure(self):
        self.grant(PlannedRack, ["add"], {"location__site__slug": self.site.slug})
        response = self.post(self.url("design-create-planned-rack", self.d.pk), self.body)
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertIn("constraints of your add permission", str(response.data["detail"]))

    def test_racks_inside_the_add_constraint_are_created_and_the_site_added(self):
        self.grant(PlannedRack, ["add"], {"location__site__slug": self.other_site.slug})
        response = self.post(self.url("design-create-planned-rack", self.d.pk), self.body)
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertEqual(PlannedRack.objects.count(), 2)
        self.assertIn(self.other_site, self.d.sites.all())


class PlannedRackNamePatternCapTest(ApiSecurityBase):
    """``R[1-999999999]`` used to be expanded into a list of a billion names
    before anything checked its size."""

    def setUp(self):
        super().setUp()
        from dcim.models import Location

        self.grant(PlannedRack, ["add"])
        self.add_permissions("netbox_rack_design.add_design", "netbox_rack_design.change_design",
                             "netbox_rack_design.view_design")
        self.d = self.design("Planning")
        self.location = Location.objects.create(name="Hall", slug="hall", site=self.site)

    def create(self, name):
        return self.post(self.url("design-create-planned-rack", self.d.pk), {
            "name": name, "u_height": 42, "location_id": self.location.pk})

    def test_a_huge_range_is_a_400_and_creates_nothing(self):
        for name in ("R[1-999999999]", "R[1-20][1-20]", "[a-z][a-z]", "R[1-100,150-250]"):
            with self.subTest(name=name):
                response = self.create(name)
                self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
                self.assertIn("more than 200 names", str(response.data["name"]))
        self.assertFalse(PlannedRack.objects.exists())

    def test_the_limit_is_inclusive(self):
        response = self.create("R[1-200]")
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertEqual(PlannedRack.objects.count(), 200)

    def test_an_ordinary_pattern_still_expands(self):
        response = self.create("R[1-3,7]")
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertEqual(
            sorted(PlannedRack.objects.values_list("name", flat=True)), ["R1", "R2", "R3", "R7"])


# --- 4. new-version needs view on the source ----------------------------------

class NewVersionSourceTest(ApiSecurityBase):

    def test_hidden_source_cannot_be_cloned(self):
        hidden = self.design("Secret source")
        mine = self.design("Mine")
        self.grant(Design, ["add"])
        self.grant(Design, ["view"], {"pk": mine.pk})
        response = self.post(self.url("design-new-version", hidden.pk))
        self.assertHttpStatus(response, status.HTTP_404_NOT_FOUND)
        self.assertEqual(Design.objects.filter(root=hidden).count(), 0)
        ok = self.post(self.url("design-new-version", mine.pk))
        self.assertHttpStatus(ok, status.HTTP_201_CREATED)


# --- 5. rebase ----------------------------------------------------------------

class RebaseTargetTest(ApiSecurityBase):

    def setUp(self):
        super().setUp()
        self.parent = self.design("Visible parent", status=APPROVED)
        self.mine = make_design(title="Mine", site=self.site, based_on=self.parent)
        self.hidden_approved = self.design("Secret approved", status=APPROVED)
        self.hidden_draft = self.design("Secret draft")
        self.grant(Design, ["change"], {"pk": self.mine.pk})
        self.grant(Design, ["view"], {"pk__in": [self.mine.pk, self.parent.pk]})

    def _rebase(self, target):
        return self.post(self.url("design-rebase", self.mine.pk), {"based_on": target.pk})

    def test_a_target_the_caller_cannot_view_is_a_valid_link(self):
        # The picker (HTML) lists only viewable designs; the REST body is a
        # writable link and resolves like any other.
        response = self._rebase(self.hidden_approved)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.based_on_id, self.hidden_approved.pk)

    def test_a_draft_target_is_refused_whoever_reads_the_error(self):
        response = self._rebase(self.hidden_draft)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Secret draft", response.content.decode())
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.based_on_id, self.parent.pk)

    def test_an_unknown_target_is_refused(self):
        response = self.post(self.url("design-rebase", self.mine.pk), {"based_on": 10**9})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_rebase_snapshots_for_the_changelog(self):
        new_parent = self.design("Visible other", status=APPROVED)
        self.grant(Design, ["view"], {"pk": new_parent.pk})
        response = self._rebase(new_parent)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        change = ObjectChange.objects.filter(
            changed_object_id=self.mine.pk, changed_object_type=ObjectType.objects.get_for_model(Design),
            action="update",
        ).latest("pk")
        self.assertEqual(change.prechange_data["based_on"], self.parent.pk)


# --- 3. racks are addressed inside design.sites -------------------------------

class RackScopeTest(ApiSecurityBase):

    def setUp(self):
        super().setUp()
        self.add_permissions(
            "netbox_rack_design.view_design", "netbox_rack_design.change_design",
            "netbox_rack_design.add_template", "netbox_rack_design.view_template",
        )
        self.d = self.design("Scope")
        panel = PowerPanel.objects.create(site=self.other_site, name="Other panel")
        self.foreign_feed = PowerFeed.objects.create(
            power_panel=panel, rack=self.other_rack, name="Other-A", voltage=230, amperage=32)

    def test_recompute_distribution_ignores_a_foreign_rack(self):
        body = {"design_id": self.d.pk, "racks": [{
            "rack_id": self.other_rack.pk,
            "front": [{"kind": "existing", "device_id": self.other_device.pk,
                       "u_position": 1, "face": "front"}],
        }]}
        response = self.post(self.url("design-recompute-distribution", self.d.pk), body)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertIsNone(response.data["distributions"][str(self.other_rack.pk)])
        self.assertIsNone(response.data["power"][str(self.other_rack.pk)])

    def test_power_source_refuses_a_foreign_rack(self):
        response = self.client.get(
            self.url("design-power-source", self.d.pk) + f"?rack_id={self.other_rack.pk}&kind=rack",
            **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_feeds_do_not_list_a_foreign_racks_real_feeds(self):
        response = self.client.get(
            self.url("design-feeds", self.d.pk) + f"?rack_id={self.other_rack.pk}", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data["real"], [])

    def test_copy_feeds_refuses_a_foreign_source_and_target(self):
        url = self.url("design-copy-feeds", self.d.pk)
        response = self.post(url, {"rack_id": self.racks[0].pk, "source_rack_id": self.other_rack.pk})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        response = self.post(url, {"rack_id": self.other_rack.pk, "source_rack_id": self.racks[0].pk})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(DesignPowerFeed.objects.filter(design=self.d).exists())

    def test_rack_power_refuses_a_foreign_rack(self):
        response = self.post(self.url("design-rack-power", self.d.pk),
                             {"rack_id": self.other_rack.pk, "power_config": {}})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(DesignRackPower.objects.exists())

    def test_preview_template_reports_a_foreign_rack_as_missing(self):
        template = Template.objects.create(name="T", u_height=42)
        key = f"r:{self.other_rack.pk}"
        response = self.post(self.url("design-preview-template", self.d.pk),
                             {"template": template.pk, "racks": [key]})
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertNotIn(key, response.data)
        self.assertEqual(response.data["skipped"][0]["reason"], "rack does not exist")

    def test_from_design_refuses_a_foreign_rack(self):
        response = self.post(self.url("template-from-design"), {
            "design": self.d.pk, "rack": f"r:{self.other_rack.pk}", "name": "X"})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Template.objects.filter(name="X").exists())

    def test_from_rack_needs_the_rack_to_be_viewable(self):
        self.grant(Rack, ["view"], {"pk": self.racks[0].pk})
        response = self.post(self.url("template-from-rack"), {
            "rack_id": self.other_rack.pk, "name": "Y"})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Template.objects.filter(name="Y").exists())
        # control: a rack the caller may view extracts
        response = self.post(self.url("template-from-rack"), {
            "rack_id": self.racks[0].pk, "name": "Y-ok"})
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertTrue(Template.objects.filter(name="Y-ok").exists())

    def test_preview_name_scopes_device_to_the_design_sites(self):
        # Planners hold no dcim perms: device_type/tenant resolve as before, but a
        # device must sit in one of the design's sites.
        url = self.url("design-preview-name", self.d.pk)
        response = self.post(url, {"device": self.other_device.pk})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        response = self.post(url, {
            "device": self.devices[0].pk, "device_type": self.device_type.pk,
            "tenant": self.tenant.pk})
        self.assertHttpStatus(response, status.HTTP_200_OK)


# --- 7. templates / groups in the previews ------------------------------------

class TemplateLookupTest(ApiSecurityBase):

    def test_hidden_template_and_group_are_not_found(self):
        from ..models import TemplateGroup

        self.add_permissions("netbox_rack_design.view_design", "netbox_rack_design.add_template")
        d = self.design("Tpl")
        d.racks.add(self.racks[0])
        hidden = Template.objects.create(name="Secret T", u_height=42)
        group = TemplateGroup.objects.create(name="Secret G")
        self.grant(Template, ["view"], {"pk": 0})
        self.grant(TemplateGroup, ["view"], {"pk": 0})
        url = self.url("design-preview-template", d.pk)
        response = self.post(url, {"template": hidden.pk, "racks": [f"r:{self.racks[0].pk}"]})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        response = self.post(url, {"group": group.pk, "racks": [f"r:{self.racks[0].pk}"]})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        response = self.post(self.url("template-from-design"), {
            "design": d.pk, "rack": f"r:{self.racks[0].pk}", "name": "Z", "group_id": group.pk})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        response = self.post(self.url("template-from-rack"), {
            "rack_id": self.racks[0].pk, "name": "Z2", "group_id": group.pk})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)


class TemplateExtractAddConstraintTest(ApiSecurityBase):
    """from-design / from-rack judge a constrained ``add_template`` permission on
    the finished row, like every other create."""

    def setUp(self):
        super().setUp()
        self.grant(Template, ["add"], {"name__startswith": "ok-"})
        self.grant(Design, ["view"])
        self.grant(Rack, ["view"])
        self.d = self.design("Extract")
        self.d.racks.add(self.racks[0])
        self.events = []

    def test_from_design_refuses_a_name_outside_the_constraint(self):
        body = {"design": self.d.pk, "rack": f"r:{self.racks[0].pk}"}
        response = self.post(self.url("template-from-design"), {**body, "name": "nope"})
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Template.objects.filter(name="nope").exists())
        response = self.post(self.url("template-from-design"), {**body, "name": "ok-1"})
        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertTrue(Template.objects.filter(name="ok-1").exists())

    def test_from_rack_refuses_a_name_outside_the_constraint(self):
        body = {"rack_id": self.racks[0].pk}
        response = self.post(self.url("template-from-rack"), {**body, "name": "nope"})
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Template.objects.filter(name="nope").exists())
        response = self.post(self.url("template-from-rack"), {**body, "name": "ok-2"})
        self.assertHttpStatus(response, status.HTTP_201_CREATED)

    def test_the_refusal_drops_the_queued_events(self):
        with mock.patch("netbox_rack_design.api.views.clear_events") as cleared:
            response = self.post(self.url("template-from-rack"), {
                "rack_id": self.racks[0].pk, "name": "nope"})
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        cleared.send.assert_called_once()


# --- 6. custom viewsets: token write check + restricted lookups ---------------

class CustomViewSetTokenTest(ApiSecurityBase):

    def test_read_only_token_cannot_write_user_state(self):
        d = self.design("Tok")
        writes = [
            (self.url("favoriteset-list"), {"name": "x"}),
            (self.url("favoritedevicetype-toggle"), {"device_type_id": self.device_type.pk}),
            (self.url("hiddendesignrack-toggle"), {"design_id": d.pk, "rack_id": self.racks[0].pk}),
            (self.url("hiddendesignrack-show-all"), {"design_id": d.pk}),
            (self.url("hiddendesignchassis-toggle"),
             {"design_id": d.pk, "chassis_id": self.devices[0].pk}),
        ]
        # control: with writes enabled the same routes are NOT refused (they may
        # 4xx for their own reasons, never 403 for the token)
        for url, body in writes:
            self.assertNotEqual(self.post(url, body).status_code, status.HTTP_403_FORBIDDEN, url)
        self.readonly_token()
        for url, body in writes:
            response = self.post(url, body)
            self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertHttpStatus(self.client.get(self.url("favoriteset-list"), **self.header),
                              status.HTTP_200_OK)
        self.assertHttpStatus(self.client.get(self.url("placement-fields"), **self.header),
                              status.HTTP_200_OK)

    def test_toggles_scope_design_rack_and_device_lookups(self):
        hidden = self.design("Hidden")
        visible = self.design("Visible")
        self.grant(Design, ["view"], {"pk": visible.pk})
        toggle_rack = self.url("hiddendesignrack-toggle")
        toggle_chassis = self.url("hiddendesignchassis-toggle")
        # a design the user may not view
        self.assertHttpStatus(
            self.post(toggle_rack, {"design_id": hidden.pk, "rack_id": self.racks[0].pk}),
            status.HTTP_400_BAD_REQUEST)
        self.assertHttpStatus(
            self.post(toggle_chassis, {"design_id": hidden.pk, "chassis_id": self.devices[0].pk}),
            status.HTTP_400_BAD_REQUEST)
        # a rack / device outside the design's sites
        self.assertHttpStatus(
            self.post(toggle_rack, {"design_id": visible.pk, "rack_id": self.other_rack.pk}),
            status.HTTP_400_BAD_REQUEST)
        self.assertHttpStatus(
            self.post(toggle_chassis, {"design_id": visible.pk, "chassis_id": self.other_device.pk}),
            status.HTTP_400_BAD_REQUEST)
        # inside the design's sites, with no dcim perms at all
        self.assertHttpStatus(
            self.post(toggle_rack, {"design_id": visible.pk, "rack_id": self.racks[0].pk}),
            status.HTTP_200_OK)
        self.assertHttpStatus(
            self.post(toggle_chassis, {"design_id": visible.pk, "chassis_id": self.devices[0].pk}),
            status.HTTP_200_OK)

    def test_device_type_power_needs_view_design_and_caps_ids(self):
        url = self.url("devicetypepower-list")
        response = self.client.get(url + f"?id={self.device_type.pk}", **self.header)
        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.add_permissions("netbox_rack_design.view_design")
        response = self.client.get(url + f"?id={self.device_type.pk}", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertIn(str(self.device_type.pk), response.data["results"])
        ids = "&".join(f"id={i}" for i in range(1, 502))
        response = self.client.get(url + "?" + ids, **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)


# --- 8. rolled-back previews send no events; serializer caps ------------------

class PreviewEventsTest(ApiSecurityBase):

    def setUp(self):
        super().setUp()
        self.add_permissions("netbox_rack_design.view_design")
        self.d = self.design("Events")

    def _layout(self):
        return {"design_id": self.d.pk, "racks": [{
            "rack_id": self.racks[0].pk,
            "front": [{"kind": "add", "device_type_id": self.device_type.pk,
                       "u_position": 30, "face": "front", "proposed_name": "ghost"}],
        }]}

    def test_recompute_distribution_queues_no_events(self):
        seen, spy = self._spy_clear_events()
        with spy, mock.patch("netbox.context_managers.flush_events") as flush:
            response = self.post(self.url("design-recompute-distribution", self.d.pk), self._layout())
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertFalse(DesignPlacement.objects.filter(design=self.d).exists())
        # the transient placement DID queue an event, and it was dropped
        self.assertTrue(seen and max(seen) > 0, seen)
        flush.assert_not_called()

    def _let_the_view_crash(self):
        # A raising engine is a 500. From NetBox 4.6 on the test client re-raises
        # the view's exception instead of returning that response; ask it not to.
        previous = self.client.raise_request_exception
        self.client.raise_request_exception = False
        self.addCleanup(setattr, self.client, "raise_request_exception", previous)

    def test_recompute_distribution_clears_events_when_the_engine_raises(self):
        self._let_the_view_crash()
        seen, spy = self._spy_clear_events()
        with spy, mock.patch(
            "netbox_rack_design.api.views.projection.project_rack", side_effect=RuntimeError("boom")
        ):
            response = self.post(self.url("design-recompute-distribution", self.d.pk), self._layout())
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertTrue(seen and max(seen) > 0, seen)
        self.assertFalse(DesignPlacement.objects.filter(design=self.d).exists())

    def test_preview_template_clears_events_when_the_engine_raises(self):
        self._let_the_view_crash()
        from ..models import TemplatePlacement

        self.add_permissions("netbox_rack_design.view_template")
        template = Template.objects.create(name="Boom", u_height=42)
        TemplatePlacement.objects.create(
            template=template, device_type=self.device_type, order=1, face="front")
        self.d.racks.add(self.racks[0])
        body = {"template": template.pk, "racks": [f"r:{self.racks[0].pk}"],
                "layout": self._layout()}
        seen, spy = self._spy_clear_events()
        with spy, mock.patch(
            "netbox_rack_design.api.views.stamping.compute_stamp", side_effect=RuntimeError("boom")
        ):
            response = self.post(self.url("design-preview-template", self.d.pk), body)
        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertTrue(seen and max(seen) > 0, seen)
        self.assertFalse(DesignPlacement.objects.filter(design=self.d).exists())

    def test_step_previews_clear_events_when_the_engine_raises(self):
        self._let_the_view_crash()
        cases = (
            ("design-simulate-steps", "post", "steps.simulate", {"steps": []}),
            ("design-auto-order", "post", "steps.auto_order", {"steps": []}),
            ("design-work-order", "get", "workorder.build", None),
        )
        for name, method, target, body in cases:
            seen, spy = self._spy_clear_events()
            with spy, mock.patch(
                f"netbox_rack_design.api.views.{target}", side_effect=RuntimeError("boom")
            ):
                if method == "post":
                    response = self.post(self.url(name, self.d.pk), body)
                else:
                    response = self.client.get(self.url(name, self.d.pk), **self.header)
            self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR, name)
            self.assertTrue(seen, f"{name}: the events were not cleared")

    def test_an_early_return_inside_a_preview_still_clears_events(self):
        """simulate-steps ``return``s from inside ``_preview_transaction`` on a
        malformed order: the rows AND the events the block queued go either way."""
        from ..steps import StepsValidationError

        def write_then_refuse(*args, **kwargs):
            Rack.objects.create(name="Phantom", site=self.site)
            raise StepsValidationError(["bad order"])

        seen, spy = self._spy_clear_events()
        with spy, mock.patch("netbox_rack_design.api.views.steps.simulate", side_effect=write_then_refuse):
            response = self.post(self.url("design-simulate-steps", self.d.pk), {"steps": []})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(seen and max(seen) > 0, seen)
        self.assertFalse(Rack.objects.filter(name="Phantom").exists())

    def test_a_real_write_does_queue_events(self):
        self.add_permissions("netbox_rack_design.change_design")
        self.d.racks.add(self.racks[0])
        with mock.patch("netbox.context_managers.flush_events") as flush:
            response = self.post(
                self.url("design-planned-feed", self.d.pk),
                {"rack_id": self.racks[0].pk, "name": "Feed X"})
        self.assertHttpStatus(response, status.HTTP_200_OK)
        flush.assert_called_once()

    def test_oversized_lists_are_refused(self):
        too_many = [{"rack_id": str(i)} for i in range(501)]
        response = self.post(self.url("design-recompute-distribution", self.d.pk),
                             {"design_id": self.d.pk, "racks": too_many})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        response = self.post(self.url("design-preview-template", self.d.pk),
                             {"template": 1, "racks": [f"r:{i}" for i in range(501)]})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_the_whole_body_has_a_total_items_cap(self):
        # 20 racks x 4 buckets x 100 items: every per-list cap is respected, the
        # total (8000) is not.
        self.add_permissions(
            "netbox_rack_design.change_design",
            "netbox_rack_design.add_designplacement", "netbox_rack_design.change_designplacement",
            "netbox_rack_design.delete_designplacement")
        item = {"kind": "existing", "device_id": self.devices[0].pk, "u_position": 1, "face": "front"}
        racks = [{"rack_id": str(self.racks[0].pk),
                  **{bucket: [item] * 100 for bucket in ("front", "rear", "other", "bays")}}
                 for _ in range(20)]
        for name in ("design-recompute-distribution", "design-save-layout"):
            response = self.post(self.url(name, self.d.pk), {"design_id": self.d.pk, "racks": racks})
            self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
            self.assertIn("in total", str(response.data))
        response = self.post(self.url("design-preview-template", self.d.pk), {
            "template": 1, "racks": [f"r:{self.racks[0].pk}"],
            "layout": {"design_id": self.d.pk, "racks": racks}})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("in total", str(response.data))

    def test_step_lists_have_a_total_ids_cap(self):
        ids = list(range(1, 1001))
        steps = [ids] * 6  # each list is under the per-list cap; 6000 ids in all
        for name in ("design-simulate-steps", "design-auto-order"):
            response = self.post(self.url(name, self.d.pk), {"steps": steps})
            self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
            self.assertIn("in total", str(response.data))
        self.add_permissions("netbox_rack_design.change_design")
        response = self.post(self.url("design-save-steps", self.d.pk), {
            "steps": [{"id": None, "title": "t", "placements": ids}] * 6})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("in total", str(response.data))


# --- 9. rack power / planned feed validation ----------------------------------

class PowerValidationTest(ApiSecurityBase):

    def setUp(self):
        super().setUp()
        self.add_permissions("netbox_rack_design.view_design", "netbox_rack_design.change_design")
        self.d = self.design("Power")

    def test_rack_power_rejects_a_malformed_config(self):
        url = self.url("design-rack-power", self.d.pk)
        for bad in (["x"], {"custom_fields": "nope"}):
            response = self.post(url, {"rack_id": self.racks[0].pk, "power_config": bad})
            self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(DesignRackPower.objects.filter(design=self.d).exists())

    def test_planned_feed_bounds(self):
        url = self.url("design-planned-feed", self.d.pk)
        for field, value in (("voltage", 10**9), ("amperage", 0), ("voltage", -5)):
            response = self.post(url, {"rack_id": self.racks[0].pk, "name": "F", field: value})
            self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(DesignPowerFeed.objects.filter(design=self.d).exists())

    def test_planned_feed_create_race_resolves_to_the_existing_row(self):
        """A concurrent request inserts the same feed between lookup and insert:
        the POST must update that row, never 500."""
        url = self.url("design-planned-feed", self.d.pk)
        original = DesignPowerFeed.full_clean
        raced = []

        def full_clean_then_race(feed, *args, **kwargs):
            original(feed, *args, **kwargs)
            if not raced and feed.pk is None:
                raced.append(True)
                DesignPowerFeed.objects.bulk_create([DesignPowerFeed(
                    design=feed.design, rack=feed.rack, name=feed.name, amperage=16)])

        with mock.patch.object(DesignPowerFeed, "full_clean", full_clean_then_race):
            response = self.post(url, {"rack_id": self.racks[0].pk, "name": "F", "amperage": 32})
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(DesignPowerFeed.objects.filter(design=self.d, name="F").count(), 1)
        self.assertEqual(DesignPowerFeed.objects.get(design=self.d, name="F").amperage, 32)

    def test_planned_feed_rest_serializer_bounds(self):
        self.add_permissions("netbox_rack_design.add_designpowerfeed",
                             "netbox_rack_design.view_designpowerfeed")
        response = self.post(self.url("designpowerfeed-list"), {
            "design": self.d.pk, "rack": self.racks[0].pk, "name": "Big", "amperage": 99999})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)


# --- 10. apply failures are 409, not 500 --------------------------------------

class ApplyFailureTest(ApiSecurityBase):

    def test_engine_exceptions_become_409_with_problems(self):
        self.add_permissions("netbox_rack_design.view_design", "netbox_rack_design.change_design")
        d = self.design("Apply", status=APPROVED)
        from django.core.exceptions import ValidationError

        for exc in (ValidationError("bad row"), IntegrityError("dup"), PermissionsViolation()):
            with mock.patch("netbox_rack_design.api.views.apply_engine.run", side_effect=exc):
                response = self.post(self.url("design-apply", d.pk))
            self.assertHttpStatus(response, status.HTTP_409_CONFLICT)
            self.assertFalse(response.data["ok"])
            self.assertTrue(response.data["problems"])

    def test_a_failed_apply_rolls_back_its_writes_and_drops_their_events(self):
        """The engine's own rollback (its transaction) plus the view's
        ``clear_events``: nothing from the half-finished run survives."""
        from django.core.exceptions import ValidationError
        from django.db import transaction

        self.add_permissions("netbox_rack_design.view_design", "netbox_rack_design.change_design")
        d = self.design("Apply", status=APPROVED)

        def half_done(design, user):
            with transaction.atomic():
                Rack.objects.create(name="Half-applied", site=self.site)
                raise ValidationError("bad row")

        seen, spy = self._spy_clear_events()
        with spy, mock.patch("netbox_rack_design.api.views.apply_engine.run", side_effect=half_done):
            response = self.post(self.url("design-apply", d.pk))
        self.assertHttpStatus(response, status.HTTP_409_CONFLICT)
        self.assertFalse(Rack.objects.filter(name="Half-applied").exists())
        self.assertTrue(seen and max(seen) > 0, seen)


# --- 11. messages name the objects involved, as core's do ---------------------

class MessagesNameTheObjectsTest(ApiSecurityBase):

    def test_save_layout_foreign_placement_message(self):
        self.add_permissions(
            "netbox_rack_design.change_design",
            "netbox_rack_design.add_designplacement", "netbox_rack_design.change_designplacement",
            "netbox_rack_design.delete_designplacement")
        other = self.design("Secret foreign")
        foreign = DesignPlacement.objects.create(
            design=other, kind=ADD, device_type=self.device_type,
            target_rack=self.racks[0], target_position=20, target_face="front")
        mine = self.design("Mine")
        self.grant(Design, ["view"], {"pk": mine.pk})
        response = self.post(self.url("design-save-layout", mine.pk), {
            "design_id": mine.pk, "racks": [{"rack_id": self.racks[0].pk, "front": [
                {"kind": "add", "placement_id": foreign.pk, "u_position": 20, "face": "front"}]}]})
        # Refused, and worded the way the engine always has: it says whose
        # placement this is, viewable or not.
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Secret foreign", response.content.decode())
        self.assertEqual(DesignPlacement.objects.filter(design=mine).count(), 0)

    def test_delete_guard_messages(self):
        self.add_permissions("netbox_rack_design.delete_design")
        parent = self.design("Parent")
        make_design(title="Secret child", site=self.site, based_on=parent)
        make_design(title="Secret version", site=self.site, root=parent, version=2)
        self.grant(Design, ["view"], {"pk": parent.pk})
        response = self.client.delete(self.url("design-detail", parent.pk), **self.header)
        # Core's ProtectedError names every dependent, viewable or not.
        self.assertHttpStatus(response, status.HTTP_409_CONFLICT)
        self.assertIn("Secret child", response.content.decode())
        self.assertIn("Secret version", response.content.decode())

    def test_planned_rack_referenced_message(self):
        from dcim.models import Location

        self.add_permissions("netbox_rack_design.delete_plannedrack",
                             "netbox_rack_design.view_plannedrack")
        location = Location.objects.create(name="L", slug="l", site=self.site)
        planned = PlannedRack.objects.create(name="PR", u_height=42, location=location)
        d = self.design("Secret referrer")
        d.planned_racks.add(planned)
        self.grant(Design, ["view"], {"pk": 0})
        response = self.client.delete(self.url("plannedrack-detail", planned.pk), **self.header)
        self.assertHttpStatus(response, status.HTTP_409_CONFLICT)
        self.assertIn("Secret referrer", response.content.decode())


class SaveLayoutLinkScopeTest(ApiSecurityBase):
    """save-layout resolves a placement or feed id like any writable link (any
    existing row), and a rack id only inside the design's sites."""

    def setUp(self):
        super().setUp()
        self.add_permissions(
            "netbox_rack_design.change_design",
            "netbox_rack_design.add_designplacement", "netbox_rack_design.change_designplacement",
            "netbox_rack_design.delete_designplacement")
        self.mine = self.design("Mine")
        self.other = self.design("Other design")
        self.grant(Design, ["view"], {"pk__in": [self.mine.pk]})

    def _layout(self, item, rack=None):
        return {"design_id": self.mine.pk, "racks": [{
            "rack_id": (rack or self.racks[0]).pk, "front": [item]}]}

    def _save(self, item, rack=None):
        return self.post(self.url("design-save-layout", self.mine.pk), self._layout(item, rack))

    def test_a_move_of_a_foreign_placement_outside_the_chain_is_refused(self):
        foreign = DesignPlacement.objects.create(
            design=self.other, kind=ADD, device_type=self.device_type,
            target_rack=self.racks[0], target_position=20, target_face="front")
        response = self._save({"kind": "move", "placement_id": foreign.pk,
                               "u_position": 30, "face": "front"})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(DesignPlacement.objects.filter(design=self.mine).count(), 0)

    def test_an_inherited_tile_of_a_hidden_ancestor_can_be_moved(self):
        # The editor drags an inherited tile: its placement belongs to an
        # ancestor the user may not be able to open, and the move still saves.
        ancestor = self.design("Secret ancestor", status=APPROVED)
        base = DesignPlacement.objects.create(
            design=ancestor, kind=ADD, device_type=self.device_type,
            target_rack=self.racks[0], target_position=20, target_face="front")
        Design.objects.filter(pk=self.mine.pk).update(based_on=ancestor)
        self.mine.racks.add(self.racks[0])
        response = self._save({"kind": "move", "placement_id": base.pk,
                               "u_position": 30, "face": "front"})
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertTrue(DesignPlacement.objects.filter(
            design=self.mine, base_placement=base, target_position=30).exists())

    def test_a_planned_power_feed_of_a_hidden_ancestor_binds(self):
        ancestor = self.design("Secret ancestor", status=APPROVED)
        feed = DesignPowerFeed.objects.create(design=ancestor, rack=self.racks[0], name="F")
        Design.objects.filter(pk=self.mine.pk).update(based_on=ancestor)
        self.mine.racks.add(self.racks[0])
        item = {"kind": "add", "device_type_id": self.device_type.pk, "u_position": 20,
                "face": "front", "proposed_name": "pdu-x", "planned_power_feed_id": feed.pk}
        self.assertHttpStatus(self._save(item), status.HTTP_200_OK)
        placed = DesignPlacement.objects.get(design=self.mine, proposed_name="pdu-x")
        self.assertEqual(placed.planned_power_feed_id, feed.pk)

    def test_planned_power_feed_outside_the_chain_is_refused(self):
        feed = DesignPowerFeed.objects.create(design=self.other, rack=self.racks[0], name="F")
        response = self._save({
            "kind": "add", "device_type_id": self.device_type.pk, "u_position": 20,
            "face": "front", "proposed_name": "pdu-y", "planned_power_feed_id": feed.pk})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("planned power feed", response.content.decode())
        self.assertFalse(DesignPlacement.objects.filter(design=self.mine).exists())

    def test_rack_outside_the_design_sites_is_unknown(self):
        item = {"kind": "add", "device_type_id": self.device_type.pk, "u_position": 20,
                "face": "front", "proposed_name": "x"}
        foreign = self._save(item, rack=self.other_rack)
        unknown = self.post(self.url("design-save-layout", self.mine.pk), {
            "design_id": self.mine.pk, "racks": [{"rack_id": 10**9, "front": [item]}]})
        self.assertHttpStatus(foreign, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            [e["detail"] for e in foreign.data["errors"]],
            [e["detail"] for e in unknown.data["errors"]])
        self.assertEqual(DesignPlacement.objects.filter(design=self.mine).count(), 0)
        # control: a rack of the design's site saves
        self.assertHttpStatus(self._save(item), status.HTTP_200_OK)

    def test_cycle_message_names_the_loop(self):
        a = self.design("Secret A", status=APPROVED)
        b = make_design(title="Secret B", site=self.site, based_on=a, status=APPROVED)
        c = make_design(title="Visible C", site=self.site, based_on=b)
        self.grant(Design, ["view"], {"pk__in": [a.pk, c.pk]})
        self.grant(Design, ["change"], {"pk": a.pk})
        response = self.client.patch(
            self.url("design-detail", a.pk), {"based_on": c.pk}, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Secret B", response.content.decode())
        a.refresh_from_db()
        self.assertIsNone(a.based_on_id)


# --- 12. snapshot() before mutating -------------------------------------------

class SnapshotTest(ApiSecurityBase):

    def _prechange(self, obj):
        return ObjectChange.objects.filter(
            changed_object_id=obj.pk, changed_object_type=ObjectType.objects.get_for_model(obj),
            action="update",
        ).latest("pk").prechange_data

    def test_planned_feed_update(self):
        self.add_permissions("netbox_rack_design.change_design", "netbox_rack_design.view_design")
        d = self.design("Snap")
        feed = DesignPowerFeed.objects.create(design=d, rack=self.racks[0], name="F", amperage=16)
        response = self.post(self.url("design-planned-feed", d.pk),
                             {"rack_id": self.racks[0].pk, "name": "F", "amperage": 32})
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(self._prechange(feed)["amperage"], 16)

    def test_copy_feeds_update(self):
        self.add_permissions("netbox_rack_design.change_design", "netbox_rack_design.view_design")
        d = self.design("Snap copy")
        panel = PowerPanel.objects.create(site=self.site, name="P")
        PowerFeed.objects.create(power_panel=panel, rack=self.racks[0], name="Rack 1-A",
                                 voltage=230, amperage=32)
        existing = DesignPowerFeed.objects.create(
            design=d, rack=self.racks[1], name="Rack 2-A", amperage=16)
        response = self.post(self.url("design-copy-feeds", d.pk), {
            "rack_id": self.racks[1].pk, "source_rack_id": self.racks[0].pk})
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(self._prechange(existing)["amperage"], 16)

    def test_save_layout_reposition(self):
        self.add_permissions(
            "netbox_rack_design.change_design", "netbox_rack_design.view_design",
            "netbox_rack_design.add_designplacement", "netbox_rack_design.change_designplacement",
            "netbox_rack_design.delete_designplacement")
        d = self.design("Snap layout")
        placement = DesignPlacement.objects.create(
            design=d, kind=ADD, device_type=self.device_type, target_rack=self.racks[0],
            target_position=20, target_face="front")
        response = self.post(self.url("design-save-layout", d.pk), {
            "design_id": d.pk, "racks": [{"rack_id": self.racks[0].pk, "front": [
                {"kind": "add", "placement_id": placement.pk, "u_position": 25, "face": "front"}]}]})
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(float(self._prechange(placement)["target_position"]), 20.0)

    def test_rerun_naming(self):
        self.add_permissions("netbox_rack_design.change_design", "netbox_rack_design.view_design")
        d = self.design("Snap naming")
        d.racks.add(self.racks[1])
        mine = DesignPlacement.objects.create(
            design=d, kind=ADD, device_type=self.device_type, target_rack=self.racks[1],
            target_position=10, proposed_name="dup-name")
        peer = self.design("Peer")
        peer.racks.add(self.racks[1])
        DesignPlacement.objects.create(
            design=peer, kind=ADD, device_type=self.device_type, target_rack=self.racks[1],
            target_position=20, proposed_name="dup-name")
        response = self.post(self.url("design-rerun-naming", d.pk), {"placement_ids": [mine.pk]})
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(self._prechange(mine)["proposed_name"], "dup-name")


# --- 13. malformed input is a 400, not a 500 ----------------------------------

class MalformedInputTest(ApiSecurityBase):

    def test_derive_and_new_version_bodies(self):
        self.add_permissions("netbox_rack_design.add_design", "netbox_rack_design.view_design")
        d = self.design("Src", status=APPROVED)
        for name in ("design-derive", "design-new-version"):
            url = self.url(name, d.pk)
            for body in ({"title": 5}, {"title": ["x"]}, {"title": "x" * 300}, ["not", "a", "dict"]):
                response = self.post(url, body)
                self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)

    def test_rerun_naming_ids(self):
        self.add_permissions("netbox_rack_design.view_design")
        d = self.design("Naming")
        url = self.url("design-rerun-naming-preview", d.pk)
        for body in ({"placement_ids": ["a"]}, {"placement_ids": [None]}, ["x"]):
            self.assertHttpStatus(self.post(url, body), status.HTTP_400_BAD_REQUEST)

    def test_hidden_lists_and_favorite_pk(self):
        for name in ("hiddendesignrack-list", "hiddendesignchassis-list"):
            response = self.client.get(self.url(name) + "?design_id=abc", **self.header)
            self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        response = self.client.patch(self.url("favoriteset-detail", "abc"), {"name": "x"},
                                     format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_404_NOT_FOUND)
        response = self.client.delete(self.url("favoriteset-detail", "abc"), **self.header)
        self.assertHttpStatus(response, status.HTTP_404_NOT_FOUND)
