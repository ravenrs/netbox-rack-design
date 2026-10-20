"""
Security regression tests for GraphQL and global search (1.2.2).

NetBox core's GraphQL restricts LISTS and plain foreign keys to what the user
may view (``tenant { devices }``, ``device { tenant }`` -> null) but returns a
tree object's ``parent`` whole (``region { parent }``). The design fields follow
suit: ``children``/``depends_on`` and the design links of placements, feeds and
steps are restricted, the ``ancestors`` chain is whole. A planned feed's search
result names its design, like core's results name a device's tenant; and a deep
lineage must not turn one query into an unbounded amount of work.
"""

import json

from core.models import ObjectType
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from users.models import ObjectPermission
from utilities.testing import APITestCase

from .. import models
from ..choices import DesignStatusChoices
from ..models import Design, DesignPowerFeed
from .utils import create_dcim_environment, make_design

APPROVED = DesignStatusChoices.STATUS_APPROVED


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
class LineageVisibilityTest(GraphQLSecurityBase):

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

    def test_ancestors_return_the_whole_chain_like_a_core_tree_parent(self):
        data = self.ok(dq(self.d.pk, "ancestors { id title }"))
        self.assertEqual([a["title"] for a in data["design"]["ancestors"]],
                         ["Secret A", "Secret B", "Visible C"])
        self.assertEqual([int(a["id"]) for a in data["design"]["ancestors"]],
                         [self.a.pk, self.b.pk, self.c.pk])

    def test_ancestors_of_a_design_whose_parent_is_hidden(self):
        data = self.ok(dq(self.c.pk, "based_on { title } ancestors { title }"))
        self.assertIsNone(data["design"]["based_on"])  # a plain FK, like device { tenant }
        self.assertEqual([a["title"] for a in data["design"]["ancestors"]],
                         ["Secret A", "Secret B"])

    def test_foreign_keys_on_returned_objects_do_not_reach_hidden_designs(self):
        # children { based_on / root } and a based_on walk from a visible design
        # resolve a hidden link null.
        for version, design in enumerate([self.c, self.d, self.visible_child], start=2):
            Design.objects.filter(pk=design.pk).update(root=self.a, version=version)
        raw = json.dumps(self.ok(
            dq(self.d.pk, "children { root { title description } based_on { title } }")))
        self.assertNotIn("Secret", raw)
        raw = json.dumps(self.ok(
            dq(self.c.pk, "based_on { title based_on { title description sites { name } } }")))
        self.assertNotIn("Secret", raw)

    def test_direct_links_to_viewable_designs_still_resolve(self):
        self.d.depends_on.add(self.c)
        self.d.depends_on.add(self.a)
        # a viewable root resolves (the hidden-root case is the next test but one)
        Design.objects.filter(pk=self.d.pk).update(root=self.c, version=2)
        data = self.ok(
            dq(self.d.pk, "based_on { title } root { title } depends_on { title }"))
        self.assertEqual(data["design"]["based_on"]["title"], "Visible C")
        self.assertEqual(data["design"]["root"]["title"], "Visible C")
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
        # The placement also binds a feed and sits in a step, so the FKs whose
        # `design` must come back null are themselves non-null (not a vacuous null).
        models.DesignPlacement.objects.filter(pk=placement.pk).update(
            planned_power_feed=feed, step=step)
        raw = json.dumps(self.ok(
            f"{{ design_placement(id: {placement.pk}) {{ design {{ title }} "
            f"planned_power_feed {{ id design {{ title }} }} step {{ id design {{ title }} }} }} "
            f"planned_power_feed(id: {feed.pk}) {{ design {{ title }} }} "
            f"design_step(id: {step.pk}) {{ design {{ title }} }} }}"))
        self.assertNotIn("Secret", raw)
        data = json.loads(raw)
        self.assertIsNone(data["design_placement"]["design"])
        self.assertEqual(int(data["design_placement"]["planned_power_feed"]["id"]), feed.pk)
        self.assertIsNone(data["design_placement"]["planned_power_feed"]["design"])
        self.assertEqual(int(data["design_placement"]["step"]["id"]), step.pk)
        self.assertIsNone(data["design_placement"]["step"]["design"])
        self.assertIsNone(data["planned_power_feed"]["design"])
        self.assertIsNone(data["design_step"]["design"])


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

    def test_a_branching_tree_costs_one_query_per_node_returned(self):
        # 1 + 3 + 9 designs: the cost is bounded by the nodes the response holds,
        # never by anything unreturned.
        root = make_design(title="Root", site=self.site, status=APPROVED)
        for i in range(3):
            mid = make_design(title=f"Mid {i}", site=self.site, status=APPROVED, based_on=root)
            for j in range(3):
                make_design(title=f"Leaf {i}{j}", site=self.site, based_on=mid)
        with CaptureQueriesContext(connection) as ctx:
            data = self.ok(dq(root.pk, "children { id children { id children { id } } }"))
        mids = data["design"]["children"]
        self.assertEqual(len(mids), 3)
        self.assertEqual(sum(len(m["children"]) for m in mids), 9)
        parents = 1 + 3 + 9  # every returned node resolves its own `children`
        self.assertLessEqual(len(ctx), 2 * parents + 20)

    def test_ancestors_cost_does_not_grow_with_the_list(self):
        with CaptureQueriesContext(connection) as ctx:
            data = self.ok("{ design_list { id ancestors { id } } }")
        self.assertGreaterEqual(len(data["design_list"]), self.DEPTH)
        # one shared (pk, based_on) query + one fetch per design: never a query
        # per hop per design (~66 for this chain alone).
        self.assertLessEqual(len(ctx), 3 * len(data["design_list"]) + 15)

    def test_design_links_cost_one_visibility_query_per_request(self):
        from ..models import DesignPlacement

        self.grant(DesignPlacement, ["view"])
        for position in range(1, 13):
            DesignPlacement.objects.create(
                design=self.chain[position % 3], kind="add", target_rack=self.racks[0],
                target_position=position, target_face="front")
        with CaptureQueriesContext(connection) as ctx:
            data = self.ok("{ design_placement_list { id design { id } } }")
        self.assertEqual(len(data["design_placement_list"]), 12)  # the rows being priced
        self.assertTrue(all(row["design"] for row in data["design_placement_list"]))
        # "which designs may this user view": one pk-only query, however many links
        visibility = [q for q in ctx.captured_queries
                      if q["sql"].startswith('SELECT "netbox_rack_design_design"."id" AS "pk" FROM')]
        self.assertEqual(len(visibility), 1, [q["sql"][:120] for q in visibility])

    def test_ancestors_walk_is_bounded_by_the_depth_cap(self):
        with CaptureQueriesContext(connection) as ctx:
            data = self.ok(dq(self.chain[-1].pk, "ancestors { id }"))
        self.assertEqual(len(data["design"]["ancestors"]), self.DEPTH - 1)
        self.assertLessEqual(len(ctx), 4 * self.DEPTH + 20)

    def test_a_lineage_deeper_than_the_cap_degrades_to_empty(self):
        original = models.MAX_LINEAGE_DEPTH
        models.MAX_LINEAGE_DEPTH = 3
        try:
            data = self.ok(dq(self.chain[-1].pk, "ancestors { id }"))
        finally:
            models.MAX_LINEAGE_DEPTH = original
        self.assertEqual(data["design"]["ancestors"], [])

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
        # the capped list is the lowest pks, not an arbitrary LIMIT subset
        ids = [int(c["id"]) for c in data["design"]["children"]]
        siblings = sorted(Design.objects.filter(based_on=parent).values_list("pk", flat=True))
        self.assertEqual(ids, siblings[:2])

    def test_depends_on_list_is_capped_in_pk_order(self):
        owner = make_design(title="Owner", site=self.site)
        targets = [make_design(title=f"Dep {i}", site=self.site) for i in range(4)]
        owner.depends_on.set(targets)
        from ..graphql import types as gql_types
        previous_cap = gql_types.MAX_CHILDREN
        gql_types.MAX_CHILDREN = 2
        try:
            data = self.ok(dq(owner.pk, "depends_on { id }"))
        finally:
            gql_types.MAX_CHILDREN = previous_cap
        self.assertEqual([int(d["id"]) for d in data["design"]["depends_on"]],
                         sorted(t.pk for t in targets)[:2])
