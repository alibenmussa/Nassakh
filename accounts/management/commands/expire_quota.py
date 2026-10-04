"""`manage.py expire_quota` (D106, daily): the `expire` entries of the grants that expired with pages left.

Expiry already counts when the balance is read (`accounts.billing.live_q`); this records it in the ledger and
sets the grants' `remaining` to 0. Safe to run any number of times.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from accounts import billing


class Command(BaseCommand):
    help = "Record the page-quota grants that expired (D106): an expire entry each, remaining set to 0."

    def handle(self, *args, **options) -> None:
        count = billing.expire()
        self.stdout.write(f"expired grants recorded: {count}")
