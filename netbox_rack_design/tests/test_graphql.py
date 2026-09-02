"""
GraphQL tests for NetBox Rack Design.

The full GraphQL get/list/permission matrix is already inherited via
``APIViewTestCases.APIViewTestCase`` in test_api.py (it mixes in
``GraphQLTestCase``). These tests add a couple of focused, end-to-end checks
against the plugin's actual snake_case query names through the unified
``/graphql`` endpoint.
"""

import json

from django.test import override_settings
from django.urls import reverse
from utilities.testing import APITestCase

from ..choices import DesignPlacementKindChoices, DesignStatusChoices
from ..models import Design, DesignGroup, DesignPlacement
from .utils import create_dcim_environment


class RackDesignGraphQLTestCase(APITestCase):

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        site = env["site"]

        cls.group = DesignGroup.objects.create(name="Group 1")
        cls.design = Design.objects.create(title="Design 1", site=site, group=cls.group)
        cls.placement = DesignPlacement.objects.create(
            design=cls.design,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=env["device_type"],
            target_rack=env["racks"][1],
            target_position=1,
        )

    def _query(self, query):
        url = reverse("graphql")
        return self.client.post(
            url, data={"query": query}, format="json", **self.header
        )

    @override_settings(LOGIN_REQUIRED=True)
    def test_query_design_list(self):
        self.add_permissions("netbox_rack_design.view_design")
        query = "query { design_list { id title } }"
        response = self._query(query)
        self.assertHttpStatus(response, 200)
        data = json.loads(response.content)
        self.assertNotIn("errors", data)
        self.assertEqual(len(data["data"]["design_list"]), 1)
        self.assertEqual(data["data"]["design_list"][0]["title"], "Design 1")

    @override_settings(LOGIN_REQUIRED=True)
    def test_query_design_group_list(self):
        self.add_permissions("netbox_rack_design.view_designgroup")
        query = "query { design_group_list { id name } }"
        response = self._query(query)
        self.assertHttpStatus(response, 200)
        data = json.loads(response.content)
        self.assertNotIn("errors", data)
        self.assertEqual(len(data["data"]["design_group_list"]), 1)

    @override_settings(LOGIN_REQUIRED=True)
    def test_query_design_placement_list(self):
        self.add_permissions("netbox_rack_design.view_designplacement")
        query = "query { design_placement_list { id kind } }"
        response = self._query(query)
        self.assertHttpStatus(response, 200)
        data = json.loads(response.content)
        self.assertNotIn("errors", data)
        self.assertEqual(len(data["data"]["design_placement_list"]), 1)


