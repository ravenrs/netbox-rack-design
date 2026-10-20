"""
Security regression tests for the HTML views (1.2.2).

Each test drives the ATTACK -- an object-constrained ObjectPermission, a hand-posted
pk, a hand-edited bulk form -- and asserts the page refuses or degrades to a
message and never 500s.

Visibility rule (NetBox core parity), shared with REST/GraphQL: a LIST
(children, "required by", rebase picker, elevation browser) shows only designs
the reader may view; a related object NAMED on a page (a parent, an ancestor
stack, a guard or error message) shows its real name, like core's breadcrumbs
and ProtectedError messages do -- the link it carries simply 404s.
"""

from unittest import mock

from core.models import ObjectChange, ObjectType
from dcim.models import DeviceType, Rack, Site
from django.contrib.messages import get_messages
from django.db import IntegrityError
from django.urls import reverse
from django.utils.html import escape
from users.models import ObjectPermission
from utilities.exceptions import PermissionsViolation
from utilities.testing import TestCase

from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignPlacement, DesignPowerFeed, PlannedRack
from .utils import create_dcim_environment, make_design

ADD = DesignPlacementKindChoices.KIND_ADD
APPROVED = DesignStatusChoices.STATUS_APPROVED
IMPLEMENTED = DesignStatusChoices.STATUS_IMPLEMENTED
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

    def spy_clear_events(self):
        """``(seen, patcher)``: ``seen`` gets the size of the request's event queue
        each time a view clears it -- proof the rolled-back writes DID queue events
        and the view dropped them."""
        from core.signals import clear_events
        from netbox.context import events_queue

        seen = []

        class Spy:
            @staticmethod
            def send(sender, **kwargs):
                seen.append(len(events_queue.get()))
                return clear_events.send(sender=sender, **kwargs)

        return seen, mock.patch("netbox_rack_design.views.clear_events", Spy())

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

    def test_ancestor_stack_names_every_ancestor_like_core_breadcrumbs(self):
        self.assertContains(self.response, "Secret ancestor A")
        self.assertContains(self.response, "Visible parent B")
        self.assertEqual(
            [a.pk for a in self.response.context["chain_ancestors"]], [self.a.pk, self.b.pk])

    def test_the_link_of_a_hidden_ancestor_does_not_open(self):
        self.assertHttpStatus(self.client.get(self.a.get_absolute_url()), 404)

    def test_hidden_implemented_ancestor_still_alerts(self):
        self.assertContains(self.response, "is marked Implemented")

    def test_children_list_drops_hidden_designs(self):
        self.assertContains(self.response, "Visible child")
        self.assertNotContains(self.response, "Secret child")

    def test_required_by_list_drops_hidden_designs(self):
        self.assertContains(self.response, "Visible dependent")
        self.assertNotContains(self.response, "Secret dependent")

    def test_direct_links_keep_their_title(self):
        # depends_on is a direct link, like based_on (Parent row) above.
        self.assertContains(self.response, "Secret dependency")


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

    def test_a_hidden_source_design_is_named_in_widgets_and_markup(self):
        self.can_view_only(self.b, self.c)
        response = self.client.get(self.url("design_editor_default", self.c.pk))
        self.assertHttpStatus(response, 200)
        widget = next(
            w for w in response.context["all_rack_blocks"][0]["widgets"]
            if w["placement_id"] == self.upstream.pk
        )
        self.assertTrue(widget["inherited"])
        self.assertIn("Secret origin A", widget["source_design_name"])
        self.assertEqual(widget["source_design_id"], self.a.pk)
        self.assertContains(response, f'data-source-design-id="{self.a.pk}"')
        self.assertContains(response, 'data-source-design-name="Secret origin A')

    def test_elevation_page_hover_names_it_too(self):
        self.can_view_only(self.b, self.c)
        response = self.client.get(self.url("design_elevation", self.c.pk))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, "Secret origin A")

    def test_a_chain_refusal_keeps_its_real_kind_and_names_the_ancestor(self):
        self.can_view_only(self.b, self.c)
        for status, kind in ((IMPLEMENTED, "ancestor_implemented"),
                             (DesignStatusChoices.STATUS_DRAFT, "ancestor_not_approved")):
            Design.objects.filter(pk=self.a.pk).update(status=status)
            response = self.client.get(self.url("design_editor_default", self.c.pk))
            self.assertHttpStatus(response, 200)
            rows = [c for c in response.context["chain_conflicts"] if c["kind"] == kind]
            self.assertTrue(rows, (kind, response.context["chain_conflicts"]))
            self.assertEqual(rows[0]["source_design_id"], self.a.pk)
            self.assertIn("Secret origin A", rows[0]["source_design_name"])
            self.assertNotIn("chain_broken", [c["kind"] for c in response.context["chain_conflicts"]])

    def test_an_occupancy_conflict_names_the_ancestor_at_fault(self):
        # c plans U10 itself while the (hidden) grand-ancestor a owns it.
        DesignPlacement.objects.create(
            design=self.c, kind=ADD, device_type=self.device_type, target_rack=self.racks[0],
            target_position=10, target_face="front", proposed_name="srv-mine")
        self.can_view_only(self.b, self.c)
        response = self.client.get(self.url("design_editor_default", self.c.pk))
        rows = [c for c in response.context["chain_conflicts"] if c["kind"] == "unit_occupied"]
        self.assertEqual(len(rows), 1, response.context["chain_conflicts"])
        self.assertEqual(rows[0]["source_design_id"], self.a.pk)
        self.assertContains(response, "Secret origin A")


