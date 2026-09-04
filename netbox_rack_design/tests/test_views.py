"""UI view tests for NetBox Rack Design (subclassing NetBox's standard suite)."""

from decimal import Decimal

from core.models import ObjectType
from dcim.choices import PowerFeedPhaseChoices, PowerFeedSupplyChoices
from dcim.models import Rack, Site
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from users.models import ObjectPermission, User
from utilities.testing import TestCase, ViewTestCases, create_tags

from .. import views
from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..forms import DesignForm
from ..models import (
    Design,
    DesignGroup,
    DesignPlacement,
    DesignPowerFeed,
    HiddenDesignRack,
)
from .utils import create_dcim_environment


class DesignGroupTest(ViewTestCases.PrimaryObjectViewTestCase):
    model = DesignGroup

    def _get_base_url(self):
        return f"plugins:netbox_rack_design:{self.model._meta.model_name}_{{}}"

    @classmethod
    def setUpTestData(cls):
        parent = DesignGroup.objects.create(name="Parent")
        DesignGroup.objects.create(name="Group 1", parent=parent)
        DesignGroup.objects.create(name="Group 2")
        DesignGroup.objects.create(name="Group 3")

        tags = create_tags("Alpha", "Bravo", "Charlie")

        cls.form_data = {
            "name": "Group X",
            "parent": parent.pk,
            "description": "A new group",
            "tags": [t.pk for t in tags],
        }
        cls.csv_data = (
            "name,description",
            "Group 4,Fourth",
            "Group 5,Fifth",
            "Group 6,Sixth",
        )
        cls.csv_update_data = (
            "id,description",
            f"{DesignGroup.objects.get(name='Group 1').pk},Updated 1",
            f"{DesignGroup.objects.get(name='Group 2').pk},Updated 2",
            f"{DesignGroup.objects.get(name='Group 3').pk},Updated 3",
        )
        cls.bulk_edit_data = {
            "description": "Bulk-edited description",
        }


class DesignTest(ViewTestCases.PrimaryObjectViewTestCase):
    model = Design

    def _get_base_url(self):
        return f"plugins:netbox_rack_design:{self.model._meta.model_name}_{{}}"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        site = env["site"]
        racks = env["racks"]

        Design.objects.create(title="Design 1", site=site)
        Design.objects.create(title="Design 2", site=site)
        Design.objects.create(title="Design 3", site=site)

        tags = create_tags("Alpha", "Bravo", "Charlie")

        cls.form_data = {
            "title": "Design X",
            "site": site.pk,
            "status": DesignStatusChoices.STATUS_DRAFT,
            "summary": "A new design",
            "racks": [r.pk for r in racks],
            "tags": [t.pk for t in tags],
        }
        cls.csv_data = (
            "title,site,status",
            f"Design 4,{site.name},{DesignStatusChoices.STATUS_DRAFT}",
            f"Design 5,{site.name},{DesignStatusChoices.STATUS_DRAFT}",
            f"Design 6,{site.name},{DesignStatusChoices.STATUS_DRAFT}",
        )
        cls.csv_update_data = (
            "id,summary",
            f"{Design.objects.get(title='Design 1').pk},Updated 1",
            f"{Design.objects.get(title='Design 2').pk},Updated 2",
            f"{Design.objects.get(title='Design 3').pk},Updated 3",
        )
        cls.bulk_edit_data = {
            "status": DesignStatusChoices.STATUS_REJECTED,
            "summary": "Bulk-edited summary",
        }


class PlacementCountAnnotationTest(TestCase):
    """
    Regression: the Designs and DesignGroups list views must annotate the
    LinkedCountColumn accessors (``placement_count`` / ``design_count``). Without
    the annotation the "Placements" / "Designs" columns silently render 0 no
    matter how many related rows exist (the column reads ``record.placement_count``
    off the queryset, not a live related-manager count).
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        site = env["site"]
        device_type = env["device_type"]
        rack = env["racks"][1]

        cls.group = DesignGroup.objects.create(name="Buildout")
        cls.d1 = Design.objects.create(title="With placements", site=site, group=cls.group)
        cls.d2 = Design.objects.create(title="Also grouped", site=site, group=cls.group)

        for u in (1, 2, 3):
            DesignPlacement.objects.create(
                design=cls.d1,
                kind=DesignPlacementKindChoices.KIND_ADD,
                device_type=device_type,
                target_rack=rack,
                target_position=u,
                target_face="front",
            )

    def test_design_list_annotates_placement_count(self):
        qs = views.DesignListView.queryset
        self.assertEqual(qs.get(pk=self.d1.pk).placement_count, 3)
        self.assertEqual(qs.get(pk=self.d2.pk).placement_count, 0)

    def test_group_list_annotates_design_count(self):
        qs = views.DesignGroupListView.queryset
        self.assertEqual(qs.get(pk=self.group.pk).design_count, 2)


class DesignFormTest(TestCase):
    """
    Direct DesignForm validation of the `racks` field. The model clean() cannot
    see the M2M before save (no pk → no through-rows), so the same-site rule is
    enforced at the form layer; these prove it on CREATE.
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.other_site = Site.objects.create(name="Other Site", slug="other-site")
        cls.foreign_rack = Rack.objects.create(name="Foreign Rack", site=cls.other_site)

    def _form_data(self, racks):
        return {
            "title": "Scoped",
            "site": self.site.pk,
            "status": DesignStatusChoices.STATUS_DRAFT,
            "racks": [r.pk for r in racks],
        }

    def test_same_site_racks_valid(self):
        form = DesignForm(data=self._form_data(self.racks))
        self.assertTrue(form.is_valid(), form.errors)

    def test_rack_from_other_site_rejected(self):
        form = DesignForm(data=self._form_data([self.foreign_rack]))
        self.assertFalse(form.is_valid())
        self.assertIn("racks", form.errors)


class DesignFormFrozenRacksTest(TestCase):
    """
    The `racks` scope of an approved design must not change through the
    generic edit form (PLAN-design-chains.md §2.2/G4, hole 2). The model's
    `clean()` cannot see this the ordinary way -- Django's ModelForm never
    touches an instance's m2m before calling its `full_clean()` -- so
    `DesignForm.clean()` carries its own check, using `self.instance`'s
    PRE-edit field values (still untouched at the point `clean()` runs,
    before `_post_clean()` applies the submitted ones).
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]

    def _form_data(self, racks, status=DesignStatusChoices.STATUS_APPROVED, title="Approved"):
        return {
            "title": title,
            "site": self.site.pk,
            "status": status,
            "racks": [r.pk for r in racks],
        }

    def test_rack_scope_change_rejected_on_approved_design(self):
        design = Design.objects.create(
            title="Approved", site=self.site, status=DesignStatusChoices.STATUS_APPROVED,
        )
        design.racks.set([self.racks[0]])
        form = DesignForm(data=self._form_data([self.racks[1]]), instance=design)
        self.assertFalse(form.is_valid())
        self.assertIn("racks", form.errors)

    def test_rack_scope_unchanged_allowed_on_approved_design(self):
        design = Design.objects.create(
            title="Approved", site=self.site, status=DesignStatusChoices.STATUS_APPROVED,
        )
        design.racks.set([self.racks[0]])
        form = DesignForm(data=self._form_data([self.racks[0]]), instance=design)
        self.assertTrue(form.is_valid(), form.errors)

    def test_rack_scope_change_allowed_on_draft_design(self):
        design = Design.objects.create(title="Draft", site=self.site)
        design.racks.set([self.racks[0]])
        form = DesignForm(
            data=self._form_data([self.racks[1]], status=DesignStatusChoices.STATUS_DRAFT),
            instance=design,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_status_change_allowed_on_approved_design(self):
        # Un-approving (the escape hatch) must still work when racks are
        # resubmitted unchanged.
        design = Design.objects.create(
            title="Approved", site=self.site, status=DesignStatusChoices.STATUS_APPROVED,
        )
        design.racks.set([self.racks[0]])
        form = DesignForm(
            data=self._form_data([self.racks[0]], status=DesignStatusChoices.STATUS_DRAFT),
            instance=design,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_summary_and_link_editable_on_approved_design(self):
        design = Design.objects.create(
            title="Approved", site=self.site, status=DesignStatusChoices.STATUS_APPROVED,
        )
        design.racks.set([self.racks[0]])
        data = self._form_data([self.racks[0]])
        data["summary"] = "Updated summary"
        data["link"] = "https://example.com/ticket/1"
        form = DesignForm(data=data, instance=design)
        self.assertTrue(form.is_valid(), form.errors)

    def test_rack_scope_settable_on_create_of_approved_design(self):
        form = DesignForm(data=self._form_data(self.racks, title="Brand new"))
        self.assertTrue(form.is_valid(), form.errors)
        design = form.save()
        self.assertEqual(set(design.racks.all()), set(self.racks))


class DesignFormBasedOnTest(TestCase):
    """
    DesignForm's `based_on` field expresses the chain rules from
    PLAN-design-chains.md §2.1/§2.2: only an approved design is derivable, a
    design cannot be offered as its own parent, and the model's cycle guard
    must surface as an ordinary form error rather than a 500.
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.other_site = Site.objects.create(name="Other Site 2", slug="other-site-2")

        cls.approved = Design.objects.create(
            title="Approved Parent", site=cls.site, status=DesignStatusChoices.STATUS_APPROVED,
        )
        cls.draft = Design.objects.create(
            title="Draft Parent", site=cls.site, status=DesignStatusChoices.STATUS_DRAFT,
        )
        cls.approved_other_site = Design.objects.create(
            title="Approved Elsewhere", site=cls.other_site, status=DesignStatusChoices.STATUS_APPROVED,
        )

    def _form_data(self, based_on=None, site=None, title="Child"):
        data = {
            "title": title,
            "site": (site or self.site).pk,
            "status": DesignStatusChoices.STATUS_DRAFT,
        }
        if based_on is not None:
            data["based_on"] = based_on.pk
        return data

    def test_queryset_offers_only_approved_designs(self):
        form = DesignForm()
        queryset = form.fields["based_on"].queryset
        self.assertIn(self.approved, queryset)

    def test_draft_design_not_offered_as_parent(self):
        form = DesignForm()
        queryset = form.fields["based_on"].queryset
        self.assertNotIn(self.draft, queryset)

    def test_draft_parent_rejected_on_submit(self):
        form = DesignForm(data=self._form_data(based_on=self.draft))
        self.assertFalse(form.is_valid())
        self.assertIn("based_on", form.errors)

    def test_design_not_offered_as_its_own_parent(self):
        form = DesignForm(instance=self.approved)
        queryset = form.fields["based_on"].queryset
        self.assertNotIn(self.approved, queryset)

    def test_valid_parent_saves(self):
        form = DesignForm(data=self._form_data(based_on=self.approved))
        self.assertTrue(form.is_valid(), form.errors)
        design = form.save()
        self.assertEqual(design.based_on_id, self.approved.pk)

    def test_cross_site_parent_rejected_on_submit(self):
        form = DesignForm(data=self._form_data(based_on=self.approved_other_site))
        self.assertFalse(form.is_valid())
        self.assertIn("based_on", form.errors)

    def test_cycle_error_surfaces_as_form_error(self):
        # approved -> child (already saved), then try to re-point approved's
        # based_on at child: a 2-node cycle. Must come back as a form error on
        # `based_on`, never as an unhandled exception / 500.
        child = Design.objects.create(
            title="Child of approved",
            site=self.site,
            status=DesignStatusChoices.STATUS_APPROVED,
            based_on=self.approved,
        )
        data = self._form_data(based_on=child, title=self.approved.title)
        data["status"] = DesignStatusChoices.STATUS_APPROVED
        form = DesignForm(data=data, instance=self.approved)
        self.assertFalse(form.is_valid())
        self.assertIn("based_on", form.errors)


