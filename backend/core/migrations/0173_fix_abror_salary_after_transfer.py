from decimal import Decimal
from django.db import migrations
from django.db.models import Sum


def fix_salary_after_transfer(apps, schema_editor):
    FloristSalaryEntry = apps.get_model("core", "FloristSalaryEntry")
    CatalogTransfer = apps.get_model("core", "CatalogTransfer")
    for row in FloristSalaryEntry.objects.select_related("catalog_item").filter(source__in=["catalog", "custom_catalog"], catalog_item__isnull=False).iterator():
        item = row.catalog_item
        transfer_qty = CatalogTransfer.objects.filter(source_item_id=item.id, created_at__date__gte="2026-09-14").aggregate(total=Sum("quantity"))["total"] or 0
        if transfer_qty <= 0:
            continue
        quantity = int(item.quantity_total or 0) + int(transfer_qty or 0)
        unit_amount = Decimal(row.unit_amount or item.florist_salary_amount or 0)
        if quantity > 0 and unit_amount > 0:
            row.quantity = quantity
            row.unit_amount = unit_amount
            row.amount = (unit_amount * Decimal(quantity)).quantize(Decimal("0.01"))
            row.save(update_fields=["quantity", "unit_amount", "amount"])
    invalid_rows = FloristSalaryEntry.objects.filter(source__in=["catalog", "custom_catalog"], catalog_item__isnull=True, created_by__username="developer")
    for row in invalid_rows.iterator():
        row.amount = Decimal("0.00")
        row.quantity = 0
        row.unit_amount = Decimal("0.00")
        row.save(update_fields=["amount", "quantity", "unit_amount"])
    test_rows = FloristSalaryEntry.objects.filter(source__in=["catalog", "custom_catalog"], catalog_item__name_uz__iexact="test", created_by__username="developer")
    for row in test_rows.iterator():
        row.amount = Decimal("0.00")
        row.quantity = 0
        row.unit_amount = Decimal("0.00")
        row.save(update_fields=["amount", "quantity", "unit_amount"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0172_correct_florist_salary_amounts"),
    ]

    operations = [
        migrations.RunPython(fix_salary_after_transfer, migrations.RunPython.noop),
    ]
