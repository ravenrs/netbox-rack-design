"""
Test cases for NetBox Rack Design models.
"""

from django.core.exceptions import ValidationError

from ..models import Rackdesign
from ..testing import PluginModelTestCase
from ..testing.utils import create_tags, get_random_string


class RackdesignTestCase(PluginModelTestCase):
    """Test Rackdesign model."""

    @classmethod
    def setUpTestData(cls):
        """Set up test data for all tests."""
        # Create test instances
        Rackdesign.objects.create(name='Test 1')
        Rackdesign.objects.create(name='Test 2')
        Rackdesign.objects.create(name='Test 3')

    def test_create_rackdesign(self):
        """Test creating a Rackdesign instance."""
        name = f'Test {get_random_string(10)}'
        instance = Rackdesign.objects.create(name=name)

        self.assertEqual(instance.name, name)
        self.assertIsNotNone(instance.pk)

    def test_rackdesign_str(self):
        """Test Rackdesign string representation."""
        instance = Rackdesign.objects.first()
        self.assertEqual(str(instance), instance.name)

    def test_rackdesign_absolute_url(self):
        """Test Rackdesign get_absolute_url method."""
        instance = Rackdesign.objects.first()
        url = instance.get_absolute_url()

        self.assertIsNotNone(url)
        self.assertIn(str(instance.pk), url)

    def test_rackdesign_unique_name(self):
        """Test that Rackdesign names must be unique."""
        name = 'Duplicate Name'
        Rackdesign.objects.create(name=name)

        with self.assertRaises(ValidationError):
            instance = Rackdesign(name=name)
            instance.full_clean()

    def test_model_to_dict(self):
        """Test model_to_dict helper method."""
        instance = Rackdesign.objects.first()
        data = self.model_to_dict(instance)

        self.assertIn('name', data)
        self.assertEqual(data['name'], instance.name)
        self.assertIn('id', data)

    def test_instance_equal(self):
        """Test assertInstanceEqual helper method."""
        instance = Rackdesign.objects.first()

        # Should pass with matching data
        self.assertInstanceEqual(
            instance,
            {'name': instance.name, 'id': instance.pk}
        )

    def test_rackdesign_with_tags(self):
        """Test Rackdesign with tags."""
        tags = create_tags(['important', 'test'])
        instance = Rackdesign.objects.first()

        instance.tags.add(*tags)
        instance.save()

        self.assertEqual(instance.tags.count(), 2)
        self.assertIn(tags[0], instance.tags.all())

    def test_bulk_create(self):
        """Test bulk creation of Rackdesign instances."""
        initial_count = Rackdesign.objects.count()

        instances = [
            Rackdesign(name=f'Bulk {i}')
            for i in range(5)
        ]
        Rackdesign.objects.bulk_create(instances)

        self.assertEqual(
            Rackdesign.objects.count(),
            initial_count + 5
        )

    def test_query_filter(self):
        """Test filtering Rackdesign instances."""
        # Create a specific instance for filtering
        test_name = f'FilterTest {get_random_string(10)}'
        Rackdesign.objects.create(name=test_name)

        # Test filter
        results = Rackdesign.objects.filter(name=test_name)
        self.assertEqual(results.count(), 1)
        self.assertEqual(results.first().name, test_name)

    def test_ordering(self):
        """Test Rackdesign default ordering."""
        instances = list(Rackdesign.objects.all())

        # Check that instances are ordered by name
        names = [instance.name for instance in instances]
        self.assertEqual(names, sorted(names))


class RackdesignValidationTestCase(PluginModelTestCase):
    """Test Rackdesign validation."""

    def test_empty_name(self):
        """Test that empty name is not allowed."""
        with self.assertRaises(ValidationError):
            instance = Rackdesign(name='')
            instance.full_clean()

    def test_name_max_length(self):
        """Test name field max length."""
        long_name = 'x' * 101  # Exceeds max_length of 100

        with self.assertRaises(ValidationError):
            instance = Rackdesign(name=long_name)
            instance.full_clean()