class DesignPlacementTest(ViewTestCases.PrimaryObjectViewTestCase):
    model = DesignPlacement

    def _get_base_url(self):
        return f"plugins:netbox_rack_design:{self.model._meta.model_name}_{{}}"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        site = env["site"]
        device_type = env["device_type"]
        rack = env["racks"][1]  # empty rack with free U slots

        design = Design.objects.create(title="Design 1", site=site)
        cls.design = design
        cls.device_type = device_type
        cls.rack = rack

        p1 = DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=device_type,
            target_rack=rack,
            target_position=1,
        )
        p2 = DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=device_type,
            target_rack=rack,
            target_position=2,
        )
        p3 = DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=device_type,
            target_rack=rack,
            target_position=3,
        )

        tags = create_tags("Alpha", "Bravo", "Charlie")

        cls.form_data = {
            "design": design.pk,
            "kind": DesignPlacementKindChoices.KIND_ADD,
            "device_type": device_type.pk,
            "target_rack": rack.pk,
            "target_position": 20.0,
            "tags": [t.pk for t in tags],
        }
        cls.csv_data = (
            "design,kind,device_type,target_rack,target_position",
            f"{design.title},add,{device_type.model},{rack.name},30.0",
            f"{design.title},add,{device_type.model},{rack.name},31.0",
            f"{design.title},add,{device_type.model},{rack.name},32.0",
        )
        cls.csv_update_data = (
            "id,proposed_name",
            f"{p1.pk},upd-1",
            f"{p2.pk},upd-2",
            f"{p3.pk},upd-3",
        )
        cls.bulk_edit_data = {
            "proposed_name": "renamed-node",
        }


class DesignPowerFeedTest(ViewTestCases.PrimaryObjectViewTestCase):
    """Planned feeds are first-class objects: list, detail, edit, delete, bulk.

    They used to exist only inside the editor's dialogs, with no route to see or
    remove one (user 2026-08-28) — and a stray feed silently inflates a
    greenfield rack's capacity bar.
    """

    model = DesignPowerFeed

    def _get_base_url(self):
        return f"plugins:netbox_rack_design:{self.model._meta.model_name}_{{}}"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        site = env["site"]
        rack = env["racks"][0]
        design = Design.objects.create(title="Feed Design", site=site)
        design.racks.set([rack])

        for name in ("Feed A", "Feed B", "Feed C"):
            DesignPowerFeed.objects.create(design=design, rack=rack, name=name)

        tags = create_tags("Feed-Alpha", "Feed-Bravo", "Feed-Charlie")

        cls.form_data = {
            "design": design.pk,
            "rack": rack.pk,
            "name": "Feed D",
            "voltage": 230,
            "amperage": 32,
            "phase": PowerFeedPhaseChoices.PHASE_SINGLE,
            "supply": PowerFeedSupplyChoices.SUPPLY_AC,
            "tags": [t.pk for t in tags],
        }
        cls.csv_data = (
            "design,rack,name,voltage,amperage,phase,supply",
            f"{design.title},{rack.name},Feed E,230,16,single-phase,ac",
            f"{design.title},{rack.name},Feed F,230,16,single-phase,ac",
            f"{design.title},{rack.name},Feed G,230,16,single-phase,ac",
        )
        cls.csv_update_data = (
            "id,amperage",
            *[
                f"{feed.pk},20"
                for feed in DesignPowerFeed.objects.filter(design=design).order_by("pk")
            ],
        )
        cls.bulk_edit_data = {"amperage": 32}


class DesignPowerFeedDerationTest(TestCase):
    """The list/detail figure must be the one the capacity bar actually uses."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.rack = env["racks"][0]
        cls.design = Design.objects.create(title="Derate Design", site=env["site"])

    def test_derated_watts_matches_the_projection(self):
        from netbox.config import get_config

        from netbox_rack_design.distribution import breaker_watts

        feed = DesignPowerFeed.objects.create(
            design=self.design, rack=self.rack, name="Feed A",
            voltage=230, amperage=32,
        )
        max_util = get_config().POWERFEED_DEFAULT_MAX_UTILIZATION or 100
        expected = int(round((breaker_watts(feed) or 0) * max_util / 100.0))
        self.assertEqual(feed.derated_watts, expected)
        self.assertGreater(feed.derated_watts, 0)


class RenamedMoveRenderTest(TestCase):
    """Tile label = ASSIGNED name (user ruling 2026-07-10), server-side: a
    SAVED renamed move renders the NEW name as the tile's visible label
    (display span) while the stable identity label stays the device's real
    name (hidden, still in the DOM for identity/read-model matching)."""

    user_permissions = ("netbox_rack_design.view_design",)

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.rack = env["racks"][0]
        cls.device = env["devices"][0]  # "Device 1" @ U1
        cls.design = Design.objects.create(title="Rename render", site=cls.site)
        cls.design.racks.set([cls.rack])
        DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.device,
            target_rack=cls.rack,
            target_position=10,
            target_face="front",
            proposed_name="renamed-node-42",
        )

    def test_saved_rename_renders_new_name_as_visible_label(self):
        url = reverse(
            "plugins:netbox_rack_design:design_elevation",
            kwargs={"pk": self.design.pk},
        )
        response = self.client.get(url)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        # The display span shows the assigned name...
        self.assertIn("nbx-rd-name-display", content)
        self.assertIn("renamed-node-42", content)
        # ...while the identity span (device's real name) stays in the DOM,
        # hidden, for identity matching.
        self.assertIn("nbx-rd-label-hidden", content)
        self.assertIn(self.device.name, content)
        # The identity-story hover data rides along: the device's real (old)
        # name + where it is going (user ruling 2026-07-10).
        self.assertIn(f'data-old-name="{self.device.name}"', content)
        self.assertIn("data-moved-to=", content)


class DisplacedElevationRenderTest(TestCase):
    """Displaced-rendering parity in the READ-ONLY elevation (spec §3 stripe,
    parity ruling 2026-07-09): a SAVED displacement -- OLD's vacating slot
    occupied by NEW's planned slot at the same rows -- must render OLD as the
    outside red stripe bar (title/hover data with OLD's info), NOT as a full
    tile composited under NEW's."""

    user_permissions = ("netbox_rack_design.view_design",)

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.rack = env["racks"][0]
        cls.old_device = env["devices"][0]  # Device 1 @ Rack1/U1/front
        cls.design = Design.objects.create(title="Displaced elevation", site=cls.site)
        cls.design.racks.set([cls.rack])
        # OLD moves away (U1 -> U10) ...
        DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.old_device,
            target_rack=cls.rack,
            target_position=10,
            target_face="front",
        )
        # ... and NEW (a catalog add) lands on the vacated U1.
        DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            target_rack=cls.rack,
            target_position=1,
            target_face="front",
            proposed_name="NEW-occupant",
        )

    def _get(self):
        url = reverse(
            "plugins:netbox_rack_design:design_elevation",
            kwargs={"pk": self.design.pk},
        )
        response = self.client.get(url)
        self.assertHttpStatus(response, 200)
        return response.content.decode()

    def test_displaced_old_renders_as_stripe_not_full_tile(self):
        content = self._get()
        # The outside stripe bar exists, naming OLD.
        self.assertIn("nbx-rd-stripe", content)
        self.assertIn(f"was: {self.old_device.name}", content)
        # OLD's ghost is NOT rendered as a full tile in the read-only view --
        # its ONLY footprint at those rows is the stripe (which itself carries
        # the state class for legend-filter parity); NEW's add tile is the
        # single full tile there. (Pre-fix: both rendered as full tiles, two
        # labels composited on top of each other.)
        self.assertNotIn("grid-stack-item nbx-rd-state-move_out_ghost", content)
        self.assertIn("nbx-rd-stripe nbx-rd-state-move_out_ghost", content)
        # NEW renders as its normal full tile.
        self.assertIn("NEW-occupant", content)
        self.assertIn("nbx-rd-state-add", content)

    def test_undisplaced_ghost_still_renders_as_tile(self):
        # Remove NEW: with nothing occupying the vacated rows the ghost must
        # keep its normal full-tile rendering (stripe only while displaced).
        DesignPlacement.objects.filter(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD
        ).delete()
        content = self._get()
        self.assertIn("nbx-rd-state-move_out_ghost", content)
        self.assertNotIn("nbx-rd-stripe", content)


