"""
Security regression tests for the HTML views (1.2.2).

Each test drives the ATTACK -- an object-constrained ObjectPermission, a hand-posted
pk, a hand-edited bulk form -- and asserts the page refuses, redacts or degrades to
a message, never discloses another design's title and never 500s.

Visibility rule (NetBox core parity), shared with REST/GraphQL: a DIRECT link
(based_on, depends_on) keeps its title; a LIST (children, "required by",
ancestor stack, chain-health rows, hover source names, rebase picker, elevation
browser) shows only designs the reader may view; a hidden ANCESTOR stays in the
stack as "(restricted design)".
"""

from unittest import mock

from core.models import ObjectChange, ObjectType
from django.contrib.messages import get_messages
from django.db import IntegrityError
from django.urls import reverse
from users.models import ObjectPermission
from utilities.exceptions import PermissionsViolation
from utilities.testing import TestCase

from .. import projection
from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignPlacement, DesignPowerFeed, PlannedRack
from .utils import create_dcim_environment, make_design

ADD = DesignPlacementKindChoices.KIND_ADD
APPROVED = DesignStatusChoices.STATUS_APPROVED
IMPLEMENTED = DesignStatusChoices.STATUS_IMPLEMENTED
RESTRICTED = projection.RESTRICTED_DESIGN_LABEL
NS = "plugins:netbox_rack_design"


class ViewSecurityBase(TestCase):
    """No blanket permissions: each test grants exactly what it needs."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.device_type = env["device_type"]
        cls.device_role = env["device_role"]

    def grant(self, model, actions, constraints=None):
        permission = ObjectPermission(
            name=f"sec-view-{model.__name__}-{'-'.join(actions)}-{ObjectPermission.objects.count()}",
            actions=list(actions), constraints=constraints,
        )
        permission.save()
        permission.users.add(self.user)
        permission.object_types.add(ObjectType.objects.get_for_model(model))
        return permission

    def can_view_only(self, *designs):
        self.grant(Design, ["view"], {"pk__in": [d.pk for d in designs]})

    def make(self, title, **kw):
        return make_design(title=title, site=self.site, **kw)

    def add_placement(self, design, position=10, name="srv"):
        return DesignPlacement.objects.create(
            design=design, kind=ADD, device_type=self.device_type,
            target_rack=self.racks[0], target_position=position,
            target_face="front", proposed_name=name,
        )

    def message_texts(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def url(self, name, *args):
        return reverse(f"{NS}:{name}", args=args)


# --- 1. Elevation browser -------------------------------------------------------

class ElevationBrowserRestrictedTest(ViewSecurityBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.mine = cls.make_in("Visible plan")
        cls.secret = cls.make_in("Secret plan", status=APPROVED)

    @classmethod
    def make_in(cls, title, **kw):
        design = make_design(title=title, site=cls.site, **kw)
        DesignPlacement.objects.create(
            design=design, kind=ADD, device_type=cls.device_type,
            target_rack=cls.racks[0], target_position=5, target_face="front",
            proposed_name=f"srv-{design.pk}",
        )
        return design

    def test_rows_and_filter_options_hold_only_viewable_designs(self):
        self.can_view_only(self.mine)
        response = self.client.get(self.url("elevation_browser"))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, "Visible plan")
        self.assertNotContains(response, "Secret plan")

    def test_filtering_by_a_hidden_design_id_shows_nothing(self):
        self.can_view_only(self.mine)
        response = self.client.get(self.url("elevation_browser"), {"design": self.secret.pk})
        self.assertHttpStatus(response, 200)
        self.assertNotContains(response, "Secret plan")


# --- 2. Design page lists --------------------------------------------------------

class DesignPageListsTest(ViewSecurityBase):
    """Chain A(hidden, implemented) <- B(visible parent) <- C (the page)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.a = cls.make_chain("Secret ancestor A", status=IMPLEMENTED)
        cls.b = cls.make_chain("Visible parent B", status=APPROVED, based_on=cls.a)
        cls.c = cls.make_chain("Page design C", based_on=cls.b)
        cls.hidden_child = cls.make_chain("Secret child", based_on=cls.c)
        cls.visible_child = cls.make_chain("Visible child", based_on=cls.c)
        cls.hidden_dependent = cls.make_chain("Secret dependent")
        cls.hidden_dependent.depends_on.add(cls.c)
        cls.visible_dependent = cls.make_chain("Visible dependent")
        cls.visible_dependent.depends_on.add(cls.c)
        cls.secret_dependency = cls.make_chain("Secret dependency")
        cls.c.depends_on.add(cls.secret_dependency)

    @classmethod
    def make_chain(cls, title, **kw):
        return make_design(title=title, site=cls.site, **kw)

    def setUp(self):
        super().setUp()
        self.can_view_only(self.b, self.c, self.visible_child, self.visible_dependent)
        self.response = self.client.get(self.url("design", self.c.pk))

    def test_page_renders(self):
        self.assertHttpStatus(self.response, 200)

    def test_hidden_ancestor_is_a_placeholder_that_keeps_chain_length(self):
        self.assertNotContains(self.response, "Secret ancestor A")
        self.assertContains(self.response, RESTRICTED)
        self.assertContains(self.response, "Visible parent B")
        # the stack still has two entries: the placeholder and the parent
        self.assertEqual(len(self.response.context["chain_ancestors"]), 2)
        self.assertTrue(self.response.context["chain_ancestors"][0]["restricted"])
        self.assertFalse(self.response.context["chain_ancestors"][1]["restricted"])

    def test_hidden_ancestor_status_does_not_surface_as_an_alert(self):
        self.assertNotContains(self.response, "is marked Implemented")

    def test_children_list_drops_hidden_designs(self):
        self.assertContains(self.response, "Visible child")
        self.assertNotContains(self.response, "Secret child")

    def test_required_by_list_drops_hidden_designs(self):
        self.assertContains(self.response, "Visible dependent")
        self.assertNotContains(self.response, "Secret dependent")

    def test_direct_links_keep_their_title(self):
        # depends_on is a direct link, like based_on (Parent row) above.
        self.assertContains(self.response, "Secret dependency")

    def test_visible_ancestor_implemented_still_alerts(self):
        self.b.status = IMPLEMENTED
        self.b.save()
        self.can_view_only(self.b)
        response = self.client.get(self.url("design", self.c.pk))
        self.assertContains(response, "is marked Implemented")


