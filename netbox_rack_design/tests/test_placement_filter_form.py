"""Tests for the DesignPlacement filters-tab form (forms.DesignPlacementFilterForm).

filtersets.py gained device_role_id/tenant_id/power_source_device_id/
real_power_feed_id/planned_power_feed_id/base_placement_id/
base_parent_placement_id filters that worked through the REST API and
{% htmx_table %} embeds but were never declared on the filter FORM, so they
never appeared on the list view's filters tab. Only device_role_id and
tenant_id are surfaced here (see forms.py's DesignPlacementFilterForm
docstring/comment for the judgement call on the rest).
"""

from dcim.models import DeviceRole
from django.urls import reverse
from tenancy.models import Tenant
from utilities.testing import TestCase

from ..choices import DesignPlacementKindChoices
from ..forms import DesignPlacementFilterForm
from ..models import Design, DesignPlacement
from .utils import create_dcim_environment


class DesignPlacementFilterFormFieldsTest(TestCase):
    """Unit-level: the two new fields exist on the form, unbound to any DB state."""

    def test_device_role_id_field_present(self):
        form = DesignPlacementFilterForm()
        self.assertIn("device_role_id", form.fields)

    def test_tenant_id_field_present(self):
        form = DesignPlacementFilterForm()
        self.assertIn("tenant_id", form.fields)

    def test_new_fields_have_a_fieldset_home(self):
        # Every declared field must be reachable through some FieldSet -- not
        # merely present on the form but omitted from the rendered layout.
        fieldset_fields = {
            name
            for fieldset in DesignPlacementFilterForm.fieldsets
            for name in fieldset.items
        }
        self.assertIn("device_role_id", fieldset_fields)
        self.assertIn("tenant_id", fieldset_fields)


class DesignPlacementFilterFormRenderAndSubmitTest(TestCase):
    """Integration: the fields actually render on the list view and actually filter."""

    user_permissions = ("netbox_rack_design.view_designplacement",)

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        site = env["site"]
        device_type = env["device_type"]
        rack = env["racks"][1]
        design = Design.objects.create(title="Filter form design", site=site)

        cls.role = DeviceRole.objects.create(name="Compute", slug="compute")
        cls.tenant = Tenant.objects.create(name="Tenant X", slug="tenant-x")

        cls.matching = DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=device_type,
            device_role=cls.role,
            tenant=cls.tenant,
            target_rack=rack,
            target_position=1,
            proposed_name="matching-node",
        )
        cls.other = DesignPlacement.objects.create(
            design=design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=device_type,
            target_rack=rack,
            target_position=2,
            proposed_name="other-node",
        )

    @property
    def _url(self):
        return reverse("plugins:netbox_rack_design:designplacement_list")

    def test_filters_tab_renders_the_new_fields(self):
        response = self.client.get(self._url)
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn('name="device_role_id"', content)
        self.assertIn('name="tenant_id"', content)

    def _placement_url(self, placement):
        return reverse(
            "plugins:netbox_rack_design:designplacement",
            kwargs={"pk": placement.pk},
        )

    def test_device_role_id_narrows_results(self):
        response = self.client.get(f"{self._url}?device_role_id={self.role.pk}")
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn(self._placement_url(self.matching), content)
        self.assertNotIn(self._placement_url(self.other), content)

    def test_tenant_id_narrows_results(self):
        response = self.client.get(f"{self._url}?tenant_id={self.tenant.pk}")
        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn(self._placement_url(self.matching), content)
        self.assertNotIn(self._placement_url(self.other), content)