class DesignElevationViewTest(TestCase):
    """
    The read-only projected elevation now renders ALL the design's scoped racks
    side by side, BOTH faces, the full-depth opposite hatch and a hover card —
    identically to the editor canvas, but with NO edit affordances.
    """

    user_permissions = ("netbox_rack_design.view_design",)

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.manufacturer = env["manufacturer"]
        cls.role = env["device_role"]
        cls.rack1 = env["racks"][0]  # has Device 1 (U1) and Device 2 (U2)
        cls.rack2 = env["racks"][1]  # empty
        cls.device1 = env["devices"][0]
        cls.device2 = env["devices"][1]

        cls.design = Design.objects.create(title="Elevation Design", site=cls.site)
        # The read-only elevation walks design.racks (the planning scope), like
        # the editor; both scoped racks must therefore render side by side.
        cls.design.racks.set([cls.rack1, cls.rack2])

        # A REAL full-depth device in rack 1 so the projection mirrors it onto the
        # opposite (rear) face as a passive "blocked" hatch (.nbx-rd-opposite).
        from dcim.models import Device, DeviceType

        fd_type = DeviceType.objects.create(
            manufacturer=cls.manufacturer, model="FD Type", slug="fd-type",
            u_height=2, is_full_depth=True,
        )
        Device.objects.create(
            name="FD Device", site=cls.site, rack=cls.rack1,
            position=10, face="front", device_type=fd_type, role=cls.role,
        )

        # add: a new device from the catalog into rack 1 at a free slot.
        DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            target_rack=cls.rack1,
            target_position=15,
            target_face="front",
            proposed_name="planned-node-1",
        )
        # move: relocate Device 1 from rack 1 (U1) to rack 1 (U20).
        DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.device1,
            target_rack=cls.rack1,
            target_position=20,
            target_face="front",
        )
        # remove: flag Device 2 (rack 1, U2) for removal.
        DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=cls.device2,
        )

    def _url(self, design):
        return reverse(
            "plugins:netbox_rack_design:design_elevation",
            kwargs={"pk": design.pk},
        )

    def test_elevation_view_returns_200(self):
        response = self.client.get(self._url(self.design))
        self.assertHttpStatus(response, 200)

    def test_elevation_rack_redirect_anchors_all_racks_view(self):
        # The legacy per-rack URL redirects to the all-racks view, anchored on
        # the requested rack's block, so old links never break.
        url = reverse(
            "plugins:netbox_rack_design:design_elevation_rack",
            kwargs={"pk": self.design.pk, "rack_id": self.rack1.pk},
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response["Location"].endswith(
                self._url(self.design) + f"#rd-rack-{self.rack1.pk}"
            )
        )

    def test_elevation_context_includes_all_scoped_rack_bundles(self):
        # The context carries one projected bundle per scoped rack (ordered by
        # name), each shaped like the editor's blocks (a widgets list).
        response = self.client.get(self._url(self.design))
        self.assertHttpStatus(response, 200)
        self.assertIn("rack_blocks", response.context)
        blocks = response.context["rack_blocks"]
        self.assertEqual([b["rack"].pk for b in blocks], [self.rack1.pk, self.rack2.pk])
        for bundle in blocks:
            self.assertIn("widgets", bundle)
            self.assertIsInstance(bundle["widgets"], list)

    def test_elevation_renders_each_rack_with_both_faces(self):
        # Every scoped rack renders its own block with BOTH faces present.
        response = self.client.get(self._url(self.design))
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        for rack in (self.rack1, self.rack2):
            self.assertIn(f'data-rack-id="{rack.pk}"', content)
            self.assertIn(f"nbx-rd-grid-front-{rack.pk}", content)
            self.assertIn(f"nbx-rd-grid-rear-{rack.pk}", content)

    def test_elevation_renders_full_depth_hatch(self):
        # The full-depth device in rack 1 yields a passive opposite-face hatch.
        response = self.client.get(self._url(self.design))
        self.assertHttpStatus(response, 200)
        self.assertIn("nbx-rd-opposite", response.content.decode())

    def test_elevation_has_no_editor_controls(self):
        # The read-only view must strip every edit affordance.
        response = self.client.get(self._url(self.design))
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertNotIn('id="rd-editor-save"', content)   # no Save button
        self.assertNotIn("nbx-rd-palette", content)        # no device-type catalog
        self.assertNotIn("nbx-rd-quick", content)          # no quick-access
        self.assertNotIn("nbx-rd-design-racks-card", content)  # no design-racks panel
        self.assertNotIn("nbx-rd-add-rack-card", content)  # no add-rack panel
        self.assertNotIn("nbx-rd-remove-btn", content)     # no per-tile × remove
        self.assertNotIn("nbx-rd-fav-btn", content)        # no favorite stars
        self.assertNotIn("nbx-rd-editable", content)       # static grids only

    def test_elevation_projection_states(self):
        from ..projection import ProjectedSlotState, project_rack

        result = project_rack(self.design, self.rack1)
        states = {slot["state"] for slot in result.front}
        # add -> ADD, move -> MOVE_IN + MOVE_OUT_GHOST, remove -> REMOVE.
        self.assertIn(ProjectedSlotState.ADD, states)
        self.assertIn(ProjectedSlotState.MOVE_IN, states)
        self.assertIn(ProjectedSlotState.MOVE_OUT_GHOST, states)
        self.assertIn(ProjectedSlotState.REMOVE, states)


class ElevationBrowserViewTest(TestCase):
    """The standalone Elevations LIST page: a filterable table of (design, rack) rows."""

    user_permissions = ("netbox_rack_design.view_design",)

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.rack1 = env["racks"][0]  # has Device 1 (U1) and Device 2 (U2)
        cls.rack2 = env["racks"][1]  # empty

        # Design 1 touches rack1 (add placement) -> one row.
        cls.design1 = Design.objects.create(title="Browser Design 1", site=cls.site)
        DesignPlacement.objects.create(
            design=cls.design1,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            target_rack=cls.rack1,
            target_position=10,
            target_face="front",
            proposed_name="planned-node-1",
        )

        # Design 2 touches BOTH rack2 and rack1 (two add placements) -> two rows.
        cls.design2 = Design.objects.create(title="Browser Design 2", site=cls.site)
        DesignPlacement.objects.create(
            design=cls.design2,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            target_rack=cls.rack2,
            target_position=5,
            target_face="front",
            proposed_name="planned-node-2",
        )
        DesignPlacement.objects.create(
            design=cls.design2,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            target_rack=cls.rack1,
            target_position=15,
            target_face="front",
            proposed_name="planned-node-3",
        )

    @property
    def _url(self):
        return reverse("plugins:netbox_rack_design:elevation_browser")

    def _elevation_url(self, design, rack):
        return reverse(
            "plugins:netbox_rack_design:design_elevation_rack",
            kwargs={"pk": design.pk, "rack_id": rack.pk},
        )

    def test_list_returns_200(self):
        response = self.client.get(self._url)
        self.assertHttpStatus(response, 200)

    def test_list_shows_expected_rows_and_elevation_links(self):
        response = self.client.get(self._url)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        # The derived (design, rack) pairs each appear with their per-pair links.
        self.assertIn(self.design1.title, content)
        self.assertIn(self.rack1.name, content)
        self.assertIn(self._elevation_url(self.design1, self.rack1), content)
        self.assertIn(self._elevation_url(self.design2, self.rack2), content)
        self.assertIn(self._elevation_url(self.design2, self.rack1), content)

    def test_single_value_filter_narrows_rows(self):
        # Filtering by design 1 keeps only its row, dropping design 2's rows.
        response = self.client.get(f"{self._url}?design={self.design1.pk}")
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn(self._elevation_url(self.design1, self.rack1), content)
        self.assertNotIn(self._elevation_url(self.design2, self.rack2), content)
        self.assertNotIn(self._elevation_url(self.design2, self.rack1), content)

    def test_multi_value_design_filter_returns_both(self):
        # ?design=A&design=B is OR within the field -> rows for both designs.
        response = self.client.get(
            f"{self._url}?design={self.design1.pk}&design={self.design2.pk}"
        )
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn(self._elevation_url(self.design1, self.rack1), content)
        self.assertIn(self._elevation_url(self.design2, self.rack2), content)
        self.assertIn(self._elevation_url(self.design2, self.rack1), content)

    def test_design_selection_narrows_rack_options(self):
        # Selecting design 1 (touches only rack1) limits the Rack field's offered
        # options to rack1, excluding rack2 (which only design 2 touches).
        response = self.client.get(f"{self._url}?design={self.design1.pk}")
        self.assertHttpStatus(response, 200)
        rack_field = response.context["form"].fields["rack"]
        rack_pks = set(rack_field.queryset.values_list("pk", flat=True))
        self.assertEqual(rack_pks, {self.rack1.pk})
        self.assertNotIn(self.rack2.pk, rack_pks)

    def test_unfiltered_rack_options_include_all_elevation_racks(self):
        # With no Design/Site selected, Rack options = all racks present in rows.
        response = self.client.get(self._url)
        self.assertHttpStatus(response, 200)
        rack_pks = set(response.context["form"].fields["rack"].queryset.values_list("pk", flat=True))
        self.assertEqual(rack_pks, {self.rack1.pk, self.rack2.pk})


