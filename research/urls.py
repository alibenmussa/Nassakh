"""Research routes (D107, D108).

`urlpatterns` are mounted at /research/ (reverse as `research:<name>`); `api_urlpatterns` at /api/ in the
shared `api` namespace (`api:research_<name>`). The MCP server is not here: it is its own ASGI app
(`research.mcp_server`, `manage.py mcp_serve`).
"""

from django.urls import path

from . import api, views

app_name = "research"

urlpatterns = [
    path("", views.research_page, name="home"),
    path("clip/<str:token>/", views.clip, name="clip"),
]

api_urlpatterns = [
    path("research/search", api.search, name="research_search"),
    path("research/verify", api.verify, name="research_verify"),
    path("research/books", api.books, name="research_books"),
    path("research/passages/<str:passage_id>/", api.passage, name="research_passage"),
    path("research/keys", api.access_keys, name="research_keys"),
    path("research/keys/<int:key_id>/revoke", api.revoke_key, name="research_key_revoke"),
]
