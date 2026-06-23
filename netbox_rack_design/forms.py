"""
Forms for NetBox Rack Design.

For more information on NetBox forms, see:
https://docs.netbox.dev/en/stable/plugins/development/forms/
"""

from netbox.forms import NetBoxModelForm

from .models import Rackdesign


class RackdesignForm(NetBoxModelForm):
    class Meta:
        model = Rackdesign
        fields = ("name", "tags")
