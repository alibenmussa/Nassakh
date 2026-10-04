"""D106: every organisation that exists before the page quota is unlimited (the owner's own and the ones the
superuser made by hand: never limited, never charged); the plans the superuser sells are seeded ($49 / 1,500,
$149 / 5,000, $349 / 15,000 pages, valid 365 days; editable on «الفوترة»). Sign-ups made later are limited."""

from decimal import Decimal

from django.db import migrations

PLANS = (
    ("الباقة الأساسية", 1500, Decimal("49.00")),
    ("الباقة المتوسطة", 5000, Decimal("149.00")),
    ("الباقة الكبيرة", 15000, Decimal("349.00")),
)


def forward(apps, schema_editor):
    Organization = apps.get_model("accounts", "Organization")
    Plan = apps.get_model("accounts", "Plan")
    Organization.objects.update(unlimited=True)
    if not Plan.objects.exists():
        for name, pages, price in PLANS:
            Plan.objects.create(name=name, pages=pages, validity_days=365, price=price, currency="USD")


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_accounts_quota"),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
