"""
Tests for the DesignViewSet ``preview-template`` action (PLAN-templates.md
Phase 3, task T3.2).

The endpoint computes where a Template's placements would land in one or more
racks -- real or planned -- and what the naming engine would call them,
WITHOUT writing anything: no ``DesignPlacement`` is saved, no ``dcim`` object
is mutated. It mirrors ``recompute-distribution``'s shape: apply the editor's
optional unsaved ``layout`` through the SAME reconciliation helpers inside a
transaction, then roll the whole thing back.

Written FIRST, against code that does not exist yet -- the import at the top
and every request below are expected to fail (404, since the action/urls do
not exist) until the view is implemented. That failure is the "confirm it
fails" gate for this task.
"""

from dcim.choices import SubdeviceRoleChoices
from dcim.models import (
    Device,
    DeviceBayTemplate,
    DeviceRole,
    DeviceType,
    Location,
    Manufacturer,
    Rack,
    Site,
)
from django.urls import reverse
from rest_framework import status
from tenancy.models import Tenant
from utilities.testing import APITestCase, create_test_device

from ..choices import DesignPlacementKindChoices, TemplatePlacementAnchorChoices
from ..models import (
    Design,
    DesignPlacement,
    PlannedRack,
    Template,
    TemplateGroup,
    TemplatePlacement,
)


def _url(design):
    return reverse(
        "plugins-api:netbox_rack_design-api:design-preview-template",
        kwargs={"pk": design.pk},
    )


