"""Core routes: `/` (the landing page for a visitor, the home redirect for a user) and `/healthz`.
Media serving is wired in `nassakh/urls.py`."""

from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("healthz", views.healthz, name="healthz"),
]
