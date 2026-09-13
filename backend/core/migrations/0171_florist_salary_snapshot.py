from decimal import Decimal, ROUND_HALF_UP
from django.db import migrations, models


def fill_salary_snapshots(apps, schema_editor):
    FloristSalaryEntry = apps.get_model("core", "FloristSalaryEntry")
    CatalogReworkOutput = apps.get_model("core", "CatalogReworkOutput")
    production_sources = {"catalog", "custom_catalog"}
    decoration_sources = {"decoration", "sale_decoration", "extra_decoration"}
    for row in FloristSalaryEntry.objects.select_related("catalog_item").iterator():
        item = row.catalog_item
        updates = []
        amount = Decimal(row.amount or 0)
        quantity = int(row.quantity or 0)
        unit_amount = Decimal(row.unit_amount or 0)
        if item:
            values = {
                "catalog_name": item.name_uz or "",
                "catalog_kind": item.catalog_kind or "",
                "arrangement_type": item.arrangement_type or "",
                "volume": item.volume or "",
            }
            for key, value in values.items():
                if getattr(row, key) != value:
                    setattr(row, key, value)
                    updates.append(key)
            if row.source in production_sources and unit_amount <= 0:
                unit_amount = Decimal(item.florist_salary_amount or 0)
            elif row.source in decoration_sources and unit_amount <= 0:
                unit_amount = Decimal(item.decoration_salary_amount or 0)
        elif row.source in production_sources and not row.catalog_name and row.note:
            row.catalog_name = row.note.split(" uchun ")[0][:180]
            updates.append("catalog_name")
        if row.source == "rework" and quantity <= 0 and row.rework_id:
            quantity = CatalogReworkOutput.objects.filter(rework_id=row.rework_id).aggregate(total=models.Sum("catalog_item__quantity_total"))["total"] or 0
        if quantity <= 0 and unit_amount > 0:
            quantity = max(int((amount / unit_amount).quantize(Decimal("1"), rounding=ROUND_HALF_UP)), 1)
        if quantity <= 0 and row.source in production_sources and item:
            quantity = int(item.quantity_total or 1)
        if quantity <= 0 and row.source in production_sources and amount > 0:
            quantity = 1
        if quantity <= 0 and row.source in decoration_sources and amount > 0 and unit_amount <= 0:
            quantity = 1
        if unit_amount <= 0 and quantity > 0:
            unit_amount = (amount / Decimal(quantity)).quantize(Decimal("0.01"))
        if row.quantity != quantity:
            row.quantity = quantity
            updates.append("quantity")
        if Decimal(row.unit_amount or 0) != unit_amount:
            row.unit_amount = unit_amount
            updates.append("unit_amount")
        if updates:
            row.save(update_fields=list(dict.fromkeys(updates)))


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0170_florist_volume_rate_box"),
    ]

    operations = [
        migrations.AddField(
            model_name="floristsalaryentry",
            name="catalog_kind",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="floristsalaryentry",
            name="catalog_name",
            field=models.CharField(blank=True, max_length=180),
        ),
        migrations.AddField(
            model_name="floristsalaryentry",
            name="arrangement_type",
            field=models.CharField(blank=True, max_length=20),
        ),
        migrations.AddField(
            model_name="floristsalaryentry",
            name="volume",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.RunPython(fill_salary_snapshots, migrations.RunPython.noop),
    ]
