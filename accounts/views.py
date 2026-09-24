"""Login and logout as function-based views around `django.contrib.auth`."""

from django.conf import settings
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from accounts.forms import LoginForm
from accounts.services import safe_next_url


@sensitive_post_parameters("password")
@csrf_protect
@never_cache
def login(request: HttpRequest) -> HttpResponse:
    """Show the login card; on success redirect to `?next=` (when local) or LOGIN_REDIRECT_URL."""
    next_url = safe_next_url(request)
    if request.user.is_authenticated:
        return redirect(next_url or settings.LOGIN_REDIRECT_URL)
    if request.method == "POST":
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            auth_login(request, form.get_user())
            return redirect(next_url or settings.LOGIN_REDIRECT_URL)
    else:
        form = LoginForm(request)
    return render(request, "accounts/login.html", {"form": form, "next": next_url})


@require_POST
def logout(request: HttpRequest) -> HttpResponse:
    """End the session (POST only) and return to the login page."""
    auth_logout(request)
    return redirect(settings.LOGOUT_REDIRECT_URL)
