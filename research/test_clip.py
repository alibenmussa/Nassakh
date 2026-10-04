"""Page clips (D107, `research.clips`): signed links served without a session, expiry, the highlight, the
cache, and tokens that cannot be forged for another page."""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core import signing
from django.test import Client

import pytest
from PIL import Image

from ocr.models import Line
from research import clips

pytestmark = pytest.mark.django_db


@pytest.fixture
def page(library):
    return library["pages"][2]  # «والعلم~» is doubtful on its second line


def _token(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def test_a_clip_is_served_without_a_session_and_highlights_its_lines(page):
    lines = list(Line.objects.filter(page=page).order_by("order"))
    url = clips.clip_url(page.pk, [lines[1].pk], [(lines[1].pk, 0)])
    response = Client().get(url)  # nobody signed in
    assert response.status_code == 200 and response["Content-Type"] == "image/webp"
    image = Image.open(io.BytesIO(response.content)).convert("RGB")
    pixels = list(image.getdata())
    yellow = sum(1 for r, g, b in pixels if r > 200 and g > 180 and b < 190)
    amber = sum(1 for r, g, b in pixels if r > 180 and 80 < g < 150 and b < 60)
    assert yellow > 500  # the line's highlight
    assert amber > 20  # the doubtful word's underline
    assert image.width <= clips.MAX_WIDTH


def test_the_clip_is_cached_on_disk_under_the_books_media(page):
    line = Line.objects.filter(page=page).first()
    payload = clips.read_token(_token(clips.clip_url(page.pk, [line.pk])))
    data = clips.clip_bytes(payload)
    cached = list((Path(settings.MEDIA_ROOT) / "books" / str(page.book_id) / "clips").glob("*.webp"))
    assert data in [path.read_bytes() for path in cached]
    assert clips.clip_bytes(payload) == data  # the second time from the cache


def test_an_expired_link_answers_410_and_a_forged_one_404(page, settings):
    line = Line.objects.filter(page=page).first()
    token = clips.make_token(page.pk, [line.pk])
    settings.NASSAKH = {**settings.NASSAKH, "CLIP_LINK_DAYS": 0}
    assert Client().get(f"/research/clip/{token}/").status_code == 410
    settings.NASSAKH = {**settings.NASSAKH, "CLIP_LINK_DAYS": 7}
    assert Client().get(f"/research/clip/{token}/").status_code == 200
    forged = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")
    assert Client().get(f"/research/clip/{forged}/").status_code == 404


def test_a_token_cannot_be_moved_to_another_page(page, library):
    line = Line.objects.filter(page=page).first()
    secret_page = library["secret_pages"][0]
    secret_line = Line.objects.filter(page=secret_page).first()
    # a payload naming the other organisation's page, signed with another key: refused
    forged = signing.dumps(
        {"p": secret_page.pk, "l": [secret_line.pk], "w": []}, key="not-the-key", salt=clips.SALT
    )
    assert Client().get(f"/research/clip/{forged}/").status_code == 404
    # a genuine token's lines are looked up on its own page only
    mixed = clips.make_token(page.pk, [secret_line.pk])
    assert Client().get(f"/research/clip/{mixed}/").status_code == 404
    assert Client().get(f"/research/clip/{clips.make_token(page.pk, [line.pk])}/").status_code == 200


def test_the_link_lasts_clip_link_days():
    token = clips.make_token(1, [2])
    assert clips.read_token(token)["p"] == 1
    with pytest.raises(signing.SignatureExpired):
        signing.loads(token, salt=clips.SALT, max_age=timedelta(seconds=-1))