class EditorHoverNamesTest(ViewSecurityBase):
    """The hover card names the design an inherited tile comes from."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.a = make_design(title="Secret origin A", site=cls.site)
        cls.a.racks.add(cls.racks[0])
        cls.upstream = DesignPlacement.objects.create(
            design=cls.a, kind=ADD, device_type=cls.device_type,
            target_rack=cls.racks[0], target_position=10, target_face="front",
            proposed_name="from-a",
        )
        cls.a.status = APPROVED
        cls.a.save()
        cls.b = make_design(title="Visible mid B", site=cls.site, based_on=cls.a, status=APPROVED)
        cls.b.racks.add(cls.racks[0])
        cls.c = make_design(title="Editing C", site=cls.site, based_on=cls.b)
        cls.c.racks.add(cls.racks[0])

    def test_hidden_source_design_is_named_restricted_in_widgets_and_markup(self):
        self.can_view_only(self.b, self.c)
        response = self.client.get(self.url("design_editor_default", self.c.pk))
        self.assertHttpStatus(response, 200)
        widget = next(
            w for w in response.context["all_rack_blocks"][0]["widgets"]
            if w["placement_id"] == self.upstream.pk
        )
        self.assertTrue(widget["inherited"])
        self.assertEqual(widget["source_design_name"], RESTRICTED)
        self.assertNotContains(response, "Secret origin A")
        self.assertContains(response, f'data-source-design-name="{RESTRICTED}"')

    def test_elevation_page_hover_is_redacted_too(self):
        self.can_view_only(self.b, self.c)
        response = self.client.get(self.url("design_elevation", self.c.pk))
        self.assertHttpStatus(response, 200)
        self.assertNotContains(response, "Secret origin A")

    def test_viewable_source_design_keeps_its_name(self):
        self.can_view_only(self.a, self.b, self.c)
        response = self.client.get(self.url("design_editor_default", self.c.pk))
        self.assertContains(response, 'data-source-design-name="Secret origin A')

    def test_hidden_implemented_ancestor_conflict_is_redacted(self):
        self.a.status = IMPLEMENTED
        self.a.save()
        self.can_view_only(self.b, self.c)
        response = self.client.get(self.url("design_editor_default", self.c.pk))
        self.assertHttpStatus(response, 200)
        self.assertNotContains(response, "Secret origin A")
        self.assertTrue(response.context["chain_conflicts"])


# --- chain-health report ---------------------------------------------------------

class ChainHealthRedactionTest(ViewSecurityBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.root = make_design(title="Secret root R", site=cls.site, status=IMPLEMENTED)
        cls.mid = make_design(title="Secret mid M", site=cls.site, status=APPROVED, based_on=cls.root)
        cls.leaf = make_design(title="Visible leaf L", site=cls.site, based_on=cls.mid)
        cls.parent_hidden = make_design(title="Secret draft parent P", site=cls.site)
        cls.child_of_hidden = make_design(
            title="Visible child of P", site=cls.site, based_on=cls.parent_hidden,
        )

    def test_hidden_non_parent_ancestor_is_named_restricted(self):
        self.can_view_only(self.leaf)
        response = self.client.get(self.url("design_chain_health"))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, "Visible leaf L")
        self.assertNotContains(response, "Secret root R")
        self.assertContains(response, RESTRICTED)

    def test_the_designs_own_parent_is_a_direct_link_and_keeps_its_title(self):
        self.can_view_only(self.child_of_hidden)
        response = self.client.get(self.url("design_chain_health"))
        self.assertContains(response, "Secret draft parent P")


# --- 3. Rebase ---------------------------------------------------------------------

class RebaseRestrictedTest(ViewSecurityBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.target = make_design(title="Visible target", site=cls.site, status=APPROVED)
        cls.secret_target = make_design(title="Secret target", site=cls.site, status=APPROVED)
        cls.current = make_design(title="Current parent", site=cls.site, status=APPROVED)
        cls.child = make_design(title="Child", site=cls.site, based_on=cls.current)

    def setUp(self):
        super().setUp()
        self.can_view_only(self.target, self.current, self.child)
        self.grant(Design, ["change"])

    def test_picker_offers_only_viewable_designs(self):
        response = self.client.get(self.url("design_rebase", self.child.pk))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, "Visible target")
        self.assertNotContains(response, "Secret target")

    def test_posting_a_hidden_design_is_not_a_valid_choice(self):
        response = self.client.post(
            self.url("design_rebase", self.child.pk), {"based_on": self.secret_target.pk},
        )
        self.assertHttpStatus(response, 200)
        self.assertNotContains(response, "Secret target")
        self.child.refresh_from_db()
        self.assertEqual(self.child.based_on_id, self.current.pk)

    def test_rebase_takes_a_snapshot_for_the_changelog(self):
        response = self.client.post(
            self.url("design_rebase", self.child.pk), {"based_on": self.target.pk},
        )
        self.assertHttpStatus(response, 302)
        change = ObjectChange.objects.filter(
            changed_object_id=self.child.pk,
            changed_object_type=ObjectType.objects.get_for_model(Design), action="update",
        ).first()
        self.assertIsNotNone(change)
        self.assertIsNotNone(change.prechange_data)


# --- 4. derive / new-version ---------------------------------------------------------

class CloneViewsSourceTest(ViewSecurityBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.secret = make_design(title="Secret source", site=cls.site, status=APPROVED)
        cls.mine = make_design(title="Visible source", site=cls.site, status=APPROVED)

    def setUp(self):
        super().setUp()
        # add_design everywhere; view_design only on one design.
        self.grant(Design, ["add"])
        self.can_view_only(self.mine)

    def test_new_version_of_a_hidden_design_is_404(self):
        for method in (self.client.get, self.client.post):
            response = method(self.url("design_new_version", self.secret.pk), {"title": "Stolen"})
            self.assertHttpStatus(response, 404)
        self.assertFalse(Design.objects.filter(title="Stolen").exists())

    def test_derive_from_a_hidden_design_is_404(self):
        for method in (self.client.get, self.client.post):
            response = method(self.url("design_derive", self.secret.pk), {"title": "Stolen"})
            self.assertHttpStatus(response, 404)
        self.assertFalse(Design.objects.filter(title="Stolen").exists())

    def test_viewable_source_still_works(self):
        response = self.client.post(self.url("design_new_version", self.mine.pk), {"title": "Mine v2"})
        self.assertHttpStatus(response, 302)
        self.assertTrue(Design.objects.filter(title="Mine v2").exists())


class CloneViewsAddConstraintTest(ViewSecurityBase):
    """An add permission constrained to status=approved can read an approved
    source but must not mint the DRAFT clone it does not cover."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.source = make_design(title="Approved source", site=cls.site, status=APPROVED)

    def setUp(self):
        super().setUp()
        self.grant(Design, ["view"])
        self.grant(Design, ["add"], {"status": "approved"})

    def test_new_version_outside_the_add_constraint_is_rolled_back(self):
        before = Design.objects.count()
        response = self.client.post(self.url("design_new_version", self.source.pk), {"title": "Draft clone"})
        self.assertHttpStatus(response, 302)
        self.assertEqual(Design.objects.count(), before)
        self.assertTrue(self.message_texts(response))

    def test_derive_outside_the_add_constraint_is_rolled_back(self):
        before = Design.objects.count()
        response = self.client.post(self.url("design_derive", self.source.pk), {"title": "Draft child"})
        self.assertHttpStatus(response, 302)
        self.assertEqual(Design.objects.count(), before)
        self.assertTrue(self.message_texts(response))