class DesignEditorViewTest(TestCase):
    """The interactive single-rack layout editor view (Stage 2, slice 2a)."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.rack1 = env["racks"][0]  # has Device 1 (U1) and Device 2 (U2)
        cls.device1 = env["devices"][0]
        cls.device2 = env["devices"][1]

        cls.rack2 = env["racks"][1]  # also in scope (drives the switcher)
        cls.design = Design.objects.create(title="Editor Design", site=cls.site)
        # Both racks are part of the design's planning scope (design.racks).
        cls.design.racks.set([cls.rack1, cls.rack2])

        # move: relocate Device 1 from rack 1 (U1) to rack 1 (U20).
        DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.device1,
            target_rack=cls.rack1,
            target_position=20,
            target_face="front",
        )
        # remove: flag Device 2 (rack 1, U2) for removal.
        DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=cls.device2,
        )

    def _url(self, design, rack):
        return reverse(
            "plugins:netbox_rack_design:design_editor",
            kwargs={"pk": design.pk, "rack_id": rack.pk},
        )

    def test_editor_view_without_permission_denied(self):
        # No permissions granted -> the view's view_design check rejects (403/404).
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertIn(response.status_code, (403, 404))

    def test_editor_view_with_change_permission_returns_200(self):
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)

    def test_editor_view_renders_catalog_palette(self):
        # The device-type catalog palette markup must be present so the editor JS
        # can wire up search + drag-in of new adds.
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn("nbx-rd-palette", content)
        self.assertIn("nbx-rd-palette-search", content)
        self.assertIn("nbx-rd-palette-list", content)
        # The dedicated per-user "Quick access" favorites panel (independent of
        # the catalog search/manufacturer filter).
        self.assertIn("nbx-rd-quick", content)
        self.assertIn("nbx-rd-quick-list", content)
        self.assertIn("data-favorites-url", content)
        # Named favorite sets: the selector plus its new/rename/delete controls,
        # and the endpoint the editor reads them from.
        self.assertIn("data-favorite-sets-url", content)
        self.assertIn("nbx-rd-favset-select", content)
        self.assertIn("nbx-rd-favset-new", content)
        self.assertIn("nbx-rd-favset-rename", content)
        self.assertIn("nbx-rd-favset-delete", content)

    def test_editor_view_renders_role_and_tenant_selectors(self):
        # Device role + Tenant now live in a compact ALWAYS-VISIBLE toolbar row
        # (outside the collapsible drawer) that drives role/tenant on new adds.
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        # The toolbar renders NetBox API-backed DynamicModelChoiceFields (Django
        # widget ids id_device_role / id_tenant), with id_manufacturer remaining
        # in the Device drawer's catalog.
        self.assertIn("nbx-rd-toolbar", content)
        self.assertIn('id="id_device_role"', content)
        self.assertIn('id="id_tenant"', content)
        self.assertIn('id="id_manufacturer"', content)
        self.assertIn("palette_form", response.context)
        # The role + tenant selects render in the always-visible toolbar, which
        # precedes the collapsible editor shell (so they are visible regardless
        # of which drawer sections are open).
        toolbar_at = content.index("nbx-rd-toolbar")
        shell_at = content.index('id="nbx-rd-editor-shell"')
        self.assertLess(toolbar_at, shell_at)
        for field in ('id="id_device_role"', 'id="id_tenant"'):
            self.assertTrue(toolbar_at < content.index(field) < shell_at)
        # The old verbose role/tenant cards are gone from the drawer.
        self.assertNotIn("nbx-rd-role-card", content)
        self.assertNotIn("nbx-rd-tenant-card", content)
        # The legend filter and the role/tenant selects now share ONE toolbar
        # line: the legend (data-rd-legend + its data-rd-state checkboxes that
        # legend_filter.js binds) lives inside the same .nbx-rd-toolbar row, and
        # precedes the role/tenant fields on it.
        self.assertIn("data-rd-legend", content)
        for state in (
            'data-rd-state="existing"',
            'data-rd-state="add"',
            'data-rd-state="move_in"',
            'data-rd-state="move_out_ghost"',
            'data-rd-state="remove"',
        ):
            self.assertTrue(toolbar_at < content.index(state) < shell_at)
        legend_at = content.index("data-rd-legend")
        self.assertTrue(toolbar_at < legend_at < content.index('id="id_device_role"'))
        # The redundant inline hint is gone: the "next drag-in" affordance is now
        # ONLY the ⓘ tooltip, never duplicated as inline body text.
        self.assertNotIn("Applied to next drag-in", content)

    def test_editor_view_context_builds_widgets(self):
        # The view must hand a list of projected widgets to the template/JS.
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)
        self.assertIn("widgets", response.context)
        self.assertIsInstance(response.context["widgets"], list)

    def test_editor_context_includes_scoped_racks(self):
        # The switcher needs the design's scoped racks (ordered by name) and a
        # flag for whether the open rack is in scope.
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)
        self.assertIn("scoped_racks", response.context)
        scoped = list(response.context["scoped_racks"])
        self.assertEqual([r.pk for r in scoped], [self.rack1.pk, self.rack2.pk])
        self.assertTrue(response.context["current_in_scope"])

    def test_editor_renders_all_visible_racks(self):
        # The multi-rack workspace renders one block per visible scoped rack,
        # each with its own grids + per-rack widget payload. (The old one-at-a-
        # time switcher pill nav was removed in slice 2d Phase B.)
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        # The old switcher markup is gone.
        self.assertNotIn("nbx-rd-rack-switcher", content)
        # Both scoped racks render side by side, each with its own front grid +
        # per-rack JSON payload keyed by rack pk.
        for rack in (self.rack1, self.rack2):
            self.assertIn(f'data-rack-id="{rack.pk}"', content)
            self.assertIn(f"nbx-rd-grid-front-{rack.pk}", content)
            self.assertIn(f"rd-editor-data-{rack.pk}", content)

    def test_editor_out_of_scope_rack_still_renders(self):
        # A rack NOT in design.racks must still load the editor (no 404) and be
        # flagged out-of-scope in the context.
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        other_rack = Rack.objects.create(name="Rack 3", site=self.site)
        response = self.client.get(self._url(self.design, other_rack))
        self.assertHttpStatus(response, 200)
        self.assertFalse(response.context["current_in_scope"])

    def test_editor_context_visible_racks_all_when_none_hidden(self):
        # With no hidden rows the multi-rack workspace shows every scoped rack,
        # each as its own widget bundle, ordered by name.
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)
        self.assertEqual(response.context["hidden_rack_ids"], [])
        visible = response.context["visible_racks"]
        self.assertEqual([b["rack"].pk for b in visible], [self.rack1.pk, self.rack2.pk])
        # Each bundle carries the projection contract (a widgets list).
        for bundle in visible:
            self.assertIn("widgets", bundle)
            self.assertIsInstance(bundle["widgets"], list)

    def test_editor_context_excludes_hidden_rack_for_user(self):
        # A rack the requesting user has hidden is dropped from visible_racks and
        # reported in hidden_rack_ids; the design's scope is unchanged.
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        HiddenDesignRack.objects.create(
            user=self.user, design=self.design, rack=self.rack2
        )
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)
        self.assertEqual(response.context["hidden_rack_ids"], [self.rack2.pk])
        visible = response.context["visible_racks"]
        self.assertEqual([b["rack"].pk for b in visible], [self.rack1.pk])
        # The full planning scope is still both racks.
        self.assertEqual(
            [r.pk for r in response.context["scoped_racks"]],
            [self.rack1.pk, self.rack2.pk],
        )

    def test_editor_visibility_is_per_user(self):
        # A rack hidden by a DIFFERENT user does not affect this user's view.
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        from users.models import User

        other_user = User.objects.create_user(username="other_editor")
        HiddenDesignRack.objects.create(
            user=other_user, design=self.design, rack=self.rack2
        )
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)
        self.assertEqual(response.context["hidden_rack_ids"], [])
        visible = response.context["visible_racks"]
        self.assertEqual([b["rack"].pk for b in visible], [self.rack1.pk, self.rack2.pk])

    def test_editor_renders_tool_panels(self):
        # The editing tools include an "Add rack" panel (location + rack choosers)
        # and a "Design racks" panel listing every scoped rack.
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        # Add-rack panel + its API-backed location/rack choosers.
        self.assertIn("nbx-rd-add-rack-card", content)
        self.assertIn("nbx-rd-add-rack-btn", content)
        self.assertIn('id="id_add_location"', content)
        self.assertIn('id="id_add_rack"', content)
        # Design-racks panel + "All" reveal control.
        self.assertIn("nbx-rd-design-racks-card", content)
        self.assertIn("nbx-rd-show-all-racks", content)
        # The choosers come from a form passed via context.
        self.assertIn("add_rack_form", response.context)

    def test_editor_tools_live_in_collapsible_drawer(self):
        # The editing tools live in ONE push/collapse drawer that is split into
        # three INDEPENDENT sections (Device / Favorites / Racks), each toggled
        # on/off by its own card-header button. The rack workspace is the PRIMARY
        # region (it follows the drawer in the shell so it spans the full width
        # when the drawer is closed).
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)
        content = response.content.decode()

        # The shell + drawer + the three section toggles exist.
        self.assertIn('id="nbx-rd-editor-shell"', content)
        self.assertIn('id="nbx-rd-drawer"', content)
        self.assertIn('data-rd-section-toggle="device"', content)
        self.assertIn('data-rd-section-toggle="favorites"', content)
        self.assertIn('data-rd-section-toggle="racks"', content)
        # The old single "Tools" toggle is gone.
        self.assertNotIn('id="nbx-rd-drawer-toggle"', content)
        # The old always-on left-rail / quick-access column wrappers are gone.
        self.assertNotIn("nbx-rd-leftrail", content)
        self.assertNotIn("nbx-rd-quick-col", content)
        # Default state is CLOSED: the server does not pre-open the drawer.
        self.assertNotIn("drawer-open", content)
        # The drawer is split into the three named sections.
        self.assertIn('data-rd-section="device"', content)
        self.assertIn('data-rd-section="favorites"', content)
        self.assertIn('data-rd-section="racks"', content)

        # Every tool lives INSIDE the drawer (between the drawer's opening tag and
        # the rack workspace that follows it in the shell).
        drawer_at = content.index('id="nbx-rd-drawer"')
        racks_at = content.index('id="nbx-rd-racks-scroll"')
        self.assertLess(drawer_at, racks_at)
        for tool in (
            "nbx-rd-palette",
            "nbx-rd-add-rack-card",
            "nbx-rd-design-racks-card",
            'id="nbx-rd-quick"',
        ):
            tool_at = content.index(tool)
            self.assertGreater(tool_at, drawer_at)
            self.assertLess(tool_at, racks_at, f"{tool} should be inside the drawer")
        # Role + tenant are NOT in the drawer: they live in the always-visible
        # toolbar that precedes the shell (and thus the drawer).
        self.assertLess(content.index("nbx-rd-toolbar"), drawer_at)
        for field in ('id="id_device_role"', 'id="id_tenant"'):
            self.assertLess(content.index(field), drawer_at)

    def test_editor_drawer_sections_group_their_panels(self):
        # Each drawer section wraps exactly the panels it owns: Device groups the
        # device-type catalog (role + tenant moved to the always-visible toolbar);
        # Racks groups add-rack + design-racks; Favorites groups quick access. We
        # assert the section markers and their panels appear in the expected order
        # in the rendered drawer.
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)
        content = response.content.decode()

        device_at = content.index('data-rd-section="device"')
        racks_at = content.index('data-rd-section="racks"')
        favorites_at = content.index('data-rd-section="favorites"')

        # Device section owns the device-type catalog only (role + tenant moved
        # to the always-visible toolbar above the shell).
        self.assertTrue(device_at < content.index("nbx-rd-palette") < racks_at)
        # Racks section owns the add-rack + design-racks panels.
        for panel in ("nbx-rd-add-rack-card", "nbx-rd-design-racks-card"):
            self.assertTrue(racks_at < content.index(panel) < favorites_at)
        # Favorites section owns the quick-access panel.
        self.assertGreater(content.index('id="nbx-rd-quick"'), favorites_at)

    def test_editor_add_rack_form_fields_in_context(self):
        # The add-rack form exposes location + rack DynamicModelChoiceFields.
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)
        form = response.context["add_rack_form"]
        self.assertIn("add_location", form.fields)
        self.assertIn("add_rack", form.fields)

    def test_editor_design_racks_panel_lists_every_scoped_rack(self):
        # The "Design racks" panel renders one row per scoped rack (with its
        # show/hide toggle + remove control), regardless of visibility.
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        rows = response.context["scoped_rack_rows"]
        self.assertEqual([r["rack"].pk for r in rows], [self.rack1.pk, self.rack2.pk])
        for rack in (self.rack1, self.rack2):
            self.assertIn(f'data-rd-rack-row="{rack.pk}"', content)
            self.assertIn(f'data-rd-visi-toggle="{rack.pk}"', content)
            self.assertIn(f'data-rd-remove-rack="{rack.pk}"', content)

    def test_editor_renders_all_scoped_blocks_with_hidden_class(self):
        # Phase C renders EVERY scoped rack block (not just visible ones); blocks
        # whose pk is hidden for this user carry the `hidden` class so the panel
        # can show/hide them with no reload.
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        HiddenDesignRack.objects.create(
            user=self.user, design=self.design, rack=self.rack2
        )
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        blocks = response.context["all_rack_blocks"]
        self.assertEqual([b["rack"].pk for b in blocks], [self.rack1.pk, self.rack2.pk])
        self.assertEqual([b["hidden"] for b in blocks], [False, True])
        # The hidden rack's block still renders (just visually hidden) so its
        # initRack controller runs and the toggle is reload-free.
        self.assertIn(f"rd-editor-data-{self.rack2.pk}", content)
        self.assertIn('class="nbx-rd-rack-block hidden"', content)
        # The visible rack's block is rendered without the hidden class.
        self.assertIn('class="nbx-rd-rack-block"', content)

    def test_all_rack_blocks_widgets_match_projection(self):
        # Every scoped rack's block must carry exactly the widgets that
        # projection.project_rack (flattened by _slot_to_widget) yields for that
        # rack -- proving the multi-rack workspace reuses the projection contract.
        from .. import projection
        from ..views import _slot_to_widget

        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)
        blocks = response.context["all_rack_blocks"]
        self.assertEqual([b["rack"].pk for b in blocks], [self.rack1.pk, self.rack2.pk])

        for block in blocks:
            result = projection.project_rack(self.design, block["rack"])
            expected = [
                _slot_to_widget(slot)
                for slot in (*result.front, *result.rear, *result.non_racked)
            ]
            self.assertEqual(block["widgets"], expected)
        # Rack 1 (with a move + a remove) actually projects some widgets, so the
        # comparison above is non-vacuous.
        self.assertTrue(blocks[0]["widgets"])


class RackFloorAlignmentMarkupTest(TestCase):
    """Racks of different heights hang from a common floor, not a common ceiling.

    The alignment itself is measured in the browser (rack_layout.js pads the
    shorter rack's face rows); what CI can hold is the wiring it needs -- the
    padded row carries the hook class, and both rack-rendering pages load the
    script. Silently dropping either leaves the racks top-aligned again with
    nothing failing.
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.rack1 = env["racks"][0]
        cls.rack2 = env["racks"][1]
        cls.design = Design.objects.create(title="Floor Design", site=cls.site)
        cls.design.racks.set([cls.rack1, cls.rack2])

    def _get(self, name, **kwargs):
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
        )
        response = self.client.get(
            reverse(f"plugins:netbox_rack_design:{name}", kwargs=kwargs))
        self.assertHttpStatus(response, 200)
        return response.content.decode()

    def test_editor_pads_face_rows_and_loads_the_layout_script(self):
        content = self._get(
            "design_editor", pk=self.design.pk, rack_id=self.rack1.pk)
        self.assertIn("nbx-rd-face-row", content)
        self.assertIn("js/rack_layout.js", content)

    def test_elevation_pads_face_rows_and_loads_the_layout_script(self):
        content = self._get("design_elevation", pk=self.design.pk)
        self.assertIn("nbx-rd-face-row", content)
        self.assertIn("js/rack_layout.js", content)