class DesignChainGraphQLTestCase(APITestCase):
    """
    GraphQL surface for design chains (PLAN-design-chains.md G9): a client
    must be able to traverse ``based_on`` lineage -- immediate parent,
    ordered ancestor chain, children, and frozen state -- and see the
    placement-level chain references, all in one round trip through the
    unified ``/graphql`` endpoint.
    """

    @classmethod
    def setUpTestData(cls):
        env = create_dcim_environment()
        cls.site = env["site"]
        cls.device_type = env["device_type"]
        cls.racks = env["racks"]

        # A -> B -> C, oldest first. A and B are approved (frozen, so each
        # may be a parent); C stays draft.
        cls.design_a = Design.objects.create(
            title="A", site=cls.site, status=DesignStatusChoices.STATUS_APPROVED
        )
        cls.design_b = Design.objects.create(
            title="B", site=cls.site, based_on=cls.design_a,
            status=DesignStatusChoices.STATUS_APPROVED,
        )
        cls.design_c = Design.objects.create(
            title="C", site=cls.site, based_on=cls.design_b,
        )

        cls.upstream_add = DesignPlacement.objects.create(
            design=cls.design_a,
            kind=DesignPlacementKindChoices.KIND_ADD,
            device_type=cls.device_type,
            target_rack=cls.racks[0],
            target_position=5,
            proposed_name="upstream-node",
        )

    def _query(self, query):
        url = reverse("graphql")
        return self.client.post(
            url, data={"query": query}, format="json", **self.header
        )

    @override_settings(LOGIN_REQUIRED=True)
    def test_design_parent_via_based_on(self):
        self.add_permissions("netbox_rack_design.view_design")
        query = f"""
        query {{
            design(id: {self.design_b.pk}) {{
                title
                based_on {{ id title }}
            }}
        }}
        """
        response = self._query(query)
        self.assertHttpStatus(response, 200)
        data = json.loads(response.content)
        self.assertNotIn("errors", data)
        self.assertEqual(data["data"]["design"]["based_on"]["title"], "A")

    @override_settings(LOGIN_REQUIRED=True)
    def test_design_with_no_parent_has_null_based_on_and_empty_ancestors(self):
        self.add_permissions("netbox_rack_design.view_design")
        query = f"""
        query {{
            design(id: {self.design_a.pk}) {{
                based_on {{ id }}
                ancestors {{ id }}
                is_frozen
            }}
        }}
        """
        response = self._query(query)
        self.assertHttpStatus(response, 200)
        data = json.loads(response.content)
        self.assertNotIn("errors", data)
        design_data = data["data"]["design"]
        self.assertIsNone(design_data["based_on"])
        self.assertEqual(design_data["ancestors"], [])
        self.assertTrue(design_data["is_frozen"])

    @override_settings(LOGIN_REQUIRED=True)
    def test_design_is_frozen(self):
        self.add_permissions("netbox_rack_design.view_design")
        query = f"""
        query {{
            design(id: {self.design_c.pk}) {{ is_frozen }}
        }}
        """
        response = self._query(query)
        self.assertHttpStatus(response, 200)
        data = json.loads(response.content)
        self.assertNotIn("errors", data)
        self.assertFalse(data["data"]["design"]["is_frozen"])

    @override_settings(LOGIN_REQUIRED=True)
    def test_design_children(self):
        self.add_permissions("netbox_rack_design.view_design")
        query = f"""
        query {{
            design(id: {self.design_a.pk}) {{
                children {{ id title }}
            }}
        }}
        """
        response = self._query(query)
        self.assertHttpStatus(response, 200)
        data = json.loads(response.content)
        self.assertNotIn("errors", data)
        children = data["data"]["design"]["children"]
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["title"], "B")

    @override_settings(LOGIN_REQUIRED=True)
    def test_design_ancestor_chain_three_deep_in_one_query(self):
        self.add_permissions("netbox_rack_design.view_design")
        query = f"""
        query {{
            design(id: {self.design_c.pk}) {{
                title
                ancestors {{ id title }}
            }}
        }}
        """
        response = self._query(query)
        self.assertHttpStatus(response, 200)
        data = json.loads(response.content)
        self.assertNotIn("errors", data)
        ancestors = data["data"]["design"]["ancestors"]
        self.assertEqual([a["title"] for a in ancestors], ["A", "B"])

    @override_settings(LOGIN_REQUIRED=True)
    def test_malformed_lineage_does_not_500(self):
        """A cycle in ``based_on`` (bypassing ``clean()``, as a pre-existing
        row could already be) must degrade ``ancestors`` to an empty list,
        never raise through to a 500."""
        self.add_permissions("netbox_rack_design.view_design")
        Design.objects.filter(pk=self.design_a.pk).update(based_on=self.design_c)
        query = f"""
        query {{
            design(id: {self.design_a.pk}) {{
                ancestors {{ id }}
            }}
        }}
        """
        response = self._query(query)
        self.assertHttpStatus(response, 200)
        data = json.loads(response.content)
        self.assertNotIn("errors", data)
        self.assertEqual(data["data"]["design"]["ancestors"], [])

    @override_settings(LOGIN_REQUIRED=True)
    def test_placement_base_placement_and_stale(self):
        self.add_permissions(
            "netbox_rack_design.view_designplacement", "netbox_rack_design.view_design"
        )
        move = DesignPlacement.objects.create(
            design=self.design_b,
            kind=DesignPlacementKindChoices.KIND_MOVE,
            base_placement=self.upstream_add,
            target_rack=self.racks[1],
            target_position=10,
        )
        query = f"""
        query {{
            design_placement(id: {move.pk}) {{
                id
                base_placement {{ id proposed_name }}
                base_parent_placement {{ id }}
                stale
                stale_device_name
            }}
        }}
        """
        response = self._query(query)
        self.assertHttpStatus(response, 200)
        data = json.loads(response.content)
        self.assertNotIn("errors", data)
        placement_data = data["data"]["design_placement"]
        self.assertEqual(placement_data["base_placement"]["proposed_name"], "upstream-node")
        self.assertIsNone(placement_data["base_parent_placement"])
        self.assertFalse(placement_data["stale"])
        self.assertEqual(placement_data["stale_device_name"], "")
