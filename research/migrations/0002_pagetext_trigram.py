"""A trigram index on `PageText.norm` (D107): on PostgreSQL the candidate rows of a search are found with
`norm LIKE '%word%'`, which a `pg_trgm` GIN index serves. Other databases (SQLite in the test suite) skip it:
the same queries work there without an index. Without the right to create the extension the index is left
out (searches still work, row by row) and a warning says so."""

import logging

from django.db import migrations, transaction

log = logging.getLogger(__name__)

INDEX = "research_pagetext_norm_trgm"


def create(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    try:
        with transaction.atomic(using=schema_editor.connection.alias):
            schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            schema_editor.execute(
                f"CREATE INDEX IF NOT EXISTS {INDEX} ON research_pagetext USING gin (norm gin_trgm_ops)"
            )
    except Exception as exc:  # noqa: BLE001 - no right to create the extension: searches work without it
        log.warning("research: the pg_trgm index on PageText.norm was not created (%s)", exc)


def drop(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(f"DROP INDEX IF EXISTS {INDEX}")


class Migration(migrations.Migration):
    dependencies = [("research", "0001_initial")]

    operations = [migrations.RunPython(create, drop, elidable=False)]
