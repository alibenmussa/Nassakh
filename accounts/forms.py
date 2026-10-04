"""Forms of the accounts app with Arabic labels and messages: the login, the sign-up (D106) and the forms of
«الفوترة» (a grant, a plan)."""

from __future__ import annotations

from datetime import datetime, time

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils import timezone

from accounts.models import Organization, Plan, QuotaGrant

INPUT = {"class": "input"}
LTR = {"class": "input", "dir": "ltr", "autocapitalize": "none", "spellcheck": "false"}


class LoginForm(AuthenticationForm):
    """Email/password form (the email is the login, in any case); Django's AuthenticationForm with Arabic
    copy. A sign-up whose email is not confirmed yet (D106) is told so, when its password is right, and
    `unconfirmed_email` lets the page offer to send the link again."""

    error_messages = {
        "invalid_login": (
            "البريد الإلكتروني أو كلمة المرور غير صحيحة. تأكد من الكتابة ثم حاول مجددًا."
        ),
        "inactive": "هذا الحساب غير مفعّل. تواصل مع مدير النظام.",
        "unconfirmed": "لم يُفعَّل حسابك بعد. افتح رابط التأكيد الذي أرسلناه إلى بريدك، أو اطلب رابطًا جديدًا.",
    }

    def __init__(self, request=None, *args, **kwargs):
        super().__init__(request, *args, **kwargs)
        self.unconfirmed_email = ""
        self.fields["username"].label = "البريد الإلكتروني"
        self.fields["username"].max_length = 254
        self.fields["username"].widget = forms.EmailInput(
            attrs={
                "class": "input w-full",
                "dir": "ltr",
                "autocomplete": "email",
                "autofocus": True,
                "autocapitalize": "none",
                "spellcheck": "false",
                "maxlength": 254,
            }
        )
        self.fields["username"].error_messages = {"required": "أدخل بريدك الإلكتروني."}
        self.fields["password"].label = "كلمة المرور"
        self.fields["password"].widget = forms.PasswordInput(
            attrs={"class": "input w-full", "dir": "ltr", "autocomplete": "current-password"}
        )
        self.fields["password"].error_messages = {"required": "أدخل كلمة المرور."}

    def clean(self):
        try:
            return super().clean()
        except ValidationError as exc:
            if getattr(exc, "code", None) != "invalid_login":
                raise
            from accounts.backends import find_user
            from accounts.signup import is_pending

            user = find_user(self.cleaned_data.get("username") or "")
            password = self.cleaned_data.get("password") or ""
            if user is not None and is_pending(user) and password and user.check_password(password):
                self.unconfirmed_email = user.email
                raise ValidationError(self.error_messages["unconfirmed"], code="unconfirmed") from None
            raise


