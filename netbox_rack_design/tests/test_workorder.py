"""
``workorder.build`` / ``workorder.render_markdown`` -- PLAN-execution-steps.md
Sec. 10.1 (E10/E13): the smart-hands work order of a design's execution plan.
"""

from dcim.choices import PowerFeedPhaseChoices, SubdeviceRoleChoices
from dcim.models import (
    Cable,
    Device,
    DeviceBayTemplate,
    DeviceRole,
    DeviceType,
    Location,
    Manufacturer,
    PowerFeed,
    PowerOutlet,
    PowerPanel,
    PowerPort,
    PowerPortTemplate,
    Rack,
    Site,
)
from django.test import TestCase, override_settings

from .. import workorder
from ..choices import DesignPlacementKindChoices as Kind
from ..models import DesignPlacement, DesignPowerFeed, DesignStep
from .utils import make_design

PKG = "netbox_rack_design"
CONFIG = {
    PKG: {
        "power_capacity_default_w": 8000,
        "power_draw_basis": "allocated",
        "power_warn_pct": 90,
        "power_critical_pct": 100,
        "distribution_mode": "none",
        "removal_status": "to_decommission",
        "planned_status": "planned",
    }
}


@override_settings(PLUGINS_CONFIG=CONFIG)
class WorkOrderTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="WO Site", slug="wo-site")
        cls.loc = Location.objects.create(site=cls.site, name="Hall 1", slug="hall-1")
        mfr = Manufacturer.objects.create(name="Acme", slug="acme")
        role = DeviceRole.objects.create(name="WO Server", slug="wo-server")
        cls.r1 = Rack.objects.create(name="R1", site=cls.site, location=cls.loc, u_height=42)
        cls.r2 = Rack.objects.create(name="R2", site=cls.site, location=cls.loc, u_height=42)

        cls.dt = DeviceType.objects.create(
            manufacturer=mfr, model="Srv-1U", slug="srv-1u", u_height=1, is_full_depth=False)
        PowerPortTemplate.objects.create(
            device_type=cls.dt, name="PSU1", allocated_draw=500, maximum_draw=500)
        cls.chassis_type = DeviceType.objects.create(
            manufacturer=mfr, model="Chassis-2U", slug="chassis-2u", u_height=2,
            is_full_depth=True, subdevice_role=SubdeviceRoleChoices.ROLE_PARENT)
        DeviceBayTemplate.objects.create(device_type=cls.chassis_type, name="b1")
        PowerPortTemplate.objects.create(
            device_type=cls.chassis_type, name="PSU1", allocated_draw=9000, maximum_draw=9000)
        cls.blade_type = DeviceType.objects.create(
            manufacturer=mfr, model="Blade-X", slug="blade-x", u_height=0,
            subdevice_role=SubdeviceRoleChoices.ROLE_CHILD)

        def real(name, rack, pos, **kw):
            dev = Device.objects.create(
                name=name, device_type=cls.dt, site=cls.site, rack=rack, position=pos,
                face="front", status="active", role=role, **kw)
            PowerPort.objects.update_or_create(
                device=dev, name="PSU1", defaults={"allocated_draw": 500, "maximum_draw": 500})
            return dev

        cls.old = real("old-55", cls.r1, 3, serial="SN-OLD", asset_tag="AT-OLD")
        cls.mover = real("mover-1", cls.r1, 6, serial="SN-MOV")

        # old-55 is cabled to bank "B" of a PDU in R2.
        pdu_type = DeviceType.objects.create(
            manufacturer=mfr, model="PDU", slug="pdu-wo", u_height=0)
        pdu = Device.objects.create(
            name="pdu-r2", device_type=pdu_type, site=cls.site, rack=cls.r2,
            role=role, status="active")
        outlet = PowerOutlet.objects.create(device=pdu, name="2/1", feed_leg="b")
        Cable(a_terminations=[PowerPort.objects.get(device=cls.old, name="PSU1")],
              b_terminations=[outlet]).save()

        cls.design = make_design(title="WO plan", site=cls.site)
        d = cls.design
        cls.step1 = DesignStep.objects.create(
            design=d, index=1, title="Window 1")
        cls.step2 = DesignStep.objects.create(design=d, index=2, title="Window 2")

        # Created in the "wrong" pk order on purpose: step_order decides.
        cls.move = DesignPlacement.objects.create(
            design=d, kind=Kind.KIND_MOVE, device=cls.mover, target_rack=cls.r2,
            target_position=5, target_face="front", step=cls.step1, step_order=2)
        cls.remove = DesignPlacement.objects.create(
            design=d, kind=Kind.KIND_REMOVE, device=cls.old, step=cls.step1, step_order=1)
        cls.chassis = DesignPlacement.objects.create(
            design=d, kind=Kind.KIND_ADD, device_type=cls.chassis_type, target_rack=cls.r1,
            target_position=20, target_face="front", proposed_name="new-chassis",
            step=cls.step2, step_order=1, preferred_feed_legs=["a", "b"])
        cls.blade = DesignPlacement.objects.create(
            design=d, kind=Kind.KIND_ADD, device_type=cls.blade_type,
            parent_placement=cls.chassis, target_bay_name="b1", proposed_name="new-blade")
        cls.loose = DesignPlacement.objects.create(
            design=d, kind=Kind.KIND_ADD, device_type=cls.dt, target_rack=cls.r2,
            target_position=30, target_face="rear", proposed_name="loose-srv")

    def setUp(self):
        self.data = workorder.build(self.design)

    # -- structure ----------------------------------------------------------

    def test_steps_and_actions_follow_the_saved_order(self):
        self.assertEqual([s["index"] for s in self.data["steps"]], [1, 2])
        first = self.data["steps"][0]
        self.assertEqual(first["title"], "Window 1")
        self.assertEqual([a["kind"] for a in first["actions"]], ["remove", "move"])
        self.assertEqual([a["order"] for a in first["actions"]], [1, 2])
        self.assertEqual(first["actions"][0]["device"]["name"], "old-55")

    def test_remove_has_from_and_no_to(self):
        act = self.data["steps"][0]["actions"][0]
        self.assertIsNone(act["to"])
        self.assertEqual(act["from"]["rack"], "R1")
        self.assertEqual(act["from"]["position"], "3")
        self.assertEqual(act["from"]["face"], "front")
        self.assertEqual(act["from"]["site"], "WO Site")
        self.assertEqual(act["from"]["location"], "Hall 1")

    def test_move_has_from_and_to(self):
        act = self.data["steps"][0]["actions"][1]
        self.assertEqual(act["from"]["rack"], "R1")
        self.assertEqual(act["from"]["position"], "6")
        self.assertEqual(act["to"]["rack"], "R2")
        self.assertEqual(act["to"]["position"], "5")
        self.assertEqual(act["to"]["face"], "front")

    def test_add_has_to_and_no_from_and_uses_the_proposed_name(self):
        act = self.data["steps"][1]["actions"][0]
        self.assertEqual(act["kind"], "add")
        self.assertIsNone(act["from"])
        self.assertEqual(act["to"]["rack"], "R1")
        self.assertEqual(act["to"]["position"], "20")
        self.assertEqual(act["device"]["name"], "new-chassis")

    def test_device_details(self):
        old = self.data["steps"][0]["actions"][0]["device"]
        self.assertEqual(old["serial"], "SN-OLD")
        self.assertEqual(old["asset_tag"], "AT-OLD")
        self.assertEqual(old["manufacturer"], "Acme")
        self.assertEqual(old["model"], "Srv-1U")
        self.assertEqual(old["u_height"], 1)
        self.assertFalse(old["full_depth"])
        chassis = self.data["steps"][1]["actions"][0]["device"]
        self.assertTrue(chassis["full_depth"])
        self.assertEqual(chassis["u_height"], 2)
        self.assertEqual(chassis["serial"], "")

    def test_status_after_uses_the_configured_removal_status(self):
        acts = self.data["steps"][0]["actions"]
        self.assertEqual(acts[0]["status_after"], "to_decommission")
        # A moved existing device keeps its current NetBox status.
        self.assertEqual(acts[1]["status_after"], "active")
        self.assertEqual(self.data["steps"][1]["actions"][0]["status_after"], "planned")
        self.assertEqual(self.data["unscheduled"][0]["status_after"], "planned")

    def test_move_status_after_is_the_devices_current_status(self):
        Device.objects.filter(pk=self.mover.pk).update(status="offline")
        data = workorder.build(self.design)
        move = data["steps"][0]["actions"][1]
        self.assertEqual(move["status_after"], "offline")
        self.assertIn("Status after: offline", workorder.render_markdown(data))

    def test_urls_are_absolute_paths(self):
        act = self.data["steps"][0]["actions"][0]
        self.assertTrue(act["device_url"].startswith("/"))
        self.assertIn(str(self.old.pk), act["device_url"])
        self.assertTrue(act["url"].startswith("/"))

    # -- power --------------------------------------------------------------

    def test_real_power_cabling_names_pdu_outlet_and_bank(self):
        cabling = self.data["steps"][0]["actions"][0]["power"]["cabling"]
        self.assertEqual(len(cabling), 1)
        self.assertEqual(cabling[0]["port"], "PSU1")
        self.assertEqual(cabling[0]["pdu"], "pdu-r2")
        self.assertEqual(cabling[0]["outlet"], "2/1")
        self.assertEqual(cabling[0]["bank"], "b")

    def test_planned_feed_legs_are_listed(self):
        power = self.data["steps"][1]["actions"][0]["power"]
        self.assertEqual(power["preferred_feed_legs"], ["a", "b"])
        self.assertEqual(power["cabling"], [])

    def test_bound_planned_feed_is_named(self):
        feed = DesignPowerFeed.objects.create(
            design=self.design, rack=self.r1, name="Feed A", voltage=230, amperage=16)
        self.chassis.planned_power_feed = feed
        self.chassis.save()
        power = workorder.build(self.design)["steps"][1]["actions"][0]["power"]
        self.assertEqual(power["feed"]["name"], "Feed A")
        self.assertEqual(power["feed"]["planned"], True)

    # -- blades -------------------------------------------------------------

    def test_blade_is_listed_under_its_chassis_not_as_an_action(self):
        actions = self.data["steps"][1]["actions"]
        self.assertEqual(len(actions), 1)
        children = actions[0]["children"]
        self.assertEqual([c["device"]["name"] for c in children], ["new-blade"])
        self.assertEqual(children[0]["bay"], "b1")
        self.assertEqual(children[0]["device"]["model"], "Blade-X")
        names = [a["device"]["name"] for s in self.data["steps"] for a in s["actions"]]
        self.assertNotIn("new-blade", names)
        self.assertNotIn("new-blade", [a["device"]["name"] for a in self.data["unscheduled"]])

    # -- unscheduled --------------------------------------------------------

    def test_unscheduled_list(self):
        self.assertEqual([a["device"]["name"] for a in self.data["unscheduled"]], ["loose-srv"])
        self.assertEqual(self.data["unscheduled"][0]["to"]["face"], "rear")

    # -- safety data --------------------------------------------------------

    def test_power_summary_per_touched_rack(self):
        step1 = self.data["steps"][0]
        racks = {p["rack"]: p for p in step1["power"]}
        self.assertEqual(set(racks), {"R1", "R2"})
        # R1: old (500) + mover (500) before, both gone after step 1 -> 0 W
        self.assertEqual(racks["R1"]["draw_w"], 0.0)
        self.assertEqual(racks["R2"]["capacity_w"], 8000.0)
        self.assertIn("kW", racks["R2"]["summary"])
        self.assertEqual(self.data["steps"][0]["racks"], ["R1", "R2"])

    def test_problems_are_carried_from_the_simulation(self):
        self.assertEqual(self.data["steps"][0]["problems"], [])
        problems = self.data["steps"][1]["problems"]
        self.assertIn("rack_over", [p["code"] for p in problems])
        self.assertTrue(all(p["detail"] for p in problems))

    # -- filtering ----------------------------------------------------------

    def test_single_step(self):
        data = workorder.build(self.design, step=2)
        self.assertEqual([s["index"] for s in data["steps"]], [2])
        self.assertIn("rack_over", [p["code"] for p in data["steps"][0]["problems"]])
        # unscheduled work is plan-level, not part of one step's ticket
        self.assertEqual(data["unscheduled"], [])

    def test_single_step_still_knows_the_total(self):
        data = workorder.build(self.design, step=2)
        self.assertEqual(data["total_steps"], 2)
        self.assertEqual(data["steps"][0]["index"], 2)
        self.assertEqual(data["steps"][0]["total_steps"], 2)
        self.assertIn("## Step 2 of 2", workorder.render_markdown(data))

    def test_all_steps_carry_the_total(self):
        self.assertEqual(self.data["total_steps"], 2)
        self.assertEqual([s["total_steps"] for s in self.data["steps"]], [2, 2])

    def test_unknown_step_raises(self):
        with self.assertRaises(workorder.StepNotFound):
            workorder.build(self.design, step=9)

    def test_design_without_a_plan_has_only_unscheduled(self):
        other = make_design(title="Bare", site=self.site)
        DesignPlacement.objects.create(
            design=other, kind=Kind.KIND_REMOVE, device=self.old)
        data = workorder.build(other)
        self.assertEqual(data["steps"], [])
        self.assertEqual(len(data["unscheduled"]), 1)

    # -- markdown -----------------------------------------------------------

    def test_markdown_checklist(self):
        md = workorder.render_markdown(self.data)
        self.assertIn("# WO plan", md)
        self.assertIn("## Step 1 of 2 – Window 1", md)
        self.assertIn("## Step 2 of 2 – Window 2", md)
        self.assertIn("1. [ ] **Remove** old-55", md)
        self.assertIn("2. [ ] **Move** mover-1", md)
        self.assertIn("1. [ ] **Add** new-chassis", md)
        self.assertIn("SN-OLD", md)
        self.assertIn("R1 U3 front", md)
        self.assertIn("R2 U5 front", md)
        self.assertIn("pdu-r2", md)
        self.assertIn("to_decommission", md)
        self.assertIn("new-blade", md)
        self.assertIn("Unscheduled", md)
        self.assertIn("loose-srv", md)
        # step order in the document
        self.assertLess(md.index("## Step 1"), md.index("## Step 2"))
        # problems are visible
        self.assertIn("rack_over", md)


