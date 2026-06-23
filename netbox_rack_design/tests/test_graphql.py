"""
Test cases for NetBox Rack Design GraphQL API.
"""
from ..models import Rackdesign
from ..testing import PluginGraphQLTestCase


class RackdesignGraphQLTestCase(PluginGraphQLTestCase):
    """Test Rackdesign GraphQL queries."""

    @classmethod
    def setUpTestData(cls):
        """Set up test data for all tests."""
        Rackdesign.objects.create(name='GraphQL Test 1')
        Rackdesign.objects.create(name='GraphQL Test 2')
        Rackdesign.objects.create(name='GraphQL Test 3')

    def test_query_rackdesign(self):
        """Test GraphQL query for a single Rackdesign."""
        self.add_permissions('netbox_rack_design.view_rackdesign')

        instance = Rackdesign.objects.first()

        query = (
            "query { "
            "rackdesign(id: " + str(instance.pk) + ") { "
            "id name "
            "} "
            "}"
        )

        response = self.execute_query(query)
        self.assertIsNone(response.get('errors'))

        data = response['data']['rackdesign']
        self.assertEqual(data['id'], str(instance.pk))
        self.assertEqual(data['name'], instance.name)

    def test_query_rackdesign_list(self):
        """Test GraphQL query for list of Rackdesigns."""
        self.add_permissions('netbox_rack_design.view_rackdesign')

        query = """
        query {
            rackdesign_list {
                id
                name
            }
        }
        """

        response = self.execute_query(query)
        self.assertIsNone(response.get('errors'))

        data = response['data']['rackdesign_list']
        self.assertEqual(len(data), 3)
        self.assertIn('id', data[0])
        self.assertIn('name', data[0])

    def test_query_rackdesign_with_all_fields(self):
        """Test GraphQL query with all available fields."""
        self.add_permissions('netbox_rack_design.view_rackdesign')

        instance = Rackdesign.objects.first()

        query = (
            "query { "
            "rackdesign(id: " + str(instance.pk) + ") { "
            "id name created last_updated "
            "} "
            "}"
        )

        response = self.execute_query(query)
        self.assertIsNone(response.get('errors'))

        data = response['data']['rackdesign']
        self.assertEqual(data['id'], str(instance.pk))
        self.assertEqual(data['name'], instance.name)
        self.assertIsNotNone(data['created'])
        self.assertIsNotNone(data['last_updated'])