# --- 5. apply ---------------------------------------------------------------------------

class ApplyViewErrorsTest(ViewSecurityBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.design = make_design(title="Apply me", site=cls.site, status=APPROVED)

    def setUp(self):
        super().setUp()
        self.grant(Design, ["view", "change"])

    def post_with(self, error):
        with mock.patch("netbox_rack_design.views.apply_engine.run", side_effect=error):
            return self.client.post(self.url("design_apply", self.design.pk))

    def assert_message_and_redirect(self, response):
        self.assertHttpStatus(response, 302)
        self.assertEqual(response.url, self.design.get_absolute_url())
        self.assertTrue(self.message_texts(response))

    def test_permissions_violation_is_a_message(self):
        self.assert_message_and_redirect(self.post_with(PermissionsViolation))

    def test_integrity_error_is_a_message(self):
        self.assert_message_and_redirect(self.post_with(IntegrityError("duplicate key")))

    def test_validation_error_is_a_message(self):
        from django.core.exceptions import ValidationError
        self.assert_message_and_redirect(self.post_with(ValidationError("device not allowed")))

    def test_integrity_error_detail_stays_out_of_the_message(self):
        response = self.post_with(IntegrityError("duplicate key violates constraint secret_idx"))
        self.assertNotIn("secret_idx", " ".join(self.message_texts(response)))


# --- 6. guard messages ---------------------------------------------------------------------

class GuardMessagesNameNoHiddenDesignTest(ViewSecurityBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.parent = make_design(title="Visible parent", site=cls.site, status=APPROVED)
        cls.secret_child = make_design(title="Secret child", site=cls.site, based_on=cls.parent)
        cls.root = make_design(title="Visible root", site=cls.site)
        cls.secret_version = make_design(title="Secret version", site=cls.site, root=cls.root, version=2)

    def setUp(self):
        super().setUp()
        self.can_view_only(self.parent, self.root)
        self.grant(Design, ["delete"])

    def joined(self, response):
        return " ".join(self.message_texts(response))

    def test_delete_children_guard(self):
        response = self.client.post(self.url("design_delete", self.parent.pk), {"confirm": "true"})
        self.assertHttpStatus(response, 302)
        text = self.joined(response)
        self.assertIn("Cannot delete", text)
        self.assertNotIn("Secret child", text)
        self.assertIn("1 design(s) you cannot view", text)
        self.assertTrue(Design.objects.filter(pk=self.parent.pk).exists())

    def test_delete_versions_guard(self):
        response = self.client.post(self.url("design_delete", self.root.pk), {"confirm": "true"})
        text = self.joined(response)
        self.assertNotIn("Secret version", text)
        self.assertIn("1 design(s) you cannot view", text)

    def test_bulk_delete_guards(self):
        response = self.client.post(
            self.url("design_bulk_delete"),
            {"pk": [self.parent.pk, self.root.pk], "confirm": "true", "_confirm": "true"},
        )
        self.assertHttpStatus(response, 302)
        text = self.joined(response)
        self.assertNotIn("Secret child", text)
        self.assertNotIn("Secret version", text)

    def test_hidden_design_being_deleted_is_not_named_by_its_own_guard(self):
        # The user may delete everything but only view the designs above: the guard
        # for a design outside that set must not print its title.
        hidden = make_design(title="Hidden deletable", site=self.site, status=APPROVED)
        make_design(title="Its child", site=self.site, based_on=hidden)
        response = self.client.post(
            self.url("design_bulk_delete"),
            {"pk": [hidden.pk], "confirm": "true", "_confirm": "true"},
        )
        text = self.joined(response)
        self.assertIn("Cannot delete", text)
        self.assertNotIn("Hidden deletable", text)
        self.assertNotIn("Its child", text)

    def test_planned_rack_referenced_guard(self):
        planned = PlannedRack.objects.create(name="PR1", location=self._location())
        secret = make_design(title="Secret planner", site=self.site)
        secret.planned_racks.add(planned)
        self.grant(PlannedRack, ["delete", "view"])
        response = self.client.post(self.url("plannedrack_delete", planned.pk), {"confirm": "true"})
        text = self.joined(response)
        self.assertIn("Cannot delete", text)
        self.assertNotIn("Secret planner", text)
        self.assertIn("1 design(s) you cannot view", text)

    def _location(self):
        from dcim.models import Location
        return Location.objects.create(name="Loc", slug="loc", site=self.site)

    def test_frozen_bulk_placement_delete_names_no_hidden_design(self):
        secret = make_design(title="Secret frozen", site=self.site, status=APPROVED)
        placement = DesignPlacement.objects.create(
            design=secret, kind=ADD, device_type=self.device_type,
            target_rack=self.racks[0], target_position=3, target_face="front", proposed_name="x",
        )
        self.grant(DesignPlacement, ["delete", "view"])
        response = self.client.post(
            self.url("designplacement_bulk_delete"),
            {"pk": [placement.pk], "confirm": "true", "_confirm": "true"},
        )
        text = self.joined(response)
        self.assertNotIn("Secret frozen", text)
        self.assertIn("you cannot view", text)

    def test_feed_add_with_a_hidden_frozen_design_names_nothing(self):
        secret = make_design(title="Secret frozen feed design", site=self.site, status=APPROVED)
        self.grant(DesignPowerFeed, ["add", "view"])
        response = self.client.post(self.url("designpowerfeed_add"), {
            "design": secret.pk, "rack": self.racks[0].pk, "name": "F1",
            "supply": "ac", "phase": "single-phase", "voltage": 230, "amperage": 16,
        })
        body = response.content.decode() + " ".join(self.message_texts(response))
        self.assertNotIn("Secret frozen feed design", body)
        self.assertFalse(DesignPowerFeed.objects.filter(design=secret).exists())


class BulkPkValidationTest(ViewSecurityBase):

    def setUp(self):
        super().setUp()
        for model in (Design, DesignPlacement, DesignPowerFeed, PlannedRack):
            self.grant(model, ["view", "change", "delete"])

    def test_non_integer_pk_is_a_message_not_a_500(self):
        for name in (
            "design_bulk_delete", "design_bulk_edit",
            "designplacement_bulk_delete", "designplacement_bulk_edit",
            "designpowerfeed_bulk_delete", "designpowerfeed_bulk_edit",
            "plannedrack_bulk_delete", "plannedrack_bulk_edit",
            "designgroup_bulk_delete", "template_bulk_delete",
        ):
            with self.subTest(view=name):
                response = self.client.post(
                    self.url(name), {"pk": ["abc"], "confirm": "true", "_confirm": "true"},
                )
                self.assertHttpStatus(response, 302)
                self.assertTrue(self.message_texts(response))


# --- 7. placement form shows the re-parent guard ------------------------------------------

class PlacementFormReparentTest(ViewSecurityBase):

    def test_moving_a_placement_out_of_an_approved_design_shows_the_error(self):
        approved = make_design(title="Frozen home", site=self.site)
        placement = self.add_placement(approved, name="pinned")
        approved.status = APPROVED
        approved.save()
        other = make_design(title="Other draft", site=self.site)
        self.grant(Design, ["view"])
        self.grant(DesignPlacement, ["view", "change"])
        response = self.client.post(self.url("designplacement_edit", placement.pk), {
            "design": other.pk, "kind": "add", "device_type": self.device_type.pk,
            "proposed_name": "pinned", "target_rack": self.racks[0].pk,
            "target_position": 10, "target_face": "front",
        })
        self.assertHttpStatus(response, 200)
        self.assertContains(response, "cannot be moved to another design")
        placement.refresh_from_db()
        self.assertEqual(placement.design_id, approved.pk)
