"""Manuscript view (Phase 4, PHASE4_SPEC §4): the rendered templates for every state (Django test client on
a small real book), the renderer's contract and escaping (`assembly.render`), the compiled CSS, and — under
Node with a tiny DOM, a fragment parser and a selector engine — the `manuscriptView` Alpine component
(polling to done and the reveal, the seam override with the scroll anchor, the source drawer and the focus
return, suggestions, the block menu, the keyboard map) plus the dashboard's manuscript logic in books.js."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

from django.contrib.auth.models import Group, User
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse

import pytest

from assembly import render, services
from assembly.models import AssemblyRun
from books.models import Book, Page
from ocr.models import Line
from processing.models import Preprocess, Region
from review import services as review_services

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "src" / "js"
CSS = ROOT / "static" / "dist" / "app.css"
FONTS = ROOT / "static" / "fonts" / "amiri"
W, H = 1000, 1500


# ---------------------------------------------------------------- a small real book


def _user(name: str, role: str | None) -> User:
    user = User.objects.create_user(name, password="pass-1234")
    if role:
        user.groups.add(Group.objects.get_or_create(name=role)[0])
    return user


def _logged(user) -> Client:
    client = Client()
    client.force_login(user)
    return client


def _tok(word: str, conf: str = "high") -> dict:
    return {"t": word, "alt": None, "tess": None, "conf": conf, "digit": False, "bbox": None, "res": None}


class Factory:
    """Pages with preprocess, regions and lines (boxes in gray pixels), like the backend's tests."""

    def __init__(self, title="كتاب المخطوطة"):
        self.book = Book.objects.create(title=title, author="المؤلف", status=Book.Status.REVIEWING)
        self.regions: dict[tuple[int, str], Region] = {}

    def page(self, number: int, status=Page.Status.REVIEWED) -> Page:
        page = Page.objects.create(
            book=self.book,
            number=number,
            source_index=number - 1,
            status=status,
            text_state=Page.TextState.FINAL,
            width=W,
            height=H,
            printed_number=str(number + 10),
        )
        Preprocess.objects.create(page=page, output_width=W, output_height=H)
        for order, kind in enumerate(("body", "footnote")):
            self.regions[(page.pk, kind)] = Region.objects.create(
                page=page, kind=kind, bbox=[0, 0, W, H], order=order
            )
        return page

    def line(self, page: Page, text: str, edges=(100, 900), kind="body", role="body", low=()) -> Line:
        order = page.lines.count()
        y = 100 + order * 40
        tokens = [_tok(word, "low" if i in low else "high") for i, word in enumerate(text.split())]
        return Line.objects.create(
            page=page,
            order=order,
            region=self.regions[(page.pk, kind)],
            bbox=[edges[0], y, edges[1], y + 30],
            text=text,
            ocr_text=text,
            tokens=tokens,
            n_low=len(low),
            role=role,
        )


def _book() -> tuple[Factory, list[Page]]:
    """Page 1 (reviewed): a heading, a paragraph cut by the page break, a note; page 2 (unreviewed)."""
    f = Factory()
    one = f.page(1)
    f.line(one, "الفصل الأول", (300, 700), role="heading")
    f.line(one, "نص الفقرة (١) يبدأ هنا", (100, 850))
    f.line(one, "ويستمر حتى آخر", (100, 900))
    f.line(one, "(١) حاشية الصفحة الأولى", kind="footnote")
    two = f.page(2, Page.Status.OCR_DONE)
    f.line(two, "الصفحة ويتم الكلام.", (100, 900), low=[1])
    f.line(two, "فقرة ثانية", (100, 850))
    return f, [one, two]


@pytest.fixture
def editor(db):
    return _user("editor", "editor")


@pytest.fixture
def proofreader(db):
    return _user("reader", "proofreader")


def _json_script(body: str, element_id: str):
    match = re.search(rf'<script id="{element_id}" type="application/json">(.*?)</script>', body, re.S)
    assert match, element_id
    return json.loads(match.group(1))


def _view(client, book) -> str:
    return client.get(reverse("assembly:manuscript", args=[book.pk])).content.decode()


# ---------------------------------------------------------------- the view: every state


def test_manuscript_view_never_assembled_shows_the_empty_state_with_options(editor, proofreader):
    f, _ = _book()
    body = _view(_logged(editor), f.book)
    assert "<title>المخطوطة · كتاب المخطوطة · نسّاخ</title>" in body
    config = _json_script(body, "manuscript-config")
    assert config["state"]["exists"] is False and config["canEdit"] is True and config["canReview"] is True
    assert config["pageCount"] == 2 and config["urls"]["review"].endswith("/review/__n__/")
    assert config["urls"]["document"] == reverse("assembly:document", args=[f.book.pk])
    assert config["urls"]["sheets"] == reverse("api:book_sheets", args=[f.book.pk])
    # the empty state: one sentence, the options (segmented numbering, the two checkboxes), the action
    assert "x-show=\"phase === 'empty'\"" in body and "لم يُجمَّع هذا الكتاب بعد" in body
    assert "تُوصل الفقرات عبر الصفحات" in body
    assert body.count('role="radiogroup"') >= 1 and "حسب الفصل" in body and "حسب الكتاب" in body
    assert "تضمين الصفحات غير المُراجَعة" in body and "حذف التطويل" in body
    assert '@click="submitConvert()"' in body and "تحويل إلى كتاب" in body
    # the document host is empty and cloaked, the layout too; the top bar has the popover for editors
    assert 'x-ref="host" x-ignore data-ms-host' in body and "<article" not in body
    assert 'class="menu ms-convert"' in body and "manuscriptBar" in body and "src/js/manuscript.js" in body
    # a proofreader sees no options and no convert: the editor starts the conversion
    body = _view(_logged(proofreader), f.book)
    assert "يبدأ التحويل محرّر الكتاب" in body and 'class="menu ms-convert"' not in body
    assert "v.primary === 'convert'" not in body and "v.primary === 'copy'" in body
    assert _json_script(body, "manuscript-config")["canEdit"] is False
    # the proofreader may still set roles (the review permission)
    assert _json_script(body, "manuscript-config")["canReview"] is True


def test_manuscript_view_assembling_state_shows_the_skeleton_and_the_steps(editor):
    f, _ = _book()
    AssemblyRun.objects.create(book=f.book, status=AssemblyRun.Status.RUNNING, stage="footnotes")
    body = _view(_logged(editor), f.book)
    config = _json_script(body, "manuscript-config")
    assert config["state"]["active"] is True and config["state"]["run"]["stage"] == "footnotes"
    # the skeleton is not cloaked (the reveal starts on the first paint), the empty state is
    assert re.search(
        r'class="ms-skeleton" x-show="phase === \'assembling\' && !hasDocument" aria-hidden', body
    )
    assert re.search(r'class="empty-state ms-empty" x-show="phase === \'empty\'" x-cloak', body)
    assert 'class="ms-steps" x-show="phase === \'assembling\'" aria-label="خطوات التجميع"' in body
    assert 'x-for="s in steps"' in body and 'class="ms-pill"' in body and "قيد التجميع" not in body
    # the toolbar and the layout are shown (no x-cloak) while assembling
    assert re.search(r'<div class="ms-toolbar" x-show="phase !== \'empty\'">', body)


def test_manuscript_view_ready_renders_the_document_the_panel_and_the_chrome(editor):
    f, (one, two) = _book()
    services.start_assembly(f.book, editor)  # eager: done
    body = _view(_logged(editor), f.book)
    config = _json_script(body, "manuscript-config")
    assert config["state"]["exists"] is True and config["state"]["stale"] is False
    assert config["countsText"] == "صفحتان · فصل واحد · حاشية واحدة"
    # the document inside the host (no Alpine bindings inside), the panel parts and the meta
    host = body[body.index("data-ms-host") : body.index('class="ms-tools"')]
    assert (
        '<article class="ms-doc" dir="rtl">' in host
        and "x-" not in host.split("<article")[1].split("</article>")[0]
    )
    assert '<h2 class="ms-h1 ms-block" id="b-h' in host and 'data-reviewed="true"' in host
    assert 'data-reviewed="false" tabindex="0" title="من صفحة لم تُراجَع بعد"' in host
    assert '<span class="ms-seam" role="button" tabindex="0" data-page="2"' in host
    assert '<button type="button" class="ms-ref" id="ref-n' in host and '<ol class="ms-notes"' in host
    assert '<mark class="ms-uncertain">' in host
    assert (
        'data-ms-part="toc"' in host and 'data-ms-part="warnings"' in host and 'data-ms-part="stats"' in host
    )
    assert 'data-goto="h' in host and "الفصل الأول" in host
    assert (
        'data-code="page_unreviewed"' in host
        and f'href="{reverse("review:page", args=[f.book.pk, 2])}"' in host
    )
    assert ">انتقال</button>" in host and ">مراجعة</a>" in host
    meta = _json_script(body, "ms-meta")
    assert meta["version"] == 1 and meta["countsText"] == config["countsText"]
    assert [w["code"] for w in meta["warnings"]] == ["page_unreviewed", "uncertain_words"]
    assert meta["toc"][0]["text"] == "الفصل الأول" and meta["seams"][0]["mode"] == "join"
    # the chrome: toolbar (seams segmented, counts, jump with G), tabs, stats host, drawer, sheet, bar
    assert "data-seam-toggle" in body and 'title="إظهار فواصل الصفحات (S)"' in body
    assert 'placeholder="إلى صفحة…"' in body and '<kbd class="kbd" aria-hidden="true">G</kbd>' in body
    assert 'role="tablist" aria-label="لوحة المخطوطة"' in body and "المحتويات" in body
    assert (
        'class="ms-drawer" role="dialog"' in body
        and 'x-ref="drawerClose"' in body
        and "فتح في المراجعة" in body
    )
    assert 'class="ms-card" role="group"' in body and 'class="ms-note-pop" role="tooltip"' in body
    assert 'class="menu ms-block-menu" role="menu"' in body and "عرض الأصل" in body and "نوع الفقرة" in body
    assert "x-show=\"v.primary === 'reassemble'\"" in body and "x-show=\"v.primary === 'copy'\"" in body
    assert "خيارات التجميع…" in body and "نسخ نص المخطوطة" in body and "لوحة الكتاب" in body
    assert 'x-text="v.pill.text"' in body and "اختصارات لوحة المفاتيح" in body
    # the fragment endpoint serves the same parts on its own
    fragment = _logged(editor).get(reverse("assembly:document", args=[f.book.pk])).content.decode()
    assert fragment.startswith('<article class="ms-doc"') and 'id="ms-meta"' in fragment
    assert 'data-ms-part="stats"' in fragment and "<html" not in fragment
    empty = Factory("فارغ")
    assert _logged(editor).get(reverse("assembly:document", args=[empty.book.pk])).status_code == 404


