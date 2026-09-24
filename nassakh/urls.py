"""Root URL configuration.

Mount points (each app fills its own `urls.py`; this file stays untouched):

- `/admin/`                 Django admin (users and groups are managed here for now)
- `/accounts/`              login / logout                       namespace `accounts`
- `/books/`                 books app HTML routes                namespace `books`
- `/books/`                 processing HTML routes (guides)      namespace `processing`
- `/ocr/`                   ocr HTML routes (none in Phase 2)    namespace `ocr`
- `/api/`                   JSON routes from books/processing/ocr `api_urlpatterns`, all in
                            the single namespace `api` (reverse as `api:<name>`)
- `/media/<path>`           uploaded and derived files through the login-protected view
"""

from django.contrib import admin
from django.urls import include, path

from books import urls as books_urls
from core import views as core_views
from ocr import urls as ocr_urls
from processing import urls as processing_urls

api_urlpatterns = books_urls.api_urlpatterns + processing_urls.api_urlpatterns + ocr_urls.api_urlpatterns

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("core.urls")),
    path("accounts/", include("accounts.urls")),
    path("books/", include("books.urls")),
    path("books/", include("processing.urls")),
    path("ocr/", include("ocr.urls")),
    path("api/", include((api_urlpatterns, "api"))),
    # Media always goes through the login-protected view. With DEBUG on this is what serves
    # uploads locally; in production nginx can front it (X-Accel-Redirect) without URL changes.
    path("media/<path:path>", core_views.protected_media, name="media"),
]
