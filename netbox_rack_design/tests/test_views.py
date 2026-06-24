"""UI view tests for NetBox Rack Design (subclassing NetBox's standard suite)."""

from utilities.testing import ViewTestCases, create_tags

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