class RenderCsvTest(WorkOrderTest):
    """One CSV row per action (children included), header first."""

    def test_header_and_rows(self):
        import csv
        import io

        rows = list(csv.reader(io.StringIO(workorder.render_csv(self.data))))
        self.assertEqual(rows[0], list(workorder.CSV_COLUMNS))
        header = rows[0]
        body = [dict(zip(header, r, strict=True)) for r in rows[1:]]
        names = [r["device"] for r in body]
        self.assertIn("old-55", names)
        self.assertIn("new-blade", names)
        old = next(r for r in body if r["device"] == "old-55")
        self.assertEqual(old["step"], "1")
        self.assertEqual(old["kind"], "remove")
        self.assertEqual(old["from_rack"], "R1")
        self.assertEqual(old["from_u"], "3")
        self.assertEqual(old["from_face"], "front")
        self.assertEqual(old["to_rack"], "")
        self.assertEqual(old["status_after"], "to_decommission")
        move = next(r for r in body if r["device"] == "mover-1")
        self.assertEqual(move["to_rack"], "R2")
        self.assertEqual(move["to_u"], "5")

    def test_unscheduled_rows_have_empty_step(self):
        import csv
        import io

        rows = list(csv.DictReader(io.StringIO(workorder.render_csv(self.data))))
        loose = next(r for r in rows if r["device"] == "loose-srv")
        self.assertEqual(loose["step"], "")


