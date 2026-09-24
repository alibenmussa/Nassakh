"""Core routes: the home redirect. Media serving is wired in `nassakh/urls.py`."""

from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
]
