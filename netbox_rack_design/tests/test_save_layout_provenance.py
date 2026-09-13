"""
Tests for carrying template provenance through save-layout (PLAN-templates.md
Sec 3, D20, task T3.5).

D20 gave ``DesignPlacement`` two fields -- ``from_template`` (FK, SET_NULL)
and ``from_template_version`` (a plain int snapshot) -- and
``test_template_provenance.py`` already covers them at the model/REST level.
What was still missing is the WIRING: nothing populated them. This module
covers that wiring, end to end:

  * ``preview-template`` (the read-only compute endpoint, D19) tells the
    client which ``Template`` and which ``Template.version`` each item it
    computed came from, so the client can echo both back.
  * ``save-layout`` (the one write path) accepts that echo -- optional
    ``from_template_id``/``from_template_version`` on an 'add' item -- and
    persists it onto the new ``DesignPlacement``, validating the pk the same
    way ``_resolve_add_refs`` already validates ``device_role_id``/
    ``tenant_id``: an unknown pk is a 400 naming it, never a silent null.
  * Provenance is meaningful only for an 'add': a 'move' or 'remove' carrying
    it is rejected, mirroring how ``DesignPlacement.clean()`` already
    restricts ``device_role``/``tenant`` to specific kinds.

Re-sync / diff-against-template is explicitly OUT of scope (D20 defers it):
nothing here keeps a stamped placement in sync with its template afterwards.
"""

from dcim.models import DeviceRole, DeviceType, Manufacturer, Rack, Site
from django.urls import reverse
from rest_framework import status
from tenancy.models import Tenant
from utilities.testing import APITestCase, create_test_device

from ..choices import TemplatePlacementAnchorChoices
from ..models import Design, DesignPlacement, Template, TemplatePlacement


def _save_layout_url(design):
    return reverse(
        "plugins-api:netbox_rack_design-api:design-save-layout",
        kwargs={"pk": design.pk},
    )


def _preview_template_url(design):
    return reverse(
        "plugins-api:netbox_rack_design-api:design-preview-template",
        kwargs={"pk": design.pk},
    )


