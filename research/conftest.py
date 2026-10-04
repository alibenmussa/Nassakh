"""Fixtures of the research suite: books of two organisations whose pages hold lines built from plain text.

A word written `word~` is a doubtful reading (`conf: low`, unresolved), `word^` a reviewer's (`res: typed`);
`make_page` gives every line and word a box on a gray page image saved to the test media folder.
"""

from __future__ import annotations

from django.contrib.auth.models import Group, User

import numpy as np
import pytest

from accounts.models import Organization
from accounts.testing import member, owned
from books.models import Book, Page
from core.storage import save_array
from ocr.models import Line
from processing.models import Preprocess, Region

W, H = 600, 900
LINE_H = 40


def tokens_of(text: str) -> list[dict]:
    """Tokens of a line: `word~` doubtful, `word^` resolved by a reviewer, the rest confident."""
    out = []
    for raw in text.split():
        word = raw.rstrip("~^")
        token = {"t": word, "alt": None, "tess": None, "conf": "high", "digit": False, "bbox": None}
        if raw.endswith("~"):
            token.update(conf="low", why=["disagree"], alt=word + "ا")
        elif raw.endswith("^"):
            token.update(res="typed")
        out.append(token)
    return out


def make_page(
    book: Book,
    number: int,
    body: list[str] = (),
    notes: list[str] = (),
    printed: str = "",
    reviewed: bool = False,
    image: bool = True,
) -> Page:
    """A page of `book` with body lines and footnote lines (each a string of words), boxes on a W×H image."""
    page = Page.objects.create(
        book=book,
        number=number,
        source_index=number - 1,
        status=Page.Status.REVIEWED if reviewed else Page.Status.OCR_DONE,
        text_state=Page.TextState.FINAL,
        width=W,
        height=H,
        printed_number=printed,
    )
    if image:
        pre = Preprocess.objects.create(page=page, output_width=W, output_height=H)
        gray = np.full((H, W), 235, dtype=np.uint8)
        gray[::LINE_H, :] = 30  # some ink
        save_array(pre.gray_image, gray, "gray.png")
        pre.save()
    body_region = Region.objects.create(page=page, kind="body", bbox=[20, 20, W - 20, 600], order=0)
    note_region = Region.objects.create(page=page, kind="footnote", bbox=[20, 620, W - 20, H - 20], order=1)
    order = 0
    for texts, region, top in ((body, body_region, 30), (notes, note_region, 630)):
        for n, text in enumerate(texts):
            y0 = top + n * LINE_H
            tokens = tokens_of(text)
            step = (W - 60) // max(1, len(tokens))
            for k, token in enumerate(tokens):  # right to left
                x1 = W - 30 - k * step
                token["bbox"] = [x1 - step + 6, y0 + 4, x1, y0 + LINE_H - 6]
            Line.objects.create(
                page=page,
                order=order,
                region=region,
                bbox=[30, y0, W - 30, y0 + LINE_H - 4],
                text=" ".join(t["t"] for t in tokens),
                ocr_text=" ".join(t["t"] for t in tokens),
                tokens=tokens,
                n_low=sum(1 for t in tokens if t["conf"] == "low"),
                is_reviewed=reviewed,
            )
            order += 1
    return page


def user_of(name: str, organization: Organization, role: str = "editor") -> User:
    person = User.objects.create_user(name, password="pass-1234", email=f"{name}@example.org")
    person.groups.add(Group.objects.get_or_create(name=role)[0])
    return member(person, organization)


# the texts of the books (a hadith collection with its editor's notes)
P1_BODY = [
    "حدثنا عبد الله بن يوسف قال أخبرنا مالك عن نافع",
    "عن عبد الله بن عمر أن رسول الله صلى الله عليه وسلم قال",
    "إِنَّمَا الأَعْمَالُ بِالنِّيَّاتِ وإنما لكل امرئ ما نوى",
]
P1_NOTES = [
    "(١) هذا الحديث رواه البخاري في أول صحيحه",
    "وهو من الأحاديث التي عليها مدار الإسلام",
]
P2_BODY = [
    "باب كيف كان بدء الوحي إلى رسول الله",
    "وقول الله جل ذكره إنا أوحينا إليك كما أوحينا",
]
P3_BODY = [
    "قال الشيخ رحمه الله في كتابه",
    "والعلم~ قبل القول والعمل",
]
P4_BODY = [
    "قال أبو عبد الله البخاري رحمه الله تعالى في آخر",
]
P5_BODY = [
    "وكان آخر ما قاله في هذا الباب أن الصبر",
]
P6_BODY = [
    "ضياء الصبر عند أول الصدمة وفيه بيان فضل الرضا",
]


@pytest.fixture
def org_a(db) -> Organization:
    return Organization.objects.create(name="دار الأولى")


@pytest.fixture
def org_b(db) -> Organization:
    return Organization.objects.create(name="دار الثانية")


@pytest.fixture
def reader(org_a) -> User:
    return user_of("reader", org_a)


@pytest.fixture
def stranger(org_b) -> User:
    return user_of("stranger", org_b)


@pytest.fixture
def library(org_a, org_b) -> dict:
    """Book A (org A): pages 1–6 (printed 11–16, page 5 without a number) — a hadith with a note, a chapter,
    a doubtful word, a sentence over a page break (5→6); another book of org A; book B (org B): a secret text
    that repeats page 1's hadith."""
    book = owned(
        Book.objects.create(title="مختصر صحيح البخاري", author="ابن أبي جمرة", original_year=1950), org_a
    )
    pages = [
        make_page(book, 1, P1_BODY, P1_NOTES, printed="11"),
        make_page(book, 2, P2_BODY, printed="12"),
        make_page(book, 3, P3_BODY, printed="13"),
        make_page(book, 4, P4_BODY, printed="14"),
        make_page(book, 5, P5_BODY, printed=""),
        make_page(book, 6, P6_BODY, printed="16"),
    ]
    other = owned(Book.objects.create(title="كتاب آخر", author="مؤلف آخر"), org_a)
    other_pages = [make_page(other, 1, ["وجدنا هذه العبارة النادرة في كتاب آخر فقط"])]
    secret = owned(Book.objects.create(title="كتاب سري", author="مؤلف سري"), org_b)
    secret_pages = [
        make_page(secret, 1, ["إنما الأعمال بالنيات وإنما لكل امرئ ما نوى", "سر مكتوم لا يراه غير أهله"])
    ]
    return {
        "book": book,
        "pages": pages,
        "other": other,
        "other_pages": other_pages,
        "secret": secret,
        "secret_pages": secret_pages,
    }
