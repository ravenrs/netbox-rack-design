"""
Data migration: pre-seed Design.racks from existing placements.

Historically the racks a design touched were only implicit -- the distinct
``target_rack`` of its DesignPlacement rows. Now that ``Design.racks`` is a
first-class M2M, back-fill it for every existing design from those distinct
target racks, restricted to racks that actually belong to the design's site
(consistent with the same-site validation on the model).

This only writes the M2M through-rows on Design; it never mutates any dcim
record. It is reversible -- the reverse clears the seeded relations.
"""

from django.db import migrations


def seed_design_racks(apps, schema_editor):
    Design = apps.get_model("netbox_rack_design", "Design")
    DesignPlacement = apps.get_model("netbox_rack_design", "DesignPlacement")
    Rack = apps.get_model("dcim", "Rack")

    for design in Design.objects.all():
        target_rack_ids = (
            DesignPlacement.objects.filter(design=design, target_rack__isnull=False)
            .values_list("target_rack_id", flat=True)
            .distinct()
        )
        # Restrict to racks in the design's site (guard against cross-site rows).
        rack_ids = list(
            Rack.objects.filter(
                pk__in=list(target_rack_ids), site_id=design.site_id
            ).values_list("pk", flat=True)
        )
        if rack_ids:
            design.racks.add(*rack_ids)


def clear_design_racks(apps, schema_editor):
    Design = apps.get_model("netbox_rack_design", "Design")
    for design in Design.objects.all():
        design.racks.clear()


class Migration(migrations.Migration):

    dependencies = [
        ("netbox_rack_design", "0004_design_racks"),
    ]

    operations = [
        migrations.RunPython(seed_design_racks, clear_design_racks),
    ]
