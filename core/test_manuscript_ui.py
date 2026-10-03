"""Manuscript view (Phase 4, PHASE4_SPEC §4): the rendered templates for every state (Django test client on
a small real book), the renderer's contract and escaping (`assembly.render`), the compiled CSS, and — under
Node with a tiny DOM, a fragment parser and a selector engine — the `manuscriptView` Alpine component
(polling to done and the reveal, the one overlay: placement against the visible column, one open at a time,
the outside click, scroll-away, the seam menu and its override with the scroll anchor and the focus restored
after the swap, the block menu, the footnote, the source drawer, suggestions, the keyboard map) plus the
dashboard's manuscript logic in books.js."""

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

from accounts.testing import member
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
    return member(user)  # the books' organisation (D102)


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
    assert '@click="submitConvert()"' in body and "تجميع المخطوطة" in body and "تحويل إلى كتاب" not in body
    assert (
        'x-text="NassakhBooks.convertLabel(convert)"' in body
    )  # D35: the button counts the unreviewed pages
    # the document host is empty, the layout cloaked; the top bar has the popover for editors
    assert '<div class="ms-host" x-ignore data-ms-host></div>' in body and "<article" not in body
    assert 'class="menu ms-convert"' in body and "manuscriptBar" in body and "src/js/manuscript.js" in body
    # a proofreader sees no options and no convert: the editor starts the conversion; the seam menu offers
    # the review link only
    body = _view(_logged(proofreader), f.book)
    assert "يجمع المخطوطةَ محرّرُ الكتاب." in body and 'class="menu ms-convert"' not in body
    assert "chooseSeam(" not in body and "فتح الصفحة" in body
    assert "v.primary === 'convert'" not in body and "v.primary === 'book'" in body
    assert "نسخ نص المخطوطة" not in body and "copyText" not in body  # D49: the book is exported, never copied
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
    assert 'class="ms-side-section ms-run" role="group" aria-label="خطوات التجميع"' in body
    assert 'aria-label="خطوات التجميع" x-show="phase === \'assembling\'">' in body
    assert '<div class="bk-side-head"><span>التجميع</span>' in body
    assert 'x-for="s in steps"' in body and 'class="ms-pill"' in body and "قيد التجميع" not in body
    # the toolbar and the layout are shown (no x-cloak) while assembling
    assert re.search(r'<div class="ms-toolbar" x-show="phase !== \'empty\'">', body)


def _host_tag(body: str) -> str:
    """The opening tag of the document host (the one x-ignore element of the view)."""
    tags = re.findall(r"<[^>]*\bx-ignore\b[^>]*>", body)
    assert len(tags) == 1, tags
    return tags[0]


