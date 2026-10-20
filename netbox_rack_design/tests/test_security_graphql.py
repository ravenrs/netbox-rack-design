"""
Security regression tests for GraphQL and global search (1.2.2).

A user constrained to a subset of designs must not learn about the others
through the lineage fields (``children``, ``ancestors``, ``lineage``), through
foreign keys on objects those fields return, or through a search result; and a
deep lineage must not turn one query into an unbounded amount of work.
"""

import json

from core.models import ObjectType
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from users.models import ObjectPermission
from utilities.testing import APITestCase

from .. import models, projection
from ..choices import DesignStatusChoices
from ..models import Design, DesignPowerFeed
from ..search import DesignPowerFeedIndex
from .utils import create_dcim_environment, make_design

APPROVED = DesignStatusChoices.STATUS_APPROVED
RESTRICTED = projection.RESTRICTED_DESIGN_LABEL


def dq(pk, body):
    """A ``design(id: pk) { body }`` query."""
    return f"{{ design(id: {pk}) {{ {body} }} }}"


class GraphQLSecurityBase(APITestCase):

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.racks = env["racks"]

    def grant(self, model, actions, constraints=None):
        permission = ObjectPermission(
            name=f"sec-gql-{model.__name__}-{ObjectPermission.objects.count()}",
            actions=list(actions), constraints=constraints,
        )
        permission.save()
        permission.users.add(self.user)
        permission.object_types.add(ObjectType.objects.get_for_model(model))
        return permission

    def gql(self, query):
        response = self.client.post(
            reverse("graphql"), data={"query": query}, format="json", **self.header)
        self.assertHttpStatus(response, 200)
        return json.loads(response.content)

    def ok(self, query):
        data = self.gql(query)
        self.assertNotIn("errors", data, data.get("errors"))
        return data["data"]


