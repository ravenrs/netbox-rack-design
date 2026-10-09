"""
save-layout creates NEW placements in the order the user performed the
actions (``action_seq``), not in payload (rack/face) order --
PLAN-execution-steps.md E6.
"""

from django.urls import reverse
from rest_framework import status
from utilities.testing import APITestCase

from ..models import DesignPlacement
from ..views import _plan_action_rows
from .utils import create_dcim_environment, make_design


class SaveLayoutActionSeqTest(APITestCase):
    view_namespace = "plugins-api:netbox_rack_design"

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]
        cls.devices = env["devices"]
        cls.device_type = env["device_type"]
        cls.design = make_design(title="Seq design", site=cls.site)

    def _post(self, racks):
        self.add_permissions(
            "netbox_rack_design.change_design",
            "netbox_rack_design.add_designplacement",
            "netbox_rack_design.change_designplacement",
            "netbox_rack_design.delete_designplacement",
        )
        url = reverse(
            "plugins-api:netbox_rack_design-api:design-save-layout",
            kwargs={"pk": self.design.pk},
        )
        return self.client.post(
            url, {"design_id": self.design.pk, "racks": racks}, format="json", **self.header)

    def _add(self, u, face, seq):
        return {"kind": "add", "device_type_id": self.device_type.pk,
                "u_position": u, "face": face, "action_seq": seq}

    def test_created_order_follows_action_seq_across_racks_and_faces(self):
        r1, r2 = self.racks
        d1, d2 = self.devices
        # User order: 1 move D1 -> rack 2, 2 add (rack 1 rear), 3 remove D2,
        # 4 add (rack 2 rear), 5 add (rack 1 rear). Payload order is scrambled.
        resp = self._post([
            {"rack_id": r1.pk,
             "front": [{"kind": "remove", "device_id": d2.pk, "action_seq": 3}],
             "rear": [self._add(20, "rear", 5), self._add(10, "rear", 2)]},
            {"rack_id": r2.pk,
             "rear": [self._add(30, "rear", 4)],
             "front": [{"kind": "move", "device_id": d1.pk, "u_position": 5,
                        "face": "front", "action_seq": 1}]},
        ])
        self.assertHttpStatus(resp, status.HTTP_200_OK)

        by_seq = {}
        for p in DesignPlacement.objects.filter(design=self.design):
            key = (p.kind, p.device_id, p.target_rack_id,
                   int(p.target_position) if p.target_position is not None else None)
            by_seq[key] = p
        expected = [
            ("move", d1.pk, r2.pk, 5),
            ("add", None, r1.pk, 10),
            ("remove", d2.pk, None, None),
            ("add", None, r2.pk, 30),
            ("add", None, r1.pk, 20),
        ]
        ordered = sorted(by_seq.values(), key=lambda p: (p.created, p.pk))
        got = [(p.kind, p.device_id, p.target_rack_id,
                int(p.target_position) if p.target_position is not None else None)
               for p in ordered]
        self.assertEqual(got, expected)
        self.assertEqual(len({p.created for p in ordered}), len(ordered))

        rows, _ = _plan_action_rows(self.design)
        seq_to_id = {r["seq"]: r["id"] for r in rows}
        self.assertEqual([seq_to_id[i] for i in range(1, 6)], [p.pk for p in ordered])

    def test_items_without_seq_follow_those_with_seq(self):
        r1, _ = self.racks
        resp = self._post([
            {"rack_id": r1.pk, "rear": [
                {"kind": "add", "device_type_id": self.device_type.pk,
                 "u_position": 10, "face": "rear"},
                self._add(20, "rear", 1)]},
        ])
        self.assertHttpStatus(resp, status.HTTP_200_OK)
        ordered = sorted(DesignPlacement.objects.filter(design=self.design),
                         key=lambda p: (p.created, p.pk))
        self.assertEqual([int(p.target_position) for p in ordered], [20, 10])