def test_manuscript_view_stale_and_failed_states(editor):
    f, (one, two) = _book()
    services.start_assembly(f.book, editor)
    # a review edit after the run: stale, with the page in the banner's links
    review_services.set_line_role(one.lines.get(text="ويستمر حتى آخر"), "subheading", editor)
    body = _view(_logged(editor), f.book)
    state = _json_script(body, "manuscript-config")["state"]
    assert state["stale"] is True and state["stale_pages"] == [1]
    assert 'class="banner ms-stale" x-show="stale"' in body and 'x-text="staleText"' in body
    assert ':href="reviewUrl(n)"' in body and "إعادة التجميع" in body
    # a failed run after a manuscript: the document stays, the banner offers a retry
    AssemblyRun.objects.create(
        book=f.book, status=AssemblyRun.Status.ERROR, error="تعذّر تجميع المخطوطة؛ بقيت."
    )
    body = _view(_logged(editor), f.book)
    state = _json_script(body, "manuscript-config")["state"]
    assert state["run"]["status"] == "error" and state["exists"] is True
    assert '<article class="ms-doc"' in body and 'x-show="failed && hasDocument"' in body
    assert "بقيت المخطوطة السابقة كما هي" in body and "إعادة المحاولة" in body
    # a failed run with no manuscript: the designed error state, not cloaked
    g = Factory("كتاب متعطّل")
    AssemblyRun.objects.create(book=g.book, status=AssemblyRun.Status.ERROR, error="تعذّر التجميع.\nboom")
    body = _view(_logged(editor), g.book)
    assert re.search(
        r'class="empty-state ms-state-error" x-show="phase === \'error\' && !hasDocument" role="alert"', body
    )
    assert _json_script(body, "manuscript-config")["state"]["run"]["error"] == "تعذّر التجميع."
    assert "لم تُكتب مخطوطة" in body and 'x-text="errorHeadline"' in body
    assert Client().get(reverse("assembly:manuscript", args=[g.book.pk])).status_code == 302


def test_dashboard_carries_the_manuscript_primary_menu_popover_and_side_block(editor, proofreader):
    f, _ = _book()
    body = _logged(editor).get(reverse("books:detail", args=[f.book.pk])).content.decode()
    for state in ("convert", "reassemble", "manuscript"):
        assert f"x-show=\"d.primary === '{state}'\"" in body, state
    assert "تحويل إلى كتاب" in body and "إعادة التجميع" in body and "فتح المخطوطة" in body
    menu = body[
        body.index('class="menu menu-popover bk-menu"') : body.index("</template>", body.index("bk-menu"))
    ]
    assert "تحويل إلى كتاب…" in menu and "فتح المخطوطة" in menu and "نسخ نص الكتاب" in menu
    assert 'class="bk-convert-host"' in body and 'class="menu ms-convert" role="dialog"' in body
    assert 'x-text="ms.convert.unreviewed"' in body and '@click="ms.submitConvert()"' in body
    side = body[body.index('<aside class="bk-side"') :]
    assert 'class="bk-manuscript" role="group" aria-label="المخطوطة"' in side
    assert (
        side.index('class="bk-summary-review"')
        < side.index('class="bk-manuscript"')
        < side.index("bk-attention")
    )
    assert 'x-text="manuscriptLine"' in side and "فتح المخطوطة" in side and "تحويل إلى كتاب…" in side
    config = _json_script(body, "dashboard-config")
    assert config["manuscript"]["exists"] is False and config["manuscriptUrls"]["page"].endswith(
        "/manuscript/"
    )
    # a proofreader: no convert, no popover, the open link stays
    body = _logged(proofreader).get(reverse("books:detail", args=[f.book.pk])).content.decode()
    assert (
        "d.primary === 'convert'" not in body
        and "ms-convert" not in body
        and "d.primary === 'manuscript'" in body
    )


# ---------------------------------------------------------------- the renderer (§4.4)


def _doc(**overrides) -> dict:
    """A synthetic document with every node kind: a chapter, a join, a split, a missing seam, notes."""
    doc = {
        "type": "doc",
        "attrs": {
            "bookId": 1,
            "runId": 3,
            "assembledAt": "2026-09-24T10:00:00+00:00",
            "footnoteNumbering": "chapter",
            "digitStyle": "western",
            "seams": [
                {"page": 2, "from_page": 1, "mode": "join", "decision": "auto", "reason": "geometry"},
                {"page": 3, "from_page": 2, "mode": "split", "decision": "override", "reason": "geometry"},
                {
                    "page": 5,
                    "from_page": 3,
                    "mode": "missing",
                    "decision": "auto",
                    "reason": "skipped_page",
                    "skipped": [4],
                },
            ],
        },
        "content": [
            {"type": "title", "attrs": {"text": "كتاب <الاختبار>", "author": "المؤلف & شريكه"}},
            {
                "type": "heading",
                "attrs": {"level": 1, "id": "h1", "sourcePages": [1], "sourceLineIds": [1], "reviewed": True},
                "content": [{"type": "text", "text": "الفصل الأول"}],
            },
            {
                "type": "paragraph",
                "attrs": {
                    "id": "p10",
                    "sourcePages": [1, 2],
                    "sourceLineIds": [10, 11, 20],
                    "reviewed": True,
                    "suggestedRole": None,
                },
                "content": [
                    {"type": "text", "text": "قال "},
                    {"type": "text", "text": "الأمير", "marks": [{"type": "uncertain"}]},
                    {"type": "text", "text": " في سنة 1966"},
                    {
                        "type": "footnote",
                        "attrs": {
                            "id": "n30",
                            "number": 1,
                            "marker": "١",
                            "sourcePage": 1,
                            "sourceLineIds": [30],
                            "orphan": False,
                        },
                        "content": [{"type": "text", "text": "انظر <script>alert(1)</script> المصدر"}],
                    },
                    {"type": "text", "text": " ثم"},
                    {"type": "pageBreak", "attrs": {"page": 2, "printed": "12"}},
                    {"type": "text", "text": " تابع الكلام."},
                ],
            },
            {
                "type": "paragraph",
                "attrs": {
                    "id": "p40",
                    "sourcePages": [3],
                    "sourceLineIds": [40],
                    "reviewed": False,
                    "suggestedRole": "heading",
                },
                "content": [{"type": "text", "text": "مقدمة"}],
            },
            {
                "type": "paragraph",
                "attrs": {
                    "id": "p50",
                    "sourcePages": [3],
                    "sourceLineIds": [50, 51],
                    "reviewed": False,
                    "suggestedRole": None,
                },
                "content": [
                    {"type": "text", "text": "نص يذكر (7) مرات"},
                    {
                        "type": "footnote",
                        "attrs": {
                            "id": "n60",
                            "number": 2,
                            "marker": "229",
                            "sourcePage": 3,
                            "sourceLineIds": [60],
                            "orphan": True,
                        },
                        "content": [{"type": "text", "text": "حاشية يتيمة"}],
                    },
                ],
            },
            {
                "type": "heading",
                "attrs": {
                    "level": 2,
                    "id": "h70",
                    "sourcePages": [5],
                    "sourceLineIds": [70],
                    "reviewed": True,
                },
                "content": [{"type": "text", "text": "مبحث"}],
            },
            {
                "type": "paragraph",
                "attrs": {
                    "id": "p80",
                    "sourcePages": [5],
                    "sourceLineIds": [80],
                    "reviewed": True,
                    "suggestedRole": None,
                },
                "content": [{"type": "text", "text": "الخاتمة."}],
            },
        ],
    }
    doc.update(overrides)
    return doc


WARNINGS = [
    {
        "code": "no_headings",
        "severity": "info",
        "page": None,
        "blockId": None,
        "lineIds": [],
        "message": "لا عنوان.",
    },
    {
        "code": "page_unreviewed",
        "severity": "warning",
        "page": 3,
        "blockId": "p40",
        "lineIds": [40],
        "message": "الصفحة 3 لم تُراجَع بعد.",
    },
    {
        "code": "marker_unmatched",
        "severity": "warning",
        "page": 3,
        "blockId": "p50",
        "lineIds": [50],
        "message": "علامة الحاشية «7» في الصفحة 3 بلا حاشية مقابلة.",
    },
    {
        "code": "note_orphan",
        "severity": "warning",
        "page": 3,
        "blockId": "p50",
        "lineIds": [60],
        "message": "الحاشية «229» في الصفحة 3 بلا علامة في المتن.",
    },
    {
        "code": "uncertain_words",
        "severity": "info",
        "page": 1,
        "blockId": "p10",
        "lineIds": [10],
        "message": "بقيت كلمات غير محسومة في الصفحة 1 (1).",
    },
]


