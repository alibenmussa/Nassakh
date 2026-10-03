"""«تحضير الغلاف» on the book page (the owner's review, 2026-10-03, item 5: slow, and a blank «يُحضَّر الغلاف…»
sheet while it was drawn): the page embeds the cover's cached render (never drawing it in the page's own
request), every stylesheet save answers with the render (no second request), and the component draws the cover
itself between a change and its render (core/test_cover_ui.py, editor/test_book_page.py)."""

from __future__ import annotations

import pathlib

from django.urls import reverse

import pytest

from core.test_layout_ui import _book, _page
from editor import services
from editor.models import StyleSheet
from editor.tests import editor_user, logged, put_json  # noqa: F401 - editor_user is a fixture

pytestmark = pytest.mark.django_db


def test_the_page_embeds_the_cached_cover_and_never_draws_it_itself(editor_user, settings):
    book = _book()
    client = logged(editor_user)
    # no cover: nothing embedded, nothing asked
    _body, config = _page(client, book)
    assert config["initial"]["cover"] is None
    StyleSheet.objects.update_or_create(
        book=book, defaults={"front_matter": {"cover": {"mode": "text", "center": "كتاب الغلاف"}}}
    )
    # a cover never drawn: `pending`, and the page's request drew nothing (api:cover will)
    _body, config = _page(client, book)
    pending = config["initial"]["cover"]
    assert pending["mode"] == "text" and pending["pending"] is True and pending["image_1x"] is None
    folder = pathlib.Path(settings.MEDIA_ROOT) / f"books/{book.pk}/cover/{pending['hash']}"
    assert not folder.exists()
    drawn = client.get(reverse("api:cover", args=[book.pk])).json()
    assert drawn["hash"] == pending["hash"] and drawn["image_1x"]
    # drawn once: the page embeds it, so its sheet paints at once
    _body, config = _page(client, book)
    assert config["initial"]["cover"] == drawn


def test_a_stylesheet_save_answers_with_the_cover_render(editor_user):
    book = _book()
    client = logged(editor_user)
    url = reverse("api:stylesheet", args=[book.pk])
    # a book without a cover: no render, none asked for
    plain = put_json(client, url, {"body_size_pt": 14}).json()
    assert plain["cover_render"] is None
    # the cover turned on: drawn in the save, its answer carries it
    on = put_json(client, url, {"front_matter": {"cover": {"mode": "info"}}}).json()
    render = on["cover_render"]
    assert render["mode"] == "info" and render["image_1x"].endswith("/cover.webp")
    assert client.get(reverse("api:cover", args=[book.pk])).json() == render
    # a change that moves the cover (the margins it reads) answers with the new render
    moved = put_json(client, url, {"bottom_mm": 30}).json()["cover_render"]
    assert moved["hash"] != render["hash"] and moved["image_1x"]
    # turned off: the render says so (no images)
    off = put_json(client, url, {"front_matter": {"cover": {"mode": "none"}}}).json()["cover_render"]
    assert off["mode"] == "none" and off["image_1x"] is None


def test_a_cover_that_cannot_be_read_never_breaks_the_page(editor_user, monkeypatch):
    book = _book()
    StyleSheet.objects.update_or_create(book=book, defaults={"front_matter": {"cover": {"mode": "info"}}})

    def broken(*_args, **_kwargs):
        raise OSError("disk")

    monkeypatch.setattr("publishing.cover.cover_payload", broken)
    assert services.cached_cover(book) is None and services.cover_after_save(book, {}) is None
    _body, config = _page(logged(editor_user), book)
    assert config["initial"]["cover"] is None
