"""Root URL configuration.

Mount points (each app fills its own `urls.py`; this file stays untouched):

- `/admin/`                 Django admin (users and groups are managed here for now)
- `/accounts/`              login / logout                       namespace `accounts`
- `/books/`                 books app HTML routes                namespace `books`
- `/books/`                 processing HTML routes (guides)      namespace `processing`
- `/books/`                 review screens (Phase 3)             namespace `review`
- `/books/`                 manuscript view (Phase 4)            namespace `assembly`
- `/books/`                 editor and layout pages (Phase 5)    namespace `editor`
- `/books/`                 the export page and downloads (6)    namespace `publishing`
- `/ocr/`                   ocr HTML routes (none in Phase 2)    namespace `ocr`
- `/api/`                   JSON routes from books/processing/ocr/review/assembly/editor/publishing
                            `api_urlpatterns`, all in
                            the single namespace `api` (reverse as `api:<name>`)
- `/media/<path>`           uploaded and derived files through the login-protected view
"""

from django.contrib import admin
from django.urls import include, path

from assembly import urls as assembly_urls
from books import urls as books_urls
from core import views as core_views
from editor import urls as editor_urls
from ocr import urls as ocr_urls
from processing import urls as processing_urls
from publishing import urls as publishing_urls
from review import urls as review_urls

api_urlpatterns = (
    books_urls.api_urlpatterns
    + processing_urls.api_urlpatterns
    + ocr_urls.api_urlpatterns
    + review_urls.api_urlpatterns
    + assembly_urls.api_urlpatterns
    + editor_urls.api_urlpatterns
    + publishing_urls.api_urlpatterns
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("core.urls")),
    path("accounts/", include("accounts.urls")),
    path("books/", include("books.urls")),
    path("books/", include("processing.urls")),
    path("books/", include("review.urls")),
    path("books/", include("assembly.urls")),
    path("books/", include("editor.urls")),
    path("books/", include("publishing.urls")),
    path("ocr/", include("ocr.urls")),
    path("api/", include((api_urlpatterns, "api"))),
    # Media always goes through the login-protected view. With DEBUG on this is what serves
    # uploads locally; in production nginx can front it (X-Accel-Redirect) without URL changes.
    path("media/<path:path>", core_views.protected_media, name="media"),
]
