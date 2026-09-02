"""FilterSet tests for NetBox Rack Design (subclassing ChangeLoggedFilterSetTests).

Every class below MUST list ``TestCase`` explicitly as its first base:
``ChangeLoggedFilterSetTests`` is a plain NetBox *mixin* (see
``utilities/testing/filtersets.py`` -- it derives from ``object``, not from
``TestCase``), so a class that inherits the mixin ALONE is not a ``TestCase``
and Django's test loader silently collects NOTHING from it. This module ran
0 tests for its whole existence because of exactly that. ``class X(TestCase,
ChangeLoggedFilterSetTests)`` is core's own spelling; keep it.
"""

from dcim.models import DeviceRole
from django.test import TestCase
from tenancy.models import Tenant
from utilities.testing import ChangeLoggedFilterSetTests, create_test_device

from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..filtersets import (
    DesignFilterSet,
    DesignGroupFilterSet,
    DesignPlacementFilterSet,
    DesignPowerFeedFilterSet,
)
from ..models import Design, DesignGroup, DesignPlacement, DesignPowerFeed
from .utils import create_dcim_environment


class DesignGroupFilterSetTest(TestCase, ChangeLoggedFilterSetTests):
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


class DesignFilterSetTest(TestCase, ChangeLoggedFilterSetTests):
    queryset = Design.objects.all()
    filterset = DesignFilterSet
    # The ``racks`` M2M filter is spelled ``racks_id``, not the ``rack_id`` this
    # check derives from the related model's verbose_name: ``?rack_id=`` is
    # already a parameter of the Design viewset's own detail actions. See the
    # comment on ``DesignFilterSet.racks_id``.
    filter_name_map = {"rack": "racks"}

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.group = DesignGroup.objects.create(name="Group 1")

        cls.design_1 = Design.objects.create(
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

    def test_racks_id(self):
        rack = self.site.racks.first()
        self.design_1.racks.add(rack)
        params = {"racks_id": [rack.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

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

    def test_based_on_id(self):
        # "designs derived from X" (PLAN-design-chains.md G9).
        child = Design.objects.create(
            title="Design 1 child", site=self.site, based_on=self.design_1,
        )
        params = {"based_on_id": [self.design_1.pk]}
        self.assertEqual(list(self.filterset(params, self.queryset).qs), [child])

    def test_no_parent(self):
        # "designs with no parent" (PLAN-design-chains.md G9). All three
        # setUpTestData designs have no based_on; add one that does, and
        # confirm no_parent=true excludes it while no_parent=false keeps only it.
        child = Design.objects.create(
            title="Design 1 child", site=self.site, based_on=self.design_1,
        )
        rootless = self.filterset({"no_parent": True}, self.queryset).qs
        self.assertEqual(rootless.count(), 3)
        self.assertNotIn(child, rootless)

        parented = self.filterset({"no_parent": False}, self.queryset).qs
        self.assertEqual(list(parented), [child])


class DesignPlacementFilterSetTest(TestCase, ChangeLoggedFilterSetTests):
    queryset = DesignPlacement.objects.all()
    filterset = DesignPlacementFilterSet
    # Opaque planning/power JSON blobs written and read only by the editor and
    # the distribution engine. There is no meaningful lookup to expose, so they
    # are declared to core's documented exclusion hook (same as dcim's
    # ``local_context_data`` / ``attribute_data``) rather than being papered
    # over by weakening the assertion.
    ignore_fields = ("planning_data", "power_config")

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

        # A fourth, stale placement: its own dedicated device is deleted so the
        # pre_delete receiver stamps stale=True. Uses a fresh device (not
        # env["devices"]) so the pre-existing device_id/count assertions above
        # are unaffected by the deletion.
        cls.stale_device = create_test_device("Device 3", site=site, rack=cls.rack, position=9)
        stale_move = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            device=cls.stale_device,
            target_rack=cls.rack,
            target_position=3,
        )
        cls.stale_device_name = cls.stale_device.name
        cls.stale_device.delete()
        stale_move.refresh_from_db()
        cls.stale_move = stale_move

    def test_design_id(self):
        params = {"design_id": [self.design.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 4)

    def test_kind(self):
        params = {"kind": [DesignPlacementKindChoices.KIND_ADD]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_target_rack_id(self):
        params = {"target_rack_id": [self.rack.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 3)

    def test_device_id(self):
        params = {"device_id": [self.device.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_search(self):
        params = {"q": "alpha"}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_stale(self):
        params = {"stale": True}
        qs = self.filterset(params, self.queryset).qs
        self.assertEqual(list(qs), [self.stale_move])

    def test_stale_device_name(self):
        # A LIST: ``stale_device_name`` comes from ``Meta.fields``, so NetBox
        # builds it as a MultiValueCharFilter, whose field cleans a sequence.
        # Handing it a bare string makes the filter iterate the string's
        # characters and quietly match the wrong rows.
        params = {"stale_device_name": [self.stale_device_name]}
        qs = self.filterset(params, self.queryset).qs
        self.assertEqual(list(qs), [self.stale_move])

    def test_device_role_id_matches_descendant_roles(self):
        # Declared as a TreeNodeMultipleChoiceFilter, so filtering by a PARENT
        # role must also return placements planned with a CHILD role. Created
        # locally so the whole-queryset count assertions above stay valid.
        parent_role = DeviceRole.objects.create(name="Compute", slug="compute")
        child_role = DeviceRole.objects.create(
            name="Compute GPU", slug="compute-gpu", parent=parent_role
        )
        placement = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            device_role=child_role,
            target_rack=self.rack,
            target_position=6,
        )
        params = {"device_role_id": [parent_role.pk]}
        self.assertEqual(list(self.filterset(params, self.queryset).qs), [placement])

    def test_tenant_id(self):
        tenant = Tenant.objects.create(name="Tenant X", slug="tenant-x")
        placement = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            tenant=tenant,
            target_rack=self.rack,
            target_position=7,
        )
        params = {"tenant_id": [tenant.pk]}
        self.assertEqual(list(self.filterset(params, self.queryset).qs), [placement])

    def test_base_placement_id(self):
        # Created locally (not in setUpTestData) so it doesn't perturb the
        # kind-count assertions above, which run against the whole queryset.
        base_add = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.rack,
            target_position=4,
        )
        chain_move = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=base_add,
            target_rack=self.rack,
            target_position=8,
        )
        params = {"base_placement_id": [base_add.pk]}
        qs = self.filterset(params, self.queryset).qs
        self.assertEqual(list(qs), [chain_move])

    def test_base_parent_placement_id(self):
        # The cross-design PARENT reference (a blade planned into a chassis an
        # ancestor planned). A missing <fk>_id filter silently breaks
        # {% htmx_table %} embeds and API filtering, so it gets its own case.
        chassis_add = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.rack,
            target_position=5,
        )
        blade = DesignPlacement.objects.create(
            design=self.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type,
            target_rack=self.rack,
            base_parent_placement=chassis_add,
            target_bay_name="bay-a",
        )
        params = {"base_parent_placement_id": [chassis_add.pk]}
        qs = self.filterset(params, self.queryset).qs
        self.assertEqual(list(qs), [blade])

        # And the negative case: a filter that quietly does nothing would
        # return every placement here instead of none.
        params = {"base_parent_placement_id": [blade.pk]}
        self.assertEqual(list(self.filterset(params, self.queryset).qs), [])


class DesignPowerFeedFilterSetTest(TestCase, ChangeLoggedFilterSetTests):
    """The fourth filterset had no test class at all, so ``test_missing_filters``
    had never been applied to it either."""

    queryset = DesignPowerFeed.objects.all()
    filterset = DesignPowerFeedFilterSet

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.rack = env["racks"][0]
        other_rack = env["racks"][1]
        cls.design = Design.objects.create(title="Design 1", site=env["site"])

        DesignPowerFeed.objects.create(
            design=cls.design, rack=cls.rack, name="Feed A", amperage=16
        )
        DesignPowerFeed.objects.create(
            design=cls.design, rack=cls.rack, name="Feed B", amperage=32
        )
        DesignPowerFeed.objects.create(
            design=cls.design, rack=other_rack, name="Feed C", amperage=32
        )

    def test_design_id(self):
        params = {"design_id": [self.design.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 3)

    def test_rack_id(self):
        params = {"rack_id": [self.rack.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_name(self):
        params = {"name": ["Feed A", "Feed B"]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_amperage(self):
        params = {"amperage": [32]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_search(self):
        params = {"q": "Feed A"}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)
