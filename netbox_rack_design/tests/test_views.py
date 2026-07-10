"""UI view tests for NetBox Rack Design (subclassing NetBox's standard suite)."""

from dcim.models import Rack, Site
from django.urls import reverse
from utilities.testing import TestCase, ViewTestCases, create_tags

from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..forms import DesignForm
from ..models import Design, DesignGroup, DesignPlacement, HiddenDesignRack
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