class DesignPlannedFeedPanelTest(TestCase):
    """The design detail page lists this design's PLANNED power feeds.

    They size a greenfield rack's capacity bar, and until 0.21.0 they were
    visible through no UI route whatsoever -- so a feed copied by mistake
    inflated the bar with nothing to point at (user 2026-08-28: "where do we
    look at the feeds we created?").
    """

    user_permissions = ("netbox_rack_design.view_design",)

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.rack = env["racks"][0]
        cls.device_type = env["device_type"]
        cls.role = env["device_role"]
        cls.design = Design.objects.create(title="Feed panel design", site=cls.site)
        cls.feed = DesignPowerFeed.objects.create(
            design=cls.design, rack=cls.rack, name="R101-A",
            voltage=230, amperage=32,
        )
        cls.pdu = DesignPlacement.objects.create(
            design=cls.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type, device_role=cls.role,
            target_rack=cls.rack, target_position=Decimal("1.0"),
            target_face="front", proposed_name="pdu-a1",
            planned_power_feed=cls.feed,
        )

    def _url(self):
        return reverse("plugins:netbox_rack_design:design", kwargs={"pk": self.design.pk})

    def test_panel_lists_the_feed_with_its_derated_capacity(self):
        response = self.client.get(self._url())
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn("Planned power feeds", content)
        self.assertIn("R101-A", content)
        rows = response.context["planned_feeds"]
        self.assertEqual([r["feed"].pk for r in rows], [self.feed.pk])
        # 230 V x 32 A = 7360 W, derated by POWERFEED_DEFAULT_MAX_UTILIZATION --
        # the same number the capacity bar sizes against, not the raw breaker.
        from netbox.config import get_config
        max_util = get_config().POWERFEED_DEFAULT_MAX_UTILIZATION or 100
        self.assertEqual(rows[0]["watts"], round(230 * 32 * max_util / 100.0))

    def test_panel_names_the_pdus_bound_to_the_feed(self):
        """A feed's row says what breaks if it is removed."""
        response = self.client.get(self._url())
        self.assertEqual(
            [p.pk for p in response.context["planned_feeds"][0]["bound"]],
            [self.pdu.pk])
        self.assertIn("pdu-a1", response.content.decode())

    def test_a_design_without_planned_feeds_says_so(self):
        other = Design.objects.create(title="No feeds", site=self.site)
        response = self.client.get(
            reverse("plugins:netbox_rack_design:design", kwargs={"pk": other.pk}))
        self.assertHttpStatus(response, 200)
        self.assertEqual(response.context["planned_feeds"], [])
        self.assertIn("No planned feeds", response.content.decode())


class DesignEditorDefaultRouteTest(TestCase):
    """
    The design-only editor route (``design_editor_default``) RENDERS the
    multi-rack editor directly — it is the primary entry point and must open even
    for a design with ZERO scoped racks (no bounce to the detail page), so the
    first rack can be added from inside the editor.
    """

    user_permissions = (
        "netbox_rack_design.view_design",
        "netbox_rack_design.change_design",
    )

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.rack1 = env["racks"][0]
        cls.rack2 = env["racks"][1]
        cls.design = Design.objects.create(title="Default Route Design", site=cls.site)
        cls.design.racks.set([cls.rack2, cls.rack1])  # set out of order on purpose

        cls.empty_design = Design.objects.create(title="Empty Scope Design", site=cls.site)

    def _default_url(self, design):
        return reverse(
            "plugins:netbox_rack_design:design_editor_default",
            kwargs={"pk": design.pk},
        )

    def test_default_route_renders_editor_with_racks(self):
        # With scoped racks the default route renders the editor (NOT a redirect)
        # with every scoped rack block side by side, ordered by name.
        response = self.client.get(self._default_url(self.design))
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn('id="rd-editor"', content)
        self.assertEqual(
            [b["rack"].pk for b in response.context["all_rack_blocks"]],
            [self.rack1.pk, self.rack2.pk],
        )
        for rack in (self.rack1, self.rack2):
            self.assertIn(f'data-rack-id="{rack.pk}"', content)
            self.assertIn(f"rd-editor-data-{rack.pk}", content)
        # No empty state when racks exist; drawer keeps its normal closed default
        # (no initial section is signalled).
        self.assertNotIn("nbx-rd-empty-state", content)
        self.assertIn('data-drawer-section-initial=""', content)

    def test_default_route_empty_scope_renders_editor(self):
        # ZERO scoped racks: the editor STILL renders (no redirect to detail),
        # showing a friendly empty state with an "Add your first rack" button and
        # defaulting the drawer OPEN on the Racks section so Add-rack is reachable.
        response = self.client.get(self._default_url(self.empty_design))
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn('id="rd-editor"', content)
        self.assertEqual(response.context["all_rack_blocks"], [])
        self.assertFalse(response.context["has_racks"])
        # Empty-state message + the add-first-rack shortcut button.
        self.assertIn("nbx-rd-empty-state", content)
        self.assertIn("nbx-rd-add-first-rack", content)
        # Drawer defaults to the Racks section for an empty design so Add-rack is
        # reachable as soon as the editor loads.
        self.assertIn('data-drawer-section-initial="racks"', content)
        # The Add-rack panel itself is present in the (open) drawer.
        self.assertIn("nbx-rd-add-rack-card", content)
        self.assertIn('id="id_add_rack"', content)
        # No rack blocks rendered.
        self.assertNotIn("rd-editor-data-", content)

    def test_default_route_empty_scope_does_not_redirect(self):
        # Explicit guard against a regression to the old bounce-to-detail flow.
        response = self.client.get(self._default_url(self.empty_design))
        self.assertEqual(response.status_code, 200)

    def test_detail_open_editor_link_targets_default_route(self):
        # The Design detail page's "Open editor" button must point at the default
        # (no-rack) route so it works even for an empty design.
        default_url = self._default_url(self.empty_design)
        response = self.client.get(self.empty_design.get_absolute_url())
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn(f'href="{default_url}"', content)
        self.assertIn("Open editor", content)


