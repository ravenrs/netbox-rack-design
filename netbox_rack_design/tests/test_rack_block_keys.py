"""
T1.5c (PLAN-templates.md D31): the BROWSER must never identify a rack by its
bare pk. ``dcim.Rack`` and ``PlannedRack`` keep separate pk sequences (D28),
so pk 5 can mean two different racks -- ``inc/rack_block.html`` used to write
``id="rd-rack-<pk>"`` / ``data-rack-id="<pk>"`` straight off ``rack.pk``,
which collided the moment a design held a real rack and a planned rack that
happened to share a pk. The fix carries the namespaced ``rack_key()``
(models.py D27) into the DOM instead, translated to a colon-free spelling by
``templatetags.rack_design.rack_dom_id`` ("r-<pk>"/"p-<pk>").

This test forces the pk collision (the only way it is proven, not "by
luck" -- see test_editor_planned_rack_context.py's docstring for the same
reasoning) and asserts the two rendered blocks carry DISTINCT DOM ids and
``data-rack-id`` values.
"""

import re

from dcim.models import Location, Rack, Site
from django.urls import reverse
from utilities.testing import TestCase

from ..models import PlannedRack
from .utils import create_dcim_environment, make_design


class RackBlockKeysTest(TestCase):
    user_permissions = (
        "netbox_rack_design.view_design",
        "netbox_rack_design.change_design",
    )

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]

        cls.other_site = Site.objects.create(
            name="Site rack-block-keys collision", slug="site-rack-block-keys-collision",
        )
        cls.other_location = Location.objects.create(
            name="Loc rack-block-keys collision",
            slug="loc-rack-block-keys-collision",
            site=cls.other_site,
        )
        cls.real_rack = Rack.objects.create(name="Collide Real RBK", site=cls.other_site)

        # Force the pk collision (D28): PlannedRack has its OWN pk sequence,
        # so this explicit assignment is the only way pk N means both "Rack
        # N" and "PlannedRack N" -- not something that happens by chance.
        colliding_planned = PlannedRack(
            name="Collide Planned RBK", location=cls.other_location, u_height=10,
        )
        colliding_planned.pk = cls.real_rack.pk
        colliding_planned.save(force_insert=True)
        assert colliding_planned.pk == cls.real_rack.pk, (
            "setup did not actually force the pk collision this test needs"
        )
        cls.planned_rack = colliding_planned

        cls.design = make_design(
            title="Rack Block Keys Design", site=cls.other_site,
        )
        cls.design.racks.add(cls.real_rack)
        cls.design.planned_racks.add(cls.planned_rack)

    def _editor_url(self):
        return reverse(
            "plugins:netbox_rack_design:design_editor_default",
            kwargs={"pk": self.design.pk},
        )

    def test_dom_ids_distinct_for_colliding_pks(self):
        """A real and a planned rack sharing a pk must not share a DOM id.

        The scheme is deliberately ASYMMETRIC (see templatetags.rack_dom_id):
        a real rack keeps its BARE pk, only a planned rack is prefixed
        ``p-``. That is sufficient, because the collision D31 describes only
        ever happens BETWEEN the two kinds -- real pks are unique among real
        racks, planned pks among planned ones. Prefixing both sides was tried
        and broke all 125 editor e2e suites at once, which build selectors
        like ``"#nbx-rd-grid-front-" + rack_pk`` from the Python side.

        So this test asserts the INVARIANT (the two ids differ, and each
        appears exactly once), not a particular spelling.
        """
        # Belt-and-braces: the collision really is forced.
        self.assertEqual(self.planned_rack.pk, self.real_rack.pk)

        response = self.client.get(self._editor_url())
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()

        real_dom_id = f"rd-rack-{self.real_rack.pk}"
        planned_dom_id = f"rd-rack-p-{self.planned_rack.pk}"
        self.assertNotEqual(
            real_dom_id, planned_dom_id,
            "the whole point: two racks sharing a pk must not share a DOM id",
        )

        for dom_id in (real_dom_id, planned_dom_id):
            self.assertEqual(
                len(re.findall(r'id="' + re.escape(dom_id) + r'"', content)), 1,
                f"expected exactly one block id={dom_id!r} in the rendered editor",
            )

        # And the same on data-rack-id, which is what the JS actually reads.
        real_data = re.findall(
            r'data-rack-id="' + re.escape(str(self.real_rack.pk)) + r'"', content)
        planned_data = re.findall(
            r'data-rack-id="p-' + re.escape(str(self.planned_rack.pk)) + r'"', content)
        self.assertGreaterEqual(len(real_data), 1)
        self.assertGreaterEqual(len(planned_data), 1)