class PreviewTemplateTest(APITestCase):
    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="PT Site", slug="pt-site")
        cls.mfr = Manufacturer.objects.create(name="PT Mfr", slug="pt-mfr")
        cls.device_type = DeviceType.objects.create(
            manufacturer=cls.mfr, model="PT Device", slug="pt-device",
            u_height=1, is_full_depth=False,
        )
        cls.device_role = DeviceRole.objects.create(name="PT Role", slug="pt-role")
        cls.tenant = Tenant.objects.create(name="PT Tenant", slug="pt-tenant")

        cls.design = Design.objects.create(title="PT Design", site=cls.site)

        # The worked example from PLAN-templates.md Sec 3: a 5-item top-anchored
        # ToR (patch panel, organizer, switch, organizer, switch), each 1U.
        cls.tor_template = Template.objects.create(name="Standard ToR", u_height=47)
        cls.tor_items = []
        for i, label in enumerate(
            ("patch panel", "organizer", "switch", "organizer", "switch"), start=1
        ):
            cls.tor_items.append(TemplatePlacement.objects.create(
                template=cls.tor_template, device_type=cls.device_type,
                anchor=TemplatePlacementAnchorChoices.ANCHOR_TOP, order=i,
                face="front", label=label,
            ))

    def _add_permission(self):
        self.add_permissions("netbox_rack_design.view_design")

    # --- the worked example -------------------------------------------------

    def test_worked_example_top_anchored_tor(self):
        self._add_permission()
        rack = Rack.objects.create(name="PT Rack 47", site=self.site, u_height=47)
        # U46 already occupied on the front face.
        create_test_device("Blocker", site=self.site, rack=rack, position=46, face="front")

        resp = self.client.post(
            _url(self.design),
            {"template": self.tor_template.pk, "racks": [f"r:{rack.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        key = f"r:{rack.pk}"
        self.assertIn(key, resp.data)
        positions = [entry["position"] for entry in resp.data[key]]
        self.assertEqual(positions, [47.0, 45.0, 44.0, 43.0, 42.0])
        self.assertEqual(resp.data["skipped"], [])
        # Every stamped entry names its origin template placement and carries
        # a computed name.
        for entry, tp in zip(resp.data[key], self.tor_items, strict=True):
            self.assertEqual(entry["template_placement"], tp.pk)
            self.assertTrue(entry["name"])

    def test_bottom_anchored_fills_from_bottom(self):
        self._add_permission()
        template = Template.objects.create(name="Bottom stack", u_height=10)
        item1 = TemplatePlacement.objects.create(
            template=template, device_type=self.device_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_BOTTOM, order=1, face="front",
        )
        item2 = TemplatePlacement.objects.create(
            template=template, device_type=self.device_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_BOTTOM, order=2, face="front",
        )
        rack = Rack.objects.create(name="PT Rack 10", site=self.site, u_height=10)

        resp = self.client.post(
            _url(self.design),
            {"template": template.pk, "racks": [f"r:{rack.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        key = f"r:{rack.pk}"
        positions = [e["position"] for e in resp.data[key]]
        self.assertEqual(positions, [1.0, 2.0])
        self.assertEqual(
            [e["template_placement"] for e in resp.data[key]], [item1.pk, item2.pk]
        )

    # --- writes nothing -------------------------------------------------------

    def test_endpoint_writes_nothing(self):
        self._add_permission()
        rack = Rack.objects.create(name="PT Rack Write", site=self.site, u_height=47)
        create_test_device("Blocker2", site=self.site, rack=rack, position=46, face="front")

        before_placements = DesignPlacement.objects.count()
        before_devices = Device.objects.count()
        before_racks = Rack.objects.count()

        resp = self.client.post(
            _url(self.design),
            {"template": self.tor_template.pk, "racks": [f"r:{rack.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)

        self.assertEqual(DesignPlacement.objects.count(), before_placements)
        self.assertEqual(Device.objects.count(), before_devices)
        self.assertEqual(Rack.objects.count(), before_racks)

    # --- does not fit ---------------------------------------------------------

    def test_template_that_does_not_fit_is_skipped(self):
        self._add_permission()
        huge_type = DeviceType.objects.create(
            manufacturer=self.mfr, model="Huge", slug="pt-huge",
            u_height=100, is_full_depth=False,
        )
        template = Template.objects.create(name="Too big", u_height=100)
        TemplatePlacement.objects.create(
            template=template, device_type=huge_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_TOP, order=1, face="front",
        )
        rack = Rack.objects.create(name="PT Rack Small", site=self.site, u_height=10)

        resp = self.client.post(
            _url(self.design),
            {"template": template.pk, "racks": [f"r:{rack.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        key = f"r:{rack.pk}"
        self.assertNotIn(key, resp.data)
        self.assertEqual(len(resp.data["skipped"]), 1)
        self.assertEqual(resp.data["skipped"][0]["rack"], key)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    # --- names ------------------------------------------------------------

    def test_names_are_distinct_and_counting_continues_across_racks(self):
        self._add_permission()
        template = Template.objects.create(name="Single item", u_height=1)
        TemplatePlacement.objects.create(
            template=template, device_type=self.device_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_TOP, order=1, face="front",
        )
        rack_a = Rack.objects.create(name="PT Rack A", site=self.site, u_height=5)
        rack_b = Rack.objects.create(name="PT Rack B", site=self.site, u_height=5)

        resp = self.client.post(
            _url(self.design),
            {"template": template.pk, "racks": [f"r:{rack_a.pk}", f"r:{rack_b.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        name_a = resp.data[f"r:{rack_a.pk}"][0]["name"]
        name_b = resp.data[f"r:{rack_b.pk}"][0]["name"]
        self.assertNotEqual(name_a, name_b)
        # Built-in sequence naming: "<design.title>-<n>", strictly increasing
        # across the whole request, not reset per rack.
        n_a = int(name_a.rsplit("-", 1)[1])
        n_b = int(name_b.rsplit("-", 1)[1])
        self.assertLess(n_a, n_b)

    # --- planned racks ----------------------------------------------------

    def test_planned_rack_is_stamped(self):
        self._add_permission()
        location = Location.objects.create(name="PT Loc", slug="pt-loc", site=self.site)
        planned = PlannedRack.objects.create(name="Planned R1", location=location, u_height=10)

        template = Template.objects.create(name="Planned template", u_height=1)
        TemplatePlacement.objects.create(
            template=template, device_type=self.device_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_TOP, order=1, face="front",
        )

        resp = self.client.post(
            _url(self.design),
            {"template": template.pk, "racks": [f"p:{planned.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        key = f"p:{planned.pk}"
        self.assertIn(key, resp.data)
        self.assertEqual(resp.data[key][0]["position"], 10.0)
        self.assertTrue(resp.data[key][0]["name"])
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    def test_real_and_planned_rack_with_same_pk_are_different_racks(self):
        """D28: PlannedRack and dcim.Rack keep separate pk sequences, so the
        same integer names two different racks under 'r:' and 'p:'."""
        self._add_permission()
        real_rack = Rack.objects.create(name="PT Real", site=self.site, u_height=5)
        create_test_device("Occupant", site=self.site, rack=real_rack, position=5, face="front")

        location = Location.objects.create(name="PT Loc2", slug="pt-loc2", site=self.site)
        planned = PlannedRack(name="PT Planned pk-collide", location=location, u_height=5)
        planned.pk = real_rack.pk
        planned.save(force_insert=True)
        self.assertEqual(planned.pk, real_rack.pk)

        template = Template.objects.create(name="Collision template", u_height=1)
        TemplatePlacement.objects.create(
            template=template, device_type=self.device_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_TOP, order=1, face="front",
        )

        resp = self.client.post(
            _url(self.design),
            {"template": template.pk, "racks": [f"r:{real_rack.pk}", f"p:{planned.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        # The real rack's U5 is occupied -> the item drifts to U4.
        self.assertEqual(resp.data[f"r:{real_rack.pk}"][0]["position"], 4.0)
        # The planned rack (same integer pk, different table) is untouched by
        # the real rack's occupant -> lands at the top, U5.
        self.assertEqual(resp.data[f"p:{planned.pk}"][0]["position"], 5.0)

    # --- peer designs do not block -----------------------------------------

    def test_peer_design_claim_does_not_block(self):
        self._add_permission()
        rack = Rack.objects.create(name="PT Rack Peer", site=self.site, u_height=5)
        peer = Design.objects.create(title="Peer design", site=self.site)
        DesignPlacement.objects.create(
            design=peer, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_rack=rack,
            target_position=5, target_face="front", proposed_name="Peer add",
        )

        template = Template.objects.create(name="Peer template", u_height=1)
        TemplatePlacement.objects.create(
            template=template, device_type=self.device_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_TOP, order=1, face="front",
        )

        resp = self.client.post(
            _url(self.design),
            {"template": template.pk, "racks": [f"r:{rack.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        # The peer's own (virtual, unsaved-by-us) claim on U5 does not block --
        # our design's stamp lands right at the top, same as if the rack were
        # empty.
        self.assertEqual(resp.data[f"r:{rack.pk}"][0]["position"], 5.0)

    # --- unsaved layout -----------------------------------------------------

    def test_unsaved_layout_pushes_placement_down(self):
        self._add_permission()
        rack = Rack.objects.create(name="PT Rack Layout", site=self.site, u_height=10)

        template = Template.objects.create(name="Layout template", u_height=1)
        TemplatePlacement.objects.create(
            template=template, device_type=self.device_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_TOP, order=1, face="front",
        )

        # Without the layout: an empty rack -> lands at U10 (the top).
        base_resp = self.client.post(
            _url(self.design),
            {"template": template.pk, "racks": [f"r:{rack.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(base_resp, status.HTTP_200_OK)
        self.assertEqual(base_resp.data[f"r:{rack.pk}"][0]["position"], 10.0)

        # With an UNSAVED layout dropping a device at U10/front: the template
        # item must drift down to U9, and nothing from the layout is persisted.
        layout = {
            "design_id": self.design.pk,
            "racks": [{
                "rack_id": rack.pk,
                "front": [{
                    "kind": "add", "device_type_id": self.device_type.pk,
                    "u_position": 10, "face": "front",
                }],
            }],
        }
        resp = self.client.post(
            _url(self.design),
            {"template": template.pk, "racks": [f"r:{rack.pk}"], "layout": layout},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        self.assertEqual(resp.data[f"r:{rack.pk}"][0]["position"], 9.0)
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    # --- planning_data (D8) --------------------------------------------------

    def test_planning_data_is_echoed_for_top_level_and_blade_entries(self):
        """D8: a template placement stores the config-declared planning
        fields (planning_fields.py) the same way a ``kind=add`` placement
        does. preview-template must echo them back so the client's stamped
        tile (templates.js -> rack.js's stampTemplateItems) can carry them
        through to save-layout exactly like a manual add's does -- otherwise
        the config-declared custom fields the template carries are silently
        dropped on every stamp."""
        self._add_permission()
        chassis_type = DeviceType.objects.create(
            manufacturer=self.mfr, model="PT Chassis PD", slug="pt-chassis-pd",
            u_height=2, subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        )
        DeviceBayTemplate.objects.create(device_type=chassis_type, name="bay-a")
        blade_type = DeviceType.objects.create(
            manufacturer=self.mfr, model="PT Blade PD", slug="pt-blade-pd",
            u_height=0, subdevice_role=SubdeviceRoleChoices.ROLE_CHILD,
        )
        template = Template.objects.create(name="Planning data template", u_height=2)
        chassis_tp = TemplatePlacement.objects.create(
            template=template, device_type=chassis_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_TOP, order=1, face="front",
            planning_data={"note": "chassis-level"},
        )
        TemplatePlacement.objects.create(
            template=template, device_type=blade_type,
            parent_placement=chassis_tp, target_bay_name="bay-a", order=2,
            planning_data={"note": "blade-level"},
        )
        rack = Rack.objects.create(name="PT Rack PlanningData", site=self.site, u_height=10)

        resp = self.client.post(
            _url(self.design),
            {"template": template.pk, "racks": [f"r:{rack.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        entry = resp.data[f"r:{rack.pk}"][0]
        self.assertEqual(entry["planning_data"], {"note": "chassis-level"})
        self.assertEqual(entry["blades"][0]["planning_data"], {"note": "blade-level"})

    # --- chassis + blades (D10) ----------------------------------------------

    def test_chassis_and_blades_are_nested_in_the_response(self):
        self._add_permission()
        chassis_type = DeviceType.objects.create(
            manufacturer=self.mfr, model="PT Chassis", slug="pt-chassis",
            u_height=2, subdevice_role=SubdeviceRoleChoices.ROLE_PARENT,
        )
        DeviceBayTemplate.objects.create(device_type=chassis_type, name="bay-a")
        blade_type = DeviceType.objects.create(
            manufacturer=self.mfr, model="PT Blade", slug="pt-blade",
            u_height=0, subdevice_role=SubdeviceRoleChoices.ROLE_CHILD,
        )
        template = Template.objects.create(name="Chassis template", u_height=2)
        chassis_tp = TemplatePlacement.objects.create(
            template=template, device_type=chassis_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_TOP, order=1, face="front",
        )
        blade_tp = TemplatePlacement.objects.create(
            template=template, device_type=blade_type,
            parent_placement=chassis_tp, target_bay_name="bay-a", order=2,
        )
        rack = Rack.objects.create(name="PT Rack Chassis", site=self.site, u_height=10)

        resp = self.client.post(
            _url(self.design),
            {"template": template.pk, "racks": [f"r:{rack.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        entry = resp.data[f"r:{rack.pk}"][0]
        self.assertEqual(entry["template_placement"], chassis_tp.pk)
        self.assertEqual(len(entry["blades"]), 1)
        blade_entry = entry["blades"][0]
        self.assertEqual(blade_entry["template_placement"], blade_tp.pk)
        self.assertEqual(blade_entry["target_bay_name"], "bay-a")
        self.assertTrue(blade_entry["name"])
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    # --- T3.6 gap 1: reporting an existing stamp of the same template ------
    # (PLAN-templates.md §6 "Still open": "Applying the same template twice
    # to one rack"). preview-template must never block or dedupe a second
    # stamp -- a planner may legitimately want two identical ToR blocks in
    # one rack -- but it must make a prior stamp VISIBLE via a new
    # "already_stamped" response key, keyed by rack, so the decision to go
    # ahead stays theirs.

    def test_reports_existing_stamp_of_same_template(self):
        self._add_permission()
        rack = Rack.objects.create(name="PT Rack Restamp", site=self.site, u_height=47)
        # A PRIOR stamp of self.tor_template already landed here, at version 1
        # (its version at the time it was stamped).
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_rack=rack,
            target_position=1, target_face="front", proposed_name="Existing 1",
            from_template=self.tor_template, from_template_version=1,
        )
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_rack=rack,
            target_position=2, target_face="front", proposed_name="Existing 2",
            from_template=self.tor_template, from_template_version=1,
        )

        resp = self.client.post(
            _url(self.design),
            {"template": self.tor_template.pk, "racks": [f"r:{rack.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        key = f"r:{rack.pk}"
        self.assertIn("already_stamped", resp.data)
        self.assertIn(key, resp.data["already_stamped"])
        self.assertEqual(
            resp.data["already_stamped"][key],
            [{"version": 1, "count": 2}],
        )
        # Still NOT blocked -- the new stamp is still computed and placed.
        self.assertIn(key, resp.data)
        self.assertTrue(len(resp.data[key]) > 0)

    def test_reports_nothing_for_placements_from_a_different_template(self):
        self._add_permission()
        rack = Rack.objects.create(name="PT Rack OtherTemplate", site=self.site, u_height=47)
        other_template = Template.objects.create(name="Some other template", u_height=1)
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_rack=rack,
            target_position=1, target_face="front", proposed_name="From other",
            from_template=other_template, from_template_version=1,
        )

        resp = self.client.post(
            _url(self.design),
            {"template": self.tor_template.pk, "racks": [f"r:{rack.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        key = f"r:{rack.pk}"
        self.assertNotIn(key, resp.data["already_stamped"])

    def test_reports_nothing_for_a_rack_with_no_template_derived_placements(self):
        self._add_permission()
        rack = Rack.objects.create(name="PT Rack Plain", site=self.site, u_height=47)
        # A manually-added placement, never stamped from any template.
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_rack=rack,
            target_position=1, target_face="front", proposed_name="Manual add",
        )

        resp = self.client.post(
            _url(self.design),
            {"template": self.tor_template.pk, "racks": [f"r:{rack.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        key = f"r:{rack.pk}"
        self.assertEqual(resp.data["already_stamped"], {})
        self.assertNotIn(key, resp.data["already_stamped"])

    def test_reported_version_is_the_one_recorded_not_the_live_one(self):
        """A drifted template (stamped once, edited since -- Template.version
        bumped by TemplatePlacement.save()) must still show the OLD recorded
        version, not the template's current live version."""
        self._add_permission()
        rack = Rack.objects.create(name="PT Rack Drift", site=self.site, u_height=47)
        DesignPlacement.objects.create(
            design=self.design, kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=self.device_type, target_rack=rack,
            target_position=1, target_face="front", proposed_name="Old stamp",
            from_template=self.tor_template, from_template_version=1,
        )
        # Drift the template: editing one of its placements bumps
        # Template.version (models.py TemplatePlacement.save()).
        item = self.tor_items[0]
        item.label = "changed after the stamp"
        item.save()
        self.tor_template.refresh_from_db()
        self.assertGreater(self.tor_template.version, 1)

        resp = self.client.post(
            _url(self.design),
            {"template": self.tor_template.pk, "racks": [f"r:{rack.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        key = f"r:{rack.pk}"
        self.assertEqual(
            resp.data["already_stamped"][key],
            [{"version": 1, "count": 1}],
        )
        # And the NEW stamp this call computes carries the LIVE version.
        self.assertEqual(resp.data[key][0]["from_template_version"], self.tor_template.version)

    # --- T3.6 gap 2: group apply with fewer target racks than members ------
    # (PLAN-templates.md §6 "Still open"). D14: a TemplateGroup is an ORDERED
    # correspondence, member 1 -> rack A, member 2 -> rack B. Decision: a
    # ``racks`` list SHORTER than the group's member count refuses the WHOLE
    # apply (400), naming every member left without a target rack, rather
    # than silently stamping only the mapped prefix -- see the view action's
    # own long comment for the reasoning.

    def test_group_apply_with_fewer_racks_than_members_is_refused(self):
        self._add_permission()
        group = TemplateGroup.objects.create(name="Compute pod")
        member_a = Template.objects.create(
            name="Spine", group=group, order=1, u_height=1,
        )
        TemplatePlacement.objects.create(
            template=member_a, device_type=self.device_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_TOP, order=1, face="front",
        )
        member_b = Template.objects.create(
            name="Leaf", group=group, order=2, u_height=1,
        )
        TemplatePlacement.objects.create(
            template=member_b, device_type=self.device_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_TOP, order=1, face="front",
        )
        rack = Rack.objects.create(name="PT Rack Group Short", site=self.site, u_height=10)

        resp = self.client.post(
            _url(self.design),
            {"group": group.pk, "racks": [f"r:{rack.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)
        self.assertIn("racks", resp.data)
        message = " ".join(str(m) for m in resp.data["racks"])
        self.assertIn("Leaf", message)
        # Nothing was written -- refusing must not leave partial state.
        self.assertEqual(DesignPlacement.objects.filter(design=self.design).count(), 0)

    def test_group_apply_with_enough_racks_maps_positionally(self):
        self._add_permission()
        group = TemplateGroup.objects.create(name="Full pod")
        member_a = Template.objects.create(
            name="Pod spine", group=group, order=1, u_height=1,
        )
        TemplatePlacement.objects.create(
            template=member_a, device_type=self.device_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_TOP, order=1, face="front",
        )
        member_b = Template.objects.create(
            name="Pod leaf", group=group, order=2, u_height=1,
        )
        TemplatePlacement.objects.create(
            template=member_b, device_type=self.device_type,
            anchor=TemplatePlacementAnchorChoices.ANCHOR_TOP, order=1, face="front",
        )
        rack_a = Rack.objects.create(name="PT Rack Group A", site=self.site, u_height=10)
        rack_b = Rack.objects.create(name="PT Rack Group B", site=self.site, u_height=10)

        resp = self.client.post(
            _url(self.design),
            {"group": group.pk, "racks": [f"r:{rack_a.pk}", f"r:{rack_b.pk}"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        self.assertIn(f"r:{rack_a.pk}", resp.data)
        self.assertIn(f"r:{rack_b.pk}", resp.data)
        self.assertEqual(
            resp.data[f"r:{rack_a.pk}"][0]["from_template"], member_a.pk,
        )
        self.assertEqual(
            resp.data[f"r:{rack_b.pk}"][0]["from_template"], member_b.pk,
        )

    def test_neither_template_nor_group_is_400(self):
        self._add_permission()
        resp = self.client.post(
            _url(self.design),
            {"racks": ["r:1"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)

    def test_both_template_and_group_is_400(self):
        self._add_permission()
        group = TemplateGroup.objects.create(name="Both group")
        resp = self.client.post(
            _url(self.design),
            {"template": self.tor_template.pk, "group": group.pk, "racks": ["r:1"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)

    # --- errors / permissions ------------------------------------------------

    def test_malformed_rack_key_is_400(self):
        self._add_permission()
        resp = self.client.post(
            _url(self.design),
            {"template": self.tor_template.pk, "racks": ["not-a-key"]},
            format="json", **self.header,
        )
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)

    def test_requires_view_permission(self):
        resp = self.client.post(
            _url(self.design),
            {"template": self.tor_template.pk, "racks": ["r:1"]},
            format="json", **self.header,
        )
        self.assertIn(
            resp.status_code,
            (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
        )
