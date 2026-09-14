from decimal import Decimal
from django.db import migrations


def correct_production_salary_amounts(apps, schema_editor):
    FloristSalaryEntry = apps.get_model("core", "FloristSalaryEntry")
    for row in FloristSalaryEntry.objects.select_related("catalog_item").filter(source__in=["catalog", "custom_catalog"]).iterator():
        item = row.catalog_item
        quantity = int(row.quantity or 0)
        unit_amount = Decimal(row.unit_amount or 0)
        updates = []
        if item:
            quantity = int(item.quantity_total or 1)
            if unit_amount <= 0:
                unit_amount = Decimal(item.florist_salary_amount or 0)
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
        elif quantity <= 0 and Decimal(row.amount or 0) > 0:
            quantity = 1
            if unit_amount <= 0:
                unit_amount = Decimal(row.amount or 0)
        if quantity > 0 and row.quantity != quantity:
            row.quantity = quantity
            updates.append("quantity")
        if unit_amount > 0 and Decimal(row.unit_amount or 0) != unit_amount:
            row.unit_amount = unit_amount
            updates.append("unit_amount")
        if quantity > 0 and unit_amount > 0:
            amount = (unit_amount * Decimal(quantity)).quantize(Decimal("0.01"))
            if Decimal(row.amount or 0) != amount:
                row.amount = amount
                updates.append("amount")
        if updates:
            row.save(update_fields=list(dict.fromkeys(updates)))


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0171_florist_salary_snapshot"),
    ]

    operations = [
        migrations.RunPython(correct_production_salary_amounts, migrations.RunPython.noop),
    ]
