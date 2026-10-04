from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"
    verbose_name = "الحسابات"

    def ready(self) -> None:
        from accounts import signals  # noqa: F401 - the email is the login (D106)
