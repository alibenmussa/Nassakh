"""D106: the email is the login. Every user with an email gets it in lower case as their username too; a user
whose username is already an email gets it as their email. A user with no email keeps their username and cannot
sign in until an email is set (Django admin, or `manage.py shell`). Two users with the same email: the second
keeps their username (printed), to be sorted out by hand."""

from django.db import migrations


def email_is_the_login(apps, schema_editor):
    User = apps.get_model("auth", "User")
    taken: set[str] = set()
    for user in User.objects.order_by("id"):
        email = (user.email or "").strip().lower()
        if not email and "@" in (user.username or ""):
            email = user.username.strip().lower()
        if not email:
            print(
                f"\n  accounts.0006: user {user.pk} ({user.username}) has no email and cannot sign in until one is set"
            )
            continue
        if email in taken or User.objects.filter(username=email).exclude(pk=user.pk).exists():
            print(
                f"\n  accounts.0006: user {user.pk} ({user.username}) shares the email {email}: left as it is"
            )
            continue
        taken.add(email)
        User.objects.filter(pk=user.pk).update(username=email, email=email)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0005_unlimited_and_plans"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(email_is_the_login, migrations.RunPython.noop)]