@override_settings(LOGIN_REQUIRED=True)
class LineageRedactionTest(GraphQLSecurityBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        make = lambda title, **kw: make_design(title=title, site=cls.site, **kw)  # noqa: E731
        cls.a = make("Secret A", status=APPROVED)
        cls.b = make("Secret B", status=APPROVED, based_on=cls.a)
        cls.c = make("Visible C", status=APPROVED, based_on=cls.b)
        cls.d = make("Visible D", based_on=cls.c)
        cls.hidden_child = make("Secret Child", based_on=cls.d)
        cls.visible_child = make("Visible Child", based_on=cls.d)

    def setUp(self):
        super().setUp()
        visible = [self.c.pk, self.d.pk, self.visible_child.pk]
        self.grant(Design, ["view"], {"pk__in": visible})

    def test_hidden_children_are_dropped(self):
        data = self.ok(dq(self.d.pk, "children { id title }"))
        self.assertEqual([c["title"] for c in data["design"]["children"]], ["Visible Child"])

    def test_lineage_keeps_the_chain_length_with_hidden_entries_redacted(self):
        raw = json.dumps(self.ok(
            dq(self.d.pk, "lineage { id title version status restricted design { id title } }")))
        lineage = json.loads(raw)["design"]["lineage"]
        self.assertEqual(len(lineage), 3)
        for entry in lineage[:2]:
            self.assertTrue(entry["restricted"])
            self.assertIsNone(entry["id"])
            self.assertIsNone(entry["design"])
            self.assertEqual(entry["title"], RESTRICTED)
        self.assertFalse(lineage[2]["restricted"])
        self.assertEqual(lineage[2]["title"], "Visible C")
        self.assertEqual(lineage[2]["design"]["title"], "Visible C")
        self.assertNotIn("Secret", raw)

    def test_ancestors_keep_the_chain_length_with_null_for_hidden_ones(self):
        raw = json.dumps(self.ok(dq(self.d.pk, "ancestors { id title sites { name } }")))
        ancestors = json.loads(raw)["design"]["ancestors"]
        self.assertEqual(len(ancestors), 3)
        self.assertEqual(ancestors[:2], [None, None])
        self.assertEqual(ancestors[2]["title"], "Visible C")
        self.assertNotIn("Secret", raw)
        self.assertNotIn(str(self.a.pk), json.dumps([a["id"] for a in ancestors if a]))

    def test_a_viewer_of_everything_gets_the_full_chain(self):
        self.grant(Design, ["view"])
        data = self.ok(dq(self.d.pk, "ancestors { title } lineage { title restricted }"))
        self.assertEqual([a["title"] for a in data["design"]["ancestors"]],
                         ["Secret A", "Secret B", "Visible C"])
        self.assertEqual([e["title"] for e in data["design"]["lineage"]],
                         ["Secret A", "Secret B", "Visible C"])
        self.assertFalse(any(e["restricted"] for e in data["design"]["lineage"]))

    def test_foreign_keys_on_returned_objects_do_not_reach_hidden_designs(self):
        # children { based_on / root } and a based_on walk from a visible design
        # must not hand out a hidden design's fields or relations.
        for version, design in enumerate([self.c, self.d, self.visible_child], start=2):
            Design.objects.filter(pk=design.pk).update(root=self.a, version=version)
        raw = json.dumps(self.ok(
            dq(self.d.pk, "children { root { title description } based_on { title } }")))
        self.assertNotIn("Secret", raw)
        raw = json.dumps(self.ok(
            dq(self.c.pk, "based_on { title based_on { title description sites { name } } }")))
        self.assertNotIn("Secret", raw)
        raw = json.dumps(self.ok(
            dq(self.d.pk, "lineage { design { based_on { title } root { title } } }")))
        self.assertNotIn("Secret", raw)

    def test_direct_links_to_viewable_designs_still_resolve(self):
        self.d.depends_on.add(self.c)
        self.d.depends_on.add(self.a)
        data = self.ok(
            dq(self.d.pk, "based_on { title } root { title } depends_on { title }"))
        self.assertEqual(data["design"]["based_on"]["title"], "Visible C")
        self.assertIsNone(data["design"]["root"])
        self.assertEqual([x["title"] for x in data["design"]["depends_on"]], ["Visible C"])

    def test_a_hidden_direct_link_resolves_null(self):
        data = self.ok(dq(self.c.pk, "based_on { title }"))
        self.assertIsNone(data["design"]["based_on"])

    def test_placement_feed_and_step_design_fk_is_restricted(self):
        from ..models import DesignPlacement, DesignStep
        feed = DesignPowerFeed.objects.create(design=self.a, rack=self.racks[0], name="Feed A")
        step = DesignStep.objects.create(design=self.a, index=1, title="Step A")
        placement = DesignPlacement.objects.create(
            design=self.a, kind="add", target_rack=self.racks[0], target_position=1)
        self.grant(models.DesignPlacement, ["view"])
        self.grant(DesignPowerFeed, ["view"])
        self.grant(DesignStep, ["view"])
        raw = json.dumps(self.ok(
            f"{{ design_placement(id: {placement.pk}) {{ design {{ title }} }} "
            f"planned_power_feed(id: {feed.pk}) {{ design {{ title }} }} "
            f"design_step(id: {step.pk}) {{ design {{ title }} }} }}"))
        self.assertNotIn("Secret", raw)
        self.assertEqual(json.loads(raw)["design_placement"]["design"], None)


@override_settings(LOGIN_REQUIRED=True)
class LineageBoundTest(GraphQLSecurityBase):

    DEPTH = 12

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        previous = None
        cls.chain = []
        for i in range(cls.DEPTH):
            previous = make_design(
                title=f"Link {i}", site=cls.site, status=APPROVED, based_on=previous)
            cls.chain.append(previous)

    def setUp(self):
        super().setUp()
        self.grant(Design, ["view"])

    def _count(self, query):
        with CaptureQueriesContext(connection) as ctx:
            self.ok(query)
        return len(ctx)

    def test_deeply_nested_children_stay_linear_in_depth(self):
        def nested(depth):
            inner = "title"
            for _ in range(depth):
                inner = f"title children {{ {inner} }}"
            return dq(self.chain[0].pk, inner)

        shallow, deep = self._count(nested(3)), self._count(nested(self.DEPTH - 1))
        # one queryset per level (plus auth/permission overhead) -- never per-row
        # multiplication of the whole tree.
        self.assertLessEqual(deep - shallow, 3 * (self.DEPTH - 4))

    def test_ancestors_and_lineage_walk_is_bounded_by_the_depth_cap(self):
        with CaptureQueriesContext(connection) as ctx:
            data = self.ok(dq(self.chain[-1].pk, "ancestors { id } lineage { id }"))
        self.assertEqual(len(data["design"]["ancestors"]), self.DEPTH - 1)
        self.assertLessEqual(len(ctx), 4 * self.DEPTH + 20)

    def test_a_lineage_deeper_than_the_cap_degrades_to_empty(self):
        original = models.MAX_LINEAGE_DEPTH
        models.MAX_LINEAGE_DEPTH = 3
        try:
            data = self.ok(dq(self.chain[-1].pk, "ancestors { id } lineage { id }"))
        finally:
            models.MAX_LINEAGE_DEPTH = original
        self.assertEqual(data["design"]["ancestors"], [])
        self.assertEqual(data["design"]["lineage"], [])

    def test_children_list_is_capped(self):
        parent = self.chain[0]
        from ..graphql import types as gql_types
        previous_cap = gql_types.MAX_CHILDREN
        gql_types.MAX_CHILDREN = 2
        try:
            for i in range(4):
                make_design(title=f"Sibling {i}", site=self.site, based_on=parent)
            data = self.ok(dq(parent.pk, "children { id }"))
        finally:
            gql_types.MAX_CHILDREN = previous_cap
        self.assertEqual(len(data["design"]["children"]), 2)


class SearchDisclosureTest(GraphQLSecurityBase):

    def test_power_feed_search_result_does_not_carry_the_design_title(self):
        self.assertNotIn("design", DesignPowerFeedIndex.display_attrs)
        design = make_design(title="Secret Feed Design", site=self.site)
        feed = DesignPowerFeed.objects.create(design=design, rack=self.racks[0], name="Feed X")
        from extras.models import CachedValue
        self.assertFalse(
            CachedValue.objects.filter(
                object_type=ObjectType.objects.get_for_model(DesignPowerFeed), object_id=feed.pk,
                value__icontains="Secret Feed Design").exists())