# --- chain-health report ---------------------------------------------------------

class ChainHealthNamesAncestorsTest(ViewSecurityBase):
    """Rows are restricted to designs the reader may view; the ancestor a row
    blames is named whether or not the reader can open it."""

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

    def test_a_row_names_the_hidden_ancestor_at_fault(self):
        self.can_view_only(self.leaf)
        response = self.client.get(self.url("design_chain_health"))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, "Visible leaf L")
        self.assertContains(response, "Secret root R")
        self.assertContains(response, "is marked implemented")

    def test_hidden_designs_get_no_row(self):
        self.can_view_only(self.leaf)
        response = self.client.get(self.url("design_chain_health"))
        self.assertNotContains(response, "Secret mid M")

    def test_the_designs_own_parent_is_named(self):
        self.can_view_only(self.child_of_hidden)
        response = self.client.get(self.url("design_chain_health"))
        self.assertContains(response, "Secret draft parent P")


class HiddenImmediateParentTest(ViewSecurityBase):
    """The immediate parent is named on the page even when the reader cannot view
    it (REST shows it too; GraphQL resolves the link null)."""

    def test_design_page_shows_a_hidden_immediate_parent(self):
        parent = self.make("Secret immediate parent", status=APPROVED)
        child = self.make("Visible child", based_on=parent)
        self.can_view_only(child)
        response = self.client.get(self.url("design", child.pk))
        self.assertHttpStatus(response, 200)
        self.assertContains(response, "Secret immediate parent")
        self.assertEqual([a.pk for a in response.context["chain_ancestors"]], [parent.pk])


# --- markup in names: every template prints it escaped ---------------------------

class MarkupInNamesTest(ViewSecurityBase):
    """A design / placement name with markup renders as text on the design page,
    in the chain-health title attribute and in the editor's tile attributes."""

    PAYLOAD = '"><img src=x onerror="window.__xss=1">'
    ESCAPED = escape(PAYLOAD)

    def assert_escaped(self, response):
        self.assertHttpStatus(response, 200)
        self.assertNotContains(response, self.PAYLOAD)
        self.assertNotContains(response, '<img src=x onerror="window.__xss=1">')
        self.assertContains(response, self.ESCAPED)

    def test_design_page(self):
        root = self.make(f"Root {self.PAYLOAD}", status=APPROVED)
        parent = self.make(f"Parent {self.PAYLOAD}", status=APPROVED, based_on=root)
        page = self.make(f"Page {self.PAYLOAD}", based_on=parent)
        self.make(f"Child {self.PAYLOAD}", based_on=page)
        self.grant(Design, ["view"])
        self.assert_escaped(self.client.get(self.url("design", page.pk)))

    def test_chain_health_title_attribute(self):
        # A visible, not-approved parent: the row's detail (a title="..." attribute)
        # carries its title.
        parent = self.make(f"Draft parent {self.PAYLOAD}")
        child = self.make("Child", based_on=parent)
        self.grant(Design, ["view"])
        response = self.client.get(self.url("design_chain_health"))
        self.assert_escaped(response)
        self.assertContains(response, escape(f"Draft parent {self.PAYLOAD}"))
        self.assertContains(response, child.title)

    def test_editor_tile_attributes(self):
        a = self.make(f"Origin {self.PAYLOAD}")
        a.racks.add(self.racks[0])
        self.add_placement(a, position=10, name=f"from-a {self.PAYLOAD}")
        a.status = APPROVED
        a.save()
        c = self.make("Editing", based_on=a)
        c.racks.add(self.racks[0])
        self.add_placement(c, position=20, name=f"mine {self.PAYLOAD}")
        self.grant(Design, ["view"])
        self.assert_escaped(self.client.get(self.url("design_editor_default", c.pk)))