def test_render_document_contract_escaping_seams_notes_and_marks():
    html = render.render_document(_doc(), warnings=WARNINGS)
    # escaping: the title, the author and the note text never carry markup
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "كتاب &lt;الاختبار&gt;" in html and "المؤلف &amp; شريكه" in html
    # the contract (§4.4)
    assert html.startswith('<article class="ms-doc" dir="rtl"><header class="ms-title">')
    assert '<h1 class="ms-title-text">' in html and '<p class="ms-title-author">' in html
    assert html.count('<section class="ms-chapter"') == 1 and 'data-chapter="0" data-heading="h1"' in html
    assert (
        '<h2 class="ms-h1 ms-block" id="b-h1" data-block="h1" data-pages="1" data-src="1" data-lines="1"'
        ' data-reviewed="true" tabindex="0">' in html
    )
    assert (
        '<p class="ms-p ms-block" id="b-p10" data-block="p10" data-pages="1,2" data-src="1–2"'
        ' data-lines="10,11,20" data-reviewed="true" tabindex="0">' in html
    )
    assert '<h3 class="ms-h2 ms-block" id="b-h70"' in html
    assert 'data-reviewed="false" data-suggested="heading" tabindex="0" title="من صفحة لم تُراجَع بعد">' in html
    assert '<mark class="ms-uncertain">الأمير</mark>' in html
    # inline join marker with the page number; the split and the missing seam between blocks
    seam = re.search(r'<span class="ms-seam" role="button" tabindex="0" ([^>]*)><bdi>2</bdi></span>', html)
    assert (
        seam
        and 'data-page="2" data-from="1" data-mode="join" data-decision="auto" data-reason="geometry"'
        in seam.group(1)
    )
    assert 'aria-label="وُصلت الفقرة بين الصفحتين 1 و2"' in seam.group(1)
    split = re.search(
        r'<div class="ms-split" data-page="3" data-from="2" data-mode="split" data-decision="override"[^>]*>'
        r"(.*?)</div>",
        html,
    )
    assert (
        split
        and 'class="ms-seam ms-seam-split ms-split-page" role="button" tabindex="0" data-page="3"'
        in split.group(1)
    )
    assert "ص <bdi>3</bdi>" in split.group(1)
    assert html.index('<div class="ms-split" data-page="3"') < html.index('id="b-p40"')
    missing = re.search(
        r'<div class="ms-split is-missing" role="note" data-page="5"[^>]*data-mode="missing"[^>]*'
        r'data-skipped="4">(.*?)</div>',
        html,
    )
    assert missing and "صفحة <bdi>4</bdi> غير مُضمَّنة" in missing.group(1)
    assert html.index('data-mode="missing"') < html.index('id="b-h70"')
    # references and the chapter's notes list, the orphan tinted and flagged, back links
    assert (
        '<button type="button" class="ms-ref" id="ref-n30" data-note="n30" data-number="1" data-page="1"'
        ' data-orphan="false" aria-label="الحاشية 1" aria-controls="note-n30"><bdi>1</bdi></button>' in html
    )
    assert (
        'class="ms-ref is-orphan" id="ref-n60"' in html
        and 'data-orphan="true" aria-label="الحاشية 2 (بلا علامة في المتن)"' in html
    )
    notes = re.search(r'<ol class="ms-notes" aria-label="الحواشي">(.*?)</ol>', html)
    assert notes and notes.group(1).count('<li class="ms-note"') == 2
    assert (
        '<li class="ms-note" id="note-n30" data-note="n30" data-number="1" data-page="1" data-orphan="false">'
        in html
    )
    assert '<a class="ms-note-num" href="#ref-n30" data-ref="n30"' in html
    assert (
        '<span class="ms-note-flag">بلا علامة في المتن · العلامة المطبوعة «229» · ص <bdi>3</bdi></span>'
        in html
    )
    assert html.index("</ol>") < html.index("</section>")  # the notes close the chapter
    # the suggestion chip and the unmatched marker's tint
    assert (
        'class="ms-suggest-btn" data-suggest="accept" data-block="p40"' in html
        and 'data-suggest="dismiss" data-block="p40"' in html
    )
    assert '<span class="ms-unmatched">(7)</span>' in html and 'data-unmatched="7"' in html
    # notes modes
    book = render.render_document(_doc(), notes="book")
    assert book.count('<ol class="ms-notes"') == 1 and book.index("</section>") < book.index(
        '<ol class="ms-notes"'
    )
    assert '<ol class="ms-notes"' not in render.render_document(_doc(), notes="none")
    with pytest.raises(ValueError):
        render.render_document(_doc(), notes="page")
    # an empty or missing document renders an empty article; a chapter opens for front matter
    assert render.render_document(None) == '<article class="ms-doc" dir="rtl"></article>'
    front = _doc()
    front["content"] = [front["content"][2]]
    assert '<section class="ms-chapter" data-chapter="0"><p' in render.render_document(front)


def test_render_view_models_outline_warnings_stats_counts():
    doc = _doc()
    assert render.outline(doc) == [
        {
            "id": "h1",
            "level": 1,
            "text": "الفصل الأول",
            "page": 1,
            "children": [{"id": "h70", "level": 2, "text": "مبحث", "page": 5, "children": []}],
        }
    ]
    groups = render.group_warnings(WARNINGS)
    assert [(g["code"], g["count"], g["severity"]) for g in groups] == [
        ("marker_unmatched", 1, "warning"),
        ("note_orphan", 1, "warning"),
        ("page_unreviewed", 1, "warning"),
        ("uncertain_words", 1, "info"),
        ("no_headings", 1, "info"),
    ]
    assert groups[0]["label"] == "علامات بلا حاشية" and groups[0]["items"][0]["index"] == 2
    flat = render.flat_warnings(WARNINGS)
    assert (
        [w["index"] for w in flat] == [0, 1, 2, 3, 4]
        and flat[0]["page"] is None
        and flat[1]["blockId"] == "p40"
    )
    assert render.ar_count(1, render.PAGES) == "صفحة واحدة" and render.ar_count(2, render.PAGES) == "صفحتان"
    assert render.ar_count(5, render.PAGES) == "5 صفحات" and render.ar_count(214, render.PAGES) == "214 صفحة"
    assert (
        render.ar_count(103, render.PAGES) == "103 صفحات" and render.ar_count(0, render.NOTES) == "0 ملاحظة"
    )
    stats = {
        "pages_included": 214,
        "pages_skipped": 0,
        "pages_unreviewed": 3,
        "headings": 40,
        "chapters": 38,
        "paragraphs": 900,
        "footnotes": 612,
        "joins": 120,
        "words": 90000,
    }
    assert render.counts_line(stats) == "214 صفحة · 38 فصلًا · 612 حاشية"
    assert (
        render.counts_line({"pages_included": 3, "paragraphs": 12, "footnotes": 0})
        == "3 صفحات · 12 فقرة · بلا حواشٍ"
    )
    rows = render.stats_rows(stats)
    assert [r["key"] for r in rows] == [
        "pages_included",
        "pages_unreviewed",
        "chapters",
        "headings",
        "paragraphs",
        "footnotes",
        "joins",
        "words",
    ]
    assert (
        render.pages_label([8, 9]) == "8–9"
        and render.pages_label([12]) == "12"
        and render.pages_label([]) == ""
    )
    ctx = render.fragment_context(
        {"document": doc, "warnings": WARNINGS, "stats": stats, "seams": doc["attrs"]["seams"], "version": 4}
    )
    assert (
        ctx["has_document"]
        and ctx["version"] == 4
        and ctx["meta"]["assembledAt"] == "2026-09-24T10:00:00+00:00"
    )
    assert (
        ctx["meta"]["countsText"] == ctx["counts_text"]
        and len(ctx["meta"]["warnings"]) == 5
        and ctx["warnings_total"] == 5
    )
    assert render.fragment_context(None)["has_document"] is False


