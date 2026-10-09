"""
``POST .../designs/<pk>/simulate-steps/`` -- PLAN-execution-steps.md Sec. 4.

Steps 1..N of a PROPOSED order are projected cumulatively (read-only), and each
touched rack reports its power, distribution, distribution_status and a list of
``problems`` (rack_over, rack_near, pdu_over, bank_over, u_conflict,
bay_conflict, unpowered, redundancy_lost, engine_error).
"""

from unittest import mock

from core.models import ObjectChange
from dcim.choices import PowerFeedPhaseChoices, SubdeviceRoleChoices
from dcim.models import (
    Cable,
    Device,
    DeviceBayTemplate,
    DeviceRole,
    DeviceType,
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
from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase

from ..choices import DesignPlacementKindChoices as Kind
from ..models import DesignPlacement, DesignStep
from .utils import make_design

PKG = "netbox_rack_design"


def _cfg(**overrides):
    cfg = {
        "power_capacity_default_w": 8000,
        "power_draw_basis": "allocated",
        "power_warn_pct": 90,
        "power_critical_pct": 100,
        "distribution_mode": "none",
    }
    cfg.update(overrides)
    return {PKG: cfg}


# --- fake distribution scripts (module-level so they are importable by path) ---

def _dist(bank):
    return {
        "scheme": "", "pdu_location": None,
        "pdus": {"pdu-x": {
            "feed_name": "F", "feed_letter": "a", "feed_source": None, "phase": 1,
            "allocated_draw": 1000, "power_bank_count": len(bank), "banks": bank,
        }},
        "rack": {"power_limitation_w": None, "power_consumption_w": 0.0,
                 "alarm": False, "warnings": []},
    }


def _bank(allocated, state, max_power=600):
    return {"max_power": max_power, "allocated_power": allocated, "planned_power": 0,
            "util_pct": allocated / max_power * 100.0, "state": state,
            "units": [], "devices": []}


def fake_bank_overload(rack, devices):
    return _dist({"1": _bank(700, "overload"), "2": _bank(100, "ok")})


def fake_bank_critical(rack, devices):
    return _dist({"1": _bank(600, "critical"), "2": _bank(100, "ok")})


def fake_bank_warn(rack, devices):
    return _dist({"1": _bank(500, "warn"), "2": _bank(100, "ok")})


def fake_pdu_over(rack, devices):
    # every bank is within its own breaker, but the PDU's input (1000 W) is not
    return _dist({"1": _bank(550, "ok", 600), "2": _bank(550, "ok", 600)})


def fake_all_fine(rack, devices):
    return _dist({"1": _bank(100, "ok"), "2": _bank(100, "ok")})


def _script(fn):
    return _cfg(distribution_mode="script", distribution_script=f"{PKG}.tests.test_simulate_steps.{fn}")


@override_settings(PLUGINS_CONFIG=_cfg())
class SimulateStepsTest(APITestCase):
    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="SS Site", slug="ss-site")
        mfr = Manufacturer.objects.create(name="SS Mfr", slug="ss-mfr")
        cls.role = DeviceRole.objects.create(name="SS Server", slug="ss-server")
        cls.pdu_role = DeviceRole.objects.create(name="SS PDU", slug="pdu")

        cls.rack_a = Rack.objects.create(name="SS A", site=cls.site, u_height=42)
        cls.rack_b = Rack.objects.create(name="SS B", site=cls.site, u_height=42)
        cls.rack_p = Rack.objects.create(name="SS P", site=cls.site, u_height=10)
        cls.key_a = f"r:{cls.rack_a.pk}"
        cls.key_b = f"r:{cls.rack_b.pk}"
        cls.key_p = f"r:{cls.rack_p.pk}"

        # A 1U server whose catalog draw is 1300 W (what an ADD of it draws).
        cls.dt_srv = DeviceType.objects.create(
            manufacturer=mfr, model="SS-Srv", slug="ss-srv", u_height=1, is_full_depth=False)
        PowerPortTemplate.objects.create(
            device_type=cls.dt_srv, name="PSU1", allocated_draw=1300, maximum_draw=1300)
        pdu_type = DeviceType.objects.create(
            manufacturer=mfr, model="SS-PDU", slug="ss-pdu-t", u_height=0)

        # Chassis + blade (blade draws 300 W, the chassis nothing of its own).
        cls.chassis_type = DeviceType.objects.create(
            manufacturer=mfr, model="SS-Chassis", slug="ss-chassis", u_height=2,
            is_full_depth=False, subdevice_role=SubdeviceRoleChoices.ROLE_PARENT)
        DeviceBayTemplate.objects.create(device_type=cls.chassis_type, name="b1")
        cls.blade_type = DeviceType.objects.create(
            manufacturer=mfr, model="SS-Blade", slug="ss-blade", u_height=0,
            subdevice_role=SubdeviceRoleChoices.ROLE_CHILD)
        PowerPortTemplate.objects.create(
            device_type=cls.blade_type, name="PSU1", allocated_draw=300, maximum_draw=300)

        def real(name, rack, pos, watts):
            dev = Device.objects.create(
                name=name, device_type=cls.dt_srv, site=cls.site, rack=rack,
                position=pos, face="front", status="active", role=cls.role)
            PowerPort.objects.update_or_create(
                device=dev, name="PSU1",
                defaults={"allocated_draw": watts, "maximum_draw": watts})
            return dev

        # Rack A: 4000 + 2400 + 200 (mover) = 6600 of 8000 W (82.5%).
        cls.base = real("ss-base", cls.rack_a, 1, 4000)
        cls.old = real("ss-old", cls.rack_a, 3, 2400)
        cls.mover = real("ss-mover", cls.rack_a, 6, 200)

        # Rack P: a PDU on a feed, one consumer cabled to bank 2.
        panel = PowerPanel.objects.create(site=cls.site, name="SS Panel")
        feed = PowerFeed.objects.create(
            power_panel=panel, name="SS Feed", voltage=230, amperage=32,
            phase=PowerFeedPhaseChoices.PHASE_SINGLE)
        cls.pdu = Device.objects.create(
            name="ss-pdu", device_type=pdu_type, site=cls.site, rack=cls.rack_p,
            role=cls.pdu_role, status="active")
        outlets = {n: PowerOutlet.objects.create(device=cls.pdu, name=n)
                   for n in ("1/1", "2/1")}
        pdu_in = PowerPort.objects.create(device=cls.pdu, name="Input")
        Cable(a_terminations=[pdu_in], b_terminations=[feed]).save()
        cls.cons = real("ss-cons", cls.rack_p, 1, 1000)
        Cable(a_terminations=[PowerPort.objects.get(device=cls.cons, name="PSU1")],
              b_terminations=[outlets["2/1"]]).save()

        cls.design = make_design(title="SS plan", site=cls.site)
        d = cls.design

        def add(name, rack, pos, dt=None):
            return DesignPlacement.objects.create(
                design=d, kind=Kind.KIND_ADD, device_type=dt or cls.dt_srv,
                target_rack=rack, target_position=pos, target_face="front",
                proposed_name=name)

        cls.add1 = add("ss-new-1", cls.rack_a, 10)
        cls.add2 = add("ss-new-2", cls.rack_a, 12)
        cls.add_conflict = add("ss-conflict", cls.rack_a, 3)  # old sits at U3
        cls.rem_old = DesignPlacement.objects.create(
            design=d, kind=Kind.KIND_REMOVE, device=cls.old)
        cls.move = DesignPlacement.objects.create(
            design=d, kind=Kind.KIND_MOVE, device=cls.mover,
            target_rack=cls.rack_b, target_position=5, target_face="front")
        cls.chassis = add("ss-chassis", cls.rack_a, 20, cls.chassis_type)
        cls.blade = DesignPlacement.objects.create(
            design=d, kind=Kind.KIND_ADD, device_type=cls.blade_type,
            parent_placement=cls.chassis, target_bay_name="b1", proposed_name="ss-blade-1")
        cls.rem_pdu = DesignPlacement.objects.create(
            design=d, kind=Kind.KIND_REMOVE, device=cls.pdu)
        cls.rem_cons = DesignPlacement.objects.create(
            design=d, kind=Kind.KIND_REMOVE, device=cls.cons)

        # A saved plan that the proposed order must NOT be read from.
        DesignStep.objects.create(design=d, index=1, title="saved")

        cls.other_design = make_design(title="SS other", site=cls.site)
        cls.foreign = DesignPlacement.objects.create(
            design=cls.other_design, kind=Kind.KIND_REMOVE, device=cls.base)

    # -- helpers ------------------------------------------------------------

    def _url(self):
        return reverse(
            "plugins-api:netbox_rack_design-api:design-simulate-steps",
            kwargs={"pk": self.design.pk})

    def _post(self, steps, **extra):
        body = {"steps": [[p.pk for p in step] for step in steps]}
        body.update(extra)
        return self.client.post(self._url(), body, format="json", **self.header)

    def _sim(self, steps, **extra):
        self.add_permissions("netbox_rack_design.view_design")
        resp = self._post(steps, **extra)
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        return resp.data["steps"]

    @staticmethod
    def _codes(step, key):
        return sorted(p["code"] for p in step["racks"][key]["problems"])

    # -- Sec. 1 scenario ----------------------------------------------------

    def test_add_before_remove_overloads_the_rack_then_clean(self):
        out = self._sim([[self.add1, self.add2], [self.rem_old]])
        self.assertEqual([s["index"] for s in out], [1, 2])
        # 6600 + 2600 = 9200 W of 8000 W
        self.assertEqual(out[0]["racks"][self.key_a]["power"]["draw_w"], 9200.0)
        self.assertIn("rack_over", self._codes(out[0], self.key_a))
        over = [p for p in out[0]["racks"][self.key_a]["problems"] if p["code"] == "rack_over"][0]
        self.assertEqual(over["severity"], "error")
        self.assertTrue(over["detail"])
        # 9200 - 2400 = 6800 W (85%)
        self.assertEqual(out[1]["racks"][self.key_a]["power"]["draw_w"], 6800.0)
        self.assertEqual(self._codes(out[1], self.key_a), [])

    def test_remove_first_keeps_both_steps_clean(self):
        out = self._sim([[self.rem_old], [self.add1, self.add2]])
        self.assertEqual(out[0]["racks"][self.key_a]["power"]["draw_w"], 4200.0)
        self.assertEqual(self._codes(out[0], self.key_a), [])
        self.assertEqual(out[1]["racks"][self.key_a]["power"]["draw_w"], 6800.0)
        self.assertEqual(self._codes(out[1], self.key_a), [])

    def test_rack_near_is_a_warning_at_the_warn_threshold(self):
        out = self._sim([[self.add1]])  # 7900 W = 98.75% of 8000, warn_pct 90
        problems = out[0]["racks"][self.key_a]["problems"]
        self.assertEqual([p["code"] for p in problems], ["rack_near"])
        self.assertEqual(problems[0]["severity"], "warning")

    def test_unscheduled_placements_are_in_no_step(self):
        out = self._sim([[self.rem_old]])  # add1/add2/... exist but are unlisted
        self.assertEqual(out[0]["racks"][self.key_a]["power"]["draw_w"], 4000.0 + 200.0)

    # -- u_conflict ---------------------------------------------------------

    def test_add_into_a_slot_freed_by_a_later_step_is_a_u_conflict(self):
        out = self._sim([[self.add_conflict], [self.rem_old]])
        problems = out[0]["racks"][self.key_a]["problems"]
        self.assertIn("u_conflict", [p["code"] for p in problems])
        self.assertEqual(
            [p["severity"] for p in problems if p["code"] == "u_conflict"], ["error"])
        self.assertNotIn("u_conflict", self._codes(out[1], self.key_a))

    def test_remove_in_an_earlier_step_means_no_u_conflict(self):
        out = self._sim([[self.rem_old], [self.add_conflict]])
        for step in out:
            self.assertNotIn("u_conflict", self._codes(step, self.key_a))

    def test_remove_and_add_in_the_same_step_is_no_conflict(self):
        out = self._sim([[self.rem_old, self.add_conflict]])
        self.assertNotIn("u_conflict", self._codes(out[0], self.key_a))

    # -- bay_conflict -------------------------------------------------------

    def test_projection_bay_conflict_surfaces_as_bay_conflict(self):
        from .. import projection
        from .. import steps as steps_mod

        real_project = projection.project_rack

        def with_bay_conflict(design, rack, *, only_placements=None):
            elev = real_project(design, rack, only_placements=only_placements)
            elev.conflicts.append(projection._conflict(
                "bay_occupied", severity="warning", placement=self.add1,
                detail="Bay 'b1' is claimed by this design, but x is already there."))
            return elev

        with mock.patch.object(steps_mod, "project_rack", with_bay_conflict):
            out = steps_mod.simulate(self.design, [[self.add1.pk]])
        problems = out[0]["racks"][self.key_a]["problems"]
        bay = [p for p in problems if p["code"] == "bay_conflict"]
        self.assertEqual(len(bay), 1)
        self.assertEqual(bay[0]["severity"], "error")
        self.assertIn("b1", bay[0]["detail"])

    # -- moves / blades -----------------------------------------------------

    def test_move_out_of_rack_a_changes_rack_a(self):
        out = self._sim([[self.move]])
        racks = out[0]["racks"]
        self.assertEqual(set(racks), {self.key_a, self.key_b})
        # 6400 W left in A (mover gone), 200 W arrived in B.
        self.assertEqual(racks[self.key_a]["power"]["draw_w"], 6400.0)
        self.assertEqual(racks[self.key_b]["power"]["draw_w"], 200.0)

    def test_untouched_racks_are_not_projected(self):
        out = self._sim([[self.add1]])
        self.assertEqual(set(out[0]["racks"]), {self.key_a})

    def test_blade_follows_its_chassis(self):
        out = self._sim([[self.chassis]])
        # 6600 + 300 (blade folded into the chassis) = 6900
        self.assertEqual(out[0]["racks"][self.key_a]["power"]["draw_w"], 6900.0)

    def test_blade_without_its_chassis_in_a_step_adds_nothing(self):
        out = self._sim([[self.add1]])
        self.assertEqual(out[0]["racks"][self.key_a]["power"]["draw_w"], 7900.0)

    def test_listing_a_blade_is_rejected(self):
        self.add_permissions("netbox_rack_design.view_design")
        resp = self._post([[self.chassis, self.blade]])
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)
        self.assertIn(str(self.blade.pk), str(resp.data))

    # -- validation ---------------------------------------------------------

    def test_foreign_placement_is_rejected(self):
        self.add_permissions("netbox_rack_design.view_design")
        resp = self._post([[self.add1], [self.foreign]])
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)
        self.assertIn(str(self.foreign.pk), str(resp.data))

    def test_unknown_placement_id_is_rejected(self):
        self.add_permissions("netbox_rack_design.view_design")
        resp = self.client.post(
            self._url(), {"steps": [[999999999]]}, format="json", **self.header)
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_id_across_steps_is_rejected(self):
        self.add_permissions("netbox_rack_design.view_design")
        resp = self._post([[self.add1], [self.add1]])
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)
        self.assertIn(str(self.add1.pk), str(resp.data))

    def test_duplicate_id_inside_one_step_is_rejected(self):
        self.add_permissions("netbox_rack_design.view_design")
        resp = self._post([[self.add1, self.add1]])
        self.assertHttpStatus(resp, status.HTTP_400_BAD_REQUEST)

    def test_malformed_body_and_rack_key_are_rejected(self):
        self.add_permissions("netbox_rack_design.view_design")
        self.assertHttpStatus(
            self.client.post(self._url(), {}, format="json", **self.header),
            status.HTTP_400_BAD_REQUEST)
        self.assertHttpStatus(self._post([[self.add1]], racks=["x:1"]),
                              status.HTTP_400_BAD_REQUEST)
        self.assertHttpStatus(self._post([[self.add1]], from_step=0),
                              status.HTTP_400_BAD_REQUEST)

    # -- limiting the output --------------------------------------------------

    def test_from_and_to_step_limit_the_output_but_stay_cumulative(self):
        out = self._sim([[self.add1], [self.add2], [self.rem_old]], from_step=2, to_step=2)
        self.assertEqual([s["index"] for s in out], [2])
        # steps 1..2 are in the state: 6600 + 1300 + 1300
        self.assertEqual(out[0]["racks"][self.key_a]["power"]["draw_w"], 9200.0)

    def test_from_step_alone_and_to_step_alone(self):
        steps = [[self.add1], [self.add2], [self.rem_old]]
        self.assertEqual([s["index"] for s in self._sim(steps, from_step=2)], [2, 3])
        self.assertEqual([s["index"] for s in self._sim(steps, to_step=2)], [1, 2])

    def test_racks_filter_limits_racks_to_the_touched_ones(self):
        out = self._sim([[self.move]], racks=[self.key_b])
        self.assertEqual(set(out[0]["racks"]), {self.key_b})
        out = self._sim([[self.add1]], racks=[self.key_b])  # B is not touched
        self.assertEqual(out[0]["racks"], {})

    # -- distribution / engine ----------------------------------------------

    def test_builtin_distribution_is_reported_per_step(self):
        with override_settings(PLUGINS_CONFIG=_cfg(distribution_mode="builtin")):
            out = self._sim([[self.rem_cons]])
        rack = out[0]["racks"][self.key_p]
        self.assertEqual(rack["distribution_status"]["state"], "ok")
        self.assertEqual(
            rack["distribution"]["pdus"]["ss-pdu"]["banks"]["2"]["allocated_power"], 0)

    def test_engine_error_is_surfaced(self):
        cfg = _cfg(distribution_mode="script",
                   distribution_script=f"{PKG}.tests.test_distribution.raising_distribution_fn")
        with override_settings(PLUGINS_CONFIG=cfg):
            out = self._sim([[self.add1]])
        rack = out[0]["racks"][self.key_a]
        self.assertEqual(rack["distribution_status"]["state"], "failed")
        errs = [p for p in rack["problems"] if p["code"] == "engine_error"]
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["severity"], "error")
        self.assertIn("RuntimeError", errs[0]["detail"])

    def test_engine_off_or_empty_is_not_an_engine_error(self):
        out = self._sim([[self.add1]])  # distribution_mode none
        self.assertEqual(out[0]["racks"][self.key_a]["distribution_status"]["state"], "off")
        self.assertNotIn("engine_error", self._codes(out[0], self.key_a))

    def test_bank_over_for_overload_and_critical_banks(self):
        for fn in ("fake_bank_overload", "fake_bank_critical"):
            with self.subTest(fn=fn), override_settings(PLUGINS_CONFIG=_script(fn)):
                out = self._sim([[self.rem_old]])
                problems = out[0]["racks"][self.key_a]["problems"]
                bank = [p for p in problems if p["code"] == "bank_over"]
                self.assertEqual(len(bank), 1, problems)
                self.assertEqual(bank[0]["severity"], "error")
                self.assertIn("pdu-x", bank[0]["detail"])

    def test_bank_near_is_a_warning_for_a_warn_bank(self):
        with override_settings(PLUGINS_CONFIG=_script("fake_bank_warn")):
            out = self._sim([[self.rem_old]])
        problems = out[0]["racks"][self.key_a]["problems"]
        near = [p for p in problems if p["code"] == "bank_near"]
        self.assertEqual(len(near), 1, problems)
        self.assertEqual(near[0]["severity"], "warning")
        self.assertIn("pdu-x", near[0]["detail"])
        self.assertNotIn("bank_over", [p["code"] for p in problems])

    def test_pdu_over_when_banks_sum_beyond_the_pdu_input(self):
        with override_settings(PLUGINS_CONFIG=_script("fake_pdu_over")):
            out = self._sim([[self.rem_old]])
        codes = self._codes(out[0], self.key_a)
        self.assertIn("pdu_over", codes)
        self.assertNotIn("bank_over", codes)

    def test_healthy_distribution_has_no_distribution_problems(self):
        with override_settings(PLUGINS_CONFIG=_script("fake_all_fine")):
            out = self._sim([[self.rem_old]])
        self.assertEqual(self._codes(out[0], self.key_a), [])

    # -- unpowered ----------------------------------------------------------

    def test_removing_a_pdu_while_its_devices_stay_is_unpowered(self):
        out = self._sim([[self.rem_pdu], [self.rem_cons]])
        problems = out[0]["racks"][self.key_p]["problems"]
        un = [p for p in problems if p["code"] == "unpowered"]
        self.assertEqual(len(un), 1)
        self.assertEqual(un[0]["severity"], "error")
        self.assertIn("ss-pdu", un[0]["detail"])
        self.assertIn("ss-cons", un[0]["detail"])
        self.assertNotIn("unpowered", self._codes(out[1], self.key_p))

    def test_removing_the_pdu_with_its_devices_or_after_them_is_fine(self):
        out = self._sim([[self.rem_pdu, self.rem_cons]])
        self.assertNotIn("unpowered", self._codes(out[0], self.key_p))
        out = self._sim([[self.rem_cons], [self.rem_pdu]])
        for step in out:
            self.assertNotIn("unpowered", self._codes(step, self.key_p))

    # -- read-only / permissions --------------------------------------------

    def test_nothing_persists(self):
        self.add_permissions("netbox_rack_design.view_design")
        before = (DesignPlacement.objects.count(), DesignStep.objects.count(),
                  ObjectChange.objects.count())
        steps = self.design.steps.get(index=1)
        saved = list(DesignPlacement.objects.values_list("pk", "step_id", "step_order"))
        resp = self._post([[self.add1, self.move], [self.rem_old]])
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        after = (DesignPlacement.objects.count(), DesignStep.objects.count(),
                 ObjectChange.objects.count())
        self.assertEqual(before, after)
        self.assertEqual(
            saved, list(DesignPlacement.objects.values_list("pk", "step_id", "step_order")))
        steps.refresh_from_db()
        self.assertEqual(steps.title, "saved")

    def test_view_only_user_gets_200(self):
        self.add_permissions("netbox_rack_design.view_design")
        self.assertHttpStatus(self._post([[self.add1]]), status.HTTP_200_OK)

    def test_read_only_token_may_simulate(self):
        # The MCP server runs with a read-only token and simulates through this POST.
        self.add_permissions("netbox_rack_design.view_design")
        self.token.write_enabled = False
        self.token.save()
        self.assertHttpStatus(self._post([[self.add1]]), status.HTTP_200_OK)

    def test_read_only_token_still_cannot_save_steps(self):
        self.add_permissions("netbox_rack_design.view_design", "netbox_rack_design.change_design")
        self.token.write_enabled = False
        self.token.save()
        url = reverse("plugins-api:netbox_rack_design-api:design-save-steps",
                      kwargs={"pk": self.design.pk})
        resp = self.client.post(url, {"steps": []}, format="json", **self.header)
        self.assertHttpStatus(resp, status.HTTP_403_FORBIDDEN)

    def test_user_without_permission_is_refused(self):
        resp = self._post([[self.add1]])
        self.assertHttpStatus(resp, status.HTTP_403_FORBIDDEN)