class FormLinkScopeTest(ViewSecurityBase):
    """The HTML model forms are core's: NetBox restricts every choice field of a
    model form to what the user may view, so a hand-posted pk or title of a hidden
    design is an invalid choice (core's own forms do the same for a hidden tenant)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.hidden = make_design(title="Secret linked", site=cls.site, status=APPROVED)
        cls.visible = make_design(title="Visible linked", site=cls.site, status=APPROVED)
        cls.mine = make_design(title="Mine", site=cls.site)

    def setUp(self):
        super().setUp()
        self.can_view_only(self.visible, self.mine)
        self.grant(Design, ["change", "add"])
        # The forms' other pickers are restricted by core's own view permissions.
        for model in (Site, Rack, DeviceType):
            self.grant(model, ["view"])

    def _design_form(self, **extra):
        return {"title": "Mine", "sites": [self.site.pk], "status": "draft", **extra}

    def test_based_on_and_depends_on_of_a_hidden_design_are_invalid_choices(self):
        for field in ("based_on", "depends_on"):
            response = self.client.post(
                self.url("design_edit", self.mine.pk), self._design_form(**{field: self.hidden.pk}))
            self.assertHttpStatus(response, 200)
            self.assertNotContains(response, "Secret linked")
            self.mine.refresh_from_db()
            self.assertIsNone(self.mine.based_on_id)
            self.assertEqual(self.mine.depends_on.count(), 0)
        ok = self.client.post(
            self.url("design_edit", self.mine.pk), self._design_form(based_on=self.visible.pk))
        self.assertHttpStatus(ok, 302)
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.based_on_id, self.visible.pk)

    def test_placement_form_cannot_write_into_a_hidden_design(self):
        self.grant(DesignPlacement, ["add", "view"])
        form = {"design": self.hidden.pk, "kind": "add", "device_type": self.device_type.pk,
                "proposed_name": "sneaky", "target_rack": self.racks[0].pk,
                "target_position": 12, "target_face": "front"}
        response = self.client.post(self.url("designplacement_add"), form)
        self.assertHttpStatus(response, 200)
        self.assertFalse(DesignPlacement.objects.filter(proposed_name="sneaky").exists())
        ok = self.client.post(self.url("designplacement_add"), {**form, "design": self.mine.pk})
        self.assertHttpStatus(ok, 302)
        self.assertTrue(DesignPlacement.objects.filter(proposed_name="sneaky", design=self.mine).exists())

    def test_csv_import_cannot_name_a_hidden_design(self):
        self.grant(DesignPlacement, ["add", "view"])
        csv = ("design,kind,device_type,proposed_name,target_rack,target_position,target_face\n"
               f"Secret linked,add,{self.device_type.model},csv-sneaky,{self.racks[0].name},14,front")
        response = self.client.post(self.url("designplacement_bulk_import"), {
            "data": csv, "format": "csv", "csv_delimiter": ","})
        self.assertHttpStatus(response, 200)
        self.assertFalse(DesignPlacement.objects.filter(proposed_name="csv-sneaky").exists())
        # positive control: the same file naming a design the user may view imports,
        # so the refusal above is the hidden design, not a malformed CSV.
        ok = self.client.post(self.url("designplacement_bulk_import"), {
            "data": csv.replace("Secret linked", "Mine").replace("csv-sneaky", "csv-fine"),
            "format": "csv", "csv_delimiter": ","})
        self.assertHttpStatus(ok, 302)
        self.assertTrue(
            DesignPlacement.objects.filter(proposed_name="csv-fine", design=self.mine).exists())


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


# --- the PDUs bound to a feed: a reverse list, restricted like core's -----------------

class BoundPlacementsRestrictedTest(ViewSecurityBase):
    """A descendant design's PDU bound to an ancestor's planned feed shows only to
    someone who may view that placement (core restricts reverse/related lists)."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.parent = make_design(title="Feed owner", site=cls.site, status=APPROVED)
        cls.feed = DesignPowerFeed.objects.create(
            design=cls.parent, rack=cls.racks[0], name="Feed-X", voltage=230, amperage=16)
        cls.child = make_design(title="Hidden child", site=cls.site, based_on=cls.parent)
        cls.hidden = DesignPlacement.objects.create(
            design=cls.child, kind=ADD, device_type=cls.device_type, target_rack=cls.racks[0],
            target_position=10, target_face="front", proposed_name="ZZ-hidden-pdu",
            planned_power_feed=cls.feed)
        cls.own = DesignPlacement.objects.create(
            design=cls.parent, kind=ADD, device_type=cls.device_type, target_rack=cls.racks[0],
            target_position=12, target_face="front", proposed_name="ZZ-own-pdu",
            planned_power_feed=cls.feed)

    def pages(self):
        return {
            "feed page": self.url("designpowerfeed", self.feed.pk),
            "feed list": self.url("designpowerfeed_list"),
            "design page": self.url("design", self.parent.pk),
        }

    def test_a_placement_the_reader_cannot_view_is_not_listed(self):
        self.grant(Design, ["view"], {"pk": self.parent.pk})
        self.grant(DesignPowerFeed, ["view"])
        self.grant(DesignPlacement, ["view"], {"design_id": self.parent.pk})
        for label, url in self.pages().items():
            with self.subTest(page=label):
                response = self.client.get(url)
                self.assertHttpStatus(response, 200)
                body = response.content.decode()
                self.assertIn("ZZ-own-pdu", body)  # the control: viewable rows still list
                self.assertNotIn("ZZ-hidden-pdu", body)

    def test_the_design_permission_alone_shows_the_designs_own_rows(self):
        # A planner holding only view_design (no placement permission) still sees
        # the PDUs of the design they may view -- the design is the boundary --
        # but not those of a descendant design they may not.
        self.grant(Design, ["view"], {"pk": self.parent.pk})
        self.grant(DesignPowerFeed, ["view"])
        for label, url in self.pages().items():
            with self.subTest(page=label):
                body = self.client.get(url).content.decode()
                self.assertIn("ZZ-own-pdu", body)
                self.assertNotIn("ZZ-hidden-pdu", body)

    def test_the_bulk_delete_confirmation_table_is_restricted_too(self):
        self.grant(Design, ["view"], {"pk": self.parent.pk})
        self.grant(DesignPowerFeed, ["view", "delete"])
        self.grant(DesignPlacement, ["view"], {"design_id": self.parent.pk})
        # Not frozen-refused until confirmed: the first POST only renders the table.
        response = self.client.post(
            self.url("designpowerfeed_bulk_delete"), {"pk": [self.feed.pk], "confirm": "true"})
        self.assertHttpStatus(response, 200)
        body = response.content.decode()
        self.assertIn("Feed-X", body)
        self.assertNotIn("ZZ-hidden-pdu", body)

    def test_a_reader_who_may_view_it_sees_it(self):
        self.grant(Design, ["view"])
        self.grant(DesignPowerFeed, ["view"])
        self.grant(DesignPlacement, ["view"])
        for label, url in self.pages().items():
            with self.subTest(page=label):
                self.assertIn("ZZ-hidden-pdu", self.client.get(url).content.decode())


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

    def test_derive_from_a_hidden_approved_design_is_allowed(self):
        # Trust model: an approved design is clonable by every add_design holder.
        self.assertHttpStatus(self.client.get(self.url("design_derive", self.secret.pk)), 200)
        response = self.client.post(self.url("design_derive", self.secret.pk), {"title": "Cloned"})
        self.assertHttpStatus(response, 302)
        self.assertEqual(Design.objects.get(title="Cloned").based_on_id, self.secret.pk)

    def test_derive_from_a_hidden_draft_is_not_found_and_names_nothing(self):
        # A draft is not clonable, so there is no reason to say it exists: 404 on
        # both verbs, and its title appears nowhere.
        draft = make_design(title="Secret draft source", site=self.site)
        for method in (self.client.get, self.client.post):
            response = method(self.url("design_derive", draft.pk), {"title": "Early"})
            self.assertHttpStatus(response, 404)
            self.assertNotIn("Secret draft source", response.content.decode())
        self.assertFalse(Design.objects.filter(title="Early").exists())

    def test_derive_from_a_visible_draft_says_to_approve_it_first(self):
        draft = make_design(title="Visible draft source", site=self.site)
        self.can_view_only(self.mine, draft)
        response = self.client.post(self.url("design_derive", draft.pk), {"title": "Early"})
        self.assertHttpStatus(response, 302)
        self.assertIn("Approve", " ".join(self.message_texts(response)))
        self.assertFalse(Design.objects.filter(title="Early").exists())

    def test_derive_page_of_a_visible_draft_says_to_approve_it_first(self):
        draft = make_design(title="Visible draft page", site=self.site)
        self.can_view_only(self.mine, draft)
        response = self.client.get(self.url("design_derive", draft.pk))
        self.assertHttpStatus(response, 200)
        self.assertIn("Approve", response.content.decode())

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

    def test_rolled_back_clones_drop_the_events_their_writes_queued(self):
        for name, title in (("design_new_version", "Draft clone"), ("design_derive", "Draft child")):
            seen, spy = self.spy_clear_events()
            with spy:
                response = self.client.post(self.url(name, self.source.pk), {"title": title})
            self.assertHttpStatus(response, 302)
            self.assertTrue(seen and max(seen) > 0, (name, seen))


