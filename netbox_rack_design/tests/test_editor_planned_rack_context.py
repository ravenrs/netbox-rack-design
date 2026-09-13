"""
The editor's multi-rack context includes a design's PLANNED racks (T1.5,
PLAN-templates.md §1), keyed alongside its real ones with the namespaced
``rack_key()`` (D27): ``"r:<pk>"`` for a real ``dcim.Rack``, ``"p:<pk>"`` for
a ``PlannedRack``. Before this task, ``views._design_editor_context`` built
its rack list from ``design.racks`` only -- the SEPARATE
``design.planned_racks`` M2M was never read, so a planner had no way to see
(or create) a greenfield rack in the editor at all.

The second test below forces a real rack and a planned rack to share the
SAME integer pk (``PlannedRack`` and ``dcim.Rack`` keep separate pk
sequences -- D28/T1.4b) and asserts both still appear as two distinct
blocks. Without the forced collision this would pass "by luck": pk 5 could
mean two different racks and a naive `{pk: block}` dict (or any bare-pk
comparison) would silently drop one of them.
"""

from dcim.models import Location, Rack, Site
from django.urls import reverse
from utilities.testing import TestCase

from ..models import Design, PlannedRack
from .utils import create_dcim_environment


class DesignEditorPlannedRackContextTest(TestCase):
    user_permissions = (
        "netbox_rack_design.view_design",
        "netbox_rack_design.change_design",
    )

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.rack1 = env["racks"][0]

        cls.location = Location.objects.create(
            name="Loc 1", slug="loc-1", site=cls.site
        )

        cls.design = Design.objects.create(title="Planned Rack Editor Design", site=cls.site)
        cls.design.racks.set([cls.rack1])

        cls.planned_rack = PlannedRack.objects.create(
            name="Planned R1", location=cls.location, u_height=42,
        )
        cls.design.planned_racks.add(cls.planned_rack)

    def _editor_url(self, design):
        return reverse(
            "plugins:netbox_rack_design:design_editor_default",
            kwargs={"pk": design.pk},
        )

    def test_planned_rack_appears_alongside_real_rack(self):
        response = self.client.get(self._editor_url(self.design))
        self.assertEqual(response.status_code, 200)

        blocks = response.context["all_rack_blocks"]
        by_id = {b["rack_meta"]["id"]: b for b in blocks}

        real_key = f"r:{self.rack1.pk}"
        planned_key = f"p:{self.planned_rack.pk}"
        self.assertIn(real_key, by_id)
        self.assertIn(planned_key, by_id)

        self.assertFalse(by_id[real_key]["is_planned"])
        self.assertTrue(by_id[planned_key]["is_planned"])
        self.assertEqual(by_id[real_key]["rack"].pk, self.rack1.pk)
        self.assertEqual(by_id[planned_key]["rack"].pk, self.planned_rack.pk)

        # The "Planned" badge (inc/rack_block.html) renders only for the
        # planned block -- a planner must never confuse a rack that exists
        # with one that does not.
        content = response.content.decode()
        self.assertIn("Planned", content)

    def test_same_pk_real_and_planned_rack_both_appear_distinct(self):
        # Force the pk collision (D28/T1.4b): PlannedRack has its OWN pk
        # sequence, so this explicit assignment is the only way pk N ever
        # means both "Rack N" and "PlannedRack N" -- not something that
        # happens by chance.
        other_site = Site.objects.create(name="Site PK collision", slug="site-pk-collision-t15")
        other_location = Location.objects.create(
            name="Loc PK collision", slug="loc-pk-collision-t15", site=other_site,
        )
        real_rack = Rack.objects.create(name="Collide Real", site=other_site)
        colliding_planned = PlannedRack(
            name="Collide Planned", location=other_location, u_height=10,
        )
        colliding_planned.pk = real_rack.pk
        colliding_planned.save(force_insert=True)
        self.assertEqual(
            colliding_planned.pk, real_rack.pk,
            "setup did not actually force the pk collision this test needs",
        )

        design = Design.objects.create(title="PK Collision Editor Design", site=other_site)
        design.racks.add(real_rack)
        design.planned_racks.add(colliding_planned)

        response = self.client.get(self._editor_url(design))
        self.assertEqual(response.status_code, 200)

        blocks = response.context["all_rack_blocks"]
        self.assertEqual(len(blocks), 2)

        by_id = {b["rack_meta"]["id"]: b for b in blocks}
        real_key = f"r:{real_rack.pk}"
        planned_key = f"p:{colliding_planned.pk}"
        # The two namespaced keys must differ even though the bare pks match.
        self.assertNotEqual(real_key, planned_key)
        self.assertIn(real_key, by_id)
        self.assertIn(planned_key, by_id)
        self.assertFalse(by_id[real_key]["is_planned"])
        self.assertTrue(by_id[planned_key]["is_planned"])