def test_compiled_css_font_and_static_states():
    css = CSS.read_text(encoding="utf-8")
    assert css.count("font-family:Amiri;") == 2 and "/static/fonts/amiri/Amiri-Regular.ttf" in css
    assert re.search(
        r"\.ms-doc\{[^}]*font-family:var\(--ms-font\)[^}]*text-align:justify[^}]*font-size:19px;line-height:1\.9\}",
        css,
    )
    assert re.search(r"\.ms-h1\{[^}]*font-size:26px", css) and re.search(r"\.ms-h2\{[^}]*font-size:20px", css)
    assert re.search(r"\.ms-p\{[^}]*text-indent:1\.5em", css) and ".ms-chapter{content-visibility:auto" in css
    assert re.search(r"\.ms-column\{[^}]*max-width:calc\(720px \+ var\(--ms-gutter\)\)", css)
    assert re.search(r"\.ms-drawer\{[^}]*width:clamp\(360px,42vw,760px\)", css)
    assert re.search(r"\.ms-uncertain\{[^}]*border-bottom:1\.5px solid var\(--color-warning\)", css)
    assert re.search(r"\.ms-seam\[data-decision=\"?override\"?\]:after\{[^}]*border-radius", css)
    assert "@keyframes ms-stitch{" in css and "@keyframes ms-rise{" in css and "@keyframes ms-flash{" in css
    reduced = css[css.index("@media (prefers-reduced-motion:reduce){.ms-host") :]
    for needle in (
        ".ms-host.is-reveal .ms-block.is-rise",
        ".ms-seam.is-stitch:before",
        ".ms-block.is-flash",
        ".ms-skeleton span",
        ".ms-drawer",
    ):
        assert needle in reduced[:900], needle
    assert re.search(r"@media \(max-width:900px\)\{[^@]*\.ms-side\{[^}]*order:-1", css)
    # the vendored face and its licence
    assert (FONTS / "Amiri-Regular.ttf").stat().st_size > 500_000 and (
        FONTS / "Amiri-Bold.ttf"
    ).stat().st_size > 500_000
    licence = (FONTS / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL Open Font License, Version 1.1" in licence and "Reserved Font Name Amiri" in licence
    src = (ROOT / "static" / "src" / "app.css").read_text(encoding="utf-8")
    assert '@import "./components/manuscript.css";' in src
    assert "src=\"{% static 'src/js/manuscript.js' %}\"" in (ROOT / "templates" / "base.html").read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------- the component under Node

HARNESS = r"""
// A tiny DOM with a fragment parser and a selector engine: enough for manuscript.js, which only walks,
// queries, moves and classes elements. Rects come from a table by block id so anchors can be checked.
const ENT = { '&lt;': '<', '&gt;': '>', '&amp;': '&', '&quot;': '"', '&#x27;': "'", '&#39;': "'" };
const decode = (s) => String(s).replace(/&(lt|gt|amp|quot|#x27|#39);/g, (m) => ENT[m]);
const encode = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
class ClassList { constructor(){ this.s = new Set(); } add(...c){ c.forEach(x => this.s.add(x)); } remove(...c){ c.forEach(x => this.s.delete(x)); }
  toggle(c, f){ if (f === undefined) f = !this.s.has(c); f ? this.s.add(c) : this.s.delete(c); return f; } contains(c){ return this.s.has(c); } toString(){ return [...this.s].join(' '); } }
class Node { constructor(){ this.childNodes = []; this.parentNode = null; }
  appendChild(n){ if (n instanceof Fragment) { n.childNodes.slice().forEach((c) => this.appendChild(c)); return n; } if (n.parentNode) n.parentNode.removeChild(n); n.parentNode = this; this.childNodes.push(n); return n; }
  removeChild(n){ const i = this.childNodes.indexOf(n); if (i >= 0) this.childNodes.splice(i, 1); n.parentNode = null; return n; }
  remove(){ if (this.parentNode) this.parentNode.removeChild(this); }
  get children(){ return this.childNodes.filter((n) => n instanceof Element); }
  get firstElementChild(){ return this.children[0] || null; }
  all(){ const out = []; const walk = (n) => n.childNodes.forEach((c) => { if (c instanceof Element) { out.push(c); walk(c); } }); walk(this); return out; }
  querySelectorAll(sel){ return this.all().filter((el) => el.matches(sel)); }
  querySelector(sel){ return this.querySelectorAll(sel)[0] || null; }
  get textContent(){ return this.childNodes.map((n) => n.textContent).join(''); }
  set textContent(v){ this.childNodes.forEach((n) => { n.parentNode = null; }); this.childNodes = v ? [new Text(String(v))] : []; }
  get innerHTML(){ return this.childNodes.map((n) => n.outerHTML).join(''); }
  set innerHTML(html){ this.textContent = ''; parseInto(this, html); }
  contains(n){ while (n) { if (n === this) return true; n = n.parentNode; } return false; }
}
class Fragment extends Node {}
class Text extends Node { constructor(d){ super(); this.nodeValue = d; } get textContent(){ return this.nodeValue; } get outerHTML(){ return encode(this.nodeValue); } }
const RECTS = {}; const focused = []; const scrolled = [];
class Element extends Node {
  constructor(tag){ super(); this.tagName = tag.toUpperCase(); this.attrs = {}; this.classList = new ClassList(); this.dataset = {}; this.hidden = false; this.handlers = {};
    this.style = { setProperty: (k, v) => { this.style[k] = v; }, getPropertyValue: (k) => this.style[k] || '' }; }
  setAttribute(k, v){ this.attrs[k] = String(v); if (k === 'class') { this.classList = new ClassList(); String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c)); }
    if (k.startsWith('data-')) this.dataset[k.slice(5).replace(/-(\w)/g, (m, c) => c.toUpperCase())] = String(v); }
  getAttribute(k){ if (k === 'class') return this.classList.toString(); return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k){ delete this.attrs[k]; } hasAttribute(k){ return k in this.attrs; }
  get id(){ return this.attrs.id || ''; } set id(v){ this.attrs.id = v; }
  get className(){ return this.classList.toString(); } set className(v){ this.setAttribute('class', v); }
  get outerHTML(){ const cls = this.classList.toString(); const attrs = Object.entries(this.attrs).map(([k, v]) => ` ${k}="${encode(v)}"`).join('');
    return `<${this.tagName.toLowerCase()}${cls ? ` class="${cls}"` : ''}${attrs}>${this.innerHTML}</${this.tagName.toLowerCase()}>`; }
  matchesCompound(c){
    const m = /^([a-zA-Z0-9-]*)(.*)$/.exec(c); if (m[1] && m[1].toUpperCase() !== this.tagName) return false;
    const rest = m[2]; const re = /\.([\w-]+)|#([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]|:not\(([^)]*)\)/g; let x;
    while ((x = re.exec(rest))) {
      if (x[1] !== undefined && !this.classList.contains(x[1])) return false;
      if (x[2] !== undefined && this.id !== x[2]) return false;
      if (x[3] !== undefined) { if (!(x[3] in this.attrs)) return false; if (x[4] !== undefined && this.attrs[x[3]] !== x[4]) return false; }
      if (x[5] !== undefined && this.matchesCompound(x[5])) return false;
    }
    return true;
  }
  matches(sel){ return sel.split(',').some((one) => { const parts = one.trim().split(/\s+/); if (!this.matchesCompound(parts[parts.length - 1])) return false;
    let el = this.parentNode; for (let i = parts.length - 2; i >= 0; i -= 1) { while (el && !(el instanceof Element && el.matchesCompound(parts[i]))) el = el.parentNode; if (!el) return false; el = el.parentNode; } return true; }); }
  closest(sel){ let el = this; while (el) { if (el instanceof Element && el.matches(sel)) return el; el = el.parentNode; } return null; }
  getBoundingClientRect(){ const id = this.attrs['data-block'] || this.attrs['data-page'] || this.attrs['data-rect'] || ''; const r = RECTS[`${id}@${this.attrs['data-lines'] || ''}`] || RECTS[id] || { top: 0, left: 0, width: 100, height: 20 };
    return { top: r.top, left: r.left, width: r.width, height: r.height, bottom: r.top + r.height, right: r.left + r.width }; }
  focus(){ globalThis.document.activeElement = this; focused.push(this.attrs['data-block'] || this.attrs['data-note'] || this.attrs['data-ref'] || this.attrs['x-ref'] || this.attrs['data-page'] || this.tagName); }
  blur(){} scrollIntoView(opts){ scrolled.push([this.attrs['data-block'] || this.attrs['data-note'] || this.tagName, opts && opts.block]); }
  addEventListener(ev, fn){ this.handlers[ev] = fn; } removeEventListener(ev){ delete this.handlers[ev]; }
}
const VOID = new Set(['br', 'img', 'input', 'use', 'path', 'meta', 'link']);
function parseInto(root, html) {
  const re = /<!--[\s\S]*?-->|<\/([a-zA-Z0-9]+)\s*>|<([a-zA-Z0-9]+)((?:\s+[^\s=>\/]+(?:="[^"]*")?)*)\s*(\/?)>|([^<]+)/g;
  const stack = [root]; let m;
  while ((m = re.exec(html))) {
    if (m[1]) { if (stack.length > 1) stack.pop(); continue; }
    if (m[2]) { const el = new Element(m[2]); const ar = /([^\s=]+)(?:="([^"]*)")?/g; let a; while ((a = ar.exec(m[3] || ''))) el.setAttribute(a[1], a[2] === undefined ? '' : decode(a[2]));
      stack[stack.length - 1].appendChild(el); if (!m[4] && !VOID.has(m[2].toLowerCase())) stack.push(el); continue; }
    if (m[5] !== undefined) { const t = m[5]; if (t.trim() || stack.length > 1) stack[stack.length - 1].appendChild(new Text(decode(t))); }
  }
}
const parse = (html) => { const f = new Fragment(); parseInto(f, html); return f; };
const reg = {}; const inits = []; const stores = {}; const calls = []; const timers = []; const scrolls = []; const assigned = [];
globalThis.window = globalThis;
globalThis.document = { hidden: false, activeElement: null, createElement: (t) => new Element(t), createTextNode: (d) => new Text(d),
  addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); }, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: (n, v) => { if (v !== undefined) stores[n] = v; return stores[n]; } };
globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; }; globalThis.clearTimeout = () => {};
globalThis.setInterval = () => 1; globalThis.clearInterval = () => {};
globalThis.scrollBy = (x, y) => scrolls.push(y);
globalThis.innerHeight = 800; globalThis.innerWidth = 1200;
globalThis.location = { assign: (u) => assigned.push(u), pathname: '/books/1/manuscript/', search: '', hash: '' };
const store = {}; globalThis.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
globalThis.Nassakh = { toast: (m) => calls.push(['toast', m]), copyText: async (t) => { calls.push(['copy', t]); return true; } };
const fs = require('fs');
const fixture = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
eval(fs.readFileSync(process.argv[2], 'utf8'));
inits.forEach((fn) => fn());
NassakhManuscript.parseFragment = parse;
const clone = (v) => JSON.parse(JSON.stringify(v));
const flush = () => new Promise((r) => setImmediate(r));
const posts = () => calls.filter((x) => x[0] === 'POST');
const reqs = () => calls.filter((x) => x[0] === 'GET').map((x) => x[1]);
// fetch routes: the state answers in sequence, the document the current fragment, sheets the page, posts 202
let stateQueue = []; let fragment = fixture.fragment_v1; let sheet = fixture.sheet; let stateGate = null;
const deferred = () => { let release; const done = new Promise((r) => { release = r; }); return { release, done }; };
globalThis.fetch = async (url, init) => {
  const method = (init && init.method) || 'GET';
  calls.push([method, url, init && init.body ? JSON.parse(init.body) : null, init && init.headers]);
  if (method === 'POST') return { ok: true, status: 202, json: async () => ({ run_id: 9, status: 'done', stage: 'save', manuscript_url: '/books/1/manuscript/', state_url: '/api/books/1/manuscript/state/' }) };
  if (url.startsWith('/api/books/1/manuscript/state/')) { if (stateGate) await stateGate.done; const s = stateQueue.length > 1 ? stateQueue.shift() : stateQueue[0]; return { ok: true, status: 200, json: async () => clone(s) }; }
  if (url.startsWith('/books/1/manuscript/document/')) return { ok: true, status: 200, text: async () => fragment, json: async () => null };
  if (url.startsWith('/api/books/1/sheets/')) return { ok: true, status: 200, json: async () => ({ pages: [clone(sheet)], book_line_h_px: 30 }) };
  return { ok: false, status: 404, json: async () => ({ detail: 'لا' }) };
};
const page = (fragmentHtml) => {
  const root = parse(`<div data-manuscript><div class="ms-column"><div class="ms-host" data-ms-host>${fragmentHtml}</div></div><aside><div data-ms-toc-host></div><div data-ms-warnings-host></div><div data-ms-stats-host></div></aside></div>`).children[0];
  return root;
};
const make = (state, fragmentHtml) => {
  const root = page(fragmentHtml);
  const c = reg.manuscriptView(clone({ ...fixture.config, state }));
  c.$el = root; c.$nextTick = (fn) => fn();
  c.$refs = { jump: { focus: () => focused.push('jump'), select: () => {} }, drawerClose: new Element('button'), toolsBtn: new Element('button'), menu: root, sheetClose: new Element('button') };
  c.$refs.drawerClose.setAttribute('x-ref', 'drawerClose'); c.$refs.toolsBtn.setAttribute('data-rect', 'tools');
  c.init();
  return { c, root };
};
const ids = (root) => root.querySelectorAll('.ms-block').map((b) => b.getAttribute('data-block'));
(async () => {
  const out = {};
  // ---- ready: the fragment is indexed, the panel parts adopted, the meta read
  const { c, root } = make(fixture.state_ready, fixture.fragment_v1);
  out.ready = { phase: c.phase, hasDocument: c.hasDocument, version: c.loadedVersion, blocks: c.blockCount(), counts: c.countsText, warnings: c.warningsTotal,
    toc: root.querySelector('[data-ms-toc-host] [data-ms-part="toc"]') !== null, warnHost: root.querySelectorAll('[data-ms-warnings-host] .ms-warn').length,
    stats: root.querySelector('[data-ms-stats-host] .ms-stat[data-stat="footnotes"] bdi').textContent, metaLeft: root.querySelector('[data-ms-host] script') === null,
    pill: c.pill, primary: c.primary, page3: c.firstBlockOfPage(3), page2: c.firstBlockOfPage(2), page4: c.firstBlockOfPage(4), steps: c.steps.length };
  // ---- keyboard map (pure) and dispatch
  const ka = NassakhManuscript.keyAction; const base = { inField: false, inMenu: false, hasBlock: false };
  out.keys = { g: ka({ key: 'g' }, base), S: ka({ key: 'S', code: 'KeyS' }, base), o: ka({ key: 'o' }, base), c: ka({ key: 'c' }, base), j: ka({ key: 'j' }, base), k: ka({ key: 'K' }, base),
    down: ka({ key: 'ArrowDown' }, base), downBlock: ka({ key: 'ArrowDown' }, { ...base, hasBlock: true }), up: ka({ key: 'ArrowUp' }, { ...base, hasBlock: true }), next: ka({ key: ']' }, base), prev: ka({ key: '[' }, base),
    esc: ka({ key: 'Escape' }, base), escField: ka({ key: 'Escape' }, { ...base, inField: true }), gField: ka({ key: 'g' }, { ...base, inField: true }), meta: ka({ key: 'g', metaKey: true }, base),
    jMenu: ka({ key: 'j' }, { ...base, inMenu: true }), sheet: ka({ key: '?' }, base), arabic: ka({ key: 'ل' }, base) };
  const key = (k, target) => { let prevented = false; c.onKey({ key: k, target: target || { tagName: 'BODY', closest: () => null }, preventDefault: () => { prevented = true; } }); return prevented; };
  key('g'); out.jumpFocused = focused.slice(-1)[0];
  key('s'); out.seamsOff = { seams: c.seams, stored: store['nassakh.manuscript.seams'] }; key('s');
  key('j'); key('j'); out.moveFocus = { focused: c.focused, active: document.activeElement.getAttribute('data-block'), tools: c.tools.blockId, toolsClass: root.querySelector('[data-block="p10"]').classList.contains('is-tools') };
  key('k'); out.moveBack = c.focused;
  key('ArrowDown'); out.arrowDown = c.focused;
  // ---- warnings: ] steps through the notes after the focused block, the row is marked, the block focused
  c.focusBlock('h1');
  key(']'); out.warn1 = { cursor: c.warningCursor(), tab: c.tab, focused: c.focused, current: root.querySelector('.ms-warn.is-current') && root.querySelector('.ms-warn.is-current').getAttribute('data-warn') };
  key(']'); out.warn2 = { cursor: c.warningCursor(), focused: c.focused };
  key('['); out.warn3 = c.warningCursor();
  // ---- the seam card and the override: click on the join marker posts split, the anchor is its block
  const seam2 = root.querySelector('.ms-seam[data-page="2"]');
  c.showCard(seam2); out.card = { open: c.card.open, text: c.card.text, action: c.card.action, label: c.card.actionLabel, decision: c.card.decision };
  const seam3 = root.querySelector('.ms-seam[data-page="3"]');
  out.cardSplit = c.seamCard(seam3);
  // rects by block (and by its lines, so the re-rendered p10 lands 60 px lower after the split: the anchor's shift)
  Object.assign(RECTS, { h1: { top: -300, left: 0, width: 600, height: 40 }, p10: { top: 120, left: 0, width: 600, height: 200 }, 'p10@10,11': { top: 180, left: 0, width: 600, height: 120 }, p40: { top: 340, left: 0, width: 600, height: 40 }, p50: { top: 400, left: 0, width: 600, height: 80 }, h70: { top: 500, left: 0, width: 600, height: 40 }, p80: { top: 560, left: 0, width: 600, height: 60 } });
  stateQueue = [fixture.state_ready_v2]; fragment = fixture.fragment_v2; calls.length = 0; scrolls.length = 0;
  stateGate = deferred(); // the 202 landed, the state poll is on the wire: the run shows as active meanwhile
  c.onHostClick({ target: seam2, preventDefault: () => {} });
  await flush();
  out.seamPost = { post: posts()[0].slice(1, 3), busy: c.busy, phase: c.phase, active: c.active, pill: c.pill.text, csrf: 'X-CSRFToken' in (posts()[0][3] || {}) };
  stateGate.release(); stateGate = null;
  await flush(); await flush(); await flush();
  // the swap: the new block p20 is in, p10 kept its id, the scroll moved by the anchor's shift, changed blocks flash
  out.afterSeam = { ids: ids(root), version: c.loadedVersion, busy: c.busy, phase: c.phase, reqs: reqs(), scrolls, flashing: c.flashingIds().sort(), seam2: root.querySelector('.ms-seam[data-page="2"]').getAttribute('data-mode'),
    decision: root.querySelector('.ms-seam[data-page="2"]').getAttribute('data-decision'), live: c.liveMessage, counts: c.countsText, focused: c.focused };
  // ---- the block menu and the roles post: the block's line ids, the anchor around it
  c.openMenu('p40'); out.menu = { open: c.menu.open, src: c.menu.src, role: c.menu.role, reviewed: c.menu.reviewed, reviewUrl: c.menu.reviewUrl, lines: c.menu.lines };
  calls.length = 0; stateQueue = [fixture.state_ready_v2]; fragment = fixture.fragment_v2;
  const rp = c.setRole('p40', 'heading'); out.roleMenuClosed = c.menu.open; await rp; await flush(); await flush(); await flush();
  out.rolePost = { post: posts()[0].slice(1, 3), busy: c.busy };
  calls.length = 0; out.sameRole = await c.setRole('p40', 'body') === false && posts().length === 0;
  // ---- suggestions: ✓ posts the heading role, × the dismissal
  calls.length = 0;
  c.onHostClick({ target: root.querySelector('.ms-suggest-btn[data-suggest="accept"]'), preventDefault: () => {} }); await flush(); await flush(); await flush();
  const acceptPost = posts()[0]; calls.length = 0;
  c.onHostClick({ target: root.querySelector('.ms-suggest-btn[data-suggest="dismiss"]'), preventDefault: () => {} }); await flush(); await flush(); await flush();
  out.suggest = { accept: acceptPost.slice(1, 3), dismiss: posts()[0].slice(1, 3) };
  // ---- the source drawer: opened from the focused block with O, the page loaded from api:book_sheets, the block's
  // lines highlighted, Esc closes it and the focus returns to the block
  c.focusBlock('p10'); focused.length = 0; calls.length = 0;
  key('o'); await flush(); await flush();
  out.drawer = { open: c.drawer.open, pages: c.drawer.pages, page: c.drawerPage, lines: c.drawer.lines, loading: c.drawer.loading, req: reqs()[0], boxes: c.drawerBoxes().map((b) => b.id), focus: focused[0], aspect: c.drawerAspect, box: c.boxStyle([0.1, 0.2, 0.9, 0.25]) };
  c.drawerStep(1); out.drawerStep = { index: c.drawer.index, page: c.drawerPage, canBack: c.drawer.index > 0 };
  focused.length = 0; key('Escape');
  out.drawerClosed = { open: c.drawer.open, focus: focused[0] };
  out.noFocusSource = (c.focused = null, await c.openSource(null), calls.filter((x) => x[0] === 'toast').pop()[1]);
  // ---- jump, copy, notes
  calls.length = 0; scrolled.length = 0;
  out.jump = { ok: c.jump('٣'), scrolled: scrolled[0], bad: c.jump('9'), toast: calls.filter((x) => x[0] === 'toast').pop()[1] };
  await c.copyText(); out.copy = calls.filter((x) => x[0] === 'copy').pop()[1];
  const ref = root.querySelector('.ms-ref[data-note="n30"]');
  c.showNote(ref); out.note = { open: c.notePop.open, number: c.notePop.number, html: c.notePop.html, hot: root.querySelector('.ms-note[data-note="n30"]').classList.contains('is-hot') };
  focused.length = 0; c.onHostClick({ target: ref, preventDefault: () => {} }); out.noteJump = { focus: focused[0], popClosed: !c.notePop.open };
  c.onHostClick({ target: root.querySelector('.ms-note-num[data-ref="n30"]'), preventDefault: () => {} }); out.noteBack = focused.slice(-1)[0];
  // ---- Esc closes the top layer only: menu, then the card
  c.openMenu('p10'); c.showCard(root.querySelector('.ms-seam[data-page="3"]'));
  out.escLayers = [c.closeTop(), c.closeTop(), c.closeTop()];
  // ---- helpers
  out.rel = [NassakhManuscript.relativeTime('2026-09-24T10:00:00Z', Date.parse('2026-09-24T10:00:20Z')), NassakhManuscript.relativeTime('2026-09-24T10:00:00Z', Date.parse('2026-09-24T10:05:00Z')),
    NassakhManuscript.relativeTime('2026-09-24T10:00:00Z', Date.parse('2026-09-24T12:00:00Z')), NassakhManuscript.relativeTime('2026-09-24T10:00:00Z', Date.parse('2026-09-27T10:00:00Z')),
    NassakhManuscript.relativeTime('2026-08-01T10:00:00Z', Date.parse('2026-09-27T10:00:00Z')), NassakhManuscript.relativeTime('', 0)];
  out.arCount = [NassakhManuscript.arCount(1, ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة']), NassakhManuscript.arCount(2, ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة']), NassakhManuscript.arCount(4, ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة']), NassakhManuscript.arCount(214, ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة'])];
  // ---- polling to done: assembling with no document, the steps tick, the fragment arrives and rises in
  calls.length = 0; timers.length = 0;
  const a = make(fixture.state_queued, ''); // init schedules the first poll
  out.assembling = { phase: a.c.phase, hasDocument: a.c.hasDocument, pill: a.c.pill, primary: a.c.primary, steps: a.c.steps.map((s) => [s.done, s.current]), label0: a.c.steps[0].label, pollScheduled: timers.filter((t) => t.ms === 700).length };
  stateQueue = [fixture.state_running_footnotes, fixture.state_ready];
  await a.c.poll(); out.running = { stage: a.c.run.stage, steps: a.c.steps.map((s) => (s.done ? 'done' : s.current ? 'current' : '')), pill: a.c.pill.text, again: timers.filter((t) => t.ms === 700).length };
  timers.length = 0; fragment = fixture.fragment_v1;
  await a.c.poll(); await flush(); await flush(); await flush();
  out.done = { phase: a.c.phase, hasDocument: a.c.hasDocument, blocks: a.c.blockCount(), reveal: a.c.reveal, rise: a.root.querySelectorAll('.ms-block.is-rise').length, i1: a.root.querySelector('[data-block="p10"]').style['--i'],
    stitch: a.root.querySelectorAll('.ms-seam.is-stitch').length, pill: a.c.pill.state, live: a.c.liveMessage, polling: timers.filter((t) => t.ms === 700).length, toc: a.root.querySelectorAll('[data-ms-toc-host] .ms-toc-link').length, primary: a.c.primary };
  // ---- a failed run with no document: the error state; with a document the old one stays
  const e = make(fixture.state_queued, ''); stateQueue = [fixture.state_failed]; await e.c.poll();
  out.failedEmpty = { phase: e.c.phase, pill: e.c.pill, headline: e.c.errorHeadline, primary: e.c.primary };
  const e2 = make(fixture.state_ready, fixture.fragment_v1); stateQueue = [fixture.state_failed_with_doc]; e2.c.state = { ...e2.c.state, active: true }; await e2.c.poll();
  out.failedDoc = { phase: e2.c.phase, hasDocument: e2.c.hasDocument, failed: e2.c.failed, pill: e2.c.pill.state, primary: e2.c.primary, blocks: e2.c.blockCount() };
  // ---- stale: the banner text and the pill
  const st = make(fixture.state_stale, fixture.fragment_v1);
  out.stale = { stale: st.c.stale, text: st.c.staleText, pages: st.c.stalePages, pill: st.c.pill, primary: st.c.primary, url: st.c.reviewUrl(4) };
  // ---- a proofreader: no override posts, roles allowed
  const pr = make(fixture.state_ready, fixture.fragment_v1); pr.c.canEdit = false; calls.length = 0;
  out.proofreader = { seam: await pr.c.postSeam(2, 'split'), dismiss: await pr.c.dismissSuggestion('p40'), posts: posts().length, primary: pr.c.primary };
  // ---- the convert popover: options from the state, submit posts assemble and the run begins
  const cv = make(fixture.state_empty, ''); calls.length = 0;
  cv.c.openConvert(); out.convertOpen = { open: cv.c.convert.open, options: clone(cv.c.convert.options), unreviewed: cv.c.convert.unreviewed, label: cv.c.convert.label, phase: cv.c.phase, primary: cv.c.primary };
  cv.c.convert.options.footnote_numbering = 'book'; stateQueue = [fixture.state_queued];
  await cv.c.submitConvert(); await flush();
  out.convertSubmit = { post: posts()[0].slice(1, 3), open: cv.c.convert.open, phase: cv.c.phase, active: cv.c.active };
  // ---- the manuscript text of the fixture
  out.text = NassakhManuscript.manuscriptText(page(fixture.fragment_v1).querySelector('article'));
  out.textV2 = NassakhManuscript.manuscriptText(page(fixture.fragment_v2).querySelector('article'));
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


def _fragment(doc: dict, warnings: list, stats: dict, version: int) -> str:
    payload = {
        "document": doc,
        "warnings": warnings,
        "stats": stats,
        "seams": doc["attrs"]["seams"],
        "version": version,
    }
    return render_to_string(
        "assembly/_document.html", {"book": SimpleNamespace(pk=1), **render.fragment_context(payload)}
    )


def _state(**overrides) -> dict:
    base = {
        "exists": True,
        "version": 1,
        "assembled_at": "2026-09-24T10:00:00+00:00",
        "run": {"id": 3, "status": "done", "stage": "save", "error": ""},
        "active": False,
        "stale": False,
        "stale_pages": [],
        "warnings_count": 5,
        "stats": {"pages_included": 4, "chapters": 1, "footnotes": 2, "paragraphs": 4},
        "options": {"footnote_numbering": "chapter", "include_unreviewed": True, "strip_tatweel": True},
        "unreviewed_pages": 1,
    }
    base.update(overrides)
    return base


def test_manuscript_component_under_node(tmp_path):
    stats = {
        "pages_included": 4,
        "pages_skipped": 1,
        "pages_unreviewed": 1,
        "headings": 2,
        "chapters": 1,
        "paragraphs": 4,
        "footnotes": 2,
        "joins": 1,
        "words": 30,
    }
    doc_v1 = _doc()
    # v2: the seam of page 2 split by an override, so p10 loses page 2 and a new block p20 appears
    doc_v2 = _doc()
    doc_v2["attrs"]["seams"][0] = {
        "page": 2,
        "from_page": 1,
        "mode": "split",
        "decision": "override",
        "reason": "geometry",
    }
    p10 = doc_v2["content"][2]
    p10["attrs"]["sourcePages"] = [1]
    p10["attrs"]["sourceLineIds"] = [10, 11]
    p10["content"] = p10["content"][:5]
    doc_v2["content"].insert(
        3,
        {
            "type": "paragraph",
            "attrs": {
                "id": "p20",
                "sourcePages": [2],
                "sourceLineIds": [20],
                "reviewed": True,
                "suggestedRole": None,
            },
            "content": [{"type": "text", "text": "تابع الكلام."}],
        },
    )
    stats_v2 = dict(stats, joins=0, paragraphs=5)
    fixture = {
        "config": {
            "bookId": 1,
            "title": "كتاب",
            "canEdit": True,
            "canReview": True,
            "pageCount": 5,
            "countsText": "",
            "urls": {
                "assemble": "/api/books/1/assemble/",
                "state": "/api/books/1/manuscript/state/",
                "data": "/api/books/1/manuscript/",
                "seams": "/api/books/1/manuscript/seams/",
                "roles": "/api/books/1/manuscript/roles/",
                "suggestions": "/api/books/1/manuscript/suggestions/",
                "page": "/books/1/manuscript/",
                "document": "/books/1/manuscript/document/",
                "sheets": "/api/books/1/sheets/",
                "review": "/books/1/review/__n__/",
                "dashboard": "/books/1/",
            },
        },
        "fragment_v1": _fragment(doc_v1, WARNINGS, stats, 1),
        "fragment_v2": _fragment(doc_v2, WARNINGS, stats_v2, 2),
        "sheet": {
            "id": 7,
            "number": 1,
            "width": 1000,
            "height": 1500,
            "display_url": "/media/p1.webp",
            "scan_url": "/media/p1.png",
            "lines": [
                {"id": 10, "bbox": [0.1, 0.1, 0.9, 0.13]},
                {"id": 11, "bbox": [0.1, 0.14, 0.9, 0.17]},
                {"id": 12, "bbox": [0.1, 0.2, 0.9, 0.23]},
                {"id": 30, "bbox": [0.1, 0.9, 0.9, 0.93]},
            ],
        },
        "state_ready": _state(),
        "state_ready_v2": _state(version=2, run={"id": 9, "status": "done", "stage": "save", "error": ""}),
        "state_empty": _state(
            exists=False,
            version=0,
            assembled_at=None,
            run=None,
            warnings_count=0,
            stats={},
            unreviewed_pages=2,
        ),
        "state_queued": _state(
            exists=False,
            version=0,
            assembled_at=None,
            run={"id": 4, "status": "queued", "stage": "", "error": ""},
            active=True,
        ),
        "state_running_footnotes": _state(
            exists=False,
            version=0,
            assembled_at=None,
            run={"id": 4, "status": "running", "stage": "footnotes", "error": ""},
            active=True,
        ),
        "state_failed": _state(
            exists=False,
            version=0,
            assembled_at=None,
            run={
                "id": 4,
                "status": "error",
                "stage": "seams",
                "error": "تعذّر تجميع المخطوطة؛ بقيت المخطوطة السابقة كما هي.",
            },
        ),
        "state_failed_with_doc": _state(
            run={"id": 5, "status": "error", "stage": "save", "error": "تعذّر الحفظ."}
        ),
        "state_stale": _state(stale=True, stale_pages=[2, 4]),
    }
    (tmp_path / "fixture.json").write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    run = subprocess.run(
        ["node", str(harness), str(JS / "manuscript.js"), str(tmp_path / "fixture.json")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])

    # ready: indexed, parts adopted, meta read, the pill and the primary
    assert (
        out["ready"]["phase"] == "ready"
        and out["ready"]["hasDocument"] is True
        and out["ready"]["version"] == 1
    )
    assert (
        out["ready"]["blocks"] == 6
        and out["ready"]["counts"] == "4 صفحات · فصل واحد · حاشيتان"
        and out["ready"]["warnings"] == 5
    )
    assert (
        out["ready"]["toc"] is True
        and out["ready"]["warnHost"] == 5
        and out["ready"]["stats"] == "2"
        and out["ready"]["metaLeft"] is True
    )
    assert (
        out["ready"]["pill"]["state"] == "saved"
        and out["ready"]["pill"]["text"].startswith("مُجمَّعة ")
        and out["ready"]["primary"] == "copy"
    )
    assert (
        out["ready"]["page3"] == "p40"
        and out["ready"]["page2"] == "p10"
        and out["ready"]["page4"] is None
        and out["ready"]["steps"] == 7
    )
    # the keyboard map
    assert out["keys"] == {
        "g": "jump",
        "S": "seams",
        "o": "source",
        "c": "copy",
        "j": "next",
        "k": "prev",
        "down": None,
        "downBlock": "next",
        "up": "prev",
        "next": "nextWarning",
        "prev": "prevWarning",
        "esc": "close",
        "escField": "blur",
        "gField": None,
        "meta": None,
        "jMenu": None,
        "sheet": "sheet",
        "arabic": None,
    }
    assert out["jumpFocused"] == "jump" and out["seamsOff"] == {"seams": False, "stored": "0"}
    assert out["moveFocus"] == {"focused": "p10", "active": "p10", "tools": "p10", "toolsClass": True}
    assert out["moveBack"] == "h1" and out["arrowDown"] == "p10"
    # warnings: ] after h1 lands on the warning of the nearest later block in document order (p10's uncertain
    # words, index 4 in the stored list); the next ] continues through the list, [ steps back
    assert out["warn1"] == {"cursor": 4, "tab": "notes", "focused": "p10", "current": "4"}
    assert out["warn2"] == {"cursor": 0, "focused": "p10"} and out["warn3"] == 4
    # the seam card and the override
    assert out["card"] == {
        "open": True,
        "text": "وُصلت الفقرة بين الصفحتين 1 و2",
        "action": "split",
        "label": "فصل هنا",
        "decision": "auto",
    }
    assert (
        out["cardSplit"]["text"] == "فاصل بين الصفحتين 2 و3 · قرار يدوي"
        and out["cardSplit"]["actionLabel"] == "وصل بما قبلها"
    )
    assert out["seamPost"] == {
        "post": ["/api/books/1/manuscript/seams/", {"page": 2, "mode": "split"}],
        "busy": True,
        "phase": "assembling",
        "active": True,
        "pill": "قيد التجميع…",
        "csrf": True,
    }
    after = out["afterSeam"]
    assert (
        after["ids"] == ["h1", "p10", "p20", "p40", "p50", "h70", "p80"]
        and after["version"] == 2
        and after["busy"] is False
        and after["phase"] == "ready"
    )
    assert after["reqs"] == ["/api/books/1/manuscript/state/", "/books/1/manuscript/document/"]
    assert after["scrolls"] == [60]  # p10 moved from top 120 to 180: the window scrolls by the shift
    assert (
        after["flashing"] == ["p10", "p20"] and after["seam2"] == "split" and after["decision"] == "override"
    )
    assert (
        after["live"] == "حُدّثت المخطوطة"
        and after["counts"] == "4 صفحات · فصل واحد · حاشيتان"
        and after["focused"] == "p10"
    )
    # the block menu and the roles post
    assert out["menu"] == {
        "open": True,
        "src": "3",
        "role": "body",
        "reviewed": False,
        "reviewUrl": "/books/1/review/3/",
        "lines": [40],
    }
    assert out["roleMenuClosed"] is False and out["rolePost"] == {
        "post": ["/api/books/1/manuscript/roles/", {"line_ids": [40], "role": "heading"}],
        "busy": False,
    }
    assert out["sameRole"] is True
    assert out["suggest"] == {
        "accept": ["/api/books/1/manuscript/roles/", {"line_ids": [40], "role": "heading"}],
        "dismiss": ["/api/books/1/manuscript/suggestions/", {"block_id": "p40", "action": "dismiss"}],
    }
    # the drawer
    d = out["drawer"]
    assert (
        d["open"] is True
        and d["pages"] == [1]
        and d["page"] == 1
        and d["lines"] == [10, 11]
        and d["loading"] is False
    )
    assert (
        d["req"] == "/api/books/1/sheets/?from=1&to=1"
        and d["boxes"] == [10, 11]
        and d["focus"] == "drawerClose"
    )
    assert d["aspect"] == "0.6667" and d["box"] == "left:10.00%;top:20.00%;width:80.00%;height:5.00%"
    assert out["drawerStep"] == {"index": 0, "page": 1, "canBack": False}
    assert (
        out["drawerClosed"] == {"open": False, "focus": "p10"}
        and out["noFocusSource"] == "حدّد فقرة أولًا (J / K)"
    )
    # jump, copy, notes, layers
    assert out["jump"] == {
        "ok": 3,
        "scrolled": ["p40", "center"],
        "bad": None,
        "toast": "لا نصّ من هذه الصفحة في المخطوطة",
    }
    assert (
        out["copy"] == out["textV2"]
    )  # the copy ran after the split: «ثم» and «تابع الكلام.» are two blocks
    assert "1966[1] ثم\n\nتابع الكلام.\n\n" in out["textV2"]
    assert out["text"] == (
        "كتاب <الاختبار>\n\nالمؤلف & شريكه\n\nالفصل الأول\n\n"
        "قال الأمير في سنة 1966[1] ثم تابع الكلام.\n\nمقدمة\n\n"
        "نص يذكر (7) مرات[2]\n\nمبحث\n\nالخاتمة.\n\n"
        "[1] انظر <script>alert(1)</script> المصدر\n\n[2] حاشية يتيمة"
    )
    assert out["note"] == {
        "open": True,
        "number": "1",
        "html": "انظر &lt;script&gt;alert(1)&lt;/script&gt; المصدر",
        "hot": True,
    }
    assert out["noteJump"] == {"focus": "n30", "popClosed": True} and out["noteBack"] == "n30"
    assert out["escLayers"] == ["menu", "card", None]
    assert out["rel"] == ["قبل لحظات", "قبل 5 دقائق", "قبل ساعتين", "قبل 3 أيام", "في 2026-08-01", ""]
    assert out["arCount"] == ["صفحة واحدة", "صفحتان", "4 صفحات", "214 صفحة"]
    # polling to done: the steps tick, then the reveal
    a = out["assembling"]
    assert (
        a["phase"] == "assembling"
        and a["hasDocument"] is False
        and a["pill"] == {"state": "saving", "text": "قيد التجميع…"}
        and a["primary"] == ""
    )
    assert (
        a["steps"] == [[False, False]] * 7
        and a["label0"] == "جمع الأسطر من 5 صفحات"
        and a["pollScheduled"] == 1
    )
    assert out["running"] == {
        "stage": "footnotes",
        "steps": ["done", "done", "done", "current", "", "", ""],
        "pill": "قيد التجميع…",
        "again": 2,
    }
    done = out["done"]
    assert (
        done["phase"] == "ready"
        and done["hasDocument"] is True
        and done["blocks"] == 6
        and done["reveal"] is True
    )
    assert (
        done["rise"] == 6
        and done["i1"] == "1"
        and done["stitch"] == 1
        and done["pill"] == "saved"
        and done["live"] == "اكتمل التجميع"
    )
    assert done["polling"] == 0 and done["toc"] == 2 and done["primary"] == "copy"
    # failures, stale, permissions, the convert popover
    assert (
        out["failedEmpty"]["phase"] == "error"
        and out["failedEmpty"]["pill"] == {"state": "error", "text": "فشل التجميع"}
        and out["failedEmpty"]["primary"] == "reassemble"
    )
    assert out["failedEmpty"]["headline"].startswith("تعذّر تجميع المخطوطة")
    assert out["failedDoc"] == {
        "phase": "ready",
        "hasDocument": True,
        "failed": True,
        "pill": "error",
        "primary": "reassemble",
        "blocks": 6,
    }
    assert out["stale"] == {
        "stale": True,
        "text": "تغيّر نص صفحتان بعد التجميع:",
        "pages": [2, 4],
        "pill": {"state": "warn", "text": "تغيّر النص بعد التجميع"},
        "primary": "reassemble",
        "url": "/books/1/review/4/",
    }
    assert out["proofreader"] == {"seam": False, "dismiss": False, "posts": 0, "primary": "copy"}
    assert out["convertOpen"] == {
        "open": True,
        "options": {"footnote_numbering": "chapter", "include_unreviewed": True, "strip_tatweel": True},
        "unreviewed": 2,
        "label": "تحويل",
        "phase": "empty",
        "primary": "convert",
    }
    assert out["convertSubmit"] == {
        "post": [
            "/api/books/1/assemble/",
            {"footnote_numbering": "book", "include_unreviewed": True, "strip_tatweel": True},
        ],
        "open": False,
        "phase": "assembling",
        "active": True,
    }


# ---------------------------------------------------------------- the dashboard's manuscript logic (books.js)

DASH_HARNESS = r"""
const reg = {}; const inits = []; const stores = {}; const calls = []; const assigned = [];
globalThis.window = globalThis;
globalThis.document = { hidden: false, addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); }, getElementById: () => null, querySelector: () => ({ content: 'tok' }), querySelectorAll: () => [] };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: (n, v) => { if (v !== undefined) stores[n] = v; return stores[n]; } };
globalThis.setTimeout = () => 1; globalThis.clearTimeout = () => {};
globalThis.location = { assign: (u) => assigned.push(u), hash: '' };
globalThis.addEventListener = () => {};
const store = {}; globalThis.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
globalThis.sessionStorage = { getItem: () => null, setItem: () => {} };
globalThis.Nassakh = { toast: (m) => calls.push(['toast', m]), copyText: async () => true };
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));
inits.forEach((fn) => fn());
const pages = [{ id: 1, number: 1, status: 'reviewed', text_state: 'final', is_reviewed: true }, { id: 2, number: 2, status: 'assembled', text_state: 'final', is_reviewed: true }];
const mk = (manuscript, extra = {}) => { const d = reg.bookDashboard({ progressUrl: '/p', bookUrl: '/books/1/', bookTextUrl: '/api/books/1/text/', canEdit: true, bookId: 1, active: false, status: 'reviewing',
  stages: [{ key: 'ocr_done', statuses: ['ocr_done', 'reviewed', 'assembled'] }], byStatus: { reviewed: 1, assembled: 1 }, pages, manuscript,
  manuscriptUrls: { assemble: '/api/books/1/assemble/', page: '/books/1/manuscript/', state: '/api/books/1/manuscript/state/' }, review: { reviewed: 2, total: 2, unresolved_total: 0, next_review_url: null }, ...extra });
  d.$watch = () => {}; d.init(); return d; };