class DeriveSitesConstraintTest(ViewSecurityBase):
    """The add constraint is judged on the FINISHED child (sites copied from the
    parent), so a ``sites__id`` constraint that the child satisfies must succeed."""

    def test_sites_constrained_add_permission_derives(self):
        source = make_design(title="Approved src", site=self.site, status=APPROVED)
        self.grant(Design, ["add"], {"sites__id": self.site.pk})
        response = self.client.post(self.url("design_derive", source.pk), {"title": "Sited child"})
        self.assertHttpStatus(response, 302)
        child = Design.objects.get(title="Sited child")
        self.assertEqual(list(child.sites.all()), [self.site])
        self.assertEqual(child.based_on_id, source.pk)


class RebaseWriteCheckTest(ViewSecurityBase):

    def test_rebase_outside_the_change_constraint_is_rolled_back(self):
        parent = make_design(title="Parent", site=self.site, status=APPROVED)
        new_parent = make_design(title="New parent", site=self.site, status=APPROVED)
        child = make_design(title="Child", site=self.site, based_on=parent)
        self.grant(Design, ["view"])
        self.grant(Design, ["change"], {"based_on_id": parent.pk})
        seen, spy = self.spy_clear_events()
        with spy:
            response = self.client.post(
                self.url("design_rebase", child.pk), {"based_on": new_parent.pk})
        self.assertHttpStatus(response, 302)
        self.assertTrue(self.message_texts(response))
        self.assertTrue(seen and max(seen) > 0, seen)  # events were queued, then dropped
        child.refresh_from_db()
        self.assertEqual(child.based_on_id, parent.pk)


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

    def test_every_error_branch_clears_the_queued_events(self):
        from django.core.exceptions import ValidationError

        for error in (PermissionsViolation, IntegrityError("dup"), ValidationError("bad")):
            seen, spy = self.spy_clear_events()
            with spy:
                self.post_with(error)
            self.assertEqual(len(seen), 1, error)

    def test_integrity_error_detail_stays_out_of_the_message(self):
        response = self.post_with(IntegrityError("duplicate key violates constraint secret_idx"))
        self.assertNotIn("secret_idx", " ".join(self.message_texts(response)))


