"""FilterSet tests for NetBox Rack Design (subclassing ChangeLoggedFilterSetTests)."""

from utilities.testing import ChangeLoggedFilterSetTests

from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..filtersets import DesignFilterSet, DesignGroupFilterSet, DesignPlacementFilterSet
from ..models import Design, DesignGroup, DesignPlacement
from .utils import create_dcim_environment


class DesignGroupFilterSetTest(ChangeLoggedFilterSetTests):
    queryset = DesignGroup.objects.all()
    filterset = DesignGroupFilterSet

    @classmethod
    def setUpTestData(cls):
        cls.parent = DesignGroup.objects.create(name="Parent")
        DesignGroup.objects.create(name="Group 1", parent=cls.parent, description="alpha")
        DesignGroup.objects.create(name="Group 2", description="bravo")
        DesignGroup.objects.create(name="Group 3")

    def test_name(self):
        params = {"name": ["Group 1", "Group 2"]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_parent_id(self):
        params = {"parent_id": [self.parent.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_search(self):
        params = {"q": "alpha"}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)


class DesignFilterSetTest(ChangeLoggedFilterSetTests):
    queryset = Design.objects.all()
    filterset = DesignFilterSet

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.group = DesignGroup.objects.create(name="Group 1")

        Design.objects.create(
            title="Design 1", site=cls.site, group=cls.group,
            status=DesignStatusChoices.STATUS_DRAFT, summary="alpha",
        )
        Design.objects.create(
            title="Design 2", site=cls.site, status=DesignStatusChoices.STATUS_APPROVED,
        )
        Design.objects.create(
            title="Design 3", site=cls.site, status=DesignStatusChoices.STATUS_REJECTED,
        )

    def test_title(self):
        params = {"title": ["Design 1", "Design 2"]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_site_id(self):
        params = {"site_id": [self.site.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 3)

    def test_group_id(self):
        params = {"group_id": [self.group.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_status(self):
        params = {"status": [DesignStatusChoices.STATUS_DRAFT]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_search(self):
        params = {"q": "alpha"}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)


class DesignPlacementFilterSetTest(ChangeLoggedFilterSetTests):
    queryset = DesignPlacement.objects.all()
    filterset = DesignPlacementFilterSet

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        site = env["site"]
        cls.device_type = env["device_type"]
        cls.rack = env["racks"][1]
        cls.device = env["devices"][0]
        cls.design = Design.objects.create(title="Design 1", site=site)

        DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            target_rack=cls.rack,
            target_position=1,
            proposed_name="alpha",
        )
        DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.device,
            target_rack=cls.rack,
            target_position=2,
        )
        DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_REMOVE,
            device=env["devices"][1],
        )

    def test_design_id(self):
        params = {"design_id": [self.design.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 3)

    def test_kind(self):
        params = {"kind": [DesignPlacementKindChoices.KIND_ADD]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_target_rack_id(self):
        params = {"target_rack_id": [self.rack.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_device_id(self):
        params = {"device_id": [self.device.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_search(self):
        params = {"q": "alpha"}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)