const none = { exists: false, version: 0, assembled_at: null, run: null, active: false, stale: false, stale_pages: [], warnings_count: 0, stats: {}, options: { footnote_numbering: 'page', include_unreviewed: false, strip_tatweel: true }, unreviewed_pages: 0 };
const fresh = { ...none, exists: true, version: 2, run: { id: 3, status: 'done', stage: 'save', error: '' }, warnings_count: 3, stats: { pages_included: 214, chapters: 38, footnotes: 612 } };
const stale = { ...fresh, stale: true, stale_pages: [3, 7, 9, 12] };
const running = { ...none, active: true, run: { id: 4, status: 'running', stage: 'seams', error: '' } };
const failed = { ...fresh, run: { id: 5, status: 'error', stage: 'save', error: 'تعذّر' } };
(async () => {
  const out = {};
  const rows = [['none', none], ['fresh', fresh], ['stale', stale], ['running', running], ['failed', failed]].map(([name, m]) => { const d = mk(m); return [name, d.primary, d.manuscriptLine, d.manuscriptDot, d.hasManuscript]; });
  out.rows = rows;
  const reader = mk(fresh, { canEdit: false }); const readerNone = mk(none, { canEdit: false }); const readerStale = mk(stale, { canEdit: false });
  out.reader = [reader.primary, readerNone.primary, readerStale.primary];
  const d = mk(none);
  d.openConvert(); out.open = { open: d.convert.open, options: d.convert.options, label: d.convert.label };
  globalThis.fetch = async (url, init) => { calls.push([init.method, url, JSON.parse(init.body), init.headers['X-CSRFToken']]); return { ok: true, status: 202, json: async () => ({ run_id: 8, status: 'queued', stage: '', manuscript_url: '/books/1/manuscript/', state_url: '/s' }) }; };
  await d.submitConvert();
  out.submitted = { call: calls.filter((x) => x[0] === 'POST')[0], assigned: assigned.slice(), open: d.convert.open, active: d.manuscript.active, primary: d.primary };
  globalThis.fetch = async () => ({ ok: false, status: 403, json: async () => ({ detail: 'هذا الإجراء يتطلب صلاحية محرّر.' }) }); assigned.length = 0;
  const d2 = mk(stale); const ok = await d2.reassemble();
  out.refused = { ok, error: d2.convert.error, assigned: assigned.length, toast: calls.filter((x) => x[0] === 'toast').pop()[1] };
  const d3 = mk(none); d3.apply({ total: 2, percent: 100, flags: 0, status: 'reviewing', status_label: 'قيد المراجعة', dot: 'dot-accent', by_status: {}, active: false, pages: [], manuscript: fresh });
  out.polled = [d3.primary, d3.manuscriptLine];
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


def test_dashboard_manuscript_logic_under_node(tmp_path):
    harness = tmp_path / "dash.js"
    harness.write_text(DASH_HARNESS, encoding="utf-8")
    run = subprocess.run(
        ["node", str(harness), str(JS / "books.js")], capture_output=True, text=True, timeout=60
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["rows"] == [
        ["none", "convert", "لم تُجمَّع بعد", "dot-neutral", False],
        ["fresh", "manuscript", "مُجمَّعة · 214 صفحة · 3 ملاحظات", "dot-success", True],
        ["stale", "reassemble", "تغيّر نص 4 صفحات بعد التجميع", "dot-warning", True],
        ["running", "manuscript", "قيد التجميع", "dot-accent", True],
        ["failed", "reassemble", "فشل التجميع", "dot-danger", True],
    ]
    assert out["reader"] == ["manuscript", "copy", "manuscript"]
    assert out["open"] == {
        "open": True,
        "options": {"footnote_numbering": "page", "include_unreviewed": False, "strip_tatweel": True},
        "label": "تحويل",
    }
    assert out["submitted"]["call"] == [
        "POST",
        "/api/books/1/assemble/",
        {"footnote_numbering": "page", "include_unreviewed": False, "strip_tatweel": True},
        "tok",
    ]
    assert out["submitted"]["assigned"] == ["/books/1/manuscript/"] and out["submitted"]["open"] is False
    assert out["submitted"]["active"] is True and out["submitted"]["primary"] == "manuscript"
    assert out["refused"] == {
        "ok": False,
        "error": "هذا الإجراء يتطلب صلاحية محرّر.",
        "assigned": 0,
        "toast": "هذا الإجراء يتطلب صلاحية محرّر.",
    }
    assert out["polled"] == ["manuscript", "مُجمَّعة · 214 صفحة · 3 ملاحظات"]