class DesignAffectedRacksTest(TestCase):
    """The Design detail page lists affected racks with per-rack view links."""

    user_permissions = (
        "netbox_rack_design.view_design",
        "dcim.view_rack",
    )

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.rack1 = env["racks"][0]  # holds the real device referenced below
        cls.rack2 = env["racks"][1]  # targeted by an add placement
        cls.device1 = env["devices"][0]

        cls.design = Design.objects.create(title="Affected Racks Design", site=cls.site)
        # add into rack2 -> rack2 is affected via target_rack
        DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            target_rack=cls.rack2,
            target_position=5,
            target_face="front",
        )
        # remove device1 -> rack1 is affected via device.rack
        DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=cls.device1,
        )

    def test_detail_page_lists_affected_racks(self):
        url = reverse("plugins:netbox_rack_design:design", kwargs={"pk": self.design.pk})
        response = self.client.get(url)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()

        for rack in (self.rack1, self.rack2):
            self.assertIn(rack.name, content)
            elevation_url = reverse(
                "plugins:netbox_rack_design:design_elevation_rack",
                kwargs={"pk": self.design.pk, "rack_id": rack.pk},
            )
            self.assertIn(elevation_url, content)


class DesignScopedRacksPanelTest(TestCase):
    """The Design detail page lists design.racks with per-rack editor links."""

    user_permissions = (
        "netbox_rack_design.view_design",
        "netbox_rack_design.change_design",
        "dcim.view_rack",
    )

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.rack1 = env["racks"][0]
        cls.rack2 = env["racks"][1]
        cls.design = Design.objects.create(title="Scoped Racks Design", site=cls.site)
        cls.design.racks.set([cls.rack1, cls.rack2])

    def test_detail_context_includes_scoped_racks(self):
        url = reverse("plugins:netbox_rack_design:design", kwargs={"pk": self.design.pk})
        response = self.client.get(url)
        self.assertHttpStatus(response, 200)
        self.assertIn("scoped_racks", response.context)
        scoped = list(response.context["scoped_racks"])
        self.assertEqual([r.pk for r in scoped], [self.rack1.pk, self.rack2.pk])

    def test_detail_page_lists_scoped_racks_with_editor_links(self):
        url = reverse("plugins:netbox_rack_design:design", kwargs={"pk": self.design.pk})
        response = self.client.get(url)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        for rack in (self.rack1, self.rack2):
            self.assertIn(rack.name, content)
            editor_url = reverse(
                "plugins:netbox_rack_design:design_editor",
                kwargs={"pk": self.design.pk, "rack_id": rack.pk},
            )
            self.assertIn(editor_url, content)


class RackDesignsPanelTest(TestCase):
    """The injected panel on the core dcim.rack page lists touching designs."""

    user_permissions = (
        "dcim.view_rack",
        "netbox_rack_design.view_design",
        "netbox_rack_design.view_designplacement",
    )

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.rack = env["racks"][1]  # empty rack with free U slots

        cls.design = Design.objects.create(title="Panel Design", site=cls.site)
        DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            target_rack=cls.rack,
            target_position=5,
            target_face="front",
        )

    def _rack_url(self, rack):
        return reverse("dcim:rack", kwargs={"pk": rack.pk})

    def test_panel_lists_touching_design(self):
        response = self.client.get(self._rack_url(self.rack))
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn("Rack Designs", content)
        elevation_url = reverse(
            "plugins:netbox_rack_design:design_elevation_rack",
            kwargs={"pk": self.design.pk, "rack_id": self.rack.pk},
        )
        self.assertIn(elevation_url, content)


class FrozenDesignStillRendersTest(TestCase):
    """
    G4 says read paths stay open: an approved (frozen) design must still
    render, project and be viewable everywhere. No GET is gated.
    """

    user_permissions = (
        "netbox_rack_design.view_design",
        "netbox_rack_design.view_designplacement",
    )

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.rack = env["racks"][0]
        cls.design = Design.objects.create(title="Frozen render design", site=cls.site)
        cls.design.racks.add(cls.rack)
        DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            target_rack=cls.rack,
            target_position=10,
            target_face="front",
        )
        cls.design.status = DesignStatusChoices.STATUS_APPROVED
        cls.design.save()

    def test_detail_page_still_renders(self):
        url = reverse("plugins:netbox_rack_design:design", kwargs={"pk": self.design.pk})
        response = self.client.get(url)
        self.assertHttpStatus(response, 200)

    def test_elevation_still_renders(self):
        url = reverse("plugins:netbox_rack_design:design_elevation", kwargs={"pk": self.design.pk})
        response = self.client.get(url)
        self.assertHttpStatus(response, 200)

    def test_editor_default_still_renders(self):
        # Note: viewing the editor needs only view_design (change_design gates
        # editing affordances client-side / on save-layout, not the page GET).
        url = reverse(
            "plugins:netbox_rack_design:design_editor_default", kwargs={"pk": self.design.pk}
        )
        response = self.client.get(url)
        self.assertHttpStatus(response, 200)