class SignUpForm(forms.Form):
    """A new account (D106): the person, then the account kind and its details (an organisation: its name,
    type, country and website; an individual: the country). The email is the login: unique, any case."""

    full_name = forms.CharField(
        label="الاسم الكامل",
        max_length=150,
        widget=forms.TextInput(attrs={**INPUT, "autocomplete": "name", "autofocus": True, "dir": "auto"}),
        error_messages={"required": "أدخل اسمك."},
    )
    email = forms.EmailField(
        label="البريد الإلكتروني",
        max_length=150,
        widget=forms.EmailInput(attrs={**LTR, "autocomplete": "email"}),
        help_text="به تسجّل الدخول، وإليه يصل رابط التأكيد.",
        error_messages={"required": "أدخل بريدك الإلكتروني.", "invalid": "أدخل بريدًا إلكترونيًا صحيحًا."},
    )
    password = forms.CharField(
        label="كلمة المرور",
        strip=False,
        widget=forms.PasswordInput(attrs={**LTR, "autocomplete": "new-password"}),
        help_text="8 أحرف على الأقل، لا أرقام وحدها ولا كلمة شائعة.",
        error_messages={"required": "أدخل كلمة المرور."},
    )
    kind = forms.ChoiceField(
        label="نوع الحساب",
        choices=Organization.Kind.choices,
        initial=Organization.Kind.ORGANIZATION,
        widget=forms.RadioSelect,
        error_messages={"required": "اختر نوع الحساب.", "invalid_choice": "اختر نوع الحساب."},
    )
    org_name = forms.CharField(
        label="اسم المؤسسة",
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={**INPUT, "autocomplete": "organization", "dir": "auto"}),
    )
    org_type = forms.ChoiceField(
        label="نوع المؤسسة",
        choices=[("", "اختر…"), *Organization.OrgType.choices],
        required=False,
        widget=forms.Select(attrs={"class": "select"}),
        error_messages={"invalid_choice": "اختر نوع المؤسسة من القائمة."},
    )
    country = forms.CharField(
        label="البلد",
        max_length=100,
        widget=forms.TextInput(attrs={**INPUT, "autocomplete": "country-name", "dir": "auto"}),
        error_messages={"required": "أدخل البلد."},
    )
    website = forms.URLField(
        label="الموقع",
        max_length=300,
        required=False,
        assume_scheme="https",
        widget=forms.TextInput(
            attrs={**LTR, "autocomplete": "url", "inputmode": "url", "placeholder": "https://"}
        ),
        help_text="اختياري.",
        error_messages={"invalid": "أدخل عنوانًا صحيحًا، مثل https://example.org"},
    )

    def clean_full_name(self) -> str:
        return " ".join((self.cleaned_data.get("full_name") or "").split())

    def clean_email(self) -> str:
        email = (self.cleaned_data.get("email") or "").strip().lower()
        users = get_user_model()._default_manager
        if email and (
            users.filter(email__iexact=email).exists() or users.filter(username__iexact=email).exists()
        ):
            raise ValidationError(
                "هذا البريد مسجّل من قبل. سجّل الدخول به، أو اطلب رابط التأكيد من صفحة الدخول."
            )
        return email

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("kind") == Organization.Kind.ORGANIZATION:
            cleaned["org_name"] = " ".join((cleaned.get("org_name") or "").split())
            if not cleaned["org_name"]:
                self.add_error("org_name", "أدخل اسم المؤسسة.")
            if not cleaned.get("org_type"):
                self.add_error("org_type", "اختر نوع المؤسسة.")
        else:
            cleaned["org_name"], cleaned["org_type"], cleaned["website"] = "", "", ""
        password = cleaned.get("password")
        if password:
            probe = get_user_model()(
                username=cleaned.get("email") or "",
                email=cleaned.get("email") or "",
                first_name=cleaned.get("full_name") or "",
            )
            try:
                validate_password(password, probe)
            except ValidationError as exc:
                self.add_error("password", exc)
        return cleaned


# ====================================================================== «الفوترة» (superusers)


def _start_of(day) -> datetime:
    return timezone.make_aware(datetime.combine(day, time.min))


