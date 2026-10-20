"""
Security regression tests for the REST API (1.2.2).

Each test drives the ATTACK (an object-constrained ObjectPermission, a read-only
token, a cross-site rack id, malformed input) and asserts the API refuses it,
redacts it or degrades to a 4xx -- never discloses another design's data and
never 500s.
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

from .. import projection
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
RESTRICTED = projection.RESTRICTED_DESIGN_LABEL
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

    def readonly_token(self):
        self.token.write_enabled = False
        self.token.save()


# --- 1. root / version / sequence are server-set ------------------------------

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
        self.assertNotEqual(created.sequence, 3)

    def test_patch_cannot_change_root_version_sequence(self):
        self.add_permissions("netbox_rack_design.change_design", "netbox_rack_design.view_design")
        victim = self.design("Victim root")
        mine = self.design("Mine")
        sequence = mine.sequence
        response = self.client.patch(
            self.url("design-detail", mine.pk),
            {"root": victim.pk, "version": 9, "sequence": 999}, format="json", **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        mine.refresh_from_db()
        self.assertIsNone(mine.root_id)
        self.assertEqual(mine.version, 1)
        self.assertEqual(mine.sequence, sequence)


# --- 2. chain / feeds redaction -----------------------------------------------

class ChainRedactionTest(ApiSecurityBase):

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

    def test_hidden_ancestors_are_redacted_keeping_the_chain_length(self):
        response = self._chain()
        self.assertHttpStatus(response, status.HTTP_200_OK)
        ancestors = response.data["ancestors"]
        self.assertEqual(len(ancestors), 3)
        self.assertEqual([a["display"] for a in ancestors[:2]], [RESTRICTED, RESTRICTED])
        self.assertTrue(all(a["restricted"] for a in ancestors[:2]))
        self.assertEqual(ancestors[2]["id"], self.c.pk)
        self.assertNotIn("Secret A", response.content.decode())
        self.assertNotIn("Secret B", response.content.decode())

    def test_hidden_children_are_dropped(self):
        response = self._chain()
        self.assertEqual([c["id"] for c in response.data["children"]], [self.visible_child.pk])
        self.assertNotIn("Secret Child", response.content.decode())

    def test_refusal_does_not_name_a_hidden_ancestor(self):
        Design.objects.filter(pk=self.b.pk).update(status=DesignStatusChoices.STATUS_DRAFT)
        response = self._chain()
        refusal = response.data["refusal"]
        self.assertEqual(refusal["kind"], "ancestor_not_approved")
        self.assertNotIn("Secret B", response.content.decode())
        self.assertTrue(refusal["source_design"]["restricted"])
        self.assertIsNone(refusal["source_design"]["id"])

    def test_feeds_do_not_name_a_hidden_ancestor(self):
        DesignPowerFeed.objects.create(design=self.a, rack=self.racks[0], name="Feed A")
        response = self.client.get(
            self.url("design-feeds", self.d.pk) + f"?rack_id={self.racks[0].pk}", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        inherited = [f for f in response.data["planned"] if f.get("inherited")]
        self.assertEqual(len(inherited), 1)
        self.assertEqual(inherited[0]["design_name"], RESTRICTED)
        self.assertIsNone(inherited[0]["design_id"])
        self.assertNotIn("Secret A", response.content.decode())

    def test_conflicts_do_not_name_a_hidden_ancestor(self):
        Design.objects.filter(pk=self.b.pk).update(status=DesignStatusChoices.STATUS_DRAFT)
        self.d.racks.add(self.racks[0])
        response = self.client.get(self.url("design-conflicts", self.d.pk), **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        refusals = [c for c in response.data["conflicts"] if c["kind"] == "ancestor_not_approved"]
        self.assertTrue(refusals)
        self.assertIsNone(refusals[0]["source_design_id"])
        self.assertNotIn("Secret B", response.content.decode())


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

    def test_hidden_approved_target_is_refused(self):
        response = self._rebase(self.hidden_approved)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("Secret", response.content.decode())
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.based_on_id, self.parent.pk)

    def test_hidden_draft_target_error_has_no_title(self):
        response = self._rebase(self.hidden_draft)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("Secret", response.content.decode())

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
        with mock.patch("netbox.context_managers.flush_events") as flush:
            response = self.post(self.url("design-recompute-distribution", self.d.pk), self._layout())
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertFalse(DesignPlacement.objects.filter(design=self.d).exists())
        flush.assert_not_called()

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


# --- 11. messages never name a design the reader cannot view ------------------

class HiddenTitleMessageTest(ApiSecurityBase):

    def test_save_layout_foreign_placement_message(self):
        self.add_permissions(
            "netbox_rack_design.change_design",
            "netbox_rack_design.add_designplacement", "netbox_rack_design.change_designplacement",
            "netbox_rack_design.delete_designplacement")
        hidden = self.design("Secret foreign")
        foreign = DesignPlacement.objects.create(
            design=hidden, kind=ADD, device_type=self.device_type,
            target_rack=self.racks[0], target_position=20, target_face="front")
        mine = self.design("Mine")
        self.grant(Design, ["view"], {"pk": mine.pk})
        response = self.post(self.url("design-save-layout", mine.pk), {
            "design_id": mine.pk, "racks": [{"rack_id": self.racks[0].pk, "front": [
                {"kind": "add", "placement_id": foreign.pk, "u_position": 20, "face": "front"}]}]})
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("Secret foreign", response.content.decode())

    def test_delete_guard_messages(self):
        self.add_permissions("netbox_rack_design.delete_design")
        parent = self.design("Parent")
        make_design(title="Secret child", site=self.site, based_on=parent)
        make_design(title="Secret version", site=self.site, root=parent, version=2)
        self.grant(Design, ["view"], {"pk": parent.pk})
        response = self.client.delete(self.url("design-detail", parent.pk), **self.header)
        self.assertHttpStatus(response, status.HTTP_409_CONFLICT)
        self.assertNotIn("Secret", response.content.decode())
        self.assertIn("1 design(s) you cannot view", response.content.decode())

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
        self.assertNotIn("Secret referrer", response.content.decode())


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
