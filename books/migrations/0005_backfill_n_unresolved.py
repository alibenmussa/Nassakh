"""Backfill `Page.n_unresolved` for pages OCR'd before Phase 3.

Before Phase 3 no token carried `res`, so each line's `n_low` already equals its unresolved count; the page
total is their sum.
"""

from django.db import migrations
from django.db.models import IntegerField, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce


def backfill(apps, schema_editor):
    Page = apps.get_model("books", "Page")
    Line = apps.get_model("ocr", "Line")
    total = (
        Line.objects.filter(page_id=OuterRef("pk"))
        .order_by()
        .values("page_id")
        .annotate(n=Sum("n_low"))
        .values("n")
    )
    Page.objects.update(n_unresolved=Coalesce(Subquery(total, output_field=IntegerField()), 0))


class Migration(migrations.Migration):
    dependencies = [
        ("books", "0004_page_n_unresolved"),
        ("ocr", "0002_line_is_manual"),
    ]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