class SaveLayoutProvenanceTest(APITestCase):
    """save-layout accepts and persists (or rejects) template provenance."""

    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="SLP Site", slug="slp-site")
        cls.mfr = Manufacturer.objects.create(name="SLP Mfr", slug="slp-mfr")
        cls.device_type = DeviceType.objects.create(
            manufacturer=cls.mfr, model="SLP Device", slug="slp-device",
            u_height=1, is_full_depth=False,
        )
        cls.device_role = DeviceRole.objects.create(name="SLP Role", slug="slp-role")
        cls.tenant = Tenant.objects.create(name="SLP Tenant", slug="slp-tenant")
        cls.rack = Rack.objects.create(name="SLP Rack", site=cls.site, u_height=47)
        cls.design = Design.objects.create(title="SLP Design", site=cls.site)
        cls.template = Template.objects.create(name="SLP Standard ToR", u_height=47)

    def _grant_all(self):
        self.add_permissions(
            "netbox_rack_design.change_design",
            "netbox_rack_design.add_designplacement",
            "netbox_rack_design.change_designplacement",
            "netbox_rack_design.delete_designplacement",
        )

    def _payload(self, racks):
        return {"design_id": self.design.pk, "racks": racks}

    # --- the happy path -------------------------------------------------------

    def test_add_persists_from_template_and_version(self):
        self._grant_all()
        payload = self._payload([
            {
                "rack_id": self.rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 10, "face": "front",
                     "from_template_id": self.template.pk,
                     "from_template_version": self.template.version},
                ],
            },
        ])
        response = self.client.post(_save_layout_url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placement = DesignPlacement.objects.get(design=self.design)
        self.assertEqual(placement.from_template_id, self.template.pk)
        self.assertEqual(placement.from_template_version, self.template.version)

    def test_omitting_provenance_leaves_both_null(self):
        """Regression: an ordinary hand-placed device (every existing caller)
        must keep working unchanged -- both fields stay null when omitted."""
        self._grant_all()
        payload = self._payload([
            {
                "rack_id": self.rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 11, "face": "front"},
                ],
            },
        ])
        response = self.client.post(_save_layout_url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placement = DesignPlacement.objects.get(design=self.design)
        self.assertIsNone(placement.from_template_id)
        self.assertIsNone(placement.from_template_version)

    # --- validation -------------------------------------------------------------

    def test_unknown_from_template_is_400_and_nothing_persisted(self):
        self._grant_all()
        before = DesignPlacement.objects.filter(design=self.design).count()
        payload = self._payload([
            {
                "rack_id": self.rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 12, "face": "front",
                     "from_template_id": 9999999, "from_template_version": 1},
                ],
            },
        ])
        response = self.client.post(_save_layout_url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            DesignPlacement.objects.filter(design=self.design).count(), before,
        )

    def test_provenance_on_move_is_rejected(self):
        self._grant_all()
        device = create_test_device(
            "SLP move device", site=self.site, rack=self.rack, position=1, face="front",
        )
        payload = self._payload([
            {
                "rack_id": self.rack.pk,
                "front": [
                    {"kind": "move", "device_id": device.pk,
                     "u_position": 20, "face": "front",
                     "from_template_id": self.template.pk,
                     "from_template_version": self.template.version},
                ],
            },
        ])
        response = self.client.post(_save_layout_url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    def test_provenance_on_remove_is_rejected(self):
        self._grant_all()
        device = create_test_device(
            "SLP remove device", site=self.site, rack=self.rack, position=2, face="front",
        )
        payload = self._payload([
            {
                "rack_id": self.rack.pk,
                "front": [
                    {"kind": "remove", "device_id": device.pk,
                     "from_template_id": self.template.pk,
                     "from_template_version": self.template.version},
                ],
            },
        ])
        response = self.client.post(_save_layout_url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    # --- preview-template's response carries provenance to echo back -----------

    def test_preview_template_response_carries_template_and_version(self):
        self.add_permissions("netbox_rack_design.view_design")
        template = Template.objects.create(name="SLP Preview ToR", u_height=47)
        TemplatePlacement.objects.create(
            template=template, device_type=self.device_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_TOP, order=1, face="front",
        )
        response = self.client.post(
            _preview_template_url(self.design),
            {"template": template.pk, "racks": [f"r:{self.rack.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(response, status.HTTP_200_OK)
        key = f"r:{self.rack.pk}"
        entry = response.data[key][0]
        self.assertEqual(entry["from_template"], template.pk)
        self.assertEqual(entry["from_template_version"], template.version)

    # --- the round trip: preview, then save what it returned --------------------

    def test_round_trip_preview_then_save_persists_provenance(self):
        self.add_permissions(
            "netbox_rack_design.view_design",
            "netbox_rack_design.change_design",
            "netbox_rack_design.add_designplacement",
            "netbox_rack_design.change_designplacement",
            "netbox_rack_design.delete_designplacement",
        )
        template = Template.objects.create(name="SLP Round Trip ToR", u_height=47)
        TemplatePlacement.objects.create(
            template=template, device_type=self.device_type,
            device_role=self.device_role, tenant=self.tenant,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_TOP, order=1, face="front",
        )
        preview = self.client.post(
            _preview_template_url(self.design),
            {"template": template.pk, "racks": [f"r:{self.rack.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(preview, status.HTTP_200_OK)
        key = f"r:{self.rack.pk}"
        entry = preview.data[key][0]

        # Nothing was written by the preview.
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

        payload = self._payload([
            {
                "rack_id": self.rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": entry["device_type"],
                     "u_position": entry["position"], "face": entry["face"],
                     "device_role_id": entry["device_role"],
                     "tenant_id": entry["tenant"],
                     "proposed_name": entry["name"],
                     "from_template_id": entry["from_template"],
                     "from_template_version": entry["from_template_version"]},
                ],
            },
        ])
        save = self.client.post(_save_layout_url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(save, status.HTTP_200_OK)

        placement = DesignPlacement.objects.get(design=self.design)
        self.assertEqual(placement.from_template_id, template.pk)
        self.assertEqual(placement.from_template_version, template.version)
        self.assertEqual(placement.proposed_name, entry["name"])

    def test_deleting_template_after_save_leaves_placement_with_null_from_template(self):
        """The SET_NULL contract (D20), reached through the real write path
        rather than a direct model-level create."""
        self._grant_all()
        payload = self._payload([
            {
                "rack_id": self.rack.pk,
                "front": [
                    {"kind": "add", "device_type_id": self.device_type.pk,
                     "u_position": 13, "face": "front",
                     "from_template_id": self.template.pk,
                     "from_template_version": self.template.version},
                ],
            },
        ])
        response = self.client.post(_save_layout_url(self.design), payload, format="json", **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        placement = DesignPlacement.objects.get(design=self.design)
        self.assertEqual(placement.from_template_id, self.template.pk)
        stamped_version = placement.from_template_version

        self.template.delete()
        placement.refresh_from_db()
        self.assertIsNone(placement.from_template_id)
        # The version snapshot is history, not a live reference -- it survives
        # the template's deletion untouched.
        self.assertEqual(placement.from_template_version, stamped_version)
