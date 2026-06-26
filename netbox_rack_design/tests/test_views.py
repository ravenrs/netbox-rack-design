"""UI view tests for NetBox Rack Design (subclassing NetBox's standard suite)."""

from django.urls import reverse
from utilities.testing import TestCase, ViewTestCases, create_tags

from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignGroup, DesignPlacement
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

        Design.objects.create(title="Design 1", site=site)
        Design.objects.create(title="Design 2", site=site)
        Design.objects.create(title="Design 3", site=site)

        tags = create_tags("Alpha", "Bravo", "Charlie")

        cls.form_data = {
            "title": "Design X",
            "site": site.pk,
            "status": DesignStatusChoices.STATUS_DRAFT,
            "summary": "A new design",
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


class DesignElevationViewTest(TestCase):
    """The read-only projected rack elevation view returns HTTP 200."""

    user_permissions = ("netbox_rack_design.view_design",)

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.rack1 = env["racks"][0]  # has Device 1 (U1) and Device 2 (U2)
        cls.rack2 = env["racks"][1]  # empty
        cls.device1 = env["devices"][0]
        cls.device2 = env["devices"][1]

        cls.design = Design.objects.create(title="Elevation Design", site=cls.site)

        # add: a new device from the catalog into rack 1 at a free slot.
        DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            target_rack=cls.rack1,
            target_position=10,
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

    def _url(self, design, rack):
        return reverse(
            "plugins:netbox_rack_design:design_elevation",
            kwargs={"pk": design.pk, "rack_id": rack.pk},
        )

    def test_elevation_view_returns_200(self):
        response = self.client.get(self._url(self.design, self.rack1))
        self.assertHttpStatus(response, 200)

    def test_elevation_view_rear_face_returns_200(self):
        response = self.client.get(self._url(self.design, self.rack1) + "?face=rear")
        self.assertHttpStatus(response, 200)

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
            "plugins:netbox_rack_design:design_elevation",
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

        cls.design = Design.objects.create(title="Editor Design", site=cls.site)

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
                "plugins:netbox_rack_design:design_elevation",
                kwargs={"pk": self.design.pk, "rack_id": rack.pk},
            )
            self.assertIn(elevation_url, content)


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
            "plugins:netbox_rack_design:design_elevation",
            kwargs={"pk": self.design.pk, "rack_id": self.rack.pk},
        )
        self.assertIn(elevation_url, content)
