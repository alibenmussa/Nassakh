"""Make the first superuser from ADMIN_EMAIL and ADMIN_PASSWORD (deploy/bootstrap.sh runs it in `web`).

The email is the login (D106): the username is the email in lower case (accounts.signals does it on every
save). An account that exists is made a superuser and active again; its password is not changed unless
ADMIN_RESET_PASSWORD=1.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nassakh.settings")

import django  # noqa: E402

django.setup()

from django.contrib.auth import get_user_model  # noqa: E402

from accounts.backends import normalize_email  # noqa: E402


def main() -> int:
    email = normalize_email(os.environ.get("ADMIN_EMAIL"))
    password = os.environ.get("ADMIN_PASSWORD") or ""
    if "@" not in email:
        print("ADMIN_EMAIL is empty or not an email: set it in deploy/.env.production", file=sys.stderr)
        return 1
    User = get_user_model()
    user = User.objects.filter(username=email).first()
    if user is None:
        if len(password) < 10:
            print("ADMIN_PASSWORD is empty or shorter than 10 characters", file=sys.stderr)
            return 1
        User.objects.create_superuser(username=email, email=email, password=password)
        print(f"superuser created: {email}")
        return 0
    user.is_superuser = user.is_staff = user.is_active = True
    if os.environ.get("ADMIN_RESET_PASSWORD") == "1" and len(password) >= 10:
        user.set_password(password)
    user.save()
    print(f"superuser exists: {email} (made sure it is active and a superuser)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