@override_settings(PLUGINS_CONFIG={PKG: dict(CONFIG[PKG], distribution_mode="builtin")})
class WorkOrderMovePowerTest(TestCase):
    """A move lists the source cabling (power_from) AND the destination bank (power_to)."""

    @classmethod
    def setUpTestData(cls):
        site = Site.objects.create(name="MP Site", slug="mp-site")
        mfr = Manufacturer.objects.create(name="MPAcme", slug="mp-acme")
        role = DeviceRole.objects.create(name="MP Role", slug="mp-role")
        pdu_role = DeviceRole.objects.get_or_create(name="PDU", slug="pdu")[0]
        srv = DeviceType.objects.create(
            manufacturer=mfr, model="MP-1U", slug="mp-1u", u_height=1, is_full_depth=False)
        pdu_type = DeviceType.objects.create(
            manufacturer=mfr, model="MP-PDU", slug="mp-pdu", u_height=0)
        panel = PowerPanel.objects.create(site=site, name="MP Panel")
        cls.racks, outlets = {}, {}
        for tag in ("a", "b"):
            rack = Rack.objects.create(name=f"MP-{tag.upper()}", site=site, u_height=42)
            feed = PowerFeed.objects.create(
                power_panel=panel, name=f"Feed {tag.upper()}", voltage=230, amperage=32,
                phase=PowerFeedPhaseChoices.PHASE_SINGLE)
            pdu = Device.objects.create(
                name=f"pdu-{tag}1", device_type=pdu_type, site=site, rack=rack,
                role=pdu_role, status="active")
            outlets[tag] = {n: PowerOutlet.objects.create(device=pdu, name=n)
                            for n in ("1/1", "2/1")}
            Cable(a_terminations=[PowerPort.objects.create(device=pdu, name="Input")],
                  b_terminations=[feed]).save()
            cls.racks[tag] = rack
        cls.mover = Device.objects.create(
            name="mp-web-3", device_type=srv, site=site, rack=cls.racks["a"],
            position=10, face="front", status="active", role=role)
        port = PowerPort.objects.create(
            device=cls.mover, name="PSU1", allocated_draw=500, maximum_draw=500)
        Cable(a_terminations=[port], b_terminations=[outlets["a"]["2/1"]]).save()

        design = make_design(title="MP plan", site=site)
        step = DesignStep.objects.create(design=design, index=1, title="Move")
        DesignPlacement.objects.create(
            design=design, kind=Kind.KIND_MOVE, device=cls.mover,
            target_rack=cls.racks["b"], target_position=5, target_face="front",
            step=step, step_order=1)
        cls.design = design

    def setUp(self):
        self.data = workorder.build(self.design)
        self.act = self.data["steps"][0]["actions"][0]

    def test_power_from_is_the_source_cabling(self):
        src = self.act["power_from"]
        self.assertEqual([c["pdu"] for c in src["cabling"]], ["pdu-a1"])
        self.assertEqual(self.act["power"]["cabling"], src["cabling"])  # old key kept

    def test_power_to_names_the_target_racks_pdu_and_bank(self):
        to = self.act["power_to"]
        self.assertEqual(len(to), 1)
        self.assertEqual(to[0]["pdu"], "pdu-b1")
        self.assertTrue(to[0]["bank"])
        self.assertIsNone(to[0]["outlet"])
        self.assertIn("any free outlet", to[0]["text"])
        self.assertIn("pdu-b1", to[0]["text"])

    def test_markdown_shows_unplug_and_plug_into(self):
        md = workorder.render_markdown(self.data)
        self.assertIn("Unplug from: PSU1 on PDU pdu-a1 outlet 2/1", md)
        self.assertIn("Plug into: PDU pdu-b1 bank", md)
        self.assertIn("any free outlet", md)

    def test_csv_has_destination_columns(self):
        import csv
        import io

        self.assertIn("to_pdu", workorder.CSV_COLUMNS)
        self.assertIn("to_bank", workorder.CSV_COLUMNS)
        row = next(csv.DictReader(io.StringIO(workorder.render_csv(self.data))))
        self.assertEqual(row["pdu"], "pdu-a1")
        self.assertEqual(row["to_pdu"], "pdu-b1")
        self.assertTrue(row["to_bank"])
