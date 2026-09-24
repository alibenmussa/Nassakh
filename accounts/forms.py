"""Authentication forms with Arabic labels and messages."""

from django import forms
from django.contrib.auth.forms import AuthenticationForm


class LoginForm(AuthenticationForm):
    """Username/password form; wraps Django's AuthenticationForm with Arabic copy."""

    error_messages = {
        "invalid_login": "اسم المستخدم أو كلمة المرور غير صحيحة. تأكد من الكتابة ثم حاول مجددًا.",
        "inactive": "هذا الحساب غير مفعّل. تواصل مع مدير النظام.",
    }

    def __init__(self, request=None, *args, **kwargs):
        super().__init__(request, *args, **kwargs)
        self.fields["username"].label = "اسم المستخدم"
        self.fields["username"].widget = forms.TextInput(
            attrs={
                "class": "input w-full",
                "dir": "ltr",
                "autocomplete": "username",
                "autofocus": True,
                "autocapitalize": "none",
                "spellcheck": "false",
            }
        )
        self.fields["username"].error_messages = {"required": "أدخل اسم المستخدم."}
        self.fields["password"].label = "كلمة المرور"
        self.fields["password"].widget = forms.PasswordInput(
            attrs={"class": "input w-full", "dir": "ltr", "autocomplete": "current-password"}
        )
        self.fields["password"].error_messages = {"required": "أدخل كلمة المرور."}