# --- a PDU removed by the design is not a power source -------------------------

@override_settings(PLUGINS_CONFIG=_cfg(distribution_mode="builtin"))
class RemovedPduIsNotAPowerSourceTest(TestCase):
    """A rack with a left and a right PDU (own feeds, banks 1 and 2). ``dual`` has
    PSU1 on the left PDU and PSU2 on the right one; ``single`` only on the left.
    The design removes the left PDU and adds a planned server."""

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="RP Site", slug="rp-site")
        mfr = Manufacturer.objects.create(name="RP Mfr", slug="rp-mfr")
        role = DeviceRole.objects.create(name="RP Server", slug="rp-server")
        pdu_role = DeviceRole.objects.create(name="RP PDU", slug="pdu")
        cls.rack = Rack.objects.create(name="RP A", site=cls.site, u_height=10)
        cls.key = f"r:{cls.rack.pk}"
        cls.dt_srv = DeviceType.objects.create(
            manufacturer=mfr, model="RP-Srv", slug="rp-srv", u_height=1, is_full_depth=False)
        PowerPortTemplate.objects.create(
            device_type=cls.dt_srv, name="PSU1", allocated_draw=1300, maximum_draw=1300)
        pdu_type = DeviceType.objects.create(
            manufacturer=mfr, model="RP-PDU", slug="rp-pdu-t", u_height=0)
        panel = PowerPanel.objects.create(site=cls.site, name="RP Panel")

        def pdu(name, feed_name):
            feed = PowerFeed.objects.create(
                power_panel=panel, name=feed_name, voltage=230, amperage=32,
                phase=PowerFeedPhaseChoices.PHASE_SINGLE)
            dev = Device.objects.create(
                name=name, device_type=pdu_type, site=cls.site, rack=cls.rack,
                role=pdu_role, status="active")
            outlets = {n: PowerOutlet.objects.create(device=dev, name=n)
                       for n in ("1/1", "2/1")}
            Cable(a_terminations=[PowerPort.objects.create(device=dev, name="Input")],
                  b_terminations=[feed]).save()
            return dev, outlets

        cls.pdu_l, out_l = pdu("rp-pdu-l", "Feed A")
        cls.pdu_r, out_r = pdu("rp-pdu-r", "Feed B")

        def server(name, pos, psus):
            dev = Device.objects.create(
                name=name, device_type=cls.dt_srv, site=cls.site, rack=cls.rack,
                position=pos, face="front", status="active", role=role)
            PowerPort.objects.filter(device=dev).delete()
            for psu, outlet in psus:
                port = PowerPort.objects.create(
                    device=dev, name=psu, allocated_draw=500, maximum_draw=500)
                Cable(a_terminations=[port], b_terminations=[outlet]).save()
            return dev

        cls.dual = server("rp-dual", 2, [("PSU1", out_l["2/1"]), ("PSU2", out_r["2/1"])])
        cls.single = server("rp-single", 4, [("PSU1", out_l["1/1"])])

        cls.design = make_design(title="RP plan", site=cls.site)
        cls.rem_pdu = DesignPlacement.objects.create(
            design=cls.design, kind=Kind.KIND_REMOVE, device=cls.pdu_l)
        cls.add = DesignPlacement.objects.create(
            design=cls.design, kind=Kind.KIND_ADD, device_type=cls.dt_srv,
            target_rack=cls.rack, target_position=8, target_face="front",
            proposed_name="rp-new")
        cls.rem_single = DesignPlacement.objects.create(
            design=cls.design, kind=Kind.KIND_REMOVE, device=cls.single)

    @staticmethod
    def _bank_total(dist, pdu, key="allocated_power"):
        return sum(b[key] for b in dist["pdus"][pdu]["banks"].values())

    def _step(self, *placements):
        from .. import steps as steps_mod
        out = steps_mod.simulate(self.design, [[p.pk for p in placements]])
        return out[0]["racks"][self.key]

    def _baseline(self):
        from .. import steps as steps_mod
        out = steps_mod.simulate(self.design, [[self.add.pk]])
        return out[0]["racks"][self.key]["distribution"]

    def test_builtin_removed_pdu_has_no_banks_and_no_planned_power(self):
        base = self._baseline()
        dual_on_r = self._bank_total(base, "rp-pdu-r")
        self.assertGreater(dual_on_r, 0)
        dist = self._step(self.rem_pdu, self.add)["distribution"]
        self.assertNotIn("rp-pdu-l", dist["pdus"])
        r = dist["pdus"]["rp-pdu-r"]["banks"]
        # dual (cabled to R bank 2) keeps its full draw there
        self.assertEqual(r["2"]["allocated_power"], base["pdus"]["rp-pdu-r"]["banks"]["2"]["allocated_power"])
        names = [d["name"] for b in r.values() for d in b["devices"]]
        self.assertIn("rp-dual", names)
        # the new server's planned power is on R, never on the removed PDU
        self.assertEqual(self._bank_total(dist, "rp-pdu-r", "planned_power"), 1300.0)

    def test_builtin_single_path_device_loses_the_removed_pdu_not_the_survivor(self):
        dist = self._step(self.rem_pdu)["distribution"]
        self.assertNotIn("rp-pdu-l", dist["pdus"])
        banks = dist["pdus"]["rp-pdu-r"]["banks"]
        total_dual = sum(d["draw_w"] for b in banks.values() for d in b["devices"] if d["name"] == "rp-dual")
        self.assertGreater(total_dual, 0)

    def test_script_mode_gets_a_rack_whose_pdus_exclude_the_removed_one(self):
        cfg = _cfg(distribution_mode="script",
                   distribution_script="netbox_rack_design.distribution_example.build")
        with override_settings(PLUGINS_CONFIG=cfg):
            rack = self._step(self.rem_pdu, self.add)
            keep = self._step(self.add)
        self.assertIn("rp-pdu-l", keep["distribution"]["pdus"])
        dist = rack["distribution"]
        self.assertNotIn("rp-pdu-l", dist["pdus"])
        self.assertEqual(self._bank_total(dist, "rp-pdu-r", "planned_power"), 1300.0)

    def test_non_step_projection_behaves_the_same(self):
        from ..projection import project_rack
        elev = project_rack(self.design, self.rack)
        dist = elev.power["distribution"]
        self.assertNotIn("rp-pdu-l", dist["pdus"])
        self.assertEqual(self._bank_total(dist, "rp-pdu-r", "planned_power"), 1300.0)

    def test_the_stamp_does_not_leak_onto_the_rack(self):
        from .. import rackinfo
        from ..projection import project_rack
        project_rack(self.design, self.rack)
        names = sorted(rackinfo.rack_devices(self.rack).values_list("name", flat=True))
        self.assertIn("rp-pdu-l", names)

    # -- unpowered vs redundancy_lost ---------------------------------------

    def test_single_path_device_is_unpowered_and_dual_path_only_redundancy_lost(self):
        problems = self._step(self.rem_pdu)["problems"]
        un = [p for p in problems if p["code"] == "unpowered"]
        lost = [p for p in problems if p["code"] == "redundancy_lost"]
        self.assertEqual(len(un), 1, problems)
        self.assertEqual(un[0]["severity"], "error")
        self.assertIn("rp-single", un[0]["detail"])
        self.assertNotIn("rp-dual", un[0]["detail"])
        self.assertEqual(len(lost), 1, problems)
        self.assertEqual(lost[0]["severity"], "warning")
        self.assertIn("rp-dual", lost[0]["detail"])
        self.assertIn("rp-pdu-l", lost[0]["detail"])
        self.assertNotIn("rp-single", lost[0]["detail"])

    def test_dual_path_device_alone_raises_no_unpowered(self):
        problems = self._step(self.rem_pdu, self.rem_single)["problems"]
        codes = [p["code"] for p in problems]
        self.assertNotIn("unpowered", codes)
        self.assertIn("redundancy_lost", codes)
