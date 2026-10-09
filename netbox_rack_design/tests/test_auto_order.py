"""
``steps.auto_order`` and ``POST .../designs/<pk>/auto-order/`` -- the
simulation-driven Auto-order (PLAN-execution-steps.md, Sec. "Auto-order").
"""

from core.models import ObjectChange
from dcim.choices import PowerFeedPhaseChoices
from dcim.models import (
    Cable,
    Device,
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

from .. import steps as steps_mod
from ..choices import DesignPlacementKindChoices as Kind
from ..models import DesignPlacement, DesignStep
from .test_simulate_steps import _cfg
from .utils import make_design


def _problems(design, order):
    """Every problem of every step of ``order`` (list of lists of pks)."""
    out = []
    for entry in steps_mod.simulate(design, order):
        for res in entry["racks"].values():
            out += res["problems"]
    return out


class _Fixture:
    @staticmethod
    def make_world(tag):
        site = Site.objects.create(name=f"AO {tag}", slug=f"ao-{tag}")
        mfr = Manufacturer.objects.create(name=f"AO Mfr {tag}", slug=f"ao-mfr-{tag}")
        role = DeviceRole.objects.create(name=f"AO Srv {tag}", slug=f"ao-srv-{tag}")
        pdu_role = DeviceRole.objects.create(name=f"AO PDU {tag}", slug="pdu")
        dt = DeviceType.objects.create(
            manufacturer=mfr, model=f"AO-Srv-{tag}", slug=f"ao-srv-t-{tag}",
            u_height=1, is_full_depth=False)
        PowerPortTemplate.objects.create(
            device_type=dt, name="PSU1", allocated_draw=1300, maximum_draw=1300)
        pdu_type = DeviceType.objects.create(
            manufacturer=mfr, model=f"AO-PDU-{tag}", slug=f"ao-pdu-t-{tag}", u_height=0)
        return site, role, pdu_role, dt, pdu_type


@override_settings(PLUGINS_CONFIG=_cfg(distribution_mode="builtin"))
class PduSwapShapeTest(_Fixture, TestCase):
    """Rack with two PDUs and two dual-PSU servers; the design removes the left
    PDU FIRST and then both servers."""

    @classmethod
    def setUpTestData(cls):
        site, role, pdu_role, dt, pdu_type = cls.make_world("swap")
        cls.rack = Rack.objects.create(name="AO Swap", site=site, u_height=10)
        panel = PowerPanel.objects.create(site=site, name="AO Swap Panel")

        def pdu(name, feed_name):
            feed = PowerFeed.objects.create(
                power_panel=panel, name=feed_name, voltage=230, amperage=32,
                phase=PowerFeedPhaseChoices.PHASE_SINGLE)
            dev = Device.objects.create(
                name=name, device_type=pdu_type, site=site, rack=cls.rack,
                role=pdu_role, status="active")
            outs = {n: PowerOutlet.objects.create(device=dev, name=n)
                    for n in ("1/1", "2/1")}
            Cable(a_terminations=[PowerPort.objects.create(device=dev, name="Input")],
                  b_terminations=[feed]).save()
            return dev, outs

        cls.pdu_l, out_l = pdu("ao-pdu-l", "AO Feed A")
        cls.pdu_r, out_r = pdu("ao-pdu-r", "AO Feed B")

        def server(name, pos, outlet_key):
            dev = Device.objects.create(
                name=name, device_type=dt, site=site, rack=cls.rack, position=pos,
                face="front", status="active", role=role)
            PowerPort.objects.filter(device=dev).delete()
            for psu, outs in (("PSU1", out_l), ("PSU2", out_r)):
                port = PowerPort.objects.create(
                    device=dev, name=psu, allocated_draw=400, maximum_draw=400)
                Cable(a_terminations=[port], b_terminations=[outs[outlet_key]]).save()
            return dev

        cls.s1 = server("ao-s1", 2, "1/1")
        cls.s2 = server("ao-s2", 4, "2/1")
        cls.design = make_design(title="AO swap", site=site)

        def rem(dev):
            return DesignPlacement.objects.create(
                design=cls.design, kind=Kind.KIND_REMOVE, device=dev)

        cls.rem_pdu = rem(cls.pdu_l)
        cls.rem_s1 = rem(cls.s1)
        cls.rem_s2 = rem(cls.s2)

    def test_pdu_removal_moves_after_the_servers(self):
        bad = [[self.rem_pdu.pk], [self.rem_s1.pk], [self.rem_s2.pk]]
        self.assertTrue(_problems(self.design, bad))  # redundancy_lost warnings
        out = steps_mod.auto_order(self.design, bad)
        self.assertEqual(out, [[self.rem_s1.pk], [self.rem_s2.pk], [self.rem_pdu.pk]])
        self.assertEqual(_problems(self.design, out), [])

    def test_already_clean_order_is_returned_unchanged(self):
        good = [[self.rem_s2.pk], [self.rem_s1.pk], [self.rem_pdu.pk]]
        self.assertEqual(steps_mod.auto_order(self.design, good), good)

    def test_steps_are_never_merged_and_unscheduled_join(self):
        # s2 is unscheduled; one two-action step stays a unit when clean.
        order = [[self.rem_pdu.pk], [self.rem_s1.pk]]
        out = steps_mod.auto_order(self.design, order)
        self.assertEqual(sorted(map(tuple, out)),
                         sorted([(self.rem_pdu.pk,), (self.rem_s1.pk,), (self.rem_s2.pk,)]))
        self.assertEqual(out[-1], [self.rem_pdu.pk])
        unit = [[self.rem_s1.pk, self.rem_s2.pk], [self.rem_pdu.pk]]
        self.assertEqual(steps_mod.auto_order(self.design, unit), unit)

    def test_cost_guard_falls_back_to_kind_order(self):
        bad = [[self.rem_pdu.pk], [self.rem_s1.pk], [self.rem_s2.pk]]
        out = steps_mod.auto_order(self.design, bad, max_evals=0)
        self.assertEqual(sorted(map(tuple, out)), sorted(map(tuple, bad)))


@override_settings(PLUGINS_CONFIG=_cfg())
class RebalanceShapeTest(_Fixture, TestCase):
    """Rack A (8000 W cap) holds 4000 + 2400; a 2500 W server from rack B moves
    in and the 2400 W one is removed. Moving first overloads A."""

    @classmethod
    def setUpTestData(cls):
        site, role, _pdu_role, dt, _pdu_type = cls.make_world("reb")
        cls.rack_a = Rack.objects.create(name="AO A", site=site, u_height=42)
        cls.rack_b = Rack.objects.create(name="AO B", site=site, u_height=42)
        cls.dt, cls.role, cls.site = dt, role, site

        def real(name, rack, pos, watts):
            dev = Device.objects.create(
                name=name, device_type=dt, site=site, rack=rack, position=pos,
                face="front", status="active", role=role)
            PowerPort.objects.update_or_create(
                device=dev, name="PSU1",
                defaults={"allocated_draw": watts, "maximum_draw": watts})
            return dev

        cls.base = real("ao-base", cls.rack_a, 1, 4000)
        cls.old = real("ao-old", cls.rack_a, 3, 2400)
        cls.mover = real("ao-mover", cls.rack_b, 1, 2500)
        cls.design = make_design(title="AO reb", site=site)
        cls.move = DesignPlacement.objects.create(
            design=cls.design, kind=Kind.KIND_MOVE, device=cls.mover,
            target_rack=cls.rack_a, target_position=10, target_face="front")
        cls.rem = DesignPlacement.objects.create(
            design=cls.design, kind=Kind.KIND_REMOVE, device=cls.old)

    def test_removal_comes_before_the_move(self):
        bad = [[self.move.pk], [self.rem.pk]]
        self.assertTrue([p for p in _problems(self.design, bad) if p["severity"] == "error"])
        out = steps_mod.auto_order(self.design, bad)
        self.assertEqual(out, [[self.rem.pk], [self.move.pk]])
        self.assertEqual(_problems(self.design, out), [])

    def test_a_step_that_stays_red_with_two_actions_is_split(self):
        def add(name, pos):
            return DesignPlacement.objects.create(
                design=self.design, kind=Kind.KIND_ADD, device_type=self.dt,
                target_rack=self.rack_a, target_position=pos, target_face="front",
                proposed_name=name)

        # Four 1300 W adds on top of 6500 W (after the unscheduled move + remove
        # join the plan) exceed 8000 W as one step, so it is split into singles.
        adds = [add(f"ao-x{i}", 20 + 2 * i) for i in range(4)]
        out = steps_mod.auto_order(self.design, [[a.pk for a in adds]])
        self.assertNotIn([a.pk for a in adds], out)
        for a in adds:
            self.assertIn([a.pk], out)


@override_settings(PLUGINS_CONFIG=_cfg())
class AutoOrderApiTest(APITestCase):
    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        site, role, _p, dt, _pt = _Fixture.make_world("api")
        rack = Rack.objects.create(name="AO API", site=site, u_height=42)
        dev = Device.objects.create(
            name="ao-api-dev", device_type=dt, site=site, rack=rack, position=1,
            face="front", status="active", role=role)
        cls.design = make_design(title="AO api", site=site)
        cls.add = DesignPlacement.objects.create(
            design=cls.design, kind=Kind.KIND_ADD, device_type=dt, target_rack=rack,
            target_position=10, target_face="front", proposed_name="ao-api-new")
        cls.rem = DesignPlacement.objects.create(
            design=cls.design, kind=Kind.KIND_REMOVE, device=dev)
        DesignStep.objects.create(design=cls.design, index=1, title="saved")

    def _url(self):
        return reverse("plugins-api:netbox_rack_design-api:design-auto-order",
                       kwargs={"pk": self.design.pk})

    def _post(self, steps):
        return self.client.post(self._url(), {"steps": steps}, format="json", **self.header)

    def test_returns_an_order_and_writes_nothing(self):
        self.add_permissions("netbox_rack_design.view_design")
        before = (DesignPlacement.objects.count(), DesignStep.objects.count(),
                  ObjectChange.objects.count())
        saved = list(DesignPlacement.objects.values_list("pk", "step_id", "step_order"))
        resp = self._post([[self.add.pk], [self.rem.pk]])
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        self.assertEqual(resp.data["steps"], [[self.add.pk], [self.rem.pk]])
        self.assertTrue(resp.data["titles_kept"])
        self.assertEqual(before, (DesignPlacement.objects.count(), DesignStep.objects.count(),
                                  ObjectChange.objects.count()))
        self.assertEqual(saved, list(
            DesignPlacement.objects.values_list("pk", "step_id", "step_order")))

    def test_read_only_token_may_call_it(self):
        self.add_permissions("netbox_rack_design.view_design")
        self.token.write_enabled = False
        self.token.save()
        self.assertHttpStatus(self._post([[self.add.pk]]), status.HTTP_200_OK)

    def test_user_without_permission_is_refused(self):
        self.assertHttpStatus(self._post([[self.add.pk]]), status.HTTP_403_FORBIDDEN)

    def test_foreign_placement_is_a_400(self):
        self.add_permissions("netbox_rack_design.view_design")
        self.assertHttpStatus(self._post([[999999]]), status.HTTP_400_BAD_REQUEST)