class DesignPlacementFrozenWriteTest(TestCase):
    """
    DesignPlacement delete/bulk-delete never run ``clean()`` (Django's delete
    path calls no clean()), so they need their own frozen check
    (PLAN-design-chains.md §2.2/G4) -- unlike create/edit, which already get
    it for free from ``DesignPlacement.clean()`` via the form's
    ``full_clean()``.
    """

    user_permissions = (
        "netbox_rack_design.view_design",
        "netbox_rack_design.view_designplacement",
        "netbox_rack_design.delete_designplacement",
    )

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.rack = env["racks"][1]
        cls.design = Design.objects.create(title="Frozen placement design", site=cls.site)
        cls.placement = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            target_rack=cls.rack,
            target_position=5,
            target_face="front",
        )
        # Set APPROVED after creating the placement: ORM .create() never runs
        # clean(), so this reaches a frozen design with a placement already
        # attached -- exactly the state delete must guard against.
        cls.design.status = DesignStatusChoices.STATUS_APPROVED
        cls.design.save()

    def test_delete_rejected_when_frozen(self):
        url = reverse(
            "plugins:netbox_rack_design:designplacement_delete", kwargs={"pk": self.placement.pk}
        )
        response = self.client.post(url, {"confirm": "true"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(DesignPlacement.objects.filter(pk=self.placement.pk).exists())

    def test_bulk_delete_rejected_when_frozen(self):
        url = reverse("plugins:netbox_rack_design:designplacement_bulk_delete")
        response = self.client.post(url, {
            "pk": [self.placement.pk], "_confirm": "1", "confirm": "true",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(DesignPlacement.objects.filter(pk=self.placement.pk).exists())


class DesignPowerFeedFrozenWriteTest(TestCase):
    """
    DesignPowerFeed has NO ``clean()`` override at all, so every one of its
    CRUD views needs an explicit frozen check (PLAN-design-chains.md §2.2/G4).
    """

    user_permissions = (
        "netbox_rack_design.view_design",
        "netbox_rack_design.view_designpowerfeed",
        "netbox_rack_design.add_designpowerfeed",
        "netbox_rack_design.change_designpowerfeed",
        "netbox_rack_design.delete_designpowerfeed",
    )

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.rack = env["racks"][0]
        cls.design = Design.objects.create(title="Frozen feed design", site=cls.site)
        cls.feed = DesignPowerFeed.objects.create(design=cls.design, rack=cls.rack, name="Feed A")
        cls.design.status = DesignStatusChoices.STATUS_APPROVED
        cls.design.save()

    def test_edit_rejected_when_frozen(self):
        url = reverse(
            "plugins:netbox_rack_design:designpowerfeed_edit", kwargs={"pk": self.feed.pk}
        )
        response = self.client.post(url, {
            "design": self.design.pk, "rack": self.rack.pk, "name": "Feed A renamed",
            "voltage": 230, "amperage": 32,
            "phase": PowerFeedPhaseChoices.PHASE_SINGLE, "supply": PowerFeedSupplyChoices.SUPPLY_AC,
        })
        self.assertEqual(response.status_code, 302)
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.name, "Feed A")

    def test_delete_rejected_when_frozen(self):
        url = reverse(
            "plugins:netbox_rack_design:designpowerfeed_delete", kwargs={"pk": self.feed.pk}
        )
        response = self.client.post(url, {"confirm": "true"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(DesignPowerFeed.objects.filter(pk=self.feed.pk).exists())

    def test_bulk_delete_rejected_when_frozen(self):
        url = reverse("plugins:netbox_rack_design:designpowerfeed_bulk_delete")
        response = self.client.post(url, {
            "pk": [self.feed.pk], "_confirm": "1", "confirm": "true",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(DesignPowerFeed.objects.filter(pk=self.feed.pk).exists())


class DesignDeriveViewTest(TestCase):
    """"Derive design" action (PLAN-design-chains.md §5 phase 1)."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.approved = Design.objects.create(
            title="Approved parent", site=cls.site, status=DesignStatusChoices.STATUS_APPROVED,
        )
        cls.draft = Design.objects.create(title="Draft parent", site=cls.site)

    def _url(self, design):
        return reverse("plugins:netbox_rack_design:design_derive", kwargs={"pk": design.pk})

    def test_derive_from_approved_creates_child_based_on_it(self):
        self.add_permissions("netbox_rack_design.view_design", "netbox_rack_design.add_design")
        response = self.client.post(self._url(self.approved), {"title": "Derived child"})
        self.assertEqual(response.status_code, 302)
        child = Design.objects.exclude(pk=self.approved.pk).exclude(pk=self.draft.pk).get()
        self.assertEqual(child.based_on_id, self.approved.pk)
        self.assertEqual(child.status, DesignStatusChoices.STATUS_DRAFT)
        self.assertEqual(child.title, "Derived child")

    def test_derive_get_renders_form_prefilled_with_suggested_title(self):
        self.add_permissions("netbox_rack_design.view_design", "netbox_rack_design.add_design")
        response = self.client.get(self._url(self.approved))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Approved parent (derived)")
        self.assertEqual(
            response.context["form"].initial["title"], "Approved parent (derived)"
        )

    def test_derive_with_custom_title_uses_it_exactly(self):
        self.add_permissions("netbox_rack_design.view_design", "netbox_rack_design.add_design")
        response = self.client.post(self._url(self.approved), {"title": "My custom title"})
        self.assertEqual(response.status_code, 302)
        child = Design.objects.exclude(pk=self.approved.pk).exclude(pk=self.draft.pk).get()
        self.assertEqual(child.title, "My custom title")

    def test_derive_with_empty_title_creates_nothing_and_shows_error(self):
        self.add_permissions("netbox_rack_design.view_design", "netbox_rack_design.add_design")
        count_before = Design.objects.count()
        response = self.client.post(self._url(self.approved), {"title": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Design.objects.count(), count_before)
        self.assertTrue(response.context["form"].errors.get("title"))

    def test_derive_copies_parents_rack_scope_as_a_snapshot(self):
        # G6: the child must open onto the parent's racks, not an empty scope.
        self.approved.racks.set(self.racks)
        self.add_permissions("netbox_rack_design.view_design", "netbox_rack_design.add_design")
        response = self.client.post(self._url(self.approved), {"title": "Derived child"})
        self.assertEqual(response.status_code, 302)
        child = Design.objects.exclude(pk=self.approved.pk).exclude(pk=self.draft.pk).get()
        self.assertEqual(
            set(child.racks.values_list("pk", flat=True)),
            {r.pk for r in self.racks},
        )

    def test_derive_from_parent_with_no_racks_succeeds_with_empty_scope(self):
        self.add_permissions("netbox_rack_design.view_design", "netbox_rack_design.add_design")
        self.assertEqual(self.approved.racks.count(), 0)
        response = self.client.post(self._url(self.approved), {"title": "Derived child"})
        self.assertEqual(response.status_code, 302)
        child = Design.objects.exclude(pk=self.approved.pk).exclude(pk=self.draft.pk).get()
        self.assertEqual(child.racks.count(), 0)

    def test_derive_rack_scope_is_a_snapshot_not_a_live_link(self):
        # Later racks added to the parent must NOT retroactively appear on
        # the child -- the child owns its own scope once derived (G6).
        self.approved.racks.set([self.racks[0]])
        self.add_permissions("netbox_rack_design.view_design", "netbox_rack_design.add_design")
        response = self.client.post(self._url(self.approved), {"title": "Derived child"})
        self.assertEqual(response.status_code, 302)
        child = Design.objects.exclude(pk=self.approved.pk).exclude(pk=self.draft.pk).get()
        self.assertEqual(
            set(child.racks.values_list("pk", flat=True)), {self.racks[0].pk}
        )

        self.approved.racks.add(self.racks[1])
        child.refresh_from_db()
        self.assertEqual(
            set(child.racks.values_list("pk", flat=True)), {self.racks[0].pk}
        )

    def test_derive_from_draft_is_refused(self):
        self.add_permissions("netbox_rack_design.view_design", "netbox_rack_design.add_design")
        before = set(Design.objects.values_list("pk", flat=True))
        response = self.client.post(self._url(self.draft), {"title": "Derived child"})
        self.assertEqual(response.status_code, 302)
        # No new design was created.
        after = set(Design.objects.values_list("pk", flat=True))
        self.assertEqual(before, after)

    def test_derive_without_permission_denied(self):
        response = self.client.get(self._url(self.approved))
        self.assertIn(response.status_code, (403, 404))
        response = self.client.post(self._url(self.approved), {"title": "Derived child"})
        self.assertIn(response.status_code, (403, 404))
        self.assertEqual(Design.objects.count(), 2)


class DesignRebaseViewTest(TestCase):
    """"Re-base" action (PLAN-design-chains.md §2.2/§9.2)."""

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.approved_a = Design.objects.create(
            title="Approved A", site=cls.site, status=DesignStatusChoices.STATUS_APPROVED,
        )
        cls.approved_b = Design.objects.create(
            title="Approved B", site=cls.site, status=DesignStatusChoices.STATUS_APPROVED,
        )
        cls.draft_target = Design.objects.create(title="Draft target", site=cls.site)
        cls.child = Design.objects.create(
            title="Child", site=cls.site, based_on=cls.approved_a,
        )

    def _url(self, design):
        return reverse("plugins:netbox_rack_design:design_rebase", kwargs={"pk": design.pk})

    def test_rebase_to_approved_target_succeeds(self):
        self.add_permissions("netbox_rack_design.view_design", "netbox_rack_design.change_design")
        response = self.client.post(self._url(self.child), {"based_on": self.approved_b.pk})
        self.assertEqual(response.status_code, 302)
        self.child.refresh_from_db()
        self.assertEqual(self.child.based_on_id, self.approved_b.pk)

    def test_rebase_to_draft_target_refused(self):
        self.add_permissions("netbox_rack_design.view_design", "netbox_rack_design.change_design")
        response = self.client.post(self._url(self.child), {"based_on": self.draft_target.pk})
        self.assertEqual(response.status_code, 200)  # re-renders the form with an error
        self.child.refresh_from_db()
        self.assertEqual(self.child.based_on_id, self.approved_a.pk)

    def test_rebase_creating_a_cycle_is_refused(self):
        self.add_permissions("netbox_rack_design.view_design", "netbox_rack_design.change_design")
        # approved_a based on nothing today; point it at `child` -> child -> a
        # would only cycle if we then re-based `child` onto something that
        # loops back to itself. Build A -> B -> child, then try to rebase A
        # onto child (A -> B -> child -> A is a cycle).
        self.approved_b.based_on = self.approved_a
        self.approved_b.full_clean()
        self.approved_b.save()
        self.child.based_on = self.approved_b
        self.child.full_clean()
        self.child.save()
        # Re-approve A isn't needed; try to rebase approved_a onto child --
        # DesignRebaseForm restricts the queryset to APPROVED designs, so
        # child (draft) cannot even be offered/selected -- approve it first
        # to exercise the cycle guard itself, not the approved-only rule.
        self.child.status = DesignStatusChoices.STATUS_APPROVED
        self.child.save()
        response = self.client.post(self._url(self.approved_a), {"based_on": self.child.pk})
        self.assertEqual(response.status_code, 200)
        self.approved_a.refresh_from_db()
        self.assertIsNone(self.approved_a.based_on_id)

    def test_rebase_without_permission_denied(self):
        response = self.client.get(self._url(self.child))
        self.assertIn(response.status_code, (403, 404))
        response = self.client.post(self._url(self.child), {"based_on": self.approved_b.pk})
        self.assertIn(response.status_code, (403, 404))
        self.child.refresh_from_db()
        self.assertEqual(self.child.based_on_id, self.approved_a.pk)


class DesignEditorChainWidgetTest(TestCase):
    """
    Phase 4 (PLAN-design-chains.md §5/G3): the editor payload must carry
    provenance (``inherited``/``source_design_id``/``source_design_name``) and
    conflict flags (``conflict``/``conflict_reason``) on every widget dict, and
    the design-level ``conflicts`` a chain produces must reach the editor
    context as ``chain_conflicts`` so the frontend can render one persistent
    panel (§8.2/§8.3).
    """

    user_permissions = (
        "netbox_rack_design.view_design",
        "netbox_rack_design.change_design",
    )

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.rack = env["racks"][0]

        cls.parent = Design.objects.create(title="Network sweep IDS-1000", site=cls.site)
        cls.parent.racks.add(cls.rack)
        cls.upstream_add = DesignPlacement.objects.create(
            design=cls.parent,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            target_rack=cls.rack,
            target_position=10,
            target_face="front",
            proposed_name="srv-a",
        )
        cls.parent.status = DesignStatusChoices.STATUS_APPROVED
        cls.parent.save()

        cls.child = Design.objects.create(
            title="Server build IDS-2000", site=cls.site, based_on=cls.parent,
        )
        cls.child.racks.add(cls.rack)
        # The child's OWN placement, so there is at least one non-inherited
        # widget in the same rack to contrast against.
        cls.own_add = DesignPlacement.objects.create(
            design=cls.child,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            target_rack=cls.rack,
            target_position=20,
            target_face="front",
            proposed_name="own-device",
        )

        # An unrelated, unchained design in the same scope for the "unchanged
        # payload" assertion.
        cls.plain = Design.objects.create(title="Plain design", site=cls.site)
        cls.plain.racks.add(cls.rack)

    def _editor_url(self, design):
        return reverse(
            "plugins:netbox_rack_design:design_editor_default",
            kwargs={"pk": design.pk},
        )

    def _widget_by_placement(self, widgets, placement_id):
        matches = [w for w in widgets if w["placement_id"] == placement_id]
        self.assertEqual(len(matches), 1, widgets)
        return matches[0]

    def test_inherited_widget_carries_provenance(self):
        response = self.client.get(self._editor_url(self.child))
        self.assertHttpStatus(response, 200)
        block = response.context["all_rack_blocks"][0]
        widget = self._widget_by_placement(block["widgets"], self.upstream_add.pk)
        self.assertTrue(widget["inherited"])
        self.assertEqual(widget["source_design_id"], self.parent.pk)
        self.assertEqual(widget["source_design_name"], str(self.parent))
        self.assertFalse(widget["conflict"])
        self.assertIsNone(widget["conflict_reason"])

    def test_own_widget_is_not_inherited(self):
        response = self.client.get(self._editor_url(self.child))
        self.assertHttpStatus(response, 200)
        block = response.context["all_rack_blocks"][0]
        widget = self._widget_by_placement(block["widgets"], self.own_add.pk)
        self.assertFalse(widget["inherited"])
        self.assertIsNone(widget["source_design_id"])
        self.assertIsNone(widget["source_design_name"])
        self.assertFalse(widget["conflict"])
        self.assertIsNone(widget["conflict_reason"])

    def test_unchained_design_payload_unchanged(self):
        # A design with no based_on gets the same five keys, all falsy/None,
        # and an empty chain_conflicts -- the new keys must not perturb the
        # existing single-layer case.
        response = self.client.get(self._editor_url(self.plain))
        self.assertHttpStatus(response, 200)
        self.assertEqual(response.context["chain_conflicts"], [])
        block = response.context["all_rack_blocks"][0]
        self.assertTrue(block["widgets"])
        for widget in block["widgets"]:
            self.assertFalse(widget["inherited"])
            self.assertIsNone(widget["source_design_id"])
            self.assertIsNone(widget["source_design_name"])
            self.assertFalse(widget["conflict"])
            self.assertIsNone(widget["conflict_reason"])

    def test_chain_conflicts_empty_for_a_clean_approved_chain(self):
        response = self.client.get(self._editor_url(self.child))
        self.assertHttpStatus(response, 200)
        self.assertEqual(response.context["chain_conflicts"], [])

    def test_chain_conflicts_populated_for_an_implemented_ancestor(self):
        self.parent.status = DesignStatusChoices.STATUS_IMPLEMENTED
        self.parent.save()
        response = self.client.get(self._editor_url(self.child))
        self.assertHttpStatus(response, 200)
        conflicts = response.context["chain_conflicts"]
        self.assertEqual(len(conflicts), 1, conflicts)
        entry = conflicts[0]
        self.assertEqual(entry["kind"], "ancestor_implemented")
        self.assertEqual(entry["severity"], "error")
        self.assertTrue(entry["detail"])
        self.assertEqual(entry["rack_id"], self.rack.pk)
        self.assertEqual(entry["source_design_id"], self.parent.pk)
        self.assertEqual(entry["source_design_name"], str(self.parent))
        # A chain-level refusal is not about any one tile.
        self.assertIsNone(entry["slot_key"])
        # And the inherited layer is gone entirely -- the upstream add no
        # longer projects at all, so the refusal is visible instead of a
        # silently-vanished rack (§9.5).
        block = response.context["all_rack_blocks"][0]
        placement_ids = [w["placement_id"] for w in block["widgets"]]
        self.assertNotIn(self.upstream_add.pk, placement_ids)

    def test_settled_name_conflict_surfaces_on_slot_and_in_chain_conflicts(self):
        # A resolvable prefix source is not configured in these tests, so a
        # named upstream placement whose name carries no derivable prefix
        # still resolves cleanly (see naming.py) -- exercise the case that
        # DOES fail: reuse the projection-level guarantee that a conflict flag
        # on a slot always has a matching chain_conflicts row referencing the
        # SAME placement (identity), by forcing settled_name resolution to
        # fail via an unresolvable planning prefix token.
        from unittest.mock import patch

        with patch(
            "netbox_rack_design.naming.settled_name_status",
            return_value=(None, {"detail": "no prefix source configured"}),
        ):
            response = self.client.get(self._editor_url(self.child))
        self.assertHttpStatus(response, 200)
        block = response.context["all_rack_blocks"][0]
        widget = self._widget_by_placement(block["widgets"], self.upstream_add.pk)
        self.assertTrue(widget["conflict"])
        self.assertIn("no prefix source configured", widget["conflict_reason"])

        conflicts = response.context["chain_conflicts"]
        matches = [c for c in conflicts if c["kind"] == "settled_name"]
        self.assertEqual(len(matches), 1, conflicts)
        entry = matches[0]
        self.assertEqual(entry["slot_key"], self.upstream_add.pk)
        self.assertEqual(entry["source_design_id"], self.parent.pk)


class DesignChainHealthViewTest(TestCase):
    """
    The cross-design staleness / re-base REPORT (PLAN-design-chains.md G4's
    reporting half): "which of my designs need attention right now" -- a
    refused chain (an implemented or unapproved ancestor, or a broken
    lineage) or inert (stale) placements. Distinct from the per-design cards
    on design.html and the editor's ``chain_conflicts`` panel (which already
    say all of this for ONE design): this is the across-every-design view,
    reached from the nav, that must not re-derive its answer per row with a
    naive per-design chain walk (that would be N designs * M ancestors deep
    queries -- see the view's own docstring for the query-count story).
    """

    user_permissions = ("netbox_rack_design.view_design",)

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.rack = env["racks"][0]
        cls.devices = env["devices"]

        # 1. Implemented parent -> child's chain is refused until re-based.
        cls.implemented_parent = Design.objects.create(
            title="Implemented parent", site=cls.site,
            status=DesignStatusChoices.STATUS_IMPLEMENTED,
        )
        cls.child_of_implemented = Design.objects.create(
            title="Child of implemented", site=cls.site, based_on=cls.implemented_parent,
        )

        # 2. Draft (never-approved) parent -> chain refused the same way.
        cls.draft_parent = Design.objects.create(title="Draft parent", site=cls.site)
        cls.child_of_draft = Design.objects.create(
            title="Child of draft", site=cls.site, based_on=cls.draft_parent,
        )

        # 3. A healthy chain: approved parent, nothing wrong -- must NOT appear.
        cls.approved_parent = Design.objects.create(
            title="Approved parent", site=cls.site, status=DesignStatusChoices.STATUS_APPROVED,
        )
        cls.healthy_child = Design.objects.create(
            title="Healthy child", site=cls.site, based_on=cls.approved_parent,
        )

        # 4. Unchained, otherwise-fine design -- must NOT appear.
        cls.unchained = Design.objects.create(title="Unchained design", site=cls.site)

        # 5. Stale placements: two inert rows (their real devices were deleted).
        cls.stale_design = Design.objects.create(title="Stale design", site=cls.site)
        cls.stale_move = DesignPlacement.objects.create(
            design=cls.stale_design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.devices[0],
            target_rack=cls.rack,
            target_position=3,
        )
        cls.stale_remove = DesignPlacement.objects.create(
            design=cls.stale_design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=cls.devices[1],
        )
        cls.devices[0].delete()
        cls.devices[1].delete()
        cls.stale_move.refresh_from_db()
        cls.stale_remove.refresh_from_db()

    @property
    def _url(self):
        return reverse("plugins:netbox_rack_design:design_chain_health")

    def test_returns_200(self):
        response = self.client.get(self._url)
        self.assertHttpStatus(response, 200)

    def test_implemented_parent_reported_with_reason_and_rebase_link(self):
        response = self.client.get(self._url)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn(self.child_of_implemented.title, content)
        self.assertIn("implemented", content.lower())
        rebase_url = reverse(
            "plugins:netbox_rack_design:design_rebase",
            kwargs={"pk": self.child_of_implemented.pk},
        )
        self.assertIn(rebase_url, content)

    def test_draft_parent_reported_with_reason(self):
        response = self.client.get(self._url)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn(self.child_of_draft.title, content)
        rebase_url = reverse(
            "plugins:netbox_rack_design:design_rebase",
            kwargs={"pk": self.child_of_draft.pk},
        )
        self.assertIn(rebase_url, content)

    def test_healthy_chained_design_not_reported(self):
        response = self.client.get(self._url)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertNotIn(self.healthy_child.title, content)

    def test_unchained_design_not_reported(self):
        response = self.client.get(self._url)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertNotIn(self.unchained.title, content)

    def test_stale_placements_reported_with_count_and_fix_link(self):
        response = self.client.get(self._url)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn(self.stale_design.title, content)
        self.assertIn("2", content)
        placements_url = reverse("plugins:netbox_rack_design:designplacement_list")
        self.assertIn(placements_url, content)
        self.assertIn(f"design_id={self.stale_design.pk}", content)

    def test_empty_state_when_nothing_needs_attention(self):
        # Drop every flagged design/placement so the install is fully healthy.
        Design.objects.filter(
            pk__in=[
                self.child_of_implemented.pk,
                self.child_of_draft.pk,
                self.stale_design.pk,
            ]
        ).delete()
        response = self.client.get(self._url)
        self.assertHttpStatus(response, 200)
        self.assertEqual(response.context["row_count"], 0)
        content = response.content.decode()
        self.assertIn("healthy", content.lower())

    def test_without_permission_denied(self):
        anonymous_client_response = self.client_class().get(self._url)
        self.assertIn(anonymous_client_response.status_code, (302, 403))

    def test_permission_restricted_user_only_sees_permitted_designs(self):
        # A user whose ObjectPermission is CONSTRAINED to the (unflagged)
        # healthy_child must see none of the flagged rows, even though they
        # exist -- the report must never leak a design's existence to a user
        # who cannot view it (task: "respect object permissions").
        restricted = User.objects.create_user(username="restricted")
        permission = ObjectPermission(
            name="chain-health-restricted", actions=["view"],
            constraints={"pk": self.healthy_child.pk},
        )
        permission.save()
        permission.users.add(restricted)
        permission.object_types.add(ObjectType.objects.get_for_model(Design))
        client = self.client_class()
        client.force_login(restricted)

        response = client.get(self._url)
        self.assertHttpStatus(response, 200)
        self.assertEqual(response.context["row_count"], 0)
        content = response.content.decode()
        self.assertNotIn(self.child_of_implemented.title, content)
        self.assertNotIn(self.stale_design.title, content)

    def test_permission_restricted_user_sees_only_their_flagged_design(self):
        restricted = User.objects.create_user(username="restricted2")
        permission = ObjectPermission(
            name="chain-health-restricted-2", actions=["view"],
            constraints={"pk": self.stale_design.pk},
        )
        permission.save()
        permission.users.add(restricted)
        permission.object_types.add(ObjectType.objects.get_for_model(Design))
        client = self.client_class()
        client.force_login(restricted)

        response = client.get(self._url)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn(self.stale_design.title, content)
        self.assertNotIn(self.child_of_implemented.title, content)
        self.assertNotIn(self.child_of_draft.title, content)

    def test_query_count_stays_bounded_as_design_count_grows(self):
        # The whole point of resolving every design's chain in ONE batched
        # graph walk (rather than one design.baseline_chain() -- itself one
        # query per ancestor hop -- per row) is that the query count does NOT
        # grow with how many designs exist. Prove it directly against the row
        # builder (bypassing the surrounding HTTP request's own unrelated
        # queries -- session, config-revision cache, notifications -- which
        # are noise this test is not about and can shift for reasons that
        # have nothing to do with the number of designs): snapshot the count
        # with the fixture as built, add a pile of unrelated healthy designs,
        # and assert the count is unchanged.
        # Warm up any per-user permission cache first (``restrict()`` caches
        # on the user instance), so it does not masquerade as a query-count
        # difference driven by the design count added below.
        views._chain_health_rows(self.user)
        with CaptureQueriesContext(connection) as before:
            views._chain_health_rows(self.user)
        baseline_queries = len(before.captured_queries)

        Design.objects.bulk_create([
            Design(title=f"Bulk design {i}", site=self.site, sequence=1000 + i)
            for i in range(25)
        ])

        with CaptureQueriesContext(connection) as after:
            views._chain_health_rows(self.user)
        self.assertEqual(
            len(after.captured_queries), baseline_queries,
            "query count must not scale with the total number of designs",
        )
        # And it should be small in absolute terms, not just stable.
        self.assertLessEqual(baseline_queries, 6, before.captured_queries)
