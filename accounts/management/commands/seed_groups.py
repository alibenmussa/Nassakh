"""`manage.py seed_groups` — make sure the three role groups exist (also done by a data migration)."""

from django.core.management.base import BaseCommand

from accounts.services import ensure_groups


class Command(BaseCommand):
    help = "Create the role groups admin, editor and proofreader if they are missing."

    def handle(self, *args, **options):
        groups = ensure_groups()
        self.stdout.write("groups: " + ", ".join(g.name for g in groups))
