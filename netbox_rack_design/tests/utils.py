"""Shared fixture factories for the NetBox Rack Design test suite."""

from dcim.models import DeviceRole, DeviceType, Manufacturer, Rack, Site
from tenancy.models import Tenant
from utilities.testing import create_test_device

__all__ = ("create_dcim_environment",)


def create_dcim_environment():
    """
    Build the real DCIM objects the plugin's models depend on (a site, a couple
    of racks, a device type, and devices placed in a rack) and return them so a
    test's ``setUpTestData`` can wire up valid Designs and DesignPlacements.
    """
    site = Site.objects.create(name="Site 1", slug="site-1")

    manufacturer = Manufacturer.objects.create(name="Manufacturer 1", slug="manufacturer-1")
    # Explicitly HALF-depth (dcim's DeviceType.is_full_depth defaults to True) so
    # this shared fixture occupies a single face and target_face is meaningful.
    # Full-depth (both-faces) behaviour is covered separately in test_fulldepth.py.
    device_type = DeviceType.objects.create(
        manufacturer=manufacturer, model="Device Type 1", slug="device-type-1",
        u_height=1, is_full_depth=False,
    )

    # Optional role/tenant a planned 'add' placement may carry.
    device_role = DeviceRole.objects.create(name="Role 1", slug="role-1")
    tenant = Tenant.objects.create(name="Tenant 1", slug="tenant-1")

    racks = [
        Rack.objects.create(name="Rack 1", site=site),
        Rack.objects.create(name="Rack 2", site=site),
    ]

    # Two real devices, placed in Rack 1 (used for 'move'/'remove' placements).
    devices = [
        create_test_device("Device 1", site=site, rack=racks[0], position=1, face="front"),
        create_test_device("Device 2", site=site, rack=racks[0], position=2, face="front"),
    ]

    return {
        "site": site,
        "manufacturer": manufacturer,
        "device_type": device_type,
        "device_role": device_role,
        "tenant": tenant,
        "racks": racks,
        "devices": devices,
    }
