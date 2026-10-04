"""«الفوترة» (D106): the page quota of every account, for superusers only; anyone else gets 404 (the screens
are not shown to exist). Mounted under /accounts/billing/.

- the accounts: kind, available, held, next expiry, pages used this month (`accounts_list`);
- an account: its grants (add one from a plan or by hand, revoke one), its ledger, its usage by book, and
  «غير محدود» (`account`, `grant_add`, `grant_revoke`, `set_unlimited`);
- the plans: add, edit, take off sale (`plans`, `plan_edit`).

Every change goes through `accounts.billing`; members see their own account read-only on the organisation's
page.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts import billing
from accounts.forms import GrantForm, PlanForm
from accounts.models import Organization, Plan, QuotaGrant

NOT_FOUND = "الصفحة غير موجودة."


def _superuser(request: HttpRequest) -> None:
    if not request.user.is_superuser:
        raise Http404(NOT_FOUND)


def _account_url(organization_id: int, anchor: str = "") -> str:
    return reverse("accounts:billing_account", args=[organization_id]) + (f"#{anchor}" if anchor else "")


def _first_error(form) -> str:
    for errors in form.errors.values():
        if errors:
            return str(errors[0])
    return "تحقّق من القيم ثم أعد المحاولة."


@login_required
@require_GET
def accounts_list(request: HttpRequest) -> HttpResponse:
    """Every account with its numbers (`billing.summaries`: a few queries whatever their number)."""
    _superuser(request)
    organizations = list(
        Organization.objects.annotate(
            n_members=Count("memberships", distinct=True), n_books=Count("books", distinct=True)
        ).order_by("unlimited", "name", "id")
    )
    numbers = billing.summaries(organizations)
    rows = [{"organization": org, "summary": numbers[org.pk]} for org in organizations]
    return render(request, "accounts/billing/accounts.html", {"rows": rows})


def _account_context(organization: Organization, form: GrantForm) -> dict:
    now = timezone.now()
    grants = QuotaGrant.objects.filter(organization=organization).select_related(
        "plan", "created_by", "revoked_by"
    )
    return {
        "organization": organization,
        "summary": billing.summary(organization, now),
        "grants": [{"grant": item, "state": item.state(now)} for item in grants],
        "ledger": billing.ledger(organization),
        "usage": billing.usage_by_book(organization),
        "members": organization.memberships.select_related("user").order_by("id"),
        "plans": Plan.objects.filter(active=True),
        "form": form,
    }


@login_required
@require_GET
def account(request: HttpRequest, organization_id: int) -> HttpResponse:
    """An account: numbers, grants (and the form to add one), usage by book, ledger, «غير محدود»."""
    _superuser(request)
    organization = get_object_or_404(Organization, pk=organization_id)
    return render(request, "accounts/billing/account.html", _account_context(organization, GrantForm()))


@login_required
@require_POST
def grant_add(request: HttpRequest, organization_id: int) -> HttpResponse:
    """Give pages to the account, from a plan or by hand (`GrantForm`, `billing.grant`)."""
    _superuser(request)
    organization = get_object_or_404(Organization, pk=organization_id)
    form = GrantForm(request.POST)
    if form.is_valid():
        try:
            item = billing.grant(
                organization, form.cleaned_data["pages"], user=request.user, **form.grant_kwargs()
            )
        except ValueError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(
                request, f"أُضيف {billing.pages_phrase(item.pages)} إلى رصيد «{organization.name}»."
            )
            return redirect(_account_url(organization.pk, "grants"))
    return render(request, "accounts/billing/account.html", _account_context(organization, form), status=400)


@login_required
@require_POST
def grant_revoke(request: HttpRequest, grant_id: int) -> HttpResponse:
    """Revoke a grant: its remaining pages no longer count (`billing.revoke`)."""
    _superuser(request)
    item = get_object_or_404(QuotaGrant, pk=grant_id)
    left = item.remaining
    billing.revoke(item, request.user)
    tail = f" سقط ما بقي منه ({billing.pages_phrase(left)})." if left else ""
    messages.success(request, f"أُلغي الرصيد.{tail}")
    return redirect(_account_url(item.organization_id, "grants"))


@login_required
@require_POST
def set_unlimited(request: HttpRequest, organization_id: int) -> HttpResponse:
    """«غير محدود» on or off: an unlimited account is never limited nor charged."""
    _superuser(request)
    organization = get_object_or_404(Organization, pk=organization_id)
    organization.unlimited = str(request.POST.get("unlimited") or "") in ("1", "on", "true")
    organization.save(update_fields=["unlimited"])
    if organization.unlimited:
        messages.success(request, f"صار رصيد «{organization.name}» غير محدود: لا تُحتسب صفحاته.")
    else:
        messages.success(request, f"صار «{organization.name}» يقرأ من رصيده.")
    return redirect(_account_url(organization.pk))


@login_required
@require_POST
def set_overdraft(request: HttpRequest, organization_id: int) -> HttpResponse:
    """The overdraft limit: pages the account may read past its balance (a debt the next grant pays first)."""
    _superuser(request)
    organization = get_object_or_404(Organization, pk=organization_id)
    try:
        pages = int(str(request.POST.get("pages") or "0").strip())
    except ValueError:
        pages = -1
    if pages < 0:
        messages.error(request, "اكتب عدد صفحات صحيحًا (صفر أو أكثر).")
        return redirect(_account_url(organization.pk))
    organization.overdraft_pages = pages
    organization.save(update_fields=["overdraft_pages"])
    if pages:
        messages.success(request, f"يستطيع «{organization.name}» أن يقرأ {pages} صفحة بعد نفاد رصيده.")
    else:
        messages.success(request, f"لا سحب على المكشوف لـ«{organization.name}».")
    return redirect(_account_url(organization.pk))


@login_required
def plans(request: HttpRequest) -> HttpResponse:
    """The plans: the list (with how many grants each made) and the form to add one."""
    _superuser(request)
    if request.method == "POST":
        form = PlanForm(request.POST)
        if form.is_valid():
            plan = form.save()
            messages.success(request, f"أُضيفت الباقة «{plan.name}».")
            return redirect("accounts:billing_plans")
    elif request.method == "GET":
        form = PlanForm(initial={"validity_days": 365, "currency": "USD", "active": True})
    else:
        return HttpResponse(status=405)
    rows = Plan.objects.annotate(n_grants=Count("grants")).order_by("-active", "pages", "id")
    status = 400 if request.method == "POST" else 200
    return render(request, "accounts/billing/plans.html", {"plans": rows, "form": form}, status=status)


@login_required
@require_POST
def plan_edit(request: HttpRequest, plan_id: int) -> HttpResponse:
    """Change a plan (its name, pages, validity, price, sale). Grants made from it keep their own values."""
    _superuser(request)
    plan = get_object_or_404(Plan, pk=plan_id)
    form = PlanForm(request.POST, instance=plan)
    if form.is_valid():
        form.save()
        messages.success(request, f"حُفظت الباقة «{plan.name}».")
    else:
        messages.error(request, _first_error(form))
    return redirect("accounts:billing_plans")