def test_document_host_carries_no_alpine_directive_besides_x_ignore(editor):
    """Regression (the empty manuscript after «تحويل إلى كتاب»): Alpine skips every other directive of an
    x-ignore element, so an x-cloak or x-show on the host would never lift once the fragment is swapped in.
    The host must be bare in every state, and the fragment endpoint must serve the document the view swaps."""
    f, _ = _book()
    client = _logged(editor)
    # never assembled, then queued (the state the view opens in right after the dashboard's convert)
    assert _host_tag(_view(client, f.book)) == '<div class="ms-host" x-ignore data-ms-host>'
    run = AssemblyRun.objects.create(book=f.book, status=AssemblyRun.Status.QUEUED)
    body = _view(client, f.book)
    assert _host_tag(body) == '<div class="ms-host" x-ignore data-ms-host>'
    assert _json_script(body, "manuscript-config")["state"]["active"] is True
    # nothing between the layout and the host hides the host once the run is done: the layout and the column
    # are not cloaked while assembling, and the skeleton is the only thing bound to `!hasDocument`
    layout = body[body.index('class="ms-layout"') : body.index("data-ms-host")]
    assert "x-cloak" not in layout.split('class="ms-skeleton"')[0]
    # not cloaked while assembling; the x-shows are the layout's, the skeleton's and the error state's
    assert layout.startswith('class="ms-layout" x-show="phase !== \'empty\'">')
    assert layout.count("x-show=") == 3
    assert "x-show=\"phase === 'assembling' && !hasDocument\"" in layout
    assert "x-show=\"phase === 'error' && !hasDocument\"" in layout
    # the run finishes: the fragment the view fetches is the document plus the panel parts and the meta
    run.delete()
    services.start_assembly(f.book, editor)
    fragment = client.get(reverse("assembly:document", args=[f.book.pk])).content.decode()
    assert fragment.startswith('<article class="ms-doc"') and 'data-ms-part="warnings"' in fragment
    assert 'id="ms-meta"' in fragment and _json_script(fragment, "ms-meta")["version"] == 1
    # ... and the view rendered afterwards holds the same document inside the bare host
    body = _view(client, f.book)
    assert _host_tag(body) == '<div class="ms-host" x-ignore data-ms-host>'
    assert '<article class="ms-doc"' in body
    for attribute in ("x-cloak", "x-show", ":class", "x-ref"):
        assert attribute not in _host_tag(body), attribute


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
    assert 'data-ms-part="toc"' in host and 'data-ms-part="warnings"' in host
    assert 'data-ms-part="stats"' not in host  # the numbers live in the toolbar's counts line only
    assert 'data-goto="h' in host and "الفصل الأول" in host and 'data-count="1"' in host
    assert (
        'data-code="page_unreviewed"' in host
        and f'href="{reverse("review:page", args=[f.book.pk, 2])}"' in host
    )
    assert 'class="link ms-warn-link" data-goto="' in host and ">انتقال</button>" in host
    assert ">مراجعة</a>" in host and "ms-warn-page" in host
    meta = _json_script(body, "ms-meta")
    assert meta["version"] == 1 and meta["countsText"] == config["countsText"]
    assert [w["code"] for w in meta["warnings"]] == ["page_unreviewed", "uncertain_words"]
    assert meta["toc"][0]["text"] == "الفصل الأول" and meta["seams"][0]["mode"] == "join"
    # the chrome: toolbar (seams segmented, counts, jump with G), the side panel in the dashboard's vocabulary
    # (bk-side, bk-side-head sections: the steps, the contents, the warnings; no tabs, no numbers), the one
    # overlay (the shared menu) with its three faces, the drawer, the sheet, the bar
    assert "data-seam-toggle" in body and 'title="إظهار فواصل الصفحات (S)"' in body
    # D69: the sheet says the letters work by their place on the Arabic layout too
    assert "تعمل الاختصارات بلوحة المفاتيح العربية أيضًا: المفتاح نفسه في مكانه." in body
    assert 'placeholder="إلى صفحة…"' in body and '<kbd class="kbd" aria-hidden="true">G</kbd>' in body
    start = body.index('<aside class="bk-side ms-side"')
    side = body[start : body.index("</aside>", start)]
    assert side.count('class="bk-side-head"') == 3 and "tablist" not in side and "الأرقام" not in side
    assert (
        "<span>التجميع</span>" in side
        and "<span>المحتويات</span>" in side
        and "<span>الملاحظات</span>" in side
    )
    assert 'aria-label="المحتويات" x-show="hasDocument">' in side  # not cloaked: the document is there
    assert 'aria-label="خطوات التجميع" x-show="phase === \'assembling\'" x-cloak>' in side
    assert 'data-ms-toc-host @click="onSideClick($event)"' in side and "data-ms-warnings-host" in side
    assert 'x-text="tocCount">1</bdi>' in side and 'x-text="warningsTotal">2</bdi>' in side
    assert (
        'class="ms-drawer" role="dialog"' in body
        and 'x-ref="drawerClose"' in body
        and "فتح في المراجعة" in body
    )
    assert body.count('class="menu ms-pop" x-ref="pop" x-show="pop.kind" x-cloak') == 1
    assert "x-if=\"pop.kind === 'menu'\"" in body and "عرض الأصل" in body and "نوع الفقرة" in body
    assert "x-if=\"pop.kind === 'seam'\"" in body and "فصل هنا" in body and "وصل بما قبلها" in body
    assert "@click=\"chooseSeam('auto')\"" in body and "x-if=\"pop.kind === 'note'\"" in body
    assert '@click.outside="onPopOutside($event)"' in body and '@scroll.window.passive="onScroll()"' in body
    assert "ms-card" not in body and "ms-note-pop" not in body and "ms-block-menu" not in body
    assert "Enter على فاصل صفحة" in body and "Enter على رقم حاشية" in body and "⌫" not in body
    # D49: the primary leads on to the book page (or re-assembles a stale text); nothing copies the book
    assert "x-show=\"v.primary === 'reassemble'\"" in body and "x-show=\"v.primary === 'book'\"" in body
    assert "data-book-open" in body and ':href="v.bookUrl"' in body and "فتح الكتاب" in body
    assert _json_script(body, "manuscript-config")["urls"]["book"] == reverse(
        "editor:layout", args=[f.book.pk]
    )
    assert "خيارات التجميع…" in body and "نسخ نص المخطوطة" not in body and "لوحة الكتاب" not in body
    assert (
        'data-stage-bar data-rail data-current="manuscript"' in body
        and 'class="ms-screen" data-manuscript data-rail' in body
    )
    assert "حذف الترويسات المتكرّرة" in body
    assert 'x-text="v.pill.text"' in body and "اختصارات لوحة المفاتيح" in body
    # the fragment endpoint serves the same parts on its own
    fragment = _logged(editor).get(reverse("assembly:document", args=[f.book.pk])).content.decode()
    assert fragment.startswith('<article class="ms-doc"') and 'id="ms-meta"' in fragment
    assert 'data-ms-part="warnings"' in fragment and "<html" not in fragment
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
    assert 'class="banner ms-stale" x-show="stale && !edited"' in body and 'x-text="staleText"' in body
    assert 'class="banner ms-edited" x-show="edited && !active"' in body and 'x-text="editedText"' in body
    assert state["edited"] is False
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
    assert "تجميع المخطوطة" in body and "إعادة التجميع" in body and "فتح المخطوطة" in body
    menu = body[
        body.index('class="menu menu-popover bk-menu"') : body.index("</template>", body.index("bk-menu"))
    ]
    assert "تجميع المخطوطة…" in menu and "فتح المخطوطة" in menu and "نسخ نص الكتاب" in menu
    assert 'class="bk-convert-host"' in body and 'class="menu ms-convert" role="dialog"' in body
    assert 'x-text="ms.convert.unreviewed"' in body and '@click="ms.submitConvert()"' in body
    side = body[body.index('<aside class="bk-side"') :]
    assert 'class="bk-manuscript" role="group" aria-label="المخطوطة"' in side
    assert (
        side.index('class="bk-summary-review"')
        < side.index('class="bk-manuscript"')
        < side.index("bk-attention")
    )
    assert 'x-text="manuscriptLine"' in side and "فتح المخطوطة" in side and "تجميع المخطوطة…" in side
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
    assert 'aria-haspopup="menu" aria-label="وُصلت الفقرة بين الصفحتين 1 و2"' in seam.group(1)
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
    assert "ص <bdi>3</bdi>" in split.group(1) and 'aria-haspopup="menu"' in split.group(1)
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
    # the one overlay is the shared menu, absolutely placed, flipped with is-above; the pending shimmer
    pop = re.search(r"\.ms-pop\{([^}]*)\}", css).group(1)
    assert "position:absolute" in pop and "z-index:40" in pop and "width:250px" in pop
    assert re.search(r"\.ms-pop\.is-above\{[^}]*ms-pop-in-up", css)
    assert re.search(r"\.ms-block\.is-pending\{[^}]*color:var\(--color-text-3\)", css)
    assert ".ms-card{" not in css and ".ms-note-pop{" not in css and ".ms-tabs" not in css
    # the side panel: the dashboard's bk-side, scrolling as one column
    side = re.search(r"\.ms-side\{top:calc\(([^}]*)\}", css).group(1)
    assert "overflow-y:auto" in side and "overscroll-behavior:contain" in side
    assert "@keyframes ms-stitch{" in css and "@keyframes ms-rise{" in css and "@keyframes ms-flash{" in css
    reduced = css[css.index("@media (prefers-reduced-motion:reduce){.ms-host") :]
    for needle in (
        ".ms-host.is-reveal .ms-block.is-rise",
        ".ms-seam.is-stitch:before",
        ".ms-block.is-flash",
        ".ms-block.is-pending",
        ".ms-skeleton span",
        ".ms-drawer,.ms-pop",
    ):
        assert needle in reduced[:1000], needle
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
  addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); }, removeEventListener: () => {}, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: (n, v) => { if (v !== undefined) stores[n] = v; return stores[n]; } };
globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; }; globalThis.clearTimeout = () => {};
globalThis.setInterval = () => 1; globalThis.clearInterval = () => {};
globalThis.scrollBy = (x, y) => scrolls.push(y);
globalThis.innerHeight = 800; globalThis.innerWidth = 1200;
globalThis.location = { assign: (u) => assigned.push(u), pathname: '/books/1/manuscript/', search: '', hash: '' };
const store = {}; globalThis.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
globalThis.Nassakh = { toast: (m) => calls.push(['toast', m]), copyText: async (t) => { calls.push(['copy', t]); return true; } };
const channels = []; globalThis.BroadcastChannel = class { constructor(name) { this.name = name; this.onmessage = null; channels.push(this); } postMessage() {} close() { this.closed = true; } };
const fs = require('fs');
const fixture = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
eval(fs.readFileSync(process.argv[4], 'utf8')); // keys.js (NassakhKeys)
eval(fs.readFileSync(process.argv[2], 'utf8'));
inits.forEach((fn) => fn());
NassakhManuscript.parseFragment = parse;
const clone = (v) => JSON.parse(JSON.stringify(v));
const flush = () => new Promise((r) => setImmediate(r));
const posts = () => calls.filter((x) => x[0] === 'POST');
const reqs = () => calls.filter((x) => x[0] === 'GET').map((x) => x[1]);
// fetch routes: the state answers in sequence, the document the current fragment, sheets the page, posts 202
let stateQueue = []; let fragment = fixture.fragment_v1; let sheet = fixture.sheet; let stateGate = null; let docGate = null;
let respond409 = false; // the next posts answer as the server does over a text edited meanwhile (D49)
const deferred = () => { let release; const done = new Promise((r) => { release = r; }); return { release, done }; };
globalThis.fetch = async (url, init) => {
  const method = (init && init.method) || 'GET';
  calls.push([method, url, init && init.body ? JSON.parse(init.body) : null, init && init.headers]);
  if (method === 'POST' && respond409) return { ok: false, status: 409, json: async () => ({ detail: 'حُرِّر نص هذا الكتاب', edited: true }) };
  if (method === 'POST') return { ok: true, status: 202, json: async () => ({ run_id: 9, status: 'done', stage: 'save', manuscript_url: '/books/1/manuscript/', state_url: '/api/books/1/manuscript/state/' }) };
  if (url.startsWith('/api/books/1/manuscript/state/')) { if (stateGate) await stateGate.done; const s = stateQueue.length > 1 ? stateQueue.shift() : stateQueue[0]; return { ok: true, status: 200, json: async () => clone(s) }; }
  if (url.startsWith('/books/1/manuscript/document/')) {
    if (docGate) { const g = docGate; docGate = null; await g.done; return { ok: true, status: 200, text: async () => g.html, json: async () => null }; }
    return { ok: true, status: 200, text: async () => fragment, json: async () => null };
  }
  if (url.startsWith('/api/books/1/sheets/')) return { ok: true, status: 200, json: async () => ({ pages: [clone(sheet)], book_line_h_px: 30 }) };
  return { ok: false, status: 404, json: async () => ({ detail: 'لا' }) };
};
const page = (fragmentHtml) => {
  const root = parse(`<div data-manuscript><div class="ms-column" data-rect="column"><div class="ms-host" data-ms-host>${fragmentHtml}</div></div><aside><div data-ms-toc-host></div><div data-ms-warnings-host></div></aside></div>`).children[0];
  return root;
};
const make = (state, fragmentHtml) => {
  const root = page(fragmentHtml);
  const c = reg.manuscriptView(clone({ ...fixture.config, state }));
  c.$el = root; c.$nextTick = (fn) => fn();
  c.$refs = { jump: { focus: () => focused.push('jump'), select: () => {} }, drawerClose: new Element('button'), toolsBtn: new Element('button'), pop: parse('<div class="ms-pop"><button class="menu-item" data-rect="i0">a</button><button class="menu-item" disabled="">b</button><a class="menu-item" data-rect="i2" href="#">c</a></div>').children[0], sheetClose: new Element('button') };
  c.$refs.drawerClose.setAttribute('x-ref', 'drawerClose'); c.$refs.toolsBtn.setAttribute('data-rect', 'tools');
  c.$refs.pop.querySelectorAll('[data-rect]').forEach((el) => { el.focus = () => { globalThis.document.activeElement = el; focused.push(el.getAttribute('data-rect')); }; });
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
    tocCount: c.tocCount, warnLinks: root.querySelectorAll('[data-ms-warnings-host] .ms-warn-link').length, statsLeft: root.querySelector('[data-ms-host] [data-ms-part]') === null, metaLeft: root.querySelector('[data-ms-host] script') === null,
    idle: { pop: c.pop.kind, tools: c.tools.blockId, drawer: c.drawer.open, sheet: c.sheetOpen, convert: c.convert.open, pending: c.pending },
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
  key(']'); out.warn1 = { cursor: c.warningCursor(), focused: c.focused, current: root.querySelector('.ms-warn.is-current') && root.querySelector('.ms-warn.is-current').getAttribute('data-warn') };
  key(']'); out.warn2 = { cursor: c.warningCursor(), focused: c.focused };
  key('['); out.warn3 = c.warningCursor();
  // rects: the column (its right edge 800, scrolled 200 px), the blocks (and p10 by its lines, so the re-rendered
  // p10 lands 60 px lower after the split: the anchor's shift), the seam markers by page
  Object.assign(RECTS, { column: { top: -200, left: 100, width: 700, height: 3000 }, h1: { top: -300, left: 0, width: 600, height: 40 }, p10: { top: 120, left: 0, width: 600, height: 200 }, 'p10@10,11': { top: 180, left: 0, width: 600, height: 120 }, p40: { top: 340, left: 0, width: 600, height: 40 }, p50: { top: 400, left: 0, width: 600, height: 80 }, h70: { top: 500, left: 0, width: 600, height: 40 }, p80: { top: 560, left: 0, width: 600, height: 60 },
    2: { top: 200, left: 300, width: 24, height: 18 }, 3: { top: 330, left: 700, width: 40, height: 22 } });
  // ---- the seam menu: a click on the join marker opens it (never applies anything), placed under the marker
  // with its start edge on the marker's; the same click again closes it; the decision reads plainly
  const seam2 = root.querySelector('.ms-seam[data-page="2"]');
  const click = (el) => c.onHostClick({ target: el, preventDefault: () => {} });
  click(seam2);
  out.seamMenu = { kind: c.pop.kind, anchor: c.pop.anchorId, text: c.seam.text, state: c.seam.state, mode: c.seam.mode, decision: c.seam.decision, style: c.pop.style, above: c.pop.above, expanded: seam2.getAttribute('aria-expanded'), posts: posts().length, layer: c.topLayer() };
  click(seam2); out.seamToggle = { kind: c.pop.kind, expanded: seam2.getAttribute('aria-expanded') };
  out.splitMenu = (c.openSeamMenu(root.querySelector('.ms-seam[data-page="3"]')), { text: c.seam.text, state: c.seam.state, style: c.pop.style });
  // ---- the outside click: the click that opened the overlay never closes it, the next one does
  click(seam2); out.outsideSameTurn = [c.onPopOutside(), c.pop.kind];
  await flush(); out.outsideLater = [c.onPopOutside(), c.pop.kind];
  // ---- one at a time: the block menu replaces the seam menu, the marker's aria-expanded drops; the menu hangs
  // from the «⋯» spot computed from the block (not the button's stale rect), flipped above near the bottom
  click(seam2); c.openMenu('p40');
  out.replace = { kind: c.pop.kind, anchor: c.pop.anchorId, seamExpanded: seam2.getAttribute('aria-expanded'), style: c.pop.style, above: c.pop.above, tools: c.tools.blockId, src: c.menu.src, role: c.menu.role, reviewed: c.menu.reviewed, reviewUrl: c.menu.reviewUrl, lines: c.menu.lines };
  c.openMenu('p80'); out.flip = { style: c.pop.style, above: c.pop.above, anchor: c.pop.anchorId };
  // the «⋯» stays on the menu's block while another block is hovered; it hides only when the menu closes
  c.onHostOver({ target: root.querySelector('[data-block="p50"]') }); out.toolsKept = c.tools.blockId;
  c.hideTools(); out.toolsKeptHidden = c.tools.blockId;
  // ---- scroll-away: the anchor leaving the visible column closes the overlay; a small scroll keeps it
  out.scrollKeep = [c.onScroll(), c.pop.kind];
  RECTS.p80.top = -900; out.scrollAway = [c.onScroll(), c.pop.kind, c.tools.blockId]; RECTS.p80.top = 560;
  // ---- the menu's arrows move between its enabled items, wrapping; Esc closes it and refocuses the block
  focused.length = 0; c.openMenu('p10', { focus: true }); c.movePop(1); c.movePop(1); c.movePop(1); c.movePop(-1);
  out.popKeys = { focused: focused.slice(), closed: c.closeTop(), back: focused.slice(-1)[0], kind: c.pop.kind };
  // ---- the override: «فصل هنا» from the seam menu posts split, the block holding the seam is pending, the
  // focus (on p10) and the «⋯» are back on p10 after the swap, the scroll moved by the anchor's shift
  click(seam2); out.sameChoice = [await c.chooseSeam('join'), await (click(seam2), c.chooseSeam('auto')), posts().length, c.pop.kind];
  c.focusBlock('p10'); click(seam2);
  stateQueue = [fixture.state_ready_v2]; fragment = fixture.fragment_v2; calls.length = 0; scrolls.length = 0; focused.length = 0;
  stateGate = deferred(); // the 202 landed, the state poll is on the wire: the run shows as active meanwhile
  const choice = c.chooseSeam('split');
  out.seamChosen = { kind: c.pop.kind, refocus: focused[0] };
  await flush();
  out.seamPost = { post: posts()[0].slice(1, 3), busy: c.busy, phase: c.phase, active: c.active, pill: c.pill.text, csrf: 'X-CSRFToken' in (posts()[0][3] || {}), pending: c.pending, pendingClass: root.querySelector('[data-block="p10"]').classList.contains('is-pending') };
  out.busyGuard = [await c.postSeam(3, 'join'), calls.filter((x) => x[0] === 'toast').pop()[1]];
  stateGate.release(); stateGate = null; await choice;
  await flush(); await flush(); await flush();
  // the swap: the new block p20 is in, p10 kept its id, the scroll moved by the anchor's shift, changed blocks flash
  out.afterSeam = { ids: ids(root), version: c.loadedVersion, busy: c.busy, phase: c.phase, reqs: reqs(), scrolls: scrolls.slice(), flashing: c.flashingIds().sort(), seam2: root.querySelector('.ms-seam[data-page="2"]').getAttribute('data-mode'),
    decision: root.querySelector('.ms-seam[data-page="2"]').getAttribute('data-decision'), live: c.liveMessage, counts: c.countsText, focused: c.focused, active: document.activeElement === root.querySelector('[data-block="p10"]'), tools: c.tools.blockId, pending: c.pending, pendingLeft: root.querySelectorAll('.is-pending').length, pop: c.pop.kind };
  // ---- the block menu and the roles post: the block's line ids, the anchor around it, the block pending
  c.openMenu('p40'); calls.length = 0; stateQueue = [fixture.state_ready_v2]; fragment = fixture.fragment_v2; stateGate = deferred();
  const rp = c.setRole('p40', 'heading'); out.roleMenuClosed = c.pop.kind; await flush(); out.rolePending = c.pending;
  stateGate.release(); stateGate = null; await rp; await flush(); await flush(); await flush();
  out.rolePost = { post: posts()[0].slice(1, 3), busy: c.busy, pending: c.pending };
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
  // ---- jump, notes
  calls.length = 0; scrolled.length = 0;
  out.jump = { ok: c.jump('٣'), scrolled: scrolled[0], bad: c.jump('9'), toast: calls.filter((x) => x[0] === 'toast').pop()[1] };
  // ---- footnotes: a click (or the focus landing) on a reference opens the note in the overlay, the list's
  // row lit; the same reference again keeps it; the link jumps to the list, the list's number back to the text
  const ref = root.querySelector('.ms-ref[data-note="n30"]');
  Object.assign(RECTS, { 1: { top: 250, left: 400, width: 14, height: 14 } }); // the reference's rect (keyed by its data-page)
  c.onHostFocusIn({ target: ref });
  out.note = { kind: c.pop.kind, anchor: c.pop.anchorId, number: c.note.number, html: c.note.html, found: c.note.found, orphan: c.note.orphan, hot: root.querySelector('.ms-note[data-note="n30"]').classList.contains('is-hot'), expanded: ref.getAttribute('aria-expanded'), style: c.pop.style };
  click(ref); out.noteAgain = c.pop.kind;
  focused.length = 0; c.goToNote('n30'); out.noteJump = { focus: focused[0], popClosed: c.pop.kind === null, hot: root.querySelector('.ms-note[data-note="n30"]').classList.contains('is-hot'), expanded: ref.getAttribute('aria-expanded') };
  click(root.querySelector('.ms-note-num[data-ref="n30"]')); out.noteBack = focused.slice(-1)[0];
  // ---- Esc closes the top layer only: the overlay (whatever it shows), then the drawer
  await c.openSource('p10'); c.openMenu('p10'); const esc1 = c.closeTop(); click(seam2); const esc2 = c.closeTop(); click(ref); const esc3 = c.closeTop();
  out.escLayers = [esc1, esc2, esc3, c.closeTop(), c.closeTop()];
  // ---- hiding the seams closes an open seam menu
  click(seam2); c.setSeams(false); out.seamsOffCloses = [c.pop.kind, c.openSeamMenu(seam2)]; c.setSeams(true);
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
    stitch: a.root.querySelectorAll('.ms-seam.is-stitch').length, pill: a.c.pill.state, live: a.c.liveMessage, polling: timers.filter((t) => t.ms === 700).length, toc: a.root.querySelectorAll('[data-ms-toc-host] .ms-toc-link').length, primary: a.c.primary,
    hostReveal: a.root.querySelector('[data-ms-host]').classList.contains('is-reveal'), stepsDone: a.c.stepsDone, tocCount: a.c.tocCount, warnRows: a.root.querySelectorAll('[data-ms-warnings-host] .ms-warn').length };
  const revealEnd = timers.find((t) => t.ms === 1400); revealEnd.fn();
  out.revealEnded = { reveal: a.c.reveal, hostReveal: a.root.querySelector('[data-ms-host]').classList.contains('is-reveal'), rise: a.root.querySelectorAll('.ms-block.is-rise').length };
  // ---- stale responses: an older fragment that lands after a newer one is dropped
  const gate = deferred(); gate.html = fixture.fragment_v1; docGate = gate; const slow = a.c.reload();
  fragment = fixture.fragment_v2; await a.c.reload();
  gate.release(); await slow; await flush();
  out.staleReload = { slow: await slow, version: a.c.loadedVersion, ids: ids(a.root) };
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
  out.proofreader = { seam: await pr.c.postSeam(2, 'split'), dismiss: await pr.c.dismissSuggestion('p40'), posts: posts().length, primary: pr.c.primary, menu: pr.c.openSeamMenu(pr.root.querySelector('.ms-seam[data-page="2"]')) };
  // ---- the convert popover: options from the state, submit posts assemble and the run begins
  const cv = make(fixture.state_empty, ''); calls.length = 0;
  cv.c.openConvert(); out.convertOpen = { open: cv.c.convert.open, options: clone(cv.c.convert.options), unreviewed: cv.c.convert.unreviewed, label: cv.c.convert.label, phase: cv.c.phase, primary: cv.c.primary };
  cv.c.convert.options.footnote_numbering = 'book'; stateQueue = [fixture.state_queued];
  await cv.c.submitConvert(); await flush();
  out.convertSubmit = { post: posts()[0].slice(1, 3), open: cv.c.convert.open, phase: cv.c.phase, active: cv.c.active };
  // ---- a paragraph that becomes a heading comes back as `h…`: the anchor tries that id first, in its place
  const an = make(fixture.state_v1, fixture.fragment_v1); await flush();
  const anchor = an.c.anchorBefore(['h40', 'p40', 'p10']);
  out.anchorRename = { ids: anchor.ids, same: anchor.tops.h40 === anchor.tops.p40 };
  // ---- D49: a text edited on the book page: the tools rest, the replacement is confirmed in the popover
  const ed = make(Object.assign({}, fixture.state_v1, { edited: true }), fixture.fragment_v1); await flush(); calls.length = 0;
  const seamBefore = posts().length;
  // D78 (PHASE7 §5.6): review's changes after the edit are named, and taken on the book page
  const edStale = make(Object.assign({}, fixture.state_v1, { edited: true, stale: true, stale_pages: [2, 4, 5] }), fixture.fragment_v1); await flush();
  out.editedDrift = { text: edStale.c.editedDriftText, url: edStale.c.changesUrl, none: ed.c.editedDriftText };
  out.edited = { edited: ed.c.edited, primary: ed.c.primary, pill: ed.c.pill, text: ed.c.editedText, screen: ed.c.$el ? 1 : 0,
    seam: await ed.c.postSeam(2, 'split'), dismiss: await ed.c.dismissSuggestion('p40'), role: await ed.c.setRole('p40', 'heading'), posts: posts().length - seamBefore };
  ed.c.reassemble();
  out.editedReassemble = { open: ed.c.convert.open, edited: ed.c.convert.edited, label: ed.c.convert.label, posts: posts().length - seamBefore };
  stateQueue = [fixture.state_queued];
  await ed.c.submitConvert(); await flush();
  out.editedSubmit = posts().slice(-1)[0].slice(1, 3);
  // the server finds the text edited meanwhile (409): the popover opens on the replacement
  const late = make(fixture.state_v1, fixture.fragment_v1); await flush();
  respond409 = true; await late.c.reassemble(); await flush(); respond409 = false;
  out.late = { edited: late.c.edited, open: late.c.convert.open, label: late.c.convert.label };
  // ---- D70: the live state. The review screen's message (this book only) fetches the state; a changed set of
  // stale pages swaps the document too (the amber marks follow the live status); one fetch per 2 s, a trigger
  // inside the window waits for its end; nothing while a run is polled
  const lv = make(fixture.state_ready, fixture.fragment_v1); await flush();
  const ch = channels[channels.length - 1];
  calls.length = 0; timers.length = 0; stateQueue = [fixture.state_stale]; fragment = fixture.fragment_v1;
  ch.onmessage({ data: { type: 'review', book: 2, page: 4 } }); await flush();
  const otherBook = reqs().length;
  ch.onmessage({ data: { type: 'review', book: 1, page: 4 } }); await flush(); await flush(); await flush();
  out.live = { channel: ch.name, otherBook, reqs: reqs(), stale: lv.c.stalePages, pill: lv.c.pill.text };
  calls.length = 0; timers.length = 0;
  ch.onmessage({ data: { type: 'review', book: 1, page: 2 } }); await flush();
  ch.onmessage({ data: { type: 'review', book: 1, page: 3 } }); await flush(); // one trailing refresh for both
  const waiting = timers.filter((t) => t.ms > 0 && t.ms <= 2000);
  out.liveThrottled = { reqs: reqs().length, waiting: waiting.length };
  // the same stale pages: the state is taken, the document stays
  lv.c.fetchLive && (await lv.c.fetchLive()); await flush();
  out.liveSame = reqs();
  lv.c.destroy();
  out.liveClosed = ch.closed === true;
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
                "blockType": "/api/books/1/manuscript/block-type/",
                "suggestions": "/api/books/1/manuscript/suggestions/",
                "page": "/books/1/manuscript/",
                "document": "/books/1/manuscript/document/",
                "sheets": "/api/books/1/sheets/",
                "review": "/books/1/review/__n__/",
                "dashboard": "/books/1/",
                "book": "/books/1/layout/",
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
        [
            "node",
            str(harness),
            str(JS / "manuscript.js"),
            str(tmp_path / "fixture.json"),
            str(JS / "keys.js"),
        ],
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
        and out["ready"]["warnLinks"] == 8  # 4 rows with a page: «انتقال» + «مراجعة»; the book-level row none
        and out["ready"]["tocCount"] == 2
        and out["ready"]["statsLeft"] is True
        and out["ready"]["metaLeft"] is True
    )
    # nothing floats on load (the empty card): no overlay, no «⋯», no drawer, no sheet, no pending block
    assert out["ready"]["idle"] == {
        "pop": None,
        "tools": None,
        "drawer": False,
        "sheet": False,
        "convert": False,
        "pending": None,
    }
    assert (
        out["ready"]["pill"]["state"] == "saved"
        and out["ready"]["pill"]["text"].startswith("مُجمَّعة ")
        and out["ready"]["primary"] == "book"
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
        "c": None,  # D49: nothing copies the book
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
    # D70: the live state
    assert out["live"]["channel"] == "nassakh" and out["live"]["otherBook"] == 0
    assert out["live"]["reqs"] == ["/api/books/1/manuscript/state/", "/books/1/manuscript/document/"]
    assert out["live"]["stale"] == [2, 4] and out["live"]["pill"] == "تغيّر النص بعد التجميع"
    assert out["liveThrottled"] == {"reqs": 0, "waiting": 1}
    assert out["liveSame"] == ["/api/books/1/manuscript/state/"]
    assert out["liveClosed"] is True
    assert out["moveFocus"] == {"focused": "p10", "active": "p10", "tools": "p10", "toolsClass": True}
    assert out["moveBack"] == "h1" and out["arrowDown"] == "p10"
    # warnings: ] after h1 lands on the warning of the nearest later block in document order (p10's uncertain
    # words, index 4 in the stored list); the next ] continues through the list, [ steps back
    assert out["warn1"] == {"cursor": 4, "focused": "p10", "current": "4"}
    assert out["warn2"] == {"cursor": 0, "focused": "p10"} and out["warn3"] == 4
    # the seam menu: a click opens it (no post), placed 6 px under the marker (top 218 → 224, column top -200
    # → 424); its right edge would sit on the marker's (324) but the 260 px menu would then cross the column's
    # left edge (108), so it is pushed to 368 (→ 800 - 368 = 432); the same click closes it
    assert out["seamMenu"] == {
        "kind": "seam",
        "anchor": "2",
        "text": "وُصلت الفقرة بين الصفحتين 1 و2",
        "state": "تلقائي",
        "mode": "join",
        "decision": "auto",
        "style": "top:424px;right:432px",
        "above": False,
        "expanded": "true",
        "posts": 0,
        "layer": "seam",
    }
    assert out["seamToggle"] == {"kind": None, "expanded": "false"}
    assert out["splitMenu"] == {
        "text": "فُصلت الفقرة عند الصفحة 3",
        "state": "قرار يدوي",
        "style": "top:558px;right:60px",  # the marker's right (740) is inside the column's visible edge (792)
    }
    # the click that opened the overlay is not a close; the next outside click is
    assert out["outsideSameTurn"] == [False, "seam"] and out["outsideLater"] == [True, None]
    # one at a time: the block menu replaced the seam menu; it hangs from the «⋯» spot (p40 top 340 + 22 + 24
    # + 6 = 392 → 592) at the column's start edge (right 8), computed from the block, not the button's rect
    assert out["replace"] == {
        "kind": "menu",
        "anchor": "p40",
        "seamExpanded": "false",
        "style": "top:592px;right:8px",
        "above": False,
        "tools": "p40",
        "src": "3",
        "role": "body",
        "reviewed": False,
        "reviewUrl": "/books/1/review/3/?from=manuscript&block=p40",  # D76: review leads back here
        "lines": [40],
    }
    # near the bottom (p80 at 560: below would end at 874 > 792) the menu flips above the «⋯»
    assert out["flip"] == {"style": "top:514px;right:8px", "above": True, "anchor": "p80"}
    assert out["toolsKept"] == "p80" and out["toolsKeptHidden"] == "p80"
    assert out["scrollKeep"] == [False, "menu"] and out["scrollAway"] == [True, None, "p80"]
    # Enter opens with the first item focused; arrows: the third (the disabled second skipped), wrapping to
    # the first, the third again, back to the first; Esc closes and refocuses the block
    assert out["popKeys"] == {
        "focused": ["i0", "i2", "i0", "i2", "i0"],
        "closed": "menu",
        "back": "p10",
        "kind": None,
    }
    # choosing what is already the case posts nothing; «تلقائي» on an automatic decision neither
    assert out["sameChoice"] == [False, False, 0, None]
    assert out["seamChosen"] == {"kind": None, "refocus": "2"}
    assert out["seamPost"] == {
        "post": ["/api/books/1/manuscript/seams/", {"page": 2, "mode": "split"}],
        "busy": True,
        "phase": "assembling",
        "active": True,
        "pill": "قيد التجميع…",
        "csrf": True,
        "pending": "p10",
        "pendingClass": True,
    }
    assert out["busyGuard"] == [False, "انتظر انتهاء التجميع الجاري"]
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
    # the focus and the «⋯» are back on the re-rendered p10, the pending mark is gone, nothing floats
    assert (
        after["active"] is True
        and after["tools"] == "p10"
        and after["pending"] is None
        and after["pendingLeft"] == 0
        and after["pop"] is None
    )
    # the block menu and the roles post: the menu closes at once, the block is pending until the swap
    assert out["roleMenuClosed"] is None and out["rolePending"] == "p40"
    assert out["rolePost"] == {
        "post": ["/api/books/1/manuscript/roles/", {"line_ids": [40], "role": "heading"}],
        "busy": False,
        "pending": None,
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
    # the footnote in the overlay: under the reference (250 + 14 + 6 = 270 → 470); 360 px wide it would cross
    # the column's left edge from the reference's right (414), so it is pushed to 468 (→ 332); the list's row
    # lit; the link to the list closes it and focuses the row's number
    assert out["note"] == {
        "kind": "note",
        "anchor": "n30",
        "number": "1",
        "html": "انظر &lt;script&gt;alert(1)&lt;/script&gt; المصدر",
        "found": True,
        "orphan": False,
        "hot": True,
        "expanded": "true",
        "style": "top:470px;right:332px",
    }
    assert out["noteAgain"] == "note"
    assert out["noteJump"] == {"focus": "n30", "popClosed": True, "hot": False, "expanded": "false"}
    assert out["noteBack"] == "n30"
    # Esc: the overlay first (whatever it shows), then the drawer, then nothing
    assert out["escLayers"] == ["menu", "seam", "note", "drawer", None]
    assert out["seamsOffCloses"] == [None, False]
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
    assert done["polling"] == 0 and done["toc"] == 2 and done["primary"] == "book"
    # the host (x-ignore: no Alpine binding) gets its reveal class from the component and loses it after
    assert done["hostReveal"] is True and done["stepsDone"] == 7
    assert done["tocCount"] == 2 and done["warnRows"] == 5
    assert out["revealEnded"] == {"reveal": False, "hostReveal": False, "rise": 0}
    # an older fragment landing after a newer one is dropped: the document stays at version 2
    assert out["staleReload"]["slow"] is False and out["staleReload"]["version"] == 2
    assert out["staleReload"]["ids"] == ["h1", "p10", "p20", "p40", "p50", "h70", "p80"]
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
        "text": "تغيّر نص صفحتين بعد التجميع:",  # the genitive after «نص»
        "pages": [2, 4],
        "pill": {"state": "warn", "text": "تغيّر النص بعد التجميع"},
        "primary": "reassemble",
        "url": "/books/1/review/4/?from=manuscript",  # D76: review leads back here
    }
    # a proofreader may open the seam menu (the review link) but never posts an override
    assert out["proofreader"] == {
        "seam": False,
        "dismiss": False,
        "posts": 0,
        "primary": "book",
        "menu": True,
    }
    assert out["convertOpen"] == {
        "open": True,
        "options": {
            "footnote_numbering": "chapter",
            "include_unreviewed": True,
            "strip_tatweel": True,
            "strip_running_heads": True,
            "strip_footnotes": False,  # D94: «حذف الحواشي», off unless the book chose it
        },
        "unreviewed": 2,
        "label": "تجميع المخطوطة",
        "phase": "empty",
        "primary": "convert",
    }
    assert out["convertSubmit"] == {
        "post": [
            "/api/books/1/assemble/",
            {
                "footnote_numbering": "book",
                "include_unreviewed": True,
                "strip_tatweel": True,
                "strip_running_heads": True,
                "strip_footnotes": False,
            },
        ],
        "open": False,
        "phase": "assembling",
        "active": True,
    }
    # D49: over a text edited on the book page the structure tools post nothing, the primary is the book
    # page, and re-assembling opens the popover whose button confirms the replacement
    edited = out["edited"]
    assert edited["edited"] is True and edited["primary"] == "book"
    assert edited["pill"] == {"state": "saved", "text": "حُرِّر في صفحة الكتاب"}
    assert edited["text"].startswith("حُرِّر نص الكتاب في «الكتاب»")
    assert out["editedDrift"] == {
        "text": "غيّرت المراجعة نص 3 صفحات بعد التحرير؛ تُؤخذ من «الكتاب».",
        "url": "/books/1/layout/?tab=changes",
        "none": "",
    }
    # the seams and the suggestions rest; «نوع الفقرة» changes the edited block itself (D94: one post to the
    # block-type endpoint, no run; its details are in test_manuscript_trust_roles_under_node)
    assert edited["seam"] is False and edited["dismiss"] is False and edited["role"] is True
    assert edited["posts"] == 1
    assert out["editedReassemble"] == {
        "open": True,
        "edited": True,
        "label": "استبدال النص المحرَّر",
        "posts": 1,
    }
    assert (
        out["editedSubmit"][0] == "/api/books/1/assemble/"
        and out["editedSubmit"][1]["replace_edited"] is True
    )
    assert out["late"] == {"edited": True, "open": True, "label": "استبدال النص المحرَّر"}
    assert out["anchorRename"] == {"ids": ["h40", "p40", "p10"], "same": True}


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
        "options": {
            "footnote_numbering": "page",
            "include_unreviewed": False,
            "strip_tatweel": True,
            "strip_running_heads": True,
        },
        "label": "تجميع المخطوطة",
    }
    assert out["submitted"]["call"] == [
        "POST",
        "/api/books/1/assemble/",
        {
            "footnote_numbering": "page",
            "include_unreviewed": False,
            "strip_tatweel": True,
            "strip_running_heads": True,
        },
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


# ------------------------------------------------ 7b: footnote and verse as roles (PHASE7_SPEC §4.4, D74)

TRUST = ROOT / "assembly" / "fixtures" / "trust"

MS_TRUST_RUN = r"""
(async () => {
  const out = {};
  const { c, root } = make(fixture.state_ready, fixture.fragment_v1);
  out.roles = c.roles.map((r) => [r.value, r.label, r.key]);
  // the menu reads what a block is (a verse line's paragraph by its style) and «حاشية للعلامة (n)» when its
  // leading marker's call is open on its page
  const menuOf = (id) => { c.openMenu(id); const m = { role: c.menu.role, noteFor: c.menu.noteFor, labels: c.roles.map((r) => c.roleLabel(r)), lines: c.menu.lines }; c.closePop(); return m; };
  out.menus = { note: menuOf('p4004'), verse: menuOf('p1005'), body: menuOf('p1002'), heading: menuOf('h1001') };
  out.blockRole = ['p1005', 'p1009', 'h1001'].map((id) => NassakhManuscript.blockRole(root.querySelector(`[data-block="${id}"]`)));
  // D94: a paragraph's style names its kind (render.py writes `data-style` for every paragraph style)
  out.styleRole = ['quote', 'center', 'verse', 'bogus'].map((s) => { const el = new Element('p'); el.setAttribute('data-style', s); return NassakhManuscript.blockRole(el); });
  // «حاشية للعلامة (1)» posts the footnote role on the block's lines (the roles endpoint's contract body)
  const run = async (fn) => { calls.length = 0; stateQueue = [fixture.state_ready_v2]; fragment = fixture.fragment_v1; const p = fn(); const live = c.liveMessage; await p; await flush(); await flush(); await flush(); return { post: posts().map((x) => x.slice(1, 3)), live }; };
  out.note = await run(() => c.setRole('p4004', 'footnote'));
  out.verse = await run(() => c.setRole('p1002', 'verse'));
  out.back = await run(() => c.setRole('p1005', 'body'));
  out.heading = await run(() => c.setRole('p1009', 'heading'));
  out.quote = await run(() => c.setRole('p1002', 'quote'));
  calls.length = 0; out.same = [await c.setRole('p1006', 'verse'), await c.setRole('p1009', 'body'), posts().length];
  // D94: a digit picks the kind while the block menu is open (by the key's place, in any script); «حاشية» has none
  const digit = (k, code, open = true) => run(() => { if (open) c.openMenu('p1002'); else c.closePop(); return c.onPopKey({ key: k, code, preventDefault: () => {} }); });
  out.digits = { center: await digit('٥', 'Digit5'), heading: await digit('1', 'Digit1'), none: await digit('6', 'Digit6'), letter: await digit('a', 'KeyA'), closed: await digit('3', 'Digit3', false) };
  // the stray_note warning's «جعلها حاشية» · «انتقال» in the side panel
  const host = root.querySelector('[data-ms-warnings-host]');
  const stray = host.querySelector('.ms-warn[data-block="p4004"]');
  out.stray = { msg: stray.querySelector('.ms-warn-msg').textContent, links: stray.querySelectorAll('.ms-warn-link').map((b) => [b.tagName, b.textContent, b.getAttribute('data-warn-role'), b.getAttribute('data-lines'), b.getAttribute('data-goto')]) };
  const marker = host.querySelector('.ms-warn[data-block="p3001"]');
  out.markerMissing = { msg: marker.querySelector('.ms-warn-msg').textContent, acts: marker.querySelectorAll('[data-warn-role]').length };
  out.act = await run(() => { c.onSideClick({ target: stray.querySelector('[data-warn-role]'), preventDefault: () => {} }); return flush(); });
  // «انتقال» goes to the paragraph
  c.onSideClick({ target: stray.querySelector('[data-goto]'), preventDefault: () => {} }); out.goto = c.focused;
  // a reader has no structure tools; an edited book refuses the source tools (the book page owns the structure,
  // D49), and «نوع الفقرة» changes the edited block itself (D94: editors, no run, the document swapped at once)
  const rd = make(fixture.state_ready, fixture.fragment_v1); rd.c.canReview = false; calls.length = 0;
  out.reader = [await rd.c.warnRole(rd.root.querySelector('[data-ms-warnings-host] [data-warn-role]')), await rd.c.setRole('p4004', 'footnote'), posts().length, rd.c.typeTitle];
  const ed = make(Object.assign({}, fixture.state_ready, { edited: true }), fixture.fragment_v1); await flush(); calls.length = 0;
  const warnEdited = await ed.c.warnRole(ed.root.querySelector('[data-ms-warnings-host] [data-warn-role]'));
  const typed = await ed.c.setRole('p1002', 'quote'); await flush();
  out.edited = { warn: warnEdited, typed, post: posts().map((x) => x.slice(1, 3)), reqs: reqs(), busy: ed.c.busy, pending: ed.c.pending, polling: ed.c.active, title: ed.c.typeTitle };
  calls.length = 0; ed.c.openMenu('p1002');
  out.editedDigit = [Boolean(await ed.c.onPopKey({ key: '5', code: 'Digit5', preventDefault: () => {} })), posts().map((x) => x.slice(1, 3))];
  const edr = make(Object.assign({}, fixture.state_ready, { edited: true }), fixture.fragment_v1); edr.c.canEdit = false; await flush(); calls.length = 0;
  out.editedReviewer = [edr.c.canSetType, edr.c.typeTitle, await edr.c.setRole('p1002', 'quote'), posts().length];
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


def test_manuscript_trust_roles_under_node(tmp_path):
    """D74 in the manuscript view, on D2's contract fixtures rendered by `assembly.render`: the paragraph
    menu's «شعر» and «حاشية» («حاشية للعلامة (1)» on a paragraph whose call is open on its page), the requests
    the roles endpoint expects, and the stray_note warning's «جعلها حاشية» · «انتقال»."""
    manuscript = json.loads((TRUST / "manuscript.json").read_text(encoding="utf-8"))
    roles = json.loads((TRUST / "block_roles.json").read_text(encoding="utf-8"))
    fixture = {
        "config": {
            "bookId": 1,
            "title": "كتاب الحواشي والشعر",
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
                "blockType": "/api/books/1/manuscript/block-type/",
                "suggestions": "/api/books/1/manuscript/suggestions/",
                "page": "/books/1/manuscript/",
                "document": "/books/1/manuscript/document/",
                "sheets": "/api/books/1/sheets/",
                "review": "/books/1/review/__n__/",
                "dashboard": "/books/1/",
                "book": "/books/1/layout/",
            },
        },
        "fragment_v1": render_to_string(
            "assembly/_document.html", {"book": SimpleNamespace(pk=1), **render.fragment_context(manuscript)}
        ),
        "sheet": {"id": 7, "number": 1, "width": 1000, "height": 1500, "lines": []},
        "state_ready": _state(warnings_count=len(manuscript["warnings"])),
        "state_ready_v2": _state(version=2, run={"id": 9, "status": "done", "stage": "save", "error": ""}),
        "roles": roles,
    }
    (tmp_path / "fixture.json").write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS[: HARNESS.index("(async () => {")] + MS_TRUST_RUN, encoding="utf-8")
    run = subprocess.run(
        [
            "node",
            str(harness),
            str(JS / "manuscript.js"),
            str(tmp_path / "fixture.json"),
            str(JS / "keys.js"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])

    # «نوع الفقرة»: the seven choices of `BLOCK_ROLES`, in order, with the digit that picks each (D94)
    assert out["roles"] == [[r["value"], r["label"], r["key"]] for r in roles["roles"]]
    labels = [r["label"] for r in roles["roles"]]
    note_for = [*labels[:6], roles["footnoteFor"].format(n=1)]
    assert note_for[6] == "حاشية للعلامة (1)"
    # the current choice: a heading by its tag, a verse line's paragraph by its style, else body text
    assert out["menus"] == {
        "note": {"role": "body", "noteFor": "1", "labels": note_for, "lines": [4004]},
        "verse": {"role": "verse", "noteFor": "", "labels": labels, "lines": [1005]},
        "body": {"role": "body", "noteFor": "", "labels": labels, "lines": [1002, 1003, 1004]},
        "heading": {"role": "heading", "noteFor": "", "labels": labels, "lines": [1001]},
    }
    assert out["blockRole"] == ["verse", "body", "heading"]
    assert out["styleRole"] == ["quote", "center", "verse", "body"]
    # every choice posts the block's lines with the role (the contract's request), with its live message
    url = "/api/books/1/manuscript/roles/"
    request = roles["request"]["body"]
    assert out["note"] == {"post": [[url, request]], "live": roles["liveMessages"]["footnote"]}
    assert out["verse"] == {
        "post": [[url, {"line_ids": [1002, 1003, 1004], "role": "verse"}]],
        "live": roles["liveMessages"]["verse"],
    }
    assert out["back"] == {"post": [[url, {"line_ids": [1005], "role": "body"}]], "live": "تصير الفقرة محتوى"}
    assert out["heading"]["post"] == [[url, {"line_ids": [1009], "role": "heading"}]]
    assert out["quote"] == {
        "post": [[url, {"line_ids": [1002, 1003, 1004], "role": "quote"}]],
        "live": roles["liveMessages"]["quote"],
    }
    assert out["same"] == [False, False, 0]  # the block's own role again: nothing is sent
    # a digit in the open menu: ٥ (any script) «ملاحظة وسط», 1 «عنوان رئيسي»; 6, a letter, no menu: nothing
    lines = [1002, 1003, 1004]
    assert {key: value["post"] for key, value in out["digits"].items()} == {
        "center": [[url, {"line_ids": lines, "role": "center"}]],
        "heading": [[url, {"line_ids": lines, "role": "heading"}]],
        "none": [],
        "letter": [],
        "closed": [],
    }
    assert out["digits"]["center"]["live"] == roles["liveMessages"]["center"]
    # the stray_note warning: «جعلها حاشية» (the footnote role on its lines) · «انتقال» · «مراجعة»
    assert out["stray"] == {
        "msg": "فقرة في الصفحة 4 تبدأ بعلامة حاشية «(1)» ولم تُربط.",
        "links": [
            ["BUTTON", "جعلها حاشية", "footnote", "4004", None],
            ["BUTTON", "انتقال", None, None, "p4004"],
            ["A", "مراجعة", None, None, None],
        ],
    }
    assert out["markerMissing"] == {"msg": "حاشية بلا علامة رُبطت بالعلامة (1)؛ تحقّق منها.", "acts": 0}
    assert out["act"] == {"post": [[url, request]], "live": "تصير الفقرة حاشية"}
    assert out["goto"] == "p4004"
    # a reader: nothing is sent
    assert out["reader"] == [False, False, 0, "تغيير نوع الفقرة متاح للمدقّقين والمحرّرين"]
    # an edited book (D94): the warning's source action rests; «نوع الفقرة» posts the block-type endpoint (the
    # contract's `editedRequest`), no run is polled, and the document is fetched and swapped at once
    edited_url = "/api/books/1/manuscript/block-type/"
    assert out["edited"] == {
        "warn": False,
        "typed": True,
        "post": [[edited_url, roles["editedRequest"]["body"]]],
        "reqs": ["/books/1/manuscript/document/"],
        "busy": False,
        "pending": None,
        "polling": False,
        "title": "",
    }
    assert out["editedDigit"] == [True, [[edited_url, {"block_id": "p1002", "type": "center"}]]]
    # a reviewer sets kinds at the source only: on an edited text they rest
    assert out["editedReviewer"] == [False, "تغيير نوع الفقرة في النص المحرَّر متاح للمحرّرين", False, 0]


def test_manuscript_trust_markup_and_styles():
    """The paragraph menu names each choice through `roleLabel(r)`, a reader's screen carries `is-reader` (the
    warning's structure action rests there and on an edited book), and a verse line's paragraph is centred."""
    view = (ROOT / "templates" / "assembly" / "manuscript.html").read_text(encoding="utf-8")
    assert '<span x-text="roleLabel(r)"></span>' in view and "'is-reader': !canReview" in view
    assert "نوع الفقرة (محتوى، عنوان، اقتباس، شعر، ملاحظة وسط، حاشية)" in view
    # D94: the kinds two to a row, a digit's hint on each, «حاشية» across the row; on an edited text the label
    # says where the change goes, and the choices follow `canSetType`
    assert '<div class="ms-role-grid" role="group" aria-label="نوع الفقرة">' in view
    assert ":class=\"{ 'is-wide': !r.key }\"" in view and ':disabled="!canSetType || busy"' in view
    assert '<kbd class="kbd ms-role-key" aria-hidden="true" x-show="r.key" x-text="r.key"></kbd>' in view
    assert '<span class="ms-role-where" x-show="edited">في النص المحرَّر</span>' in view
    assert '@keydown="onPopKey($event)"' in view
    options = (ROOT / "templates" / "assembly" / "_convert_options.html").read_text(encoding="utf-8")
    assert 'x-model="ms.convert.options.strip_footnotes"' in options and "<span>حذف الحواشي</span>" in options
    assert options.count(':disabled="ms.convert.options.strip_footnotes"') == 3
    fragment = (ROOT / "templates" / "assembly" / "_document.html").read_text(encoding="utf-8")
    assert 'class="link ms-warn-link ms-warn-act" data-warn-role="{{ a.role }}"' in fragment
    assert fragment.index("data-warn-role") < fragment.index('data-goto="{{ w.blockId }}"')
    css = (ROOT / "static" / "src" / "components" / "manuscript.css").read_text(encoding="utf-8")
    assert '.ms-p[data-style="verse"] {' in css
    assert ".ms-screen:is(.is-reader, .is-edited) .ms-warn-act { display: none; }" in css
    # D94: «اقتباس» set in, «ملاحظة وسط» centred, the kinds two to a row, the numbering resting without notes
    assert '.ms-p[data-style="quote"] { margin-inline: 2em; text-indent: 0; }' in css
    assert '.ms-p[data-style="center"] {' in css
    assert ".ms-role-grid { display: grid; grid-template-columns: 1fr 1fr;" in css
    assert ".ms-convert-group.is-resting { opacity: 0.45; }" in css
