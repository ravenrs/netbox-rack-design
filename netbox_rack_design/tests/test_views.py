"""
Test cases for NetBox Rack Design views.
"""

from django.urls import reverse

from ..models import Rackdesign
from ..testing import PluginViewTestCase
from ..testing.utils import disable_warnings, get_random_string


class RackdesignViewTestCase(PluginViewTestCase):
    """Test Rackdesign views."""

    @classmethod
    def setUpTestData(cls):
        """Set up test data for all tests."""
        Rackdesign.objects.create(name='View Test 1')
        Rackdesign.objects.create(name='View Test 2')
        Rackdesign.objects.create(name='View Test 3')

    def setUp(self):
        """Set up each test."""
        super().setUp()
        self.base_url = 'plugins:netbox_rack_design:rackdesign'

    def test_list_rackdesigns(self):
        """Test Rackdesign list view."""
        self.add_permissions('netbox_rack_design.view_rackdesign')

        url = reverse('plugins:netbox_rack_design:rackdesign_list')
        response = self.client.get(url)

        self.assertHttpStatus(response, 200)

    def test_list_rackdesigns_without_permission(self):
        """Test Rackdesign list view without permission."""
        url = reverse('plugins:netbox_rack_design:rackdesign_list')

        with disable_warnings('django.request'):
            response = self.client.get(url)
            self.assertHttpStatus(response, 403)

    def test_view_rackdesign(self):
        """Test Rackdesign detail view."""
        self.add_permissions('netbox_rack_design.view_rackdesign')

        instance = Rackdesign.objects.first()
        url = reverse('plugins:netbox_rack_design:rackdesign', kwargs={'pk': instance.pk})
        response = self.client.get(url)

        self.assertHttpStatus(response, 200)
        self.assertEqual(response.context['object'], instance)

    def test_create_rackdesign(self):
        """Test creating a Rackdesign via form."""
        self.add_permissions(
            'netbox_rack_design.add_rackdesign',
            'netbox_rack_design.view_rackdesign'
        )

        url = reverse('plugins:netbox_rack_design:rackdesign_add')
        name = f'Created {get_random_string(10)}'

        form_data = self.post_data({
            'name': name,
        })

        response = self.client.post(url, form_data, follow=True)
        self.assertHttpStatus(response, 200)

        # Verify object was created
        instance = Rackdesign.objects.get(name=name)
        self.assertEqual(instance.name, name)

    def test_create_rackdesign_without_permission(self):
        """Test creating a Rackdesign without permission."""
        url = reverse('plugins:netbox_rack_design:rackdesign_add')

        with disable_warnings('django.request'):
            response = self.client.get(url)
            self.assertHttpStatus(response, 403)

    def test_edit_rackdesign(self):
        """Test editing a Rackdesign via form."""
        self.add_permissions(
            'netbox_rack_design.change_rackdesign',
            'netbox_rack_design.view_rackdesign'
        )

        instance = Rackdesign.objects.first()
        url = reverse('plugins:netbox_rack_design:rackdesign_edit', kwargs={'pk': instance.pk})

        new_name = f'Edited {get_random_string(10)}'
        form_data = self.post_data({
            'name': new_name,
        })

        response = self.client.post(url, form_data, follow=True)
        self.assertHttpStatus(response, 200)

        # Verify object was updated
        instance.refresh_from_db()
        self.assertEqual(instance.name, new_name)

    def test_delete_rackdesign(self):
        """Test deleting a Rackdesign."""
        self.add_permissions(
            'netbox_rack_design.delete_rackdesign',
            'netbox_rack_design.view_rackdesign'
        )

        instance = Rackdesign.objects.first()
        url = reverse('plugins:netbox_rack_design:rackdesign_delete', kwargs={'pk': instance.pk})

        # Confirm deletion
        response = self.client.post(url, {'confirm': True}, follow=True)
        self.assertHttpStatus(response, 200)

        # Verify object was deleted
        self.assertFalse(
            Rackdesign.objects.filter(pk=instance.pk).exists()
        )

    def test_delete_rackdesign_without_permission(self):
        """Test deleting a Rackdesign without permission."""
        instance = Rackdesign.objects.first()
        url = reverse('plugins:netbox_rack_design:rackdesign_delete', kwargs={'pk': instance.pk})

        with disable_warnings('django.request'):
            response = self.client.get(url)
            self.assertHttpStatus(response, 403)


class RackdesignFormTestCase(PluginViewTestCase):
    """Test Rackdesign form validation."""

    def setUp(self):
        """Set up each test."""
        super().setUp()
        self.add_permissions(
            'netbox_rack_design.add_rackdesign',
            'netbox_rack_design.view_rackdesign'
        )

    def test_form_validation_empty_name(self):
        """Test form validation with empty name."""
        url = reverse('plugins:netbox_rack_design:rackdesign_add')
        form_data = self.post_data({'name': ''})

        response = self.client.post(url, form_data)
        self.assertHttpStatus(response, 200)  # Form redisplay

        # Should not create object
        self.assertEqual(Rackdesign.objects.filter(name='').count(), 0)

    def test_form_validation_duplicate_name(self):
        """Test form validation with duplicate name."""
        Rackdesign.objects.create(name='Duplicate')

        url = reverse('plugins:netbox_rack_design:rackdesign_add')
        form_data = self.post_data({'name': 'Duplicate'})

        response = self.client.post(url, form_data)
        self.assertHttpStatus(response, 200)  # Form redisplay

        # Should only have one instance with this name
        self.assertEqual(Rackdesign.objects.filter(name='Duplicate').count(), 1)