# --- 6. guard messages ---------------------------------------------------------------------

class GuardMessagesNameTheDependentsTest(ViewSecurityBase):
    """Core's ProtectedError message names every dependent object, viewable or
    not; the delete guards here do the same."""

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
        self.assertIn("Secret child", text)
        self.assertTrue(Design.objects.filter(pk=self.parent.pk).exists())

    def test_delete_versions_guard(self):
        response = self.client.post(self.url("design_delete", self.root.pk), {"confirm": "true"})
        self.assertIn("Secret version", self.joined(response))

    def test_bulk_delete_guards(self):
        response = self.client.post(
            self.url("design_bulk_delete"),
            {"pk": [self.parent.pk, self.root.pk], "confirm": "true", "_confirm": "true"},
        )
        self.assertHttpStatus(response, 302)
        text = self.joined(response)
        self.assertIn("Secret child", text)
        self.assertIn("Secret version", text)
        self.assertTrue(Design.objects.filter(pk=self.parent.pk).exists())

    def test_bulk_delete_only_guards_the_rows_the_user_may_delete(self):
        # The user may delete only what the delete permission covers: a pk
        # outside it is not in the queryset, so it is neither guarded nor named.
        hidden = make_design(title="Hidden deletable", site=self.site, status=APPROVED)
        make_design(title="Its child", site=self.site, based_on=hidden)
        ObjectPermission.objects.filter(users=self.user, actions=["delete"]).delete()
        self.grant(Design, ["delete"], {"pk__in": [self.parent.pk, self.root.pk]})
        response = self.client.post(
            self.url("design_bulk_delete"),
            {"pk": [hidden.pk], "confirm": "true", "_confirm": "true"},
        )
        self.assertNotIn("Hidden deletable", self.joined(response))
        self.assertTrue(Design.objects.filter(pk=hidden.pk).exists())

    def test_planned_rack_referenced_guard(self):
        planned = PlannedRack.objects.create(name="PR1", location=self._location())
        secret = make_design(title="Secret planner", site=self.site)
        secret.planned_racks.add(planned)
        self.grant(PlannedRack, ["delete", "view"])
        response = self.client.post(self.url("plannedrack_delete", planned.pk), {"confirm": "true"})
        text = self.joined(response)
        self.assertIn("Cannot delete", text)
        self.assertIn("Secret planner", text)

    def _location(self):
        from dcim.models import Location
        return Location.objects.create(name="Loc", slug="loc", site=self.site)

    def test_frozen_bulk_placement_delete_names_the_frozen_design(self):
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
        self.assertIn("Secret frozen", self.joined(response))
        self.assertTrue(DesignPlacement.objects.filter(pk=placement.pk).exists())

    def test_feed_add_with_a_viewable_frozen_design_is_refused_by_name(self):
        secret = make_design(title="Secret frozen feed design", site=self.site, status=APPROVED)
        self.can_view_only(self.parent, self.root, secret)
        self.grant(DesignPowerFeed, ["add", "view"])
        response = self.client.post(self.url("designpowerfeed_add"), {
            "design": secret.pk, "rack": self.racks[0].pk, "name": "F1",
            "supply": "ac", "phase": "single-phase", "voltage": 230, "amperage": 16,
        })
        self.assertIn("Secret frozen feed design", " ".join(self.message_texts(response)))
        self.assertFalse(DesignPowerFeed.objects.filter(design=secret).exists())


class BulkPkValidationTest(ViewSecurityBase):

    def setUp(self):
        super().setUp()
        for model in (Design, DesignPlacement, DesignPowerFeed, PlannedRack):
            self.grant(model, ["view", "change", "delete"])

    def test_valid_pks_still_bulk_delete(self):
        # Positive control: the pk validation must not refuse a good selection.
        doomed = make_design(title="Doomed", site=self.site)
        survivor = make_design(title="Survivor", site=self.site)
        response = self.client.post(
            self.url("design_bulk_delete"),
            {"pk": [doomed.pk], "confirm": "true", "_confirm": "true"},
        )
        self.assertHttpStatus(response, 302)
        self.assertFalse(Design.objects.filter(pk=doomed.pk).exists())
        self.assertTrue(Design.objects.filter(pk=survivor.pk).exists())

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