class GrantForm(forms.Form):
    """Pages given to an account by hand or from a plan: a plan fills what is left blank (its pages, validity,
    price and currency). The expiry is a date (the end of that day) or a validity in days from the start;
    neither: never expires."""

    plan = forms.ModelChoiceField(
        label="من باقة",
        queryset=Plan.objects.filter(active=True),
        required=False,
        empty_label="بلا باقة",
        widget=forms.Select(attrs={"class": "select"}),
    )
    kind = forms.ChoiceField(
        label="النوع",
        choices=QuotaGrant.Kind.choices,
        initial=QuotaGrant.Kind.PURCHASE,
        widget=forms.Select(attrs={"class": "select"}),
    )
    pages = forms.IntegerField(
        label="الصفحات",
        min_value=1,
        required=False,
        widget=forms.NumberInput(attrs={**INPUT, "dir": "ltr", "inputmode": "numeric", "min": 1}),
        error_messages={"min_value": "اكتب عدد صفحات أكبر من صفر.", "invalid": "اكتب عددًا صحيحًا."},
    )
    starts_on = forms.DateField(
        label="يبدأ في",
        required=False,
        widget=forms.DateInput(attrs={**INPUT, "type": "date", "dir": "ltr"}),
        help_text="فارغًا: الآن.",
        error_messages={"invalid": "اكتب تاريخًا صحيحًا."},
    )
    expires_on = forms.DateField(
        label="ينتهي في",
        required=False,
        widget=forms.DateInput(attrs={**INPUT, "type": "date", "dir": "ltr"}),
        help_text="آخر يوم يُستعمل فيه.",
        error_messages={"invalid": "اكتب تاريخًا صحيحًا."},
    )
    days = forms.IntegerField(
        label="أو الصلاحية (أيام)",
        min_value=1,
        required=False,
        widget=forms.NumberInput(attrs={**INPUT, "dir": "ltr", "inputmode": "numeric", "min": 1}),
        help_text="من يوم البدء. فارغان معًا: لا ينتهي.",
        error_messages={"min_value": "اكتب عدد أيام أكبر من صفر.", "invalid": "اكتب عددًا صحيحًا."},
    )
    amount = forms.DecimalField(
        label="المبلغ",
        max_digits=10,
        decimal_places=2,
        min_value=0,
        required=False,
        widget=forms.NumberInput(
            attrs={**INPUT, "dir": "ltr", "inputmode": "decimal", "step": "0.01", "min": 0}
        ),
        error_messages={"invalid": "اكتب مبلغًا صحيحًا."},
    )
    currency = forms.CharField(
        label="العملة",
        max_length=3,
        required=False,
        widget=forms.TextInput(attrs={**LTR, "maxlength": 3, "placeholder": "USD"}),
    )
    reference = forms.CharField(
        label="المرجع",
        max_length=120,
        required=False,
        widget=forms.TextInput(attrs={**INPUT, "dir": "auto"}),
        help_text="رقم الإيصال أو الفاتورة.",
    )
    note = forms.CharField(
        label="ملاحظة",
        required=False,
        widget=forms.TextInput(attrs={**INPUT, "dir": "auto"}),
    )

    def clean(self):
        cleaned = super().clean()
        plan = cleaned.get("plan")
        if plan is not None:
            if not cleaned.get("pages"):
                cleaned["pages"] = plan.pages
            if not cleaned.get("expires_on") and not cleaned.get("days"):
                cleaned["days"] = plan.validity_days or None
            if cleaned.get("amount") is None:
                cleaned["amount"] = plan.price
            if not cleaned.get("currency"):
                cleaned["currency"] = plan.currency
        if not cleaned.get("pages") and "pages" not in self.errors:
            self.add_error("pages", "اكتب عدد الصفحات أو اختر باقة.")
        if cleaned.get("expires_on") and cleaned.get("days"):
            self.add_error("days", "اكتب تاريخ الانتهاء أو مدة الصلاحية، لا كليهما.")
        return cleaned

    def grant_kwargs(self) -> dict:
        """The arguments of `accounts.billing.grant` (after `is_valid`)."""
        data = self.cleaned_data
        today = timezone.localdate()
        starts_on = data.get("starts_on")
        starts_at = timezone.now() if not starts_on or starts_on == today else _start_of(starts_on)
        expires_on = data.get("expires_on")
        expires_at = timezone.make_aware(datetime.combine(expires_on, time.max)) if expires_on else None
        return {
            "kind": data.get("kind") or QuotaGrant.Kind.PURCHASE,
            "plan": data.get("plan"),
            "starts_at": starts_at,
            "expires_at": expires_at,
            "days": data.get("days") if not expires_at else None,
            "amount": data.get("amount"),
            "currency": (data.get("currency") or "").strip().upper(),
            "reference": (data.get("reference") or "").strip(),
            "note": (data.get("note") or "").strip(),
        }


class PlanForm(forms.ModelForm):
    """A plan the superuser sells (`accounts.Plan`)."""

    class Meta:
        model = Plan
        fields = ["name", "pages", "validity_days", "price", "currency", "active", "note"]
        widgets = {
            "name": forms.TextInput(attrs={**INPUT, "dir": "auto"}),
            "pages": forms.NumberInput(attrs={**INPUT, "dir": "ltr", "inputmode": "numeric", "min": 1}),
            "validity_days": forms.NumberInput(
                attrs={**INPUT, "dir": "ltr", "inputmode": "numeric", "min": 1}
            ),
            "price": forms.NumberInput(attrs={**INPUT, "dir": "ltr", "inputmode": "decimal", "step": "0.01"}),
            "currency": forms.TextInput(attrs={**LTR, "maxlength": 3}),
            "note": forms.TextInput(attrs={**INPUT, "dir": "auto"}),
        }
        error_messages = {
            "name": {"required": "اكتب اسم الباقة."},
            "pages": {"required": "اكتب عدد الصفحات.", "invalid": "اكتب عددًا صحيحًا."},
            "validity_days": {"required": "اكتب مدة الصلاحية بالأيام.", "invalid": "اكتب عددًا صحيحًا."},
            "price": {"required": "اكتب السعر.", "invalid": "اكتب سعرًا صحيحًا."},
            "currency": {"required": "اكتب العملة، مثل USD."},
        }

    def clean_pages(self) -> int:
        pages = self.cleaned_data.get("pages") or 0
        if pages <= 0:
            raise ValidationError("اكتب عدد صفحات أكبر من صفر.")
        return pages

    def clean_currency(self) -> str:
        return (self.cleaned_data.get("currency") or "").strip().upper()
