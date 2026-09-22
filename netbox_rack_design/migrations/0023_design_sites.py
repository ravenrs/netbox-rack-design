"""
Design.site (FK) -> Design.sites (M2M) -- PLAN-multi-site.md M1/§3.

A design may now plan across one OR MORE sites. Every existing design keeps
its one site (backfilled below); ``clean()`` (models.py) enforces "at least
one" going forward.

Step order matters:
  1. Add ``sites`` with a TEMPORARY related_name (``rack_designs_m2m``) --
     the final name (``rack_designs``) is still claimed by the ``site`` FK
     below until step 4 removes it.
  2. Backfill: every design's one ``site`` becomes its first (only) member of
     ``sites``.
  3. Make ``site`` nullable in-place, purely so its REVERSE (a `migrate ...
     0022`) recreates it as a nullable column rather than the original
     PROTECT/NOT NULL one -- reversing a RemoveField replays whatever field
     definition preceded it in this same migration (see
     ``RemoveField.database_backwards``), and a NOT NULL column can't be
     re-added onto rows that have no value for it yet.
  4. Remove ``site``.
  5. Claim the final related_name (``rack_designs``) now that ``site`` no
     longer holds it, and update ``Meta.ordering`` (``site, sequence, pk`` ->
     ``sequence, pk`` -- M6, sequence is now global, not per site).

Reverse (``migrate ... 0022``): recreates ``site`` (nullable -- see step 3),
copies ``sites.first()`` back into it per design (a design with more than one
site can only contribute its first back), then drops ``sites`` entirely.
"""

from django.db import migrations, models


def copy_site_to_sites(apps, schema_editor):
    Design = apps.get_model("netbox_rack_design", "Design")
    for design in Design.objects.all():
        design.sites.add(design.site_id)


def copy_sites_to_site(apps, schema_editor):
    Design = apps.get_model("netbox_rack_design", "Design")
    for design in Design.objects.all():
        first_site_id = design.sites.values_list("pk", flat=True).first()
        if first_site_id is not None:
            design.site_id = first_site_id
            design.save(update_fields=["site"])


class Migration(migrations.Migration):

    dependencies = [
        ("dcim", "0216_latitude_longitude_validators"),
        ("netbox_rack_design", "0022_designplacement_preferred_feed_legs"),
    ]

    operations = [
        migrations.AddField(
            model_name="design",
            name="sites",
            field=models.ManyToManyField(related_name="rack_designs_m2m", to="dcim.site"),
        ),
        migrations.RunPython(copy_site_to_sites, copy_sites_to_site),
        migrations.AlterField(
            model_name="design",
            name="site",
            field=models.ForeignKey(
                to="dcim.site", on_delete=models.PROTECT,
                related_name="rack_designs", null=True,
            ),
        ),
        migrations.RemoveField(
            model_name="design",
            name="site",
        ),
        migrations.AlterField(
            model_name="design",
            name="sites",
            field=models.ManyToManyField(related_name="rack_designs", to="dcim.site"),
        ),
        migrations.AlterModelOptions(
            name="design",
            options={"ordering": ("sequence", "pk")},
        ),
    ]
