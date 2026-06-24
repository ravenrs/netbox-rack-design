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

from ..choices import DesignPlacementKindChoices
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
