"""Phase 3 theatre UI as rebuilt by docs/DASHBOARD_SPEC.md: the dashboard's static shells, toolbar and chrome
(Django test client), the text panel's decode hooks, the compiled CSS, and — under Node with a tiny DOM stub —
the decode engine (D28 dwell-and-veil), the pure text layout, the sheet handle and the dashboard logic."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.template.loader import render_to_string
from django.urls import NoReverseMatch, reverse

import pytest

from books.models import Book, Page

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "src" / "js"
CSS = ROOT / "static" / "dist" / "app.css"


def _user(name: str, group: str) -> User:
    user = User.objects.create_user(name, password="pass-1234")
    user.groups.add(Group.objects.get_or_create(name=group)[0])
    return user


@pytest.fixture
def editor_client(client):
    client.force_login(_user("editor", "editor"))
    return client


def _book(states, status: str = Book.Status.OCR):
    """A book with one page per (page status, text_state) pair."""
    book = Book.objects.create(title="كتاب المسرح", status=status)
    pages = [
        Page.objects.create(
            book=book,
            number=i,
            source_index=i - 1,
            status=page_status,
            text_state=text_state,
            provisional_text="نص مبدئي من تسراكت" if text_state != "none" else "",
            final_text="نص نهائي" if text_state == "final" else "",
            width=1100,
            height=1600,
        )
        for i, (page_status, text_state) in enumerate(states, start=1)
    ]
    return book, pages


def _optional(name: str, *args) -> str:
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ""


def _shell(body: str, n: int = 0) -> str:
    """The n-th server-rendered sheet shell (its markup up to the next shell or the grid)."""
    starts = [m.start() for m in re.finditer(r'<article class="page-sheet', body)]
    end = starts[n + 1] if n + 1 < len(starts) else body.index('<div class="page-grid"')
    return body[starts[n] : end]


# ---------------------------------------------------------- dashboard: chrome and static shells (§2, §3, §11)


def test_dashboard_toolbar_static_shells_and_grid_cards(editor_client):
    book, pages = _book(
        [
            (Page.Status.UPLOADED, "none"),
            (Page.Status.LAYOUT_DONE, "provisional"),
            (Page.Status.OCR_DONE, "final"),
        ]
    )
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    # sticky toolbar: segmented «صفحات | شبكة», five filter chips with live counts, jump field with G, follow
    # toggle
    toggle = re.search(
        r'<div class="segmented" role="group" aria-label="طريقة عرض الصفحات" data-view-toggle>(.*?)</div>',
        body,
        re.S,
    )
    assert toggle and "صفحات" in toggle.group(1) and "شبكة" in toggle.group(1)
    assert "@click=\"setView('sheets')\"" in body and "@click=\"setView('grid')\"" in body
    chips = re.search(r'<div class="bk-chips"[^>]*data-filter-chips>(.*?)</div>', body, re.S).group(1)
    assert chips.count('class="bk-chip"') == 5
    for name in ("all", "processing", "review", "attention", "reviewed"):
        assert f"setFilter('{name}')" in chips and f'x-text="counts.{name}"' in chips
    assert 'inputmode="numeric" dir="ltr" placeholder="إلى صفحة…" aria-label="الانتقال إلى صفحة"' in body
    assert '<kbd class="kbd" aria-hidden="true">G</kbd>' in body and 'class="bk-follow"' in body
    # owner 14: the follow toggle says what it does and lives in the viewer only
    assert "تتبّع الصفحة الجارية" in body and "x-show=\"active &amp;&amp; view === 'sheets'\"" in body or (
        "x-show=\"active && view === 'sheets'\"" in body
    )
    assert 'class="bk-toolbar"' in body and 'class="bk-summary"' in body and 'class="bk-attention"' in body
    # summary strip: the five stage counters replace the cards
    assert "stage-card" not in body and "attention-card" not in body
    for key in ("uploaded", "preprocessed", "layout_done", "ocr_done", "error"):
        assert f"count('{key}')" in body
    # one static shell per page (+ the inert clone template), ids and data attributes from the first paint
    assert body.count('<article class="page-sheet') == 4 and '<template id="sheet-shell">' in body
    assert (
        f'id="sheet-2" data-page-id="{pages[1].pk}" data-number="2" '
        'data-status="layout_done" data-text="provisional"' in body
    )
    assert 'style="--sheet-ar: 1100 / 1600"' in body
    shell = _shell(body, 1)
    for needle in (
        "صفحة <bdi",
        "الرقم المطبوع",
        "غير مؤكَّدة",
        "bk-reviewed",
        'class="badge bk-provisional">نص مبدئي',
        ">مراجعة</a>",
        ">نسخ</span>",
        "sheet-retry",
        '<div class="sheet-body"></div>',
    ):
        assert needle in shell, needle
    assert "x-" not in shell and ":class" not in shell  # static: no per-shell Alpine bindings
    assert (
        'sheet-review" href="{}" hidden>'.format(_optional("review:page", book.pk, 2)) in shell
    )  # provisional: no review action
    assert 'sheet-review" href="{}">'.format(_optional("review:page", book.pk, 3)) in _shell(body, 2)
    # the body template: scan with line boxes, the mirrored text pane with its designed states
    tpl = body[
        body.index('<template id="sheet-body">') : body.index(
            "</template>", body.index('<template id="sheet-body">')
        )
    ]
    for needle in (
        "sheet-img-scan",
        "sheet-img-clean",
        '<div class="sheet-lines" dir="ltr"',
        '<div class="sheet-fac" dir="rtl"',
        "fac-state-empty",
        "fac-state-excluded",
        "fac-state-error",
        "fac-retry",
        "fac-restore",
    ):
        assert needle in tpl, needle
    # grid: static tiles with the muted marks, no text; the guarded URLs and the live region
    assert body.count('<div class="page-tile') == 4 and '<template id="tile-shell">' in body
    tile = body[
        body.index('<div class="page-tile') : body.index("</form>", body.index('<div class="page-tile'))
    ]
    assert (
        "tile-stage-fill" in tile
        and "bk-mark-check" in tile
        and "bk-mark-count" in tile
        and "sheet-fac" not in tile
    )
    assert 'class="tile-number num">1</span>' in tile and "page-tile-status" not in tile
    assert f"sheetsUrl: '{_optional('api:book_sheets', book.pk)}'" in body and "reviewNextUrl: '" in body
    assert '<p class="sr-only" aria-live="polite" x-text="liveMessage"></p>' in body
    assert 'class="toast bk-toast"' in body and "ابدأ المراجعة" in body
    # D33: «صفحات» is a book viewer: one stage with turn buttons and a filmstrip, no long scroll
    assert 'class="bk-viewer" data-viewer' in body and 'class="bk-stage" x-ref="stage"' in body
    assert '@wheel="onStageWheel($event)"' in body and 'class="bk-film-track" x-ref="film"' in body
    # D34: the pages beside a side panel holding the summary and the filmstrip
    assert 'class="bk-layout"' in body and 'class="bk-main"' in body and '<aside class="bk-side"' in body
    side = body[body.index('<aside class="bk-side"') :]
    assert side.index('class="bk-summary"') < side.index('class="bk-film"') < side.index("</aside>")
    assert body.index('class="bk-viewer"') < body.index('<aside class="bk-side"')
    assert 'class="bk-counts"' in body and body.count('class="bk-count') >= 5
    assert "'has-pages': nPages > 0" in body
    assert 'class="bk-turn bk-turn-prev"' in body and 'class="bk-turn bk-turn-next"' in body
    assert 'aria-label="الصفحة السابقة"' in body and 'aria-label="الصفحة التالية"' in body
    assert 'class="bk-counter"' in body
    assert "'is-viewer': view === 'sheets' && nPages > 0" in body and "filmstripUrl: '" in body
    assert 'class="bk-pos"' not in body  # the scroll position chip went with the long scroll
    # the filmstrip is static like the shells: one thumb template cloned per page and patched from the poll,
    # no x-for over 800 items (§11); the header count is a plain number
    assert 'class="bk-film-track" x-ref="film" data-film-track></div>' in body
    assert '<template id="thumb-shell">' in body and 'x-text="filmCount"' in body
    thumb = body[
        body.index('<template id="thumb-shell">') : body.index(
            "</template>", body.index('<template id="thumb-shell">')
        )
    ]
    assert 'class="bk-thumb" data-number="" title=""' in thumb and "x-" not in thumb and ":class" not in thumb
    for needle in ("bk-thumb-img", "bk-thumb-num", "is-check", "is-count", "is-live"):
        assert needle in thumb, needle
    assert 'x-for="f in film"' not in body and "film.length" not in body
    # the skeleton shimmer is gated on the book being active (D24, §6): the root carries `is-active`
    assert "'is-active': active" in body


def test_dashboard_top_bar_primary_candidates_menu_and_banners(editor_client):
    book, pages = _book([(Page.Status.OCR_DONE, "final"), (Page.Status.REVIEWED, "final")])
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    # every primary candidate is rendered role-gated and toggled from the poll (one visible per state)
    for state in ("start", "review", "copy", "book"):
        assert f"x-show=\"d.primary === '{state}'\"" in body, state
    # PHASE7 §3.12: «ضبط الأدلة» is gone as a primary (and as a word); the «التخطيط» mode is not rendered
    assert "d.primary === 'guides'" not in body and "ضبط الأدلة" not in body and "is-guides" not in body
    assert "data-review-next" in body and "الصفحة التالية للمراجعة" in body
    # Phase 5 (D47): «فتح الكتاب» is the primary once the manuscript is fresh: the book page, the only place
    # to
    # preview and edit; nothing links to the old editor address
    primary = body[body.index("data-book-open") - 120 : body.index("data-book-open") + 200]
    assert (
        "x-show=\"d.primary === 'book'\"" in primary
        and ':href="d.layoutUrl"' in primary
        and "<span>فتح الكتاب</span>" in primary
    )
    assert (
        "d.editorUrl" not in body
        and ':href="editorUrl"' not in body
        and "/editor/" not in body
        and "فتح المحرّر" not in body
        and "data-editor-open" not in body
    )
    assert "x-text=\"d.status === 'error' ? 'إعادة بدء المعالجة' : 'بدء المعالجة'\"" in body
    # D76 (PHASE7 §5.1): the status chip and its bar fold into the stage bar's current step («المعالجة»; the
    # chip's words stay for a screen reader); the poll pill, «⋯» menu with copy / «التخطيط» / re-run items
    # (each asks first, §3.6) / «حذف الكتاب…» / the shortcut sheet / all books
    assert 'class="bk-status"' not in body and 'aria-label="نسبة إتمام المعالجة"' not in body
    assert 'data-stage-bar data-rail data-current="ocr"' in body
    assert '<span class="sr-only" role="status" x-text="d.statusText"></span>' in body
    # the «كل الكتب» ghost is retired (§5.2): the rail's «الكتب» and the «⋯» keep the way to the list
    head = body[
        body.index('<div class="bk-bar" x-data>') : body.index(
            '<template x-if="$store.book && $store.book.dash">'
        )
    ]
    assert "كل الكتب" not in head
    assert "تعذّر التحديث · إعادة المحاولة" in body and "انتهت الجلسة · تسجيل الدخول" in body
    menu = body[
        body.index('class="menu menu-popover bk-menu"') : body.index(
            "</template>", body.index('class="menu menu-popover bk-menu"')
        )
    ]
    assert (  # one book re-run, named by the view (D101): «إعادة المعالجة…» here, for every editor
        menu.count("data-rerun-action=") == 1
        and 'data-rerun-action="ocr"' in menu
        and "<span>إعادة المعالجة…</span>" in menu
        and "data-rerun-stage" not in menu
        and 'name="stage"' not in menu
        and "إعادة التشغيل من مرحلة" not in menu
        and "<span>التخطيط</span>" in menu
        and "data-guides-menu-item" in menu
        and "حذف الكتاب…" in menu
        and "كل الكتب" in menu
        and "اختصارات لوحة المفاتيح" in menu
        and "d.openSheet()" in menu
    )
    assert "d.openRerun('ocr', 'إعادة المعالجة')" in menu and "d.openDelete()" in menu
    # the re-run dialog posts the stage it names, its button says the action; the delete dialog names what
    # is lost (§3.13)
    rerun = body[body.index("data-rerun-dialog") : body.index("data-delete-dialog")]
    assert (
        'name="stage" :value="dialog.stage"' in rerun
        and 'x-ref="rerunSafe"' in rerun
        and 'x-text="rerunButton">إعادة التشغيل</button>' in rerun
    )
    delete = body[body.index("data-delete-dialog") :]
    assert f"حذف الكتاب «{book.title}»؟" in delete and "ولا يمكن التراجع عن ذلك." in delete
    assert 'class="btn btn-danger">حذف الكتاب</button>' in delete and 'x-ref="deleteSafe"' in delete
    assert "نسخ نص الكتاب" in menu and "x-show=\"d.primary !== 'copy'\"" in menu
    # «⋯»: «الكتاب» → the book page, hidden while it is the primary
    item = menu[menu.index("data-book-menu-item") - 200 : menu.index("data-book-menu-item") + 160]
    assert ':href="d.layoutUrl"' in item and "d.primary !== 'book'" in item and "<span>الكتاب</span>" in item
    assert "تنسيق الكتاب ومعاينته" not in menu
    # the side panel's «الكتاب» block (PHASE5 §5, §9): trim and page count, the drift line (D41), the book
    # page
    side = body[body.index('<aside class="bk-side"') :]
    assert side.index('class="bk-manuscript"') < side.index('class="bk-book"') < side.index("bk-attention")
    book_block = side[side.index('class="bk-book"') : side.index("bk-attention")]
    assert (
        'aria-label="الكتاب"' in book_block
        and 'x-text="bookLine"' in book_block
        and ':class="bookDot"' in book_block
    )
    assert 'x-text="driftLine"' in book_block
    assert (
        '<a class="link bk-book-open" :href="layoutUrl" x-show="layoutUrl" data-book-link>فتح الكتاب</a>'
        in book_block
    )
    config = json.loads(
        re.search(r'<script id="dashboard-config" type="application/json">(.*?)</script>', body, re.S).group(
            1
        )
    )
    assert (
        config["editorUrls"]["layout"] == reverse("editor:layout", args=[book.pk])
        and "editor" not in config["editorUrls"]
    )
    assert (
        config["editor"] == {"edited": False, "version": 0, "drift_pages": []}
        and config["layout"]["trim"] == "17x24"
    )
    # banners follow the poll, the review summary lives in the strip
    assert (
        "x-show=\"status === 'error'\"" in body
        and "x-show=\"status === 'needs_guides'\"" not in body
        and 'x-text="errorHeadline' in body
    )
    assert (
        "مُراجَعة <bdi" in body
        and 'x-text="reviewSummary.reviewed"' in body
        and "كلمة غير مؤكَّدة" in body
        and "رُوجعت كل الصفحات" in body
    )
    # a proofreader sees no start form, no rerun forms, no retry / restore forms in the shells
    client = editor_client
    client.force_login(_user("reader", "proofreader"))
    body = client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert "d.primary === 'start'" not in body and 'name="stage"' not in body and "fac-restore" not in body
    assert "data-rerun-dialog" not in body and "data-rerun-action" not in body and "حذف الكتاب" not in body
    assert "d.primary === 'book'" not in body  # the manuscript stays a proofreader's primary
    assert (
        "data-book-menu-item" in body and "data-book-link" in body
    )  # the book page (read-only) is for everyone


def test_dashboard_stays_light_with_800_sheet_placeholders(editor_client):
    book = Book.objects.create(title="كتاب ضخم", status=Book.Status.OCR)
    Page.objects.bulk_create(
        [
            Page.objects.model(
                book=book,
                number=i,
                source_index=i - 1,
                status=Page.Status.OCR_DONE,
                text_state="final",
                width=1000,
                height=1500,
            )
            for i in range(1, 801)
        ]
    )
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert body.count('<article class="page-sheet') == 801  # 800 shells + the clone template
    shell = _shell(body, 0)
    # off-screen shells carry no images, no text and no Alpine bindings: the body mounts from the template
    assert "sheet-img" not in shell and "sheet-fac" not in shell and "x-" not in shell
    assert shell.count("<") < 80
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"\.page-sheet\{[^}]*content-visibility:auto", css)
    assert re.search(r"\.page-sheet\{[^}]*contain-intrinsic-size:auto var\(--sheet-h,640px\)", css)


# ---------------------------------------------------------------- text panel (§2) and page detail


def test_compiled_css_has_the_mirror_pane_effects_and_their_static_fallbacks():
    css = CSS.read_text(encoding="utf-8")
    # the mirror: same box, flex column with space-between, justified text, font from the page (§4)
    assert re.search(
        r"\.sheet-fac\{[^}]*aspect-ratio:var\(--sheet-ar,7 / 10\)[^}]*container-type:inline-size", css
    )
    assert re.search(
        r"\.fac-block\{[^}]*font-size:clamp\(9px, calc\(var\(--fs\) \* 100cqw\), 28px\)"
        r"[^}]*justify-content:space-between",
        css,
    )
    assert re.search(r"\.fac-text\{[^}]*text-align:justify;text-align-last:start", css)
    # a boxed line is placed from the block's right edge (the cross-start of an RTL column flexbox): the
    # start margin is the inset from the block's right edge, so a centred heading or an indented first line
    # lands at its printed x, never flush right
    boxed = re.search(r"\.fac-line\[data-boxed\]\{([^}]*)\}", css)
    assert boxed, "no .fac-line[data-boxed] rule"
    assert "margin-inline-start:calc((var(--x1) - var(--lx1)) * 100cqw)" in boxed.group(1)
    assert "width:calc((var(--lx1) - var(--lx0)) * 100cqw)" in boxed.group(1)
    assert re.search(r"\.fac-line\{[^}]*transition:[^}]*margin-inline-start \.26s", css)
    assert "margin-left:calc((var(--lx0)" not in css
    assert re.search(r"\.sheet-body\{[^}]*grid-template-columns:minmax\(0,1fr\) minmax\(0,1fr\)", css)
    # the viewer takes the room left in `.main` (messages and the column gap included) instead of a
    # viewport calc that overflowed by the gap after a redirect with a message
    assert re.search(
        r"\.main:has\(>\.bk-dashboard\.is-viewer\)\{[^}]*height:calc\(100dvh - var\(--topbar-height\)\)", css
    )
    assert re.search(r"\.bk-dashboard\.is-viewer\{[^}]*height:auto", css)
    # the viewer never grows the page: `.main` keeps its height (no flex basis override)
    assert re.search(r"\.main:has\(>\.bk-dashboard\.is-viewer\)\{[^}]*flex:none", css)
    # the whole side panel scrolls in the viewer (the thumbnails are too small a box to scroll alone)
    assert re.search(r"\.bk-dashboard\.is-viewer \.bk-side\{[^}]*overflow-y:auto", css)
    assert re.search(r"\.bk-dashboard\.is-viewer \.bk-attention-list\{[^}]*position:static", css)
    assert "calc(100dvh - var(--topbar-height) - 16px)" not in css
    # static thumbs: `hidden` beats their display rules
    assert ".bk-thumb[hidden],.bk-thumb-mark[hidden],.bk-thumb-img img[hidden]{display:none}" in css
    # the generation: gray provisional layer, veil as opacity, the lit line's sheen, the scan band (§5)
    assert re.search(r"\.fac-layer\[data-phase=\"?provisional\"?\][^{]*\{color:var\(--color-text-3\)\}", css)
    assert re.search(r"\.fac-text \.tok\.is-veiled\{opacity:\.45", css)
    assert re.search(r"\.fac-line\.is-lit \.fac-text\{[^}]*background-clip:text[^}]*bk-sheen", css)
    assert "@keyframes bk-sheen{0%{background-position-x:0%}to{background-position-x:100%}}" in css
    # owner review 2026-10-03: the lit line carries the scan's band in the text too, a glow crossing it, and is
    # written in ink; the line just read keeps half the band; the review's pending rows the same, word by word
    assert re.search(r"\.fac-layer:not\(\[data-phase=final\]\) \.fac-line\.is-lit:before\{opacity:1;[^}]*bk-glow", css)
    assert re.search(r"\.fac-layer:not\(\[data-phase=final\]\) \.fac-line\.is-lit-2:before\{opacity:\.5", css)
    assert re.search(r"\.fac-layer:not\(\[data-phase=final\]\) \.fac-line:before\{[^}]*var\(--color-accent-soft\)", css)
    assert "@keyframes bk-glow{" in css
    assert re.search(r"\.decode-line\.is-lit:before\{opacity:1;[^}]*bk-glow", css)
    assert re.search(r"\.decode-line\.is-lit \.decode-w\{[^}]*transition:color \.16s ease calc\(var\(--k,0\) \* 26ms\)", css)
    assert (
        "@keyframes bk-shimmer{" in css
        and "@keyframes bk-line-in{" in css
        and "@keyframes bk-stamp-in{" in css
    )
    assert re.search(r"\.sheet-line\.is-lit\{opacity:1;background:var\(--color-accent-soft\)", css)
    assert re.search(r"\.sheet-line\.is-hot:before\{content:attr\(data-n\)", css)
    assert (
        re.search(r"\.fac-layer\[data-phase=\"?final\"?\] \.tok\.is-landed\{[^}]*decode-land", css)
        and "@keyframes decode-land" in css
    )
    assert re.search(r"\.scan-sweep:after\{[^}]*height:14%", css)
    assert "@" not in re.search(r"\.fac-bar\{[^}]*\}", css).group(0)[:0] and "bk-shimmer" in re.search(
        r"\.fac-bar\{[^}]*\}", css
    ).group(0)
    # skeleton bars shimmer only while the book is active (D24, §6): a book that stopped shows still bars
    assert ".bk-dashboard.is-active .page-sheet.is-near .fac-bar{animation-play-state:running}" in css
    assert "}.page-sheet.is-near .fac-bar{" not in css
    # no rv- class is referenced by the dashboard styles (review.css stays untouched)
    theatre = (ROOT / "static" / "src" / "components" / "theatre.css").read_text(encoding="utf-8")
    assert not re.search(r"\.rv-", theatre)
    # reduced motion: sweep, sheen, shimmer, line entrance and the landing wash are all off
    reduced = css[css.index("prefers-reduced-motion:reduce") :]
    assert re.search(r"[^{}]*\.scan-sweep:after[^{}]*\{display:none\}", reduced)
    assert re.search(r"[^{}]*\.fac-bar[^{}]*\{animation:none\}", reduced)
    assert re.search(r"[^{}]*\.fac-line\.is-lit \.fac-text\{[^}]*animation:none", reduced)
    assert re.search(r"[^{}]*\.decode-w\.is-landed[^{}]*\{animation:none\}", reduced)


# ---------------------------------------------------------------- Node: decode.js, books.js

HARNESS = r"""
// tiny DOM: enough for decode.js (text nodes, spans, classList, custom properties) and the Alpine components
class ClassList { constructor(){ this.s = new Set(); } add(...c){ c.forEach(x => this.s.add(x)); } remove(...c){ c.forEach(x => this.s.delete(x)); }
  toggle(c, f){ if (f === undefined) f = !this.s.has(c); f ? this.s.add(c) : this.s.delete(c); return f; } contains(c){ return this.s.has(c); } toString(){ return [...this.s].join(' '); } }
class Node { constructor(){ this.childNodes = []; this.parentNode = null; this.isConnected = true; }
  appendChild(n){ if (n.parentNode) n.parentNode.removeChild(n); n.parentNode = this; this.childNodes.push(n); return n; }
  insertBefore(n, ref){ if (n.parentNode) n.parentNode.removeChild(n); const i = ref ? this.childNodes.indexOf(ref) : -1; n.parentNode = this; if (i < 0) this.childNodes.push(n); else this.childNodes.splice(i, 0, n); return n; }
  removeChild(n){ const i = this.childNodes.indexOf(n); if (i >= 0) this.childNodes.splice(i, 1); n.parentNode = null; return n; }
  remove(){ if (this.parentNode) this.parentNode.removeChild(this); } }
class Text extends Node { constructor(d){ super(); this.nodeValue = d; } get textContent(){ return this.nodeValue; } }
class Element extends Node {
  constructor(tag){ super(); this.tagName = tag; this.classList = new ClassList(); this.attrs = {}; this.style = { setProperty: (k, v) => { this.style[k] = v; }, getPropertyValue: (k) => this.style[k] || '' }; this.dataset = {}; this.hidden = false; }
  set className(v){ this.classList = new ClassList(); v.split(/\s+/).filter(Boolean).forEach(c => this.classList.add(c)); } get className(){ return this.classList.toString(); }
  setAttribute(k, v){ this.attrs[k] = String(v); } getAttribute(k){ return k in this.attrs ? this.attrs[k] : null; } removeAttribute(k){ delete this.attrs[k]; }
  set textContent(v){ this.childNodes.forEach(n => { n.parentNode = null; }); this.childNodes = v ? [new Text(String(v))] : []; }
  get textContent(){ return this.childNodes.map(n => n.textContent).join(''); }
  get children(){ return this.childNodes.filter(n => n instanceof Element); }
  querySelectorAll(){ return []; } querySelector(){ return null; }
}
const inits = []; const reg = {}; const timers = []; const toasts = [];
globalThis.window = globalThis;
globalThis.document = { hidden: false, createElement: (t) => new Element(t), createTextNode: (d) => new Text(d),
  addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); }, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: () => null };
globalThis.CustomEvent = class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } };
globalThis.dispatchEvent = () => true;
globalThis.location = { hash: '', assign: () => {}, reload: () => { out.reloaded = true; } };
globalThis.addEventListener = () => {};
let NOW = 1000; globalThis.performance = { now: () => NOW };
globalThis.requestAnimationFrame = () => 1; // the tests drive NassakhDecode.step(t) by hand
globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
globalThis.clearTimeout = () => {};
const store = {}; globalThis.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
globalThis.sessionStorage = { getItem: () => null, setItem: () => {} };
const fs = require('fs');
for (const f of process.argv.slice(2)) eval(fs.readFileSync(f, 'utf8'));
inits.forEach((fn) => fn());
window.Nassakh.toast = (m) => toasts.push(m);
const D = NassakhDecode;
D.measure = (text) => Array.from(text).length * 50; // a deterministic width at 100 px
const out = {};
const run = (el, ms, every = 41) => { for (let t = NOW; t <= NOW + ms; t += every) D.step(t); NOW += ms; };
const linesOf = (el) => el.children.map((row) => ({ region: row.getAttribute('data-region') || '', words: row.children.map((w) => ({ t: w.textContent, cls: w.className })) }));
const FAMILIES = ['بتثني', 'جحخ', 'دذ', 'رز', 'سش', 'صض', 'طظ', 'عغ', 'فق', 'هة', 'وؤ', 'اأإآ', 'كل'];
const familyOf = (ch) => FAMILIES.find((f) => f.includes(ch)) || 'بنت';
const BASE = /[ء-غف-يٮ-ٯٱ-ۓۺ-ۼ]/;
// D28 invariants of one shown string against its real word: exact, or 1–3 same-family base letters differ,
// never the first letter, never a digit or a mark, same length, no ASCII.
function veilOk(real, shown) {
  if (real === shown) return true;
  const r = Array.from(real), s = Array.from(shown);
  if (r.length !== s.length) return 'length';
  if (/[\x00-\x7f]/.test(shown)) return 'ascii';
  const firstBase = r.findIndex((c) => BASE.test(c));
  const diffs = r.map((c, i) => (c === s[i] ? -1 : i)).filter((i) => i >= 0);
  if (diffs.length < 1 || diffs.length > 3) return 'count ' + diffs.length;
  for (const i of diffs) {
    if (i === firstBase) return 'first';
    if (!BASE.test(r[i])) return 'not-base';
    if (!familyOf(r[i]).includes(s[i]) || s[i] === r[i]) return 'family';
  }
  return true;
}

// --- §5.2 provisional (text panel path): dwell on the exact word, veil briefly, return
const el = new Element('div');
D.attach(el, { mode: 'provisional', text: 'قال الأمير في سنة 1966\nوَفِي الشهر الثاني' });
const words = D.inspect(el);
out.words = words.map((w) => w.real);
out.lines = el.children.length;
const everExact = words.map(() => false), everVeiled = words.map(() => false);
let veilBad = null, maxVeiled = 0, changed = 0, veiledClassOk = true;
let prev = D.inspect(el).map((w) => w.shown);
for (let i = 0; i < 20000 / 41; i += 1) {
  NOW += 41; D.step(NOW);
  const snap = D.inspect(el);
  let nv = 0;
  snap.forEach((w, k) => {
    const ok = veilOk(w.real, w.shown);
    if (ok !== true && !veilBad) veilBad = [w.real, w.shown, ok];
    if (w.shown === w.real) everExact[k] = true; else everVeiled[k] = true;
    if (w.veiled) nv += 1;
    if (w.veiled !== el.children[w.line].children.filter((s) => s.textContent === w.shown).some((s) => s.classList.contains('is-veiled')) && w.veiled) veiledClassOk = false;
    if (w.shown !== prev[k]) changed += 1;
  });
  maxVeiled = Math.max(maxVeiled, nv);
  prev = snap.map((w) => w.shown);
}
const veilable = (real) => Array.from(real).filter((c) => BASE.test(c)).length >= 2;
out.veilBad = veilBad; out.maxVeiled = maxVeiled; out.changed = changed > 0; out.veiledClassOk = veiledClassOk;
out.everExact = everExact.every(Boolean);
out.everVeiled = words.every((w, k) => !veilable(w.real) || everVeiled[k]);
out.digitsStill = words.filter((w) => !veilable(w.real)).every((w, k) => !everVeiled[words.indexOf(w)]);
out.decodingClass = el.classList.contains('is-decoding') && el.getAttribute('aria-hidden') === 'true';
out.tokenNodes = el.children[0].children.every((w) => w.childNodes.length === 1 && w.childNodes[0] instanceof Text);
out.spaces = el.children[0].childNodes.filter((n) => n instanceof Text && n.nodeValue === ' ').length;

// --- noise mode: Arabic-only pseudo-words sized to the page, low-opacity class
const noise = new Element('div');
D.attach(noise, { mode: 'noise', lines: 7 });
run(noise, 300);
out.noiseLines = noise.children.length;
out.noiseClass = noise.classList.contains('is-noise');
out.noiseArabic = D.inspect(noise).every((w) => /^[ء-ي]+$/.test(w.shown) && w.nReal === 0);

// --- the reading cursor of an attached element (review's pending page, owner review 2026-10-03): Tesseract's lines
// as given, a band walking them row by row (the row just read half lit), `onLine` hearing each step, the words under
// the band exact, each word carrying its place in the row (`--k`); new lines keep the cursor; the wave stops it
const rows = new Element('div');
const heard = [];
const tessLines = [{ region_kind: 'body', words: ['قال', 'الأمير', 'الكبير'] }, { words: ['وفي', 'الشهر'] }, { region_kind: 'footnote', words: ['حاشية'] }];
D.attach(rows, { mode: 'provisional', lines: tessLines, cursor: true, onLine: (k) => heard.push(k) });
const litOf = () => rows.children.map((r) => (r.classList.contains('is-lit') ? 'L' : r.classList.contains('is-lit-2') ? 'h' : '.')).join('');
const seen = []; let rowsExact = true;
for (let i = 0; i < 50; i += 1) {
  NOW += 41; D.step(NOW);
  const s = litOf(); if (seen[seen.length - 1] !== s) seen.push(s);
  const k = s.indexOf('L');
  if (k >= 0 && D.inspect(rows).some((w) => w.line === k && w.shown !== w.real)) rowsExact = false;
}
const realLines = (el) => el.children.map((_, k) => D.inspect(el).filter((w) => w.line === k).map((w) => w.real).join(' '));
out.rows = { text: realLines(rows), region: rows.children[2].getAttribute('data-region'), seen, heard: heard.slice(), exact: rowsExact,
  k: rows.children[0].children.map((w) => w.style['--k']), decoding: rows.classList.contains('is-decoding') };
D.update(rows, { lines: [{ words: ['سطر', 'جديد'] }, { words: ['وآخر'] }] });
heard.length = 0; run(rows, 1000);
out.rowsUpdated = { text: realLines(rows), heard: heard.slice() };
D.resolve(rows, { lines: [{ tokens: [{ t: 'سطر', conf: 'high' }] }], onDone: () => {} });
out.rowsResolved = { last: heard[heard.length - 1], lit: litOf() };
const quiet = new Element('div');
D.attach(quiet, { mode: 'noise', lines: 3 }); run(quiet, 1000);
out.noCursor = quiet.children.every((r) => !r.classList.contains('is-lit'));
D.detach(rows); D.detach(quiet); // the registry count below is the other elements'

// --- resolve wave: reading order, final tokens, tok-low only on unresolved low-confidence words
let done = 0;
const finalLines = [
  { region_kind: 'body', tokens: [{ t: 'قال', conf: 'high' }, { t: 'الأمير', conf: 'low' }, { t: 'في', conf: 'high' }] },
  { region_kind: 'footnote', tokens: [{ t: 'حاشية', conf: 'low', res: 'primary' }, { t: '12', conf: 'low', digit: true }] },
];
D.resolve(el, { lines: finalLines, onDone: () => { done += 1; } });
out.resolvingClass = el.classList.contains('is-resolving');
NOW += 41; D.step(NOW);
const mid = () => D.inspect(el).map((w) => w.landed);
let landedPrefix = true;
for (let i = 0; i < 12; i += 1) { // 5 words: the wave lasts 600 ms, onDone follows 300 ms later
  NOW += 41; D.step(NOW);
  const l = mid(); const firstNo = l.indexOf(false);
  if (firstNo !== -1 && l.slice(firstNo).some(Boolean)) landedPrefix = false; // words land right-to-left, line by line
}
out.landedPrefix = landedPrefix;
out.doneEarly = done;
run(el, 3000);
out.done = done;
out.final = linesOf(el);
out.resolvedClass = el.classList.contains('is-resolved') && el.getAttribute('aria-hidden') === 'false';

// --- an unmounted element is forgotten by the loop
const gone = new Element('div');
D.attach(gone, { mode: 'provisional', text: 'كلمة أخرى' });
const before = D.size; gone.isConnected = false; D.step(NOW + 41);
out.forgotten = [before, D.size];

// --- reduced motion: static faded text, instant resolve
D.reducedMotion = true;
const rm = new Element('div');
D.attach(rm, { mode: 'provisional', text: 'نص ثابت هنا' });
out.rmStatic = rm.classList.contains('is-static') && D.inspect(rm).every((w) => w.shown === w.real);
let rmDone = false;
D.resolve(rm, { lines: finalLines, onDone: () => { rmDone = true; } });
out.rmResolve = rmDone && linesOf(rm)[0].words.map((w) => w.t).join(' ');
D.reducedMotion = false;

// --- §4 layout(): pure geometry
const W = 1000, H = 1500, HW = H / W;
const long = 'كلمة كلمة كلمة كلمة كلمة كلمة كلمة كلمة'; // 8 words → r ≈ 1.02 at the box font: justify
const boxLine = (k, text, kind = 'body') => ({ region_kind: kind, bbox: [0.15, 0.1 + 0.05 * k, 0.85, 0.13 + 0.05 * k], words: text.split(' ') });
const regular = { width: W, height: H, text_state: 'provisional', line_boxes: [], provisional_lines: [...Array.from({ length: 9 }, (_, k) => boxLine(k, long)), boxLine(9, 'كلمة كلمة كلمة')] };
const L1 = D.layout(regular);
const g1 = L1.groups[0];
out.layRegular = { mode: L1.mode, groups: L1.groups.length, kind: g1.kind, source: g1.source, block: g1.block.map((v) => +v.toFixed(4)), fs: +g1.fsCw.toFixed(4), lh: +g1.lhCw.toFixed(4), few: g1.few,
  fits: g1.lines.map((l) => l.fit), rs: g1.lines.map((l) => l.r), ids: g1.lines.map((l) => l.i), lx: [g1.lines[0].lx0, g1.lines[0].lx1], rule: L1.rule };
out.layRegularExpect = { fs: +(0.78 * 0.03 * HW).toFixed(4), lh: +(1.24 * 0.03 * HW).toFixed(4), y0: +(0.1 - 0.12 * 0.03).toFixed(4), y1: +(0.58 + 0.12 * 0.03).toFixed(4) };
const plain = { width: W, height: H, text_state: 'provisional', provisional_text: Array.from({ length: 12 }, () => long).join('\n') };
const L2 = D.layout(plain); const g2 = L2.groups[0];
out.layPlain = { source: g2.source, block: g2.block.map((v) => +v.toFixed(4)), fs: +g2.fsCw.toFixed(4), few: g2.few, n: g2.lines.length, boxed: g2.lines.some((l) => l.box), lx: [g2.lines[3].lx0, g2.lines[3].lx1] };
const pitch = 0.8 / 12, hgPitch = 0.62 * pitch;
out.layPlainExpect = { fs: +Math.max(76 / 1950, 0.75 * hgPitch * HW).toFixed(4), y0: +(0.1 - 0.12 * hgPitch).toFixed(4) };
const title = { width: W, height: H, text_state: 'provisional', provisional_text: 'عنوان الكتاب\nاسم المؤلف' };
const L3 = D.layout(title); const g3 = L3.groups[0];
out.layTitle = { few: g3.few, fs: +g3.fsCw.toFixed(4), n: g3.lines.length, fits: g3.lines.map((l) => l.fit) };
out.layTitleExpect = +(0.016 * HW).toFixed(4);
const withNotes = { width: W, height: H, text_state: 'provisional', footnote_y: 0.8, median_line_h: 0.03,
  provisional_lines: [boxLine(0, long), boxLine(1, long), { region_kind: 'footnote', bbox: [0.15, 0.82, 0.85, 0.845], words: long.split(' ') }, { region_kind: 'footnote', bbox: [0.15, 0.86, 0.85, 0.885], words: ['حاشية', 'ثانية'] }] };
const L4 = D.layout(withNotes);
out.layNotes = { groups: L4.groups.map((g) => g.kind), rule: L4.rule, noteY0: +L4.groups[1].block[1].toFixed(3), noteFsCapped: L4.groups[1].fsCw <= 0.9 * L4.groups[0].fsCw + 1e-9, ids: L4.groups.flatMap((g) => g.lines.map((l) => l.i)), noteKinds: L4.groups[1].lines.map((l) => l.kind) };
// D34 footnotes set solid: line box = 1.5 × type size, block = the lines' height, never spread, never "few"-centred
const solidNotes = (lines) => D.layout({ width: W, height: H, text_state: 'final', footnote_y: 0.7, median_line_h: 0.03, lines });
const noteLine = (y0, y1, words) => ({ region_kind: 'footnote', bbox: [0.15, y0, 0.85, y1], tokens: words.split(' ').map((t) => ({ t, conf: 'high' })) });
const bodyLine = (y0) => ({ region_kind: 'body', bbox: [0.12, y0, 0.88, y0 + 0.03], tokens: long.split(' ').map((t) => ({ t, conf: 'high' })) });
const spread = solidNotes([bodyLine(0.1), bodyLine(0.15), noteLine(0.72, 0.745, long), noteLine(0.80, 0.825, long), noteLine(0.88, 0.905, 'حاشية قصيرة')]);
const gN = spread.groups[1]; const HWn = H / W;
out.laySolid = { solid: gN.solid, few: gN.few, bodySolid: spread.groups[0].solid, lead: +(gN.lhCw / gN.fsCw).toFixed(3) <= 1.5,
  packed: Math.abs((gN.block[3] - gN.block[1]) * HWn - gN.lines.length * gN.lhCw) < 1e-6, top: +gN.block[1].toFixed(4), topExpect: +(0.72 - 0.12 * 0.025).toFixed(4),
  shorter: gN.block[3] < 0.905 };
// three notes printed side by side on one row: stacked at their size, running on below the row instead of shrinking to fit it
const row = solidNotes([bodyLine(0.1), noteLine(0.90, 0.925, 'أولى قصيرة'), noteLine(0.90, 0.925, 'ثانية قصيرة'), noteLine(0.90, 0.925, 'ثالثة قصيرة')]);
const gR = row.groups[1];
out.laySolidRow = { fsKept: gR.fsCw > 0.8 * Math.min(0.78 * 0.025 * HWn, 0.9 * row.groups[0].fsCw), bottom: +gR.block[3].toFixed(4), within: gR.block[3] <= 0.97 + 1e-9 };
// footnotes without text boxes: the detected boxes are the letters' core band (a quarter of a line), so the
// notes take their size from the body's lines instead of shrinking to a few pixels
const core = D.layout({ width: W, height: H, text_state: 'final', footnote_y: 0.7, median_line_h: 0.008, line_boxes: [[0.15, 0.80, 0.85, 0.804]],
  lines: [bodyLine(0.1), bodyLine(0.15), bodyLine(0.2), { ...noteLine(0, 0, 'حاشية بلا صندوق'), bbox: null }, { ...noteLine(0, 0, 'سطر ثان'), bbox: null }] });
out.layNoteFromBody = { source: core.groups[1].source, ratio: +(core.groups[1].fsCw / core.groups[0].fsCw).toFixed(2) };
const finalNoBox = { width: W, height: H, text_state: 'final', line_boxes: [], lines: Array.from({ length: 6 }, () => ({ region_kind: 'body', bbox: null, tokens: long.split(' ').map((t) => ({ t, conf: 'high' })) })) };
const L5 = D.layout(finalNoBox); const g5 = L5.groups[0];
out.layFinalNoBox = { mode: L5.mode, source: g5.source, boxed: g5.lines.some((l) => l.box), spans: g5.lines.every((l) => l.lx0 === g5.block[0] && l.lx1 === g5.block[2]), ids: g5.lines.map((l) => l.i), tokens: g5.lines[0].tokens.length };
const skeletonBoxes = { width: W, height: H, text_state: 'none', status: 'preprocessed', line_boxes: Array.from({ length: 5 }, (_, k) => [0.2, 0.2 + 0.06 * k, 0.8, 0.23 + 0.06 * k]) };
const L6 = D.layout(skeletonBoxes);
out.laySkeleton = { mode: L6.mode, n: L6.groups[0].lines.length, boxed: L6.groups[0].lines.every((l) => l.box), source: L6.groups[0].source };
const L7 = D.layout({ width: W, height: H, text_state: 'none', status: 'uploaded' });
out.layBare = { mode: L7.mode, n: L7.groups[0].lines.length, bars: L7.groups[0].lines.map((l) => l.bar).slice(0, 4), block: L7.groups[0].block.map((v) => +v.toFixed(3)) };
out.layEmpty = D.layout({ width: W, height: H, text_state: 'final', lines: [] }).mode;
const centred = { width: W, height: H, text_state: 'final', lines: [
  { region_kind: 'heading', bbox: [0.4, 0.1, 0.6, 0.13], tokens: [{ t: 'باب' }] },
  ...Array.from({ length: 4 }, (_, k) => ({ region_kind: 'body', bbox: [0.15, 0.2 + 0.05 * k, 0.85, 0.23 + 0.05 * k], tokens: long.split(' ').map((t) => ({ t })) })) ] };
out.layCentred = D.layout(centred).groups[0].lines[0].fit;

// --- §4.7 (D30): one size per group, shared paragraph edges, lines fitted by word spacing, book line height
const toks = (text) => text.split(' ').map((t) => ({ t }));
const jitter = [0.150, 0.152, 0.149, 0.153, 0.151, 0.150];
const para = { width: W, height: H, text_state: 'final', median_line_h: 0.03, lines: [
  { region_kind: 'body', bbox: [0.4, 0.06, 0.6, 0.09], tokens: toks('باب') }, // 0 centred heading
  { region_kind: 'body', bbox: [0.151, 0.10, 0.82, 0.13], tokens: toks(long) }, // 1 indented first line
  ...jitter.map((x0, k) => ({ region_kind: 'body', bbox: [x0, 0.15 + 0.05 * k, 0.85 - 0.002 * (k % 2), 0.18 + 0.05 * k], tokens: toks(long) })), // 2..7 full, jittered
  { region_kind: 'body', bbox: [0.55, 0.45, 0.851, 0.48], tokens: toks('كلمة كلمة') }, // 8 the paragraph's last line
  { region_kind: 'body', bbox: [0.15, 0.50, 0.85, 0.53], tokens: toks(long + ' ' + long) }, // 9 two lines merged: far too long
  { region_kind: 'body', bbox: [0.15, 0.55, 0.85, 0.58], tokens: toks(long + ' كلمة') }, // 10 one word too many
] };
const gp = D.layout(para).groups[0];
out.layPara = { fs: gp.fsCw, hasScale: gp.lines.some((l) => 'scale' in l),
  lines: gp.lines.map((l) => ({ shape: l.shape, fit: l.fit, lx0: +l.lx0.toFixed(4), lx1: +l.lx1.toFixed(4), ws: l.ws, sx: l.sx, r: l.r })) };
out.layParaHeightFs = 0.78 * 0.03 * HW;
// D31: a paragraph's first line stays flush left with its indent; little text → end-aligned; long text uses the indent
const indentPage = { width: W, height: H, text_state: 'final', median_line_h: 0.03, lines: [
  { region_kind: 'body', bbox: [0.15, 0.10, 0.83, 0.13], tokens: toks('كلمة كلمة') },
  ...Array.from({ length: 5 }, (_, k) => ({ region_kind: 'body', bbox: [0.15, 0.15 + 0.05 * k, 0.85, 0.18 + 0.05 * k], tokens: toks(long) })),
  { region_kind: 'body', bbox: [0.15, 0.45, 0.80, 0.48], tokens: toks(long + ' كلمة') },
] };
out.layIndent = D.layout(indentPage).groups[0].lines.map((l) => ({ shape: l.shape, fit: l.fit, lx0: +l.lx0.toFixed(4), lx1: +l.lx1.toFixed(4) }));
out.layParaSpace = (50 * gp.fsCw) / 100; // one space at the group size (the harness measures 50 per char)
const six = 'كلمة كلمة كلمة كلمة كلمة كلمة';
const bookBase = { width: W, height: H, text_state: 'final', median_line_h: 0.03,
  lines: Array.from({ length: 6 }, (_, k) => ({ region_kind: 'body', bbox: [0.15, 0.1 + 0.05 * k, 0.85, 0.13 + 0.05 * k], tokens: toks(six) })) };
out.layBook = { own: +D.layout(bookBase).groups[0].fsCw.toFixed(5), near: +D.layout({ ...bookBase, book_line_h_px: 49.5 }).groups[0].fsCw.toFixed(5),
  far: +D.layout({ ...bookBase, book_line_h_px: 70 }).groups[0].fsCw.toFixed(5) };
// rendered: word spacing and condense on the line element, never a per-line font size
const findAll = (node, cls, acc = []) => { (node.children || []).forEach((c) => { if (c.classList && c.classList.contains(cls)) acc.push(c); findAll(c, cls, acc); }); return acc; };
const hostP = new Element('div');
D.sheet(hostP, { scan: new Element('div'), figure: new Element('figure') }).update({ page: { ...para, id: 99, number: 9, status: 'ocr_done' }, active: false });
const facLines = findAll(hostP, 'fac-line');
out.layParaDom = { n: facLines.length, fits: facLines.map((e) => e.getAttribute('data-fit')), anyFit: facLines.some((e) => e.style['--fit'] !== undefined),
  tightWs: facLines[10] && facLines[10].style['--ws'], overSx: facLines[9] && facLines[9].style['--sx'], plainWs: facLines[3] && facLines[3].style['--ws'] };

// --- §13.1 sheet handle: skeleton → provisional (cursor, sheen, exact under the band) → final wave; hot both sides
const host = new Element('div'), scan = new Element('div'), figure = new Element('figure');
const h = D.sheet(host, { scan, figure });
const skel = { id: 1, number: 1, width: W, height: H, status: 'preprocessed', text_state: 'none', line_boxes: skeletonBoxes.line_boxes };
out.hSkel = { mode: h.update({ page: skel, active: true }), phase: host.children[0].getAttribute('data-phase'), bars: host.children[0].children[0].children.length, boxes: scan.children.length, dataMode: host.getAttribute('data-mode') };
NOW += 41; D.step(NOW);
out.hSkelCursor = { cursor: h.cursor, scanLit: scan.children[0].classList.contains('is-lit'), lineLit: host.children[0].children[0].children[0].classList.contains('is-lit') };
const provWords = Array.from({ length: 12 }, (_, k) => `س${k}طر كلمة أخرى ثالثة رابعة خامسة`.replace(/\d/g, ''));
const prov = { ...skel, status: 'layout_done', text_state: 'provisional', line_boxes: [], provisional_lines: provWords.map((t, k) => ({ region_kind: 'body', bbox: [0.15, 0.1 + 0.05 * k, 0.85, 0.13 + 0.05 * k], words: t.split(' ') })) };
out.hProv = { mode: h.update({ page: prov, active: true }), phase: host.children[0].getAttribute('data-phase'), layers: host.children.length, lines: h.lines, boxes: scan.children.length, toks: host.children[0].children[0].children[0].children[0].children.length };
let litOk = true, trailOk = true, visited = new Set(), maxSheetVeiled = 0, sheetBad = null; const sheetExact = new Set(), sheetVeiled = new Set();
for (let i = 0; i < 20000 / 41; i += 1) {
  NOW += 41; D.step(NOW);
  const k = h.cursor; if (k >= 0) visited.add(k);
  // the line just read keeps half the band on both sides (owner review 2026-10-03); nothing else is lit
  if (k > 0 && !(h.lineEl(k - 1).classList.contains('is-lit-2') && scan.children[k - 1].classList.contains('is-lit-2'))) trailOk = false;
  if (Array.from({ length: h.lines }, (_, j) => h.lineEl(j)).filter((l) => l.classList.contains('is-lit') || l.classList.contains('is-lit-2')).length > 2) trailOk = false;
  const snap = D.inspect(host);
  let nv = 0;
  snap.forEach((w, j) => {
    const ok = veilOk(w.real, w.shown); if (ok !== true && !sheetBad) sheetBad = [w.real, w.shown, ok];
    if (w.veiled) nv += 1;
    if (w.shown === w.real) sheetExact.add(j); else sheetVeiled.add(j);
    if (k >= 0 && w.line === k && w.shown !== w.real) litOk = false; // the lit line's words are exact
  });
  if (k >= 0) { const lineEl = h.lineEl(k); if (!lineEl.classList.contains('is-lit') || !scan.children[k].classList.contains('is-lit')) litOk = false; }
  maxSheetVeiled = Math.max(maxSheetVeiled, nv);
}
const nWords = D.inspect(host).length;
out.hCycle = { litOk, trailOk, visited: visited.size, words: nWords, maxVeiled: maxSheetVeiled, bad: sheetBad, allExact: sheetExact.size === nWords, allVeiled: sheetVeiled.size === nWords };
h.hot(2);
out.hHot = [h.hotIndex, scan.children[2].classList.contains('is-hot'), h.lineEl(2).classList.contains('is-hot')];
h.hot(-1);
out.hHotOff = [h.hotIndex, scan.children[2].classList.contains('is-hot')];
const finalPage = { ...prov, status: 'ocr_done', text_state: 'final', provisional_lines: [], lines: provWords.map((t, k) => ({ id: k + 1, region_kind: 'body', bbox: [0.15, 0.1 + 0.05 * k, 0.85, 0.13 + 0.05 * k], tokens: t.split(' ').map((w, j) => ({ t: w, conf: j === 1 ? 'low' : 'high' })) })) };
out.hFinal = { mode: h.update({ page: finalPage, active: true }), layers: host.children.length, phases: host.children.map((c) => c.getAttribute('data-phase')), leaving: host.children[1].classList.contains('is-leaving') };
let wavePrefix = true, front = [];
for (let i = 0; i < 20; i += 1) {
  NOW += 41; D.step(NOW);
  const l = D.inspect(host).map((w) => w.landed); const firstNo = l.indexOf(false);
  if (firstNo !== -1 && l.slice(firstNo).some(Boolean)) wavePrefix = false;
  const lit = scan.children.findIndex((b) => b.classList.contains('is-lit')); if (lit >= 0 && front[front.length - 1] !== lit) front.push(lit);
}
run(host, 3000);
out.hWave = { wavePrefix, frontMonotonic: front.every((v, i) => i === 0 || v > front[i - 1]), frontMoved: front.length > 3, landed: D.inspect(host).every((w) => w.landed), layers: host.children.length, resolved: host.children[0].classList.contains('is-resolved'), aria: host.children[0].getAttribute('aria-hidden'), lit: scan.children.some((b) => b.classList.contains('is-lit')), low: host.children[0].children[0].children[0].children[0].children[1].classList.contains('tok-low'), tabindex: h.lineEl(0).getAttribute('tabindex') };
// designed states and a static (inactive) provisional sheet
const host2 = new Element('div'); const h2 = D.sheet(host2, { scan: new Element('div') });
out.hStatic = { mode: h2.update({ page: prov, active: false }), cls: host2.children[0].classList.contains('is-static'), exact: D.inspect(host2).every((w) => w.shown === w.real) };
out.hResume = h2.update({ page: prov, active: true });
out.hStop = [h2.update({ page: prov, active: false }), host2.children.length];
out.hStates = [h2.update({ page: { ...prov, is_excluded: true } }), h2.update({ page: { ...prov, is_excluded: false, error: 'فشل' } }), h2.update({ page: { ...skel, text_state: 'final', lines: [] } }), h2.update({ page: { ...skel, text_state: 'final' } })];
const host3 = new Element('div'); const h3 = D.sheet(host3, {});
out.hLate = { mode: h3.update({ page: finalPage, active: false }), resolved: host3.children[0].classList.contains('is-resolved'), landed: D.inspect(host3).every((w) => w.landed) };
D.reducedMotion = true;
const host4 = new Element('div'); const h4 = D.sheet(host4, { scan: new Element('div') });
out.hReduced = [h4.update({ page: prov, active: true }), h4.update({ page: finalPage, active: true }), host4.children.length, host4.children[0].classList.contains('is-resolved')];
D.reducedMotion = false;
h.destroy(); h2.destroy(); h3.destroy(); h4.destroy();

// --- §13.2 books.js: compact merge, changed set, filters, jump, primary per state, no reload, follow target
const pagesCfg = [
  { id: 1, number: 1, status: 'ocr_done', status_label: 'تم التعرّف', dot: 'dot-success', text_state: 'final', n_unresolved: 4, url: '/books/1/pages/1/', review_url: '/books/1/review/1/', width: 700, height: 1000 },
  { id: 2, number: 2, status: 'reviewed', status_label: 'مُراجَعة', dot: 'dot-success', text_state: 'final', is_reviewed: true, url: '/books/1/pages/2/', width: 800, height: 1000 },
  { id: 3, number: 3, status: 'uploaded', status_label: 'مرفوعة', dot: 'dot-neutral', text_state: 'none', is_excluded: true, url: '/books/1/pages/3/' },
];
const mk = (extra = {}) => { const d = reg.bookDashboard({ progressUrl: '/p', sheetsUrl: '/api/books/1/sheets/', reviewNextUrl: '/books/1/review/next/', bookUrl: '/books/1/', bookTextUrl: '/api/books/1/text/', canEdit: true, bookId: 1, active: true, status: 'ocr',
  stages: [{ key: 'ocr_done', statuses: ['ocr_done', 'reviewed', 'assembled'] }], byStatus: { ocr_done: 1, reviewed: 1 }, pages: pagesCfg, ...extra }); d.$watch = () => {}; d.init(); return d; };
const dash = mk();
out.viewDefault = dash.view; dash.setView('grid'); out.viewStored = store['nassakh.bookView']; dash.setView('sheets');
out.ranges = dash.batchRanges([3, 1, 2, 5, 6, 7, 50, 51, ...Array.from({ length: 45 }, (_, i) => 100 + i)]);
out.counts = { ...dash.counts };
out.review = dash.reviewSummary; out.nextReview = dash.nextReviewUrl; out.primaryReview = dash.primary;
dash.review = { reviewed: 1, total: 2, unresolved_total: 9, next_review_url: '' };
out.reviewFromPoll = dash.reviewSummary.next_review_url;
out.aspect = [dash.aspectOf(dash.tile(2)), dash.aspectOf(dash.tile(3))];
dash.applySheets({ pages: [{ id: 1, number: 1, width: 700, height: 1000, text_state: 'final', status: 'ocr_done', line_boxes: [[0.1, 0.2, 0.9, 0.25]],
  lines: [{ region_kind: 'body', tokens: [{ t: 'قال' }, { t: 'الأمير' }] }, { region_kind: 'footnote', tokens: [{ t: 'حاشية' }] }] }] });
out.sheetText = dash.sheetText(1); out.canCopy = [dash.canCopy(1), dash.canCopy(2)];
out.stage = [dash.stagePercent({ status: 'uploaded' }), dash.stagePercent({ status: 'layout_done' }), dash.stagePercent({ status: 'ocr_done' }), dash.stagePercent({ status: 'error', error: true })];
dash.mountSheet({ dataset: { pageId: '1' } }); // a near sheet (no body template under Node: mounted only)
const fetched = [];
globalThis.fetch = (url) => { fetched.push(url); return Promise.resolve({ ok: true, json: async () => ({ pages: [] }) }); };
dash.apply({ total: 4, percent: 50, flags: 0, status: 'ocr', status_label: 'قيد التعرّف', dot: 'dot-accent', by_status: { reviewed: 2, uploaded: 2 }, active: true, error_headline: '', error_detail: '',
  pages: [{ id: 1, number: 1, status: 'reviewed', text_state: 'final', is_reviewed: true, n_unresolved: 0 }, { id: 2, number: 2, status: 'reviewed', text_state: 'final', is_reviewed: true },
          { id: 3, number: 3, status: 'uploaded', text_state: 'none', is_excluded: true }, { id: 4, number: 4, status: 'uploaded', text_state: 'none' }] });
out.merged = { label: dash.page(1).status_label, dot: dash.page(1).dot, url: dash.page(1).url, review: dash.page(1).review_url, stale: dash.page(1).stale, unresolved: dash.page(1).n_unresolved };
out.added = { url: dash.page(4).url, rerun: dash.page(4).rerun_url, label: dash.page(4).status_label, dot: dash.page(4).dot };
out.live = dash.liveMessage; out.countsAfter = { ...dash.counts };
const flush = timers.filter((t) => t.ms === 60).pop(); flush && flush.fn();
out.refetch = fetched;
// filters
dash.setFilter('reviewed'); out.filterStored = store['nassakh.bookFilter.1']; out.filteredOut1 = dash.filteredOut;
dash.setFilter('attention'); out.filteredOut2 = dash.filteredOut; dash.setFilter('all');
out.matches = ['processing', 'review', 'attention', 'reviewed'].map((f) => { dash.setFilter(f); return [dash.matches(dash.page(4)), dash.matches(dash.page(1)), dash.matches(dash.page(3))]; }); dash.setFilter('all');
// jump
out.jump = [dash.jumpTarget('٢'), dash.jumpTarget(' 4 '), dash.jumpTarget('99'), dash.jumpTarget('abc'), dash.jump('99'), toasts.slice(-1)[0]];
// primary per state
const states = [];
dash.status = 'uploaded'; states.push(dash.primary);
dash.status = 'error'; states.push(dash.primary);
dash.status = 'needs_guides'; states.push(dash.primary);
const dg = mk({ status: 'needs_guides', active: false }); states.push(dg.primary); // no «ضبط الأدلة» any more (D65)
dash.status = 'ocr'; dash.review = { reviewed: 1, total: 2, unresolved_total: 0, next_review_url: '/books/1/review/next/' }; states.push(dash.primary);
dash.review = { reviewed: 2, total: 2, unresolved_total: 0, next_review_url: null }; states.push(dash.primary);
dash.canEdit = false; dash.status = 'uploaded'; states.push(dash.primary);
out.primary = states;
out.statusText = [dash.statusText, (dash.active = false, dash.statusText)];
// --- Phase 5: the book page is the primary once the manuscript is fresh (and after edits, with the drift resolved
// per chapter, D41); the «الكتاب» line follows the newest render; a proofreader or a missing URL keeps the manuscript
const fresh = { exists: true, active: false, stale: false, stale_pages: [], run: { status: 'done' }, warnings_count: 2, stats: { pages_included: 214 } };
const p5cfg = { active: false, status: 'reviewing', review: { reviewed: 2, total: 2, unresolved_total: 0, next_review_url: null }, manuscript: fresh, manuscriptUrls: { page: '/books/1/manuscript/' },
  editorUrls: { layout: '/books/1/layout/' }, editor: { edited: false, version: 1, drift_pages: [] }, layout: { trim: '17x24', trim_label: '17×24 سم', page_count: null, rendering: true } };
const p5 = mk(p5cfg);
const p5fresh = [p5.primary, p5.manuscriptLine, p5.bookLine, p5.bookDot, p5.driftLine, p5.editorUrl === undefined, p5.layoutUrl, p5.manuscriptActive];
p5.apply({ total: 2, percent: 100, flags: 0, status: 'reviewing', status_label: 'قيد المراجعة', dot: 'dot-accent', by_status: {}, active: false, pages: [],
  manuscript: { ...fresh, stale: true, stale_pages: [3, 7] }, editor: { edited: true, version: 5, drift_pages: [3, 7] }, layout: { trim: 'a5', trim_label: 'A5', page_count: 412, rendering: false } });
const p5drift = [p5.primary, p5.manuscriptLine, p5.manuscriptDot, p5.bookLine, p5.bookDot, p5.driftLine];
p5.editor = { edited: false, version: 1, drift_pages: [] };
const p5stale = [p5.primary, p5.manuscriptLine, p5.driftLine];
p5.manuscript = fresh; p5.canEdit = false;
const p5reader = p5.primary;
p5.layout = { trim: '17x24', trim_label: '17×24 سم', page_count: null, rendering: false };
out.phase5 = { fresh: p5fresh, drift: p5drift, stale: p5stale, reader: p5reader, noUrl: mk({ ...p5cfg, editorUrls: {} }).primary, noPages: p5.bookLine, running: mk({ ...p5cfg, manuscript: { ...fresh, active: true } }).primary };
// end of processing: no reload, effects stop, toast with the next review page
const dEnd = mk();
dEnd.apply({ total: 3, percent: 100, flags: 0, status: 'ready_for_review', status_label: 'جاهز للمراجعة', dot: 'dot-success', by_status: {}, active: false, review: { reviewed: 0, total: 2, unresolved_total: 4, next_review_url: '/books/1/review/1/' }, pages: [] });
out.end = { active: dEnd.active, toast: { ...dEnd.doneToast }, reloaded: out.reloaded || false, primary: dEnd.primary, label: dEnd.statusText };
// owner 14: «تتبّع الصفحة الجارية» shows the page the models read now (the lowest page in progress with Tesseract's
// provisional text; error and excluded pages skipped), at once when switched on; the page on screen that just got its
// final text keeps the viewer until its wave has played (a 2.6 s timer), then the viewer moves on
const reader = mk({ pages: [
  { id: 21, number: 1, status: 'ocr_done', text_state: 'final' },
  { id: 22, number: 2, status: 'layout_done', text_state: 'provisional', error: true },
  { id: 23, number: 3, status: 'layout_done', text_state: 'provisional', is_excluded: true },
  { id: 24, number: 4, status: 'layout_done', text_state: 'provisional' },
  { id: 25, number: 5, status: 'layout_done', text_state: 'provisional' },
  { id: 26, number: 6, status: 'preprocessed', text_state: 'none' },
] });
const turned = [];
reader.goTo = (n) => { turned.push(n); reader.current = n; };
reader.view = 'sheets'; reader.follow = false; reader.current = 1;
const followOut = { target: reader.readingPage() };
reader.toggleFollow();
followOut.onTurn = turned.slice();
const lingerFrom = timers.length;
reader.apply({ total: 6, percent: 17, flags: 0, status: 'ocr', status_label: 'قيد المعالجة', dot: 'dot-accent', by_status: {}, active: true,
  pages: [{ id: 24, number: 4, status: 'ocr_done', text_state: 'final' }] });
followOut.afterFinish = turned.slice();
const linger = timers.slice(lingerFrom).filter((t) => t.ms === 2600);
followOut.linger = linger.length;
linger.forEach((t) => t.fn());
followOut.afterWave = turned.slice();
reader.view = 'grid'; reader.apply({ total: 6, percent: 33, flags: 0, status: 'ocr', status_label: 'قيد المعالجة', dot: 'dot-accent', by_status: {}, active: true,
  pages: [{ id: 25, number: 5, status: 'ocr_done', text_state: 'final' }] });
timers.slice(lingerFrom).filter((t) => t.ms === 2600).forEach((t) => t.fn());
followOut.inGrid = turned.slice(); // the grid never moves by itself
reader.toggleFollow();
followOut.off = reader.follow;
out.follow = followOut;
out.keys = [dash.keyAction({ key: 'g', code: 'KeyG' }, false), dash.keyAction({ key: 'g', code: 'KeyG' }, true), dash.keyAction({ key: 'ArrowLeft' }, false), dash.keyAction({ key: 'Escape' }, true), dash.keyAction({ key: 'n', code: 'KeyN', metaKey: true }, false), dash.keyAction({ key: '2' }, false),
  dash.keyAction({ key: 'ر', code: 'KeyV' }, false), dash.keyAction({ key: 'ل', code: 'KeyG' }, false), dash.keyAction({ key: 'v', code: 'KeyV', isComposing: true }, false)];
// auth loss stops polling quietly
const dAuth = mk();
globalThis.fetch = () => Promise.resolve({ ok: false, status: 403, json: async () => ({}) });

// --- the auth loss, then the page viewer
(async () => {
  await dAuth.poll();
  out.auth = [dAuth.pollState, dAuth.stopped];

  // --- D33 the page viewer: one sheet at a time, turned with a timed transition, filter-aware sequence.
  // The stub DOM: shells (each with a body) in a stack, a film track whose thumbs books.js clones from the
  // thumb template and patches (static like the shells, §11), the templates by id.
  const mkThumb = () => {
    const btn = new Element('button'); btn.classList.add('bk-thumb');
    const parts = { '.bk-thumb-img': new Element('span'), img: new Element('img'), '.bk-thumb-num': new Element('span'), '.bk-thumb-mark.is-check': new Element('span'), '.bk-thumb-mark.is-count': new Element('span'), '.bk-thumb-mark.is-live': new Element('span') };
    parts['.bk-thumb-img'].appendChild(parts.img); parts.img.hidden = true;
    ['.bk-thumb-img', '.bk-thumb-num', '.bk-thumb-mark.is-check', '.bk-thumb-mark.is-count', '.bk-thumb-mark.is-live'].forEach((k) => btn.appendChild(parts[k]));
    ['.bk-thumb-mark.is-check', '.bk-thumb-mark.is-count', '.bk-thumb-mark.is-live'].forEach((k) => { parts[k].hidden = true; });
    btn.querySelector = (sel) => parts[sel] || null;
    btn.closest = (sel) => (sel === '.bk-thumb' ? btn : null);
    return btn;
  };
  const tpls = { 'thumb-shell': { content: { cloneNode: () => { const frag = new Element('frag'); const btn = mkThumb(); frag.appendChild(btn); frag.querySelector = (sel) => (sel === '.bk-thumb' ? btn : null); return frag; } } } };
  document.getElementById = (id) => tpls[id] || null;
  const viewerDom = (list, withGrid = false) => {
    const shellsDom = list.map((pg) => {
      const el = new Element('article'); el.dataset.pageId = String(pg.id); el.classList.add('page-sheet');
      const body = new Element('div'); const parts = { '.sheet-fac': new Element('div'), '.sheet-lines': new Element('div'), '.sheet-scan': new Element('figure') };
      body.querySelector = (sel) => parts[sel] || null; el.querySelector = (sel) => (sel === '.sheet-body' ? body : null); el.fac = parts['.sheet-fac'];
      return el;
    });
    const stack = new Element('div'); stack.querySelectorAll = () => shellsDom; stack.addEventListener = () => {}; stack.clientWidth = 1000;
    const film = new Element('div'); const filmHandlers = {}; film.addEventListener = (ev, fn) => { filmHandlers[ev] = fn; };
    film.querySelector = (sel) => { const m = /^\[data-number="(\d+)"\]$/.exec(sel); return m ? film.children.find((c) => c.dataset.number === m[1]) || null : null; };
    const tilesDom = withGrid ? list.map((pg) => { const el = new Element('div'); el.dataset.pageId = String(pg.id); el.classList.add('page-tile'); return el; }) : [];
    const grid = new Element('div'); grid.querySelectorAll = () => tilesDom; grid.addEventListener = () => {};
    const root = new Element('div'); root.querySelector = (sel) => (sel === '[data-sheet-stack]' ? stack : sel === '[data-film-track]' ? film : sel === '[data-page-grid]' && withGrid ? grid : null);
    return { root, stack, shellsDom, film, filmHandlers, tilesDom };
  };
  const filmOf = (film) => film.children.filter((t) => !t.hidden).map((t) => [Number(t.dataset.number), t.querySelector('img').getAttribute('src') || '', !t.querySelector('.bk-thumb-mark.is-check').hidden,
    t.querySelector('.bk-thumb-mark.is-count').hidden ? 0 : Number(t.querySelector('.bk-thumb-mark.is-count').textContent), !t.querySelector('.bk-thumb-mark.is-live').hidden]);
  const currentThumbs = (film) => film.children.filter((t) => t.classList.contains('is-current')).map((t) => [Number(t.dataset.number), t.getAttribute('aria-current')]);
  const fire = async (ms) => { const t = timers.filter((x) => x.ms === ms).pop(); timers.length = 0; if (t) await t.fn(); return Boolean(t); };
  const viewerPages = [
    { id: 11, number: 1, status: 'ocr_done', status_label: 'تم التعرّف', text_state: 'final', n_unresolved: 2, width: 700, height: 1000, thumb_url: '/t/1.webp' },
    { id: 12, number: 2, status: 'reviewed', status_label: 'مُراجَعة', text_state: 'final', is_reviewed: true, width: 800, height: 1000 },
    { id: 13, number: 3, status: 'layout_done', status_label: 'تم التخطيط', text_state: 'provisional' },
    { id: 14, number: 4, status: 'ocr_done', status_label: 'تم التعرّف', text_state: 'final', n_unresolved: 1 },
  ];
  const vd = viewerDom(viewerPages); const shellsDom = vd.shellsDom;
  const winListeners = {}; globalThis.addEventListener = (ev, fn) => { winListeners[ev] = fn; };
  const v = reg.bookDashboard({ progressUrl: '/p', sheetsUrl: '/api/books/2/sheets/', filmstripUrl: '/api/books/2/filmstrip/', bookUrl: '/books/2/', canEdit: true, bookId: 2,
    active: true, status: 'ocr', stages: [], byStatus: {}, pages: viewerPages });
  v.$el = vd.root; v.$watch = () => {}; v.init();
  const cur = () => shellsDom.filter((el) => el.classList.contains('is-current')).map((el) => Number(el.dataset.pageId));
  out.vInit = { current: v.current, shown: cur(), film: filmOf(vd.film), ar: shellsDom[0].style['--ar-n'], marked: currentThumbs(vd.film), count: v.filmCount, thumbs: vd.film.children.length, noFilmState: v.film === undefined };
  out.vWindowListeners = Object.keys(winListeners).sort(); // no window wheel / touch / key listeners that would end the follow mode
  // a turn: out (200 ms), then the new sheet lands; pressing again during the turn only moves the target
  const realRaf = globalThis.requestAnimationFrame; globalThis.requestAnimationFrame = (fn) => { fn(); return 1; };
  timers.length = 0;
  v.turn(1);
  const midTurn = { turning: v.turning, current: v.current, timer: timers.filter((t) => t.ms === 200).length };
  v.turn(1); // pressed again: lands on page 3, not 2
  timers.filter((t) => t.ms === 200).forEach((t) => t.fn());
  out.vTurn = { midTurn, after: { turning: v.turning, current: v.current, shown: cur(), hiddenOld: shellsDom[0].classList.contains('is-current'), marked: currentThumbs(vd.film) } };
  // a click on a thumb (delegated on the track) turns to that page and moves the mark
  vd.filmHandlers.click({ target: vd.film.children[3] }); timers.filter((t) => t.ms === 200).forEach((t) => t.fn());
  out.vThumbClick = { current: v.current, marked: currentThumbs(vd.film) };
  out.vEnds = { canPrev: v.canTurn(-1), canNextFrom4: (v.showPage(4, { instant: true }), v.canTurn(1)), turnAtEnd: v.turn(1) };
  // the filter decides the sequence; the page on screen is never hidden, and moves when filtered out
  v.showPage(1, { instant: true });
  v.setFilter('review'); // ocr_done, not reviewed: pages 1 and 4
  out.vFilter = { current: v.current, film: filmOf(vd.film).map((f) => f[0]), count: v.filmCount, next: v.neighbour(1), prev: v.neighbour(-1) };
  v.apply({ total: 4, percent: 80, flags: 0, status: 'ocr', status_label: 'قيد التعرّف', dot: 'dot-accent', by_status: {}, active: true,
    pages: [{ id: 11, number: 1, status: 'reviewed', text_state: 'final', is_reviewed: true, n_unresolved: 0 }] });
  out.vKeptOnScreen = { current: v.current, hidden: shellsDom[0].hidden, inFilm: filmOf(vd.film).map((f) => f[0]) };
  v.setFilter('reviewed');
  out.vMoved = v.current; // page 1 (now reviewed) still matches «reviewed»: stays
  v.setFilter('processing');
  out.vMovedToProcessing = v.current;
  v.setFilter('all');
  // input: keys (RTL), a trackpad swipe, a wheel gesture that must pause before the next one counts, a touch swipe
  out.vKeys = ['PageDown', 'PageUp', 'Home', 'End', 'ArrowLeft', 'ArrowRight'].map((key) => v.keyAction({ key }, false));
  v.showPage(1, { instant: true }); timers.length = 0;
  const wheel = (dx, dy) => v.onStageWheel({ deltaX: dx, deltaY: dy, cancelable: true, preventDefault: () => {} });
  wheel(-30, 0); const afterOne = v.current; wheel(-30, 0); // 60 px to the right in total: one page forward
  timers.filter((t) => t.ms === 200).forEach((t) => t.fn());
  const afterSwipe = v.current; wheel(-80, 0); // inertia of the same gesture: ignored
  out.vWheel = { afterOne, afterSwipe, inertia: v.current, idle: timers.some((t) => t.ms === 260) };
  timers.filter((t) => t.ms === 260).forEach((t) => t.fn()); // the gesture ended
  wheel(0, 100); timers.filter((t) => t.ms === 200).forEach((t) => t.fn()); // scrolling down: forward
  out.vWheelDown = v.current;
  v.onStagePointerDown({ pointerType: 'touch', clientX: 100, clientY: 100 }); v.onStagePointerUp({ pointerType: 'touch', clientX: 30, clientY: 104 }); // finger to the left: back
  timers.filter((t) => t.ms === 200).forEach((t) => t.fn());
  out.vTouchBack = v.current;
  v.onStagePointerDown({ pointerType: 'mouse', clientX: 100, clientY: 100 }); v.onStagePointerUp({ pointerType: 'mouse', clientX: 300, clientY: 100 });
  out.vMouseDragIgnored = v.current;
  // reduced motion: turns are instant
  D.reducedMotion = true; v.turn(1); out.vReduced = { current: v.current, turning: v.turning }; D.reducedMotion = false;
  // a thumbnail arriving with the sheets data reaches the filmstrip
  v.applySheets({ pages: [{ id: 13, number: 3, width: 700, height: 1000, thumb_url: '/t/3.webp', text_state: 'provisional', status: 'layout_done' }] });
  out.vFilmThumb = vd.film.children.find((t) => t.dataset.number === '3').querySelector('img').getAttribute('src');
  // the follow mode (§8.3) ends only with a page the reader chooses: the hot-line keys keep it, a turn ends it
  v.follow = true;
  v.onKey({ key: 'ArrowDown', target: {}, preventDefault: () => {} });
  const followAfterKey = v.follow;
  v.turn(1); timers.filter((t) => t.ms === 200).forEach((t) => t.fn());
  out.vFollow = { afterKey: followAfterKey, afterTurn: v.follow, toast: toasts.slice(-1)[0] };
  globalThis.requestAnimationFrame = realRaf;

  // --- C and ↑/↓ act on the page on screen: after «شبكة» (where the far observer unmounted the sheet and
  // cleared the focused page) a switch to «صفحات» remounts without a turn, and the keys must still work
  const observers = [];
  globalThis.IntersectionObserver = class { constructor(cb, opts) { this.cb = cb; this.opts = opts || {}; observers.push(this); } observe() {} unobserve() {} disconnect() {} };
  store['nassakh.bookView'] = 'grid';
  const copied = []; window.Nassakh.copyText = (text) => { copied.push(text); return Promise.resolve(true); };
  const gPages = [{ id: 21, number: 1, status: 'ocr_done', status_label: 'تم التعرّف', text_state: 'final', width: 700, height: 1000 }];
  const gd = viewerDom(gPages, true); // grid tiles present: the viewer opens on page 1 at init, as in the browser
  const g = reg.bookDashboard({ progressUrl: '/p', sheetsUrl: '/api/books/3/sheets/', bookUrl: '/books/3/', canEdit: true, bookId: 3, active: false, status: 'ready_for_review', stages: [], byStatus: {}, pages: gPages });
  g.$el = gd.root; g.$watch = () => {}; g.init();
  const gAtInit = { view: g.view, current: g.current, mounted: g.isMounted(21) };
  g.applySheets({ pages: [{ id: 21, number: 1, width: 700, height: 1000, text_state: 'final', status: 'ocr_done', lines: [{ region_kind: 'body', tokens: [{ t: 'نص' }, { t: 'الصفحة' }] }] }] });
  observers.find((o) => o.opts.rootMargin === '4000px').cb([{ target: gd.shellsDom[0], isIntersecting: false }]); // «شبكة»: the viewer is display:none
  const gUnmounted = !g.isMounted(21);
  g.setView('sheets'); g.onViewChange(); // the page is already current: no turn, the near observer remounts it in the browser
  const hotCalls = []; g.moveHot = (dir, pid) => { hotCalls.push([dir, pid]); return true; };
  g.onKey({ key: 'c', code: 'KeyC', target: {} });
  g.onKey({ key: 'ArrowDown', target: {}, preventDefault: () => {} });
  out.gKeys = { atInit: gAtInit, unmounted: gUnmounted, view: g.view, current: g.current, copied, hotCalls };
  store['nassakh.bookView'] = 'sheets';

  // --- the turn buttons' `:disabled` (Alpine's reactivity = @vue/reactivity) re-evaluates when a poll moves
  // a page into the filter without adding pages
  const { reactive, effect } = require(require('path').resolve(process.argv[2], '../../../../node_modules/@vue/reactivity'));
  store['nassakh.bookFilter.4'] = 'review';
  const rPages = [{ id: 31, number: 1, status: 'ocr_done', status_label: 'تم التعرّف', text_state: 'final' }, { id: 32, number: 2, status: 'layout_done', status_label: 'تم التخطيط', text_state: 'provisional' }];
  const rd = viewerDom(rPages);
  const r = reactive(reg.bookDashboard({ progressUrl: '/p', sheetsUrl: '', bookUrl: '/books/4/', canEdit: true, bookId: 4, active: true, status: 'ocr', stages: [], byStatus: {}, pages: rPages }));
  r.$el = rd.root; r.$watch = () => {}; r.init();
  let runs = 0; let canNext = null;
  effect(() => { runs += 1; canNext = r.canTurn(1); });
  const rBefore = { runs, canNext };
  r.apply({ total: 2, percent: 100, flags: 0, status: 'ocr', status_label: 'قيد التعرّف', dot: 'dot-accent', by_status: {}, active: true, pages: [{ id: 32, number: 2, status: 'ocr_done', text_state: 'final' }] });
  out.rTurn = { before: rBefore, after: { runs, canNext, fresh: r.canTurn(1) } };

  // --- a failed sheets request is retried with a backoff, also for an inactive book (which never polls);
  // the banner stays until the range arrives
  const fPages = [{ id: 41, number: 1, status: 'ocr_done', status_label: 'تم التعرّف', text_state: 'final', width: 700, height: 1000 }];
  const fd = viewerDom(fPages);
  const f = reg.bookDashboard({ progressUrl: '/p', sheetsUrl: '/api/books/5/sheets/', bookUrl: '/books/5/', canEdit: true, bookId: 5, active: false, status: 'ready_for_review', stages: [], byStatus: {}, pages: fPages });
  f.$el = fd.root; f.$watch = () => {}; timers.length = 0; f.init();
  let sheetCalls = 0; let sheetsOk = false;
  globalThis.fetch = () => { sheetCalls += 1; return sheetsOk ? Promise.resolve({ ok: true, json: async () => ({ pages: [{ id: 41, number: 1, width: 700, height: 1000, text_state: 'final', status: 'ocr_done', lines: [{ region_kind: 'body', tokens: [{ t: 'نص' }] }] }] }) }) : Promise.reject(new Error('down')); };
  const fired = await fire(60); // the mount's request fails
  const afterFail = { fired, calls: sheetCalls, failed: f.sheetsFailed, retry: timers.map((t) => t.ms) };
  await fire(2000); // still down: the backoff doubles
  const afterSecond = { calls: sheetCalls, failed: f.sheetsFailed, retry: timers.map((t) => t.ms) };
  sheetsOk = true;
  await fire(4000); // the server is back: the data arrives, the banner goes
  out.fRetry = { afterFail, afterSecond, afterOk: { calls: sheetCalls, failed: f.sheetsFailed, loaded: Boolean(f.sheet(41)), retry: timers.map((t) => t.ms) } };

  // --- a turn onto a page whose cached data went stale renders that data (the refetch then plays the
  // provisional → final wave) instead of a skeleton; a poll that changes the viewer's neighbours queues
  // their refetch ahead of the turn
  const rafQueue = []; globalThis.requestAnimationFrame = (fn) => { rafQueue.push(fn); return 1; };
  const flushRaf = () => rafQueue.splice(0).forEach((fn) => fn(NOW));
  tpls['sheet-body'] = { content: { cloneNode: () => new Element('frag') } };
  const provLines = Array.from({ length: 6 }, (_, k) => ({ region_kind: 'body', bbox: [0.15, 0.1 + 0.05 * k, 0.85, 0.13 + 0.05 * k], words: ['كلمة', 'أخرى', 'ثالثة'] }));
  const finalLines2 = provLines.map((l) => ({ region_kind: 'body', bbox: l.bbox, tokens: l.words.map((t) => ({ t, conf: 'high' })) }));
  const payloads = { 1: { id: 51, number: 1, width: 700, height: 1000, text_state: 'final', status: 'ocr_done', lines: finalLines2 },
    2: { id: 52, number: 2, width: 700, height: 1000, text_state: 'provisional', status: 'layout_done', provisional_lines: provLines } };
  const sheetUrls = [];
  globalThis.fetch = (url) => { sheetUrls.push(url); const m = /from=(\d+)&to=(\d+)/.exec(url); const items = []; for (let n = Number(m[1]); n <= Number(m[2]); n += 1) items.push(payloads[n]); return Promise.resolve({ ok: true, json: async () => ({ pages: items }) }); };
  const sPages = [{ id: 51, number: 1, status: 'ocr_done', status_label: 'تم التعرّف', text_state: 'final', width: 700, height: 1000 }, { id: 52, number: 2, status: 'layout_done', status_label: 'تم التخطيط', text_state: 'provisional', width: 700, height: 1000 }];
  const sd = viewerDom(sPages);
  const s = reg.bookDashboard({ progressUrl: '/p', sheetsUrl: '/api/books/6/sheets/', bookUrl: '/books/6/', canEdit: true, bookId: 6, active: true, status: 'ocr', stages: [], byStatus: {}, pages: sPages });
  s.$el = sd.root; s.$watch = () => {}; timers.length = 0; s.init();
  await fire(60); // the mount's request: the current page and its prefetched neighbour
  const prefetched = sheetUrls.slice();
  const modeBefore = sd.shellsDom[0].fac.getAttribute('data-mode');
  s.apply({ total: 2, percent: 60, flags: 0, status: 'ocr', status_label: 'قيد التعرّف', dot: 'dot-accent', by_status: {}, active: true,
    pages: [{ id: 52, number: 2, status: 'layout_done', text_state: 'provisional', n_unresolved: 3 }] }); // the neighbour changed: stale, not mounted
  const queuedAfterPoll = timers.filter((t) => t.ms === 60).length;
  s.turn(1); timers.filter((t) => t.ms === 200).forEach((t) => t.fn()); flushRaf(); flushRaf();
  const modeAtLanding = sd.shellsDom[1].fac.getAttribute('data-mode');
  payloads[2] = { ...payloads[2], text_state: 'final', status: 'ocr_done', provisional_lines: [], lines: finalLines2 };
  await fire(60); // the refetch of the stale page lands its final text
  out.sStale = { prefetched, modeBefore, queuedAfterPoll, modeAtLanding, current: s.current, refetched: sheetUrls.slice(prefetched.length), modeAfter: sd.shellsDom[1].fac.getAttribute('data-mode') };
  s.destroy(); delete tpls['sheet-body']; delete globalThis.IntersectionObserver;
  globalThis.requestAnimationFrame = realRaf;
  console.log(JSON.stringify(out));
})();
"""  # noqa: E501


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_decode_engine_layout_sheet_handle_and_dashboard_logic_under_node(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    files = [str(JS / name) for name in ("ui.js", "keys.js", "decode.js", "books.js")]
    run = subprocess.run(["node", str(harness), *files], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])

    # --- D33 the page viewer
    # static thumbs, one per page, patched from the records (thumbnail, reviewed check, uncertain count,
    # live dot); the current one is marked; no reactive `film` array
    assert out["vInit"] == {
        "current": 1,
        "shown": [11],
        "film": [
            [1, "/t/1.webp", False, 2, False],
            [2, "", True, 0, False],
            [3, "", False, 0, True],
            [4, "", False, 1, False],
        ],
        "ar": "0.7",
        "marked": [[1, "page"]],
        "count": 4,
        "thumbs": 4,
        "noFilmState": True,
    }
    # the only window listeners are resize and scroll, and the focus that refreshes the progress once (D76,
    # §5.4): no wheel / touch / key listener ends the follow mode
    assert out["vWindowListeners"] == ["focus", "resize", "scroll"]
    # a turn slides out for 200 ms; a second press mid-turn moves the target, the sheet lands once
    assert out["vTurn"]["midTurn"] == {"turning": "out-next", "current": 1, "timer": 1}
    assert out["vTurn"]["after"] == {
        "turning": "",
        "current": 3,
        "shown": [13],
        "hiddenOld": False,
        "marked": [[3, "page"]],
    }
    assert out["vThumbClick"] == {"current": 4, "marked": [[4, "page"]]}
    assert out["vEnds"] == {"canPrev": True, "canNextFrom4": False, "turnAtEnd": False}
    # the filter decides the viewer's sequence and the filmstrip
    assert out["vFilter"] == {"current": 1, "film": [1, 4], "count": 2, "next": 4, "prev": None}
    # the page on screen stays visible when the poll takes it out of the filter
    assert out["vKeptOnScreen"] == {"current": 1, "hidden": False, "inFilm": [4]}
    assert out["vMoved"] == 1 and out["vMovedToProcessing"] == 3
    assert out["vKeys"] == ["nextSheet", "prevSheet", "firstSheet", "lastSheet", "nextSheet", "prevSheet"]
    # trackpad: a 60 px swipe to the right is one page forward (RTL), its inertia does not flip another page;
    # after the gesture pauses, scrolling down goes forward; a touch swipe to the left goes back; mouse drags
    # don't turn
    assert out["vWheel"] == {"afterOne": 1, "afterSwipe": 2, "inertia": 2, "idle": True}
    assert out["vWheelDown"] == 3 and out["vTouchBack"] == 2 and out["vMouseDragIgnored"] == 2
    assert out["vReduced"] == {"current": 3, "turning": ""}
    assert out["vFilmThumb"] == "/t/3.webp"
    # follow (§8.3) survives the hot-line keys and ends with a page the reader chose
    assert out["vFollow"] == {"afterKey": True, "afterTurn": False, "toast": "أُوقف التتبّع"}
    # C and ↑/↓ act on the page on screen after «شبكة» → «صفحات» (no turn in between)
    assert out["gKeys"] == {
        "atInit": {"view": "grid", "current": 1, "mounted": True},
        "unmounted": True,
        "view": "sheets",
        "current": 1,
        "copied": ["نص الصفحة"],
        "hotCalls": [[1, "21"]],
    }
    # the turn buttons re-evaluate when a poll moves a page into the filter (nPages unchanged)
    assert out["rTurn"]["before"] == {"runs": 1, "canNext": False}
    assert out["rTurn"]["after"]["runs"] > 1 and out["rTurn"]["after"]["canNext"] is True
    assert out["rTurn"]["after"]["fresh"] is True
    # a failed sheets request is retried after 2 s, then 4 s; the banner stays until the range arrives
    assert out["fRetry"] == {
        "afterFail": {"fired": True, "calls": 1, "failed": True, "retry": [2000]},
        "afterSecond": {"calls": 2, "failed": True, "retry": [4000]},
        "afterOk": {"calls": 3, "failed": False, "loaded": True, "retry": []},
    }
    # a stale cached payload is rendered on mount (provisional, not a skeleton); the changed neighbour was
    # queued by the poll; the refetch lands the final text
    assert out["sStale"] == {
        "prefetched": ["/api/books/6/sheets/?from=1&to=2"],
        "modeBefore": "final",
        "queuedAfterPoll": 1,
        "modeAtLanding": "provisional",
        "current": 2,
        "refetched": ["/api/books/6/sheets/?from=2&to=2"],
        "modeAfter": "final",
    }

    # --- decode (D28): words and lines from the Tesseract text, one text node per word, real spaces between
    # words
    assert (
        out["words"] == ["قال", "الأمير", "في", "سنة", "1966", "وَفِي", "الشهر", "الثاني"] and out["lines"] == 2
    )
    assert out["tokenNodes"] is True and out["spaces"] == 4
    # every shown string is exact or 1–3 same-family letters off (never the first, never a digit or mark, no
    # ASCII,
    # same length); over 20 s every word was exact at some tick and every veilable word veiled at some tick
    assert out["veilBad"] is None, out["veilBad"]
    assert out["everExact"] is True and out["everVeiled"] is True and out["digitsStill"] is True
    assert out["changed"] is True and out["veiledClassOk"] is True and out["maxVeiled"] <= 40
    assert out["decodingClass"] is True
    # noise mode: Arabic letters only
    assert out["noiseLines"] == 7 and out["noiseClass"] is True and out["noiseArabic"] is True
    # the reading cursor of the review's pending page (owner review 2026-10-03): Tesseract's lines as given, the
    # band walks them (the row just read half lit), rests, starts again; the host hears every step; the words under
    # the band are exact; new lines keep the cursor; the wave stops it and says so; no cursor unless asked
    rows = out["rows"]
    assert rows["text"] == ["قال الأمير الكبير", "وفي الشهر", "حاشية"] and rows["region"] == "footnote"
    assert rows["seen"][:4] == ["L..", "hL.", ".hL", "..."] and rows["heard"][:4] == [0, 1, 2, -1]
    assert rows["exact"] is True and rows["k"] == ["0", "1", "2"] and rows["decoding"] is True
    assert out["rowsUpdated"]["text"] == ["سطر جديد", "وآخر"] and out["rowsUpdated"]["heard"][:2] == [0, 1]
    assert out["rowsResolved"] == {"last": -1, "lit": "."}
    assert out["noCursor"] is True
    # resolve: right-to-left / line-by-line landing, exactly the final tokens, amber underline on unresolved
    assert out["resolvingClass"] is True and out["landedPrefix"] is True and out["doneEarly"] == 0
    assert out["done"] == 1 and out["resolvedClass"] is True
    body, note = out["final"]
    assert body["region"] == "body" and [w["t"] for w in body["words"]] == ["قال", "الأمير", "في"]
    assert [("tok-low" in w["cls"]) for w in body["words"]] == [False, True, False]
    assert note["region"] == "footnote" and [w["t"] for w in note["words"]] == ["حاشية", "12"]
    assert [("tok-low" in w["cls"]) for w in note["words"]] == [False, True]  # resolved word: no underline
    assert all("is-landed" in w["cls"] for w in body["words"] + note["words"])
    assert out["forgotten"] == [3, 2]
    assert out["rmStatic"] is True and out["rmResolve"] == "قال الأمير في"

    # --- layout(): regular body page with boxes → block from the boxes (padded), font from the median line
    # height
    lay = out["layRegular"]
    exp = out["layRegularExpect"]
    assert (
        lay["mode"] == "provisional"
        and lay["groups"] == 1
        and lay["kind"] == "body"
        and lay["source"] == "text"
    )
    assert (
        lay["block"] == [0.15, exp["y0"], 0.85, exp["y1"]]
        and lay["fs"] == exp["fs"]
        and lay["lh"] == exp["lh"]
    )
    assert (
        lay["few"] is False
        and lay["rule"] is None
        and lay["ids"] == list(range(10))
        and lay["lx"] == [0.15, 0.85]
    )
    assert (
        lay["fits"] == ["justify"] * 9 + ["start"] and all(r > 0 for r in lay["rs"]) and lay["rs"][-1] >= 1.4
    )
    # no boxes at all → default block, pitch-derived font, lines span the block
    plain = out["layPlain"]
    assert (
        plain["source"] == "default"
        and plain["n"] == 12
        and plain["boxed"] is False
        and plain["few"] is False
    )
    assert plain["fs"] == out["layPlainExpect"]["fs"] and plain["block"][1] == out["layPlainExpect"]["y0"]
    assert plain["lx"] == [0.12, 0.88]
    # a title page: 1–3 lines without geometry → centred group at the title size
    assert (
        out["layTitle"]["few"] is True
        and out["layTitle"]["n"] == 2
        and out["layTitle"]["fs"] == out["layTitleExpect"]
    )
    assert out["layTitle"]["fits"] == ["start", "start"]
    # footnotes: a second group under the printed rule, capped at 90 % of the body size, one index space
    notes = out["layNotes"]
    assert notes["groups"] == ["body", "footnote"] and notes["rule"] == 0.8 and notes["noteY0"] >= 0.8
    assert (
        notes["noteFsCapped"] is True
        and notes["ids"] == [0, 1, 2, 3]
        and notes["noteKinds"] == ["footnote", "footnote"]
    )
    # D34 footnotes are set solid, packed from the first note: no spread, no centring, a tighter lead
    assert out["laySolid"] == {
        "solid": True,
        "few": False,
        "bodySolid": False,
        "lead": True,
        "packed": True,
        "top": out["laySolid"]["topExpect"],
        "topExpect": out["laySolid"]["topExpect"],
        "shorter": True,
    }
    assert out["laySolidRow"]["fsKept"] is True and out["laySolidRow"]["within"] is True
    assert out["laySolidRow"]["bottom"] > 0.925
    assert out["layNoteFromBody"]["source"] == "boxes" and out["layNoteFromBody"]["ratio"] >= 0.8
    # final lines with bbox null → distributed inside the default block
    fnb = out["layFinalNoBox"]
    assert (
        fnb["mode"] == "final"
        and fnb["source"] == "default"
        and fnb["boxed"] is False
        and fnb["spans"] is True
    )
    assert fnb["ids"] == list(range(6)) and fnb["tokens"] == 8
    # skeleton: bars at the detected boxes, else 14 default bars in the default block; empty final page
    assert out["laySkeleton"] == {"mode": "skeleton", "n": 5, "boxed": True, "source": "boxes"}
    assert out["layBare"] == {
        "mode": "skeleton",
        "n": 14,
        "bars": [100, 100, 92, 100],
        "block": [0.12, 0.096, 0.88, 0.904],
    }
    assert out["layEmpty"] == "empty" and out["layCentred"] == "center"

    # --- D30: one type size per group; lines share the paragraph edges; fitted by word spacing
    para = out["layPara"]
    lines = para["lines"]
    assert para["hasScale"] is False  # no per-line font size any more
    assert [ln["fit"] for ln in lines] == ["center", "justify"] + ["justify"] * 6 + ["start", "over", "tight"]
    assert [ln["shape"] for ln in lines] == ["center", "indent"] + ["full"] * 6 + ["short", "full", "full"]
    # jittered full lines snap to one width: the paragraph's end edge (left) and start edge (right)
    assert {(ln["lx0"], ln["lx1"]) for ln in lines[2:8]} == {(0.15, 0.85)}
    assert (lines[1]["lx0"], lines[1]["lx1"]) == (0.15, 0.82)  # the indented first line keeps its indent
    assert (lines[8]["lx0"], lines[8]["lx1"]) == (0.55, 0.85)  # the last line keeps its own width
    assert (lines[0]["lx0"], lines[0]["lx1"]) == (0.4, 0.6)  # the heading stays centred as printed
    # the size is lowered only as far as the regular full lines need (they fit at natural spacing)
    assert 0.75 * out["layParaHeightFs"] <= para["fs"] < out["layParaHeightFs"]
    assert all(ln["r"] >= 1 and ln["ws"] == 0 and ln["sx"] == 1 for ln in lines[1:8])
    # one word too many: word spaces tighten (within half a space), no condense needed
    assert lines[10]["ws"] < 0 and lines[10]["sx"] == 1
    assert abs(lines[10]["ws"]) <= 0.5 * out["layParaSpace"] + 1e-9
    # two lines merged: spaces at their limit, condensed to 90 % and clipped with a fade
    assert lines[9]["sx"] == 0.9 and abs(abs(lines[9]["ws"]) - 0.5 * out["layParaSpace"]) < 1e-5
    # D31: a paragraph's first line keeps its printed indent and stays flush left; with little text it is
    # end-aligned (never hanging from the right); when its text needs the room it grows into the indent
    indent = out["layIndent"]
    assert indent[0] == {"shape": "indent", "fit": "end", "lx0": 0.15, "lx1": 0.83}
    assert all(ln == {"shape": "full", "fit": "justify", "lx0": 0.15, "lx1": 0.85} for ln in indent[1:6])
    assert indent[6] == {"shape": "indent", "fit": "tight", "lx0": 0.15, "lx1": 0.85}
    # the book's typical line height sets the size when the page agrees within 20 %, else the page's own
    book = out["layBook"]
    assert book["own"] == round(0.78 * 0.03 * 1.5, 5)
    assert book["near"] == round(0.78 * 0.03 * 1.5 * 1.1, 5) and book["far"] == book["own"]
    # rendered: fits on the line elements, word spacing / condense as properties, no --fit anywhere
    dom = out["layParaDom"]
    assert dom["n"] == 11 and dom["fits"] == [ln["fit"] for ln in lines] and dom["anyFit"] is False
    assert (
        dom["tightWs"] and float(dom["tightWs"]) < 0 and dom["overSx"] == "0.9" and dom.get("plainWs") is None
    )

    # --- sheet handle: skeleton bars and scan boxes from one index space, the cursor lights both sides
    assert out["hSkel"] == {
        "mode": "skeleton",
        "phase": "skeleton",
        "bars": 5,
        "boxes": 5,
        "dataMode": "skeleton",
    }
    assert out["hSkelCursor"] == {"cursor": 0, "scanLit": True, "lineLit": True}
    prov = out["hProv"]
    assert prov["mode"] == "provisional" and prov["phase"] == "provisional" and prov["layers"] == 1
    assert prov["lines"] == 12 and prov["boxes"] == 12 and prov["toks"] == 6
    cyc = out["hCycle"]
    assert cyc["bad"] is None, cyc["bad"]
    assert cyc["litOk"] is True and cyc["visited"] == 12 and cyc["words"] == 72 and cyc["maxVeiled"] <= 40
    assert cyc["allExact"] is True and cyc["allVeiled"] is True
    assert cyc["trailOk"] is True  # the line just read keeps half the band, on the scan and in the text
    assert out["hHot"] == [2, True, True] and out["hHotOff"] == [-1, False]
    # final arrives: the final layer beneath the leaving provisional one, a right-to-left wave with the band
    # following
    fin = out["hFinal"]
    assert (
        fin["mode"] == "final"
        and fin["layers"] == 2
        and fin["phases"] == ["final", "provisional"]
        and fin["leaving"] is True
    )
    wave = out["hWave"]
    assert wave["wavePrefix"] is True and wave["frontMonotonic"] is True and wave["frontMoved"] is True
    assert (
        wave["landed"] is True
        and wave["layers"] == 1
        and wave["resolved"] is True
        and wave["aria"] == "false"
    )
    assert wave["lit"] is False and wave["low"] is True and wave["tabindex"] == "-1"
    # inactive book: static gray text; resume / stop toggle without a rebuild; designed states; late final
    # sheets render at once
    assert out["hStatic"] == {"mode": "static", "cls": True, "exact": True}
    assert out["hResume"] == "provisional" and out["hStop"] == ["static", 1]
    assert out["hStates"] == ["excluded", "error", "empty", "skeleton"]
    assert out["hLate"] == {"mode": "final", "resolved": True, "landed": True}
    assert out["hReduced"] == ["static", "final", 1, True]

    # --- books.js: view, batches, counts, review summary, aspect
    assert out["viewDefault"] == "sheets" and out["viewStored"] == "grid"
    assert out["ranges"] == [[1, 3], [5, 7], [50, 51], [100, 139], [140, 144]]
    assert out["counts"] == {"all": 3, "processing": 0, "review": 1, "attention": 0, "reviewed": 1}
    assert out["review"] == {
        "reviewed": 1,
        "total": 2,
        "unresolved_total": 4,
        "next_review_url": "/books/1/review/next/",
    }
    assert (
        out["nextReview"] == "/books/1/review/next/"
        and out["primaryReview"] == "review"
        and out["reviewFromPoll"] == ""
    )
    assert out["aspect"] == ["800 / 1000", "0.8000 / 1"]  # the median ratio stands in for the unknown one
    assert out["sheetText"] == "قال الأمير\n\nحاشية" and out["canCopy"] == [True, False]
    assert out["stage"] == [12, 72, 100, 100]
    # compact poll merge: labels, dots and URLs are completed client-side; the changed set is patched and
    # refetched
    assert out["merged"] == {
        "label": "مُراجَعة",
        "dot": "dot-success",
        "url": "/books/1/pages/1/",
        "review": "/books/1/review/1/",
        "stale": True,
        "unresolved": 0,
    }
    assert out["added"] == {
        "url": "/books/1/pages/4/",
        "rerun": "/books/1/pages/4/rerun/",
        "label": "مرفوعة",
        "dot": "dot-neutral",
    }
    assert out["live"] == "الصفحة 1: مُراجَعة"
    assert out["countsAfter"] == {"all": 4, "processing": 1, "review": 0, "attention": 0, "reviewed": 2}
    assert out["refetch"] == ["/api/books/1/sheets/?from=1&to=1"]  # only the mounted sheet that changed
    # filters: persisted, empty message, excluded pages only under «الكل»
    assert out["filterStored"] == "reviewed" and out["filteredOut1"] is False and out["filteredOut2"] is True
    assert out["matches"] == [
        [True, False, False],
        [False, False, False],
        [False, False, False],
        [False, True, False],
    ]
    # jump parsing: Eastern digits, whitespace, unknown numbers
    assert out["jump"] == [2, 4, None, None, None, "لا صفحة بهذا الرقم"]
    # exactly one primary per state; once every page is reviewed an editor converts the book (Phase 4 §4.1)
    # and a proofreader copies its text; a proofreader never gets «بدء المعالجة»
    assert out["primary"] == [
        "start",
        "start",
        "",
        "review",
        "review",
        "convert",
        "copy",
    ]
    assert out["statusText"] == ["قيد المعالجة · 2 من 4 صفحة", "قيد التعرّف"]
    # Phase 5 (D47): the book page primary once the manuscript is fresh; after edits the drift line (D41)
    # and the
    # «الكتاب» line from the poll; a proofreader or a missing URL keeps the manuscript; a run keeps it too
    assert out["phase5"]["fresh"] == [
        "book",
        "مُجمَّعة · 214 صفحة · ملاحظتان",
        "17×24 سم · يُحسب…",
        "dot-accent",
        "",
        True,
        "/books/1/layout/",
        False,
    ]
    assert out["phase5"]["drift"] == [
        "book",
        "مُحرَّرة · 214 صفحة · ملاحظتان",
        "dot-success",
        "A5 · 412 صفحة",
        "dot-success",
        "تغيّر نص صفحتان في المراجعة بعد التحرير",
    ]
    assert out["phase5"]["stale"] == ["reassemble", "تغيّر نص صفحتان بعد التجميع", ""]
    assert out["phase5"]["reader"] == "manuscript" and out["phase5"]["noUrl"] == "manuscript"
    assert (
        out["phase5"]["noPages"] == "17×24 سم · لم تُرتَّب صفحاته بعد"
        and out["phase5"]["running"] == "manuscript"
    )
    # the end of processing: no reload, a toast with the review entry, the primary swaps
    assert out["end"] == {
        "active": False,
        "toast": {"visible": True, "count": 3, "url": "/books/1/review/1/"},
        "reloaded": False,
        "primary": "review",
        "label": "جاهز للمراجعة",
    }
    # owner 14: the page the models read now; at once when switched on; after the wave of the page on screen
    assert out["follow"] == {
        "target": 4,
        "onTurn": [4],
        "afterFinish": [4],
        "linger": 1,
        "afterWave": [4, 5],
        "inGrid": [4, 5],
        "off": False,
    }
    # D69: the view moved from 1 / 2 to V, matched by the physical key (the Arabic layout types «ر» there); a
    # composing key is never a shortcut
    assert out["keys"] == ["jump", None, "nextSheet", "blur", None, None, "toggleView", "jump", None]
    assert out["auth"] == ["auth", True]


# ------------------------------------------------------------ «التخطيط» mode (PHASE7_SPEC §3.12–§3.14, D67)

FIXTURES = ROOT / "books" / "fixtures" / "guides"


def _awaiting(status: str, states=(), **book_kwargs) -> tuple[Book, list[Page]]:
    """A book awaiting «بدء المعالجة» (D64) with one page per page status."""
    book = Book.objects.create(
        title="الحوليات الليبية",
        status=status,
        awaits_ocr_start=True,
        source_page_count=555,
        skip_first=186,
        skip_last=362,
        **book_kwargs,
    )
    pages = [
        Page.objects.create(
            book=book, number=i, source_index=185 + i, status=page_status, width=1000, height=3000
        )
        for i, page_status in enumerate(states, start=1)
    ]
    return book, pages


def _config(body: str) -> dict:
    return json.loads(
        re.search(r'<script id="dashboard-config" type="application/json">(.*?)</script>', body, re.S).group(
            1
        )
    )


def _between(body: str, start: str, end: str) -> str:
    i = body.index(start)
    return body[i : body.index(end, i)]


def test_guides_mode_for_a_book_awaiting_the_start(editor_client):
    book, pages = _awaiting(Book.Status.NEEDS_GUIDES, [Page.Status.PREPROCESSED] * 3)
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    config = _config(body)
    assert (
        config["guidesMode"] is True and config["layoutStage"] is True and config["startAction"] == "startOcr"
    )
    assert 'class="bk-dashboard is-guides has-pages' in body
    # the top bar: «بدء المعالجة» posts the start (disabled with its title while the pages are prepared), and
    # «استخراج الصفحات» is the candidate of an uploaded or failed book; today's «بدء المعالجة» form is gone
    bar = _between(body, 'class="bk-bar" x-data>', "</template>")
    start = _between(bar, f'action="{reverse("books:start_ocr", args=[book.pk])}"', "</form>")
    assert "d.primary === 'startOcr' || d.primary === 'startOcrDisabled'" in start
    assert ":disabled=\"d.primary !== 'startOcr' || sent\"" in start and "<span>بدء المعالجة</span>" in start
    assert "'يُتاح بعد اكتمال تجهيز الصفحات'" in start
    assert "d.primary === 'extract' || d.primary === 'reextract'" in bar and "استخراج الصفحات" in bar
    assert (
        "d.primary === 'start'\"" not in bar and "d.primary === 'back'" in bar and "العودة إلى الصفحات" in bar
    )
    # «⋯»: «إعادة التخطيط…» (asks first, D101), «حذف الكتاب…», «كل الكتب» — nothing of «المعالجة»
    menu = _between(body, 'class="menu menu-popover bk-menu"', "</template>")
    item = _between(menu, "<button", "</button>")
    assert "x-show=\"d.status === 'needs_guides' || (d.status === 'error' && d.nPages > 0)\"" in item
    assert "d.openRerun('preprocess', 'إعادة التخطيط')" in item and "<span>إعادة التخطيط…</span>" in item
    assert menu.count("data-rerun-action=") == 1 and "حذف الكتاب…" in menu and "كل الكتب" in menu
    for gone in (
        "تحويل إلى كتاب",
        "الإخراج",
        "نسخ نص الكتاب",
        "إعادة التشغيل من مرحلة",
        "data-rerun-stage",
        "إعادة المعالجة",
        "إعادة تجهيز الصفحات",
    ):
        assert gone not in menu, gone
    # the toolbar: V toggles the view (D69); the mode's chips; no follow toggle
    chips = _between(body, "data-filter-chips>", "</div>")
    assert chips.count('class="bk-chip"') == 3
    for name, label in (("all", "الكل"), ("doubt", "تستحق نظرة"), ("override", "بضبط خاص")):
        assert f"setFilter('{name}')" in chips and f'x-text="counts.{name}"' in chips and label in chips
    assert 'title="صفحات (V)"' in body and 'title="شبكة (V)"' in body and "bk-follow" not in body
    # the look banner, with the pages worth a look
    assert "جُهّزت الصفحات واكتُشفت مناطقها. ألقِ نظرة واضبط ما يلزم، ثم اضغط «بدء المعالجة»." in body
    assert 'x-text="doubtLine"' in body and 'x-text="doubtShow"' in body
    assert "للكتاب طبقة نصية" not in body
    # the side panel (_guides_side.html) replaces the summary; the component sits on .bk-layout
    assert 'x-data="bookGuides()" @nassakh:page-shown.window="onPageShown($event.detail)"' in body
    side = _between(body, '<aside class="bk-side gd-side"', "</aside>")
    for needle in (
        "التخطيط",
        'x-text="preparedLine"',
        "الصفحات 187–193 من 555",
        "حذف الكتاب…",
        "لكل الصفحات",
        "ترويسة أعلى كل الصفحات",
        "% من الأعلى",
        "من هذه الصفحة",
        "حاشية حيث لم تُكتشف",
        'x-text="medianLabel"',
        "إزالة الضبط العام",
        "معاينة",
        "تطبيق على كل الصفحات",
        'x-text="keptText"',
        "متن",
        "حاشية",
        "ترويسة",
        "رقم الصفحة",
        "تستحق نظرة",
        "الصفحات</span>",
        "data-film-track",
    ):
        assert needle in side, needle
    assert "bk-summary" not in body and "bk-attention" not in body
    # the toast's «تراجع» is bound to a boolean and never runs by itself (2a0bca2)
    toast = _between(body, 'class="toast bk-toast gd-toast"', "</div>")
    assert 'x-show="toast.hasAction" @click="runToastAction()">تراجع</button>' in toast
    # the sheet body template: the prepared page, sliders, grips, the band menu, the lock, the save bar
    tpl = _between(body, '<template id="sheet-guides">', "</template>")
    for needle in (
        '<figure class="gd-page"',
        '<div class="gd-layer" dir="ltr">',
        'class="guide-line gd-line is-header" role="slider"',
        'aria-orientation="vertical" aria-label="حدّ الترويسة"',
        'aria-label="بداية الحاشية"',
        "+ ترويسة",
        "+ حاشية",
        "إزالة من هذه الصفحة",
        "تطبيق على كل الصفحات…",
        "ليس رقم صفحة",
        "حفظ وإعادة التعرّف على الصفحة",
        "gd-lock-text",
        'class="sr-only gd-sr"',
    ):
        assert needle in tpl, needle
    # shells, tiles and thumbs carry the mode's hidden parts
    shell = _shell(body, 0)
    for needle in (
        'class="sheet-doubts" hidden',
        'class="badge gd-own" hidden>بضبط خاص',
        "gd-auto",
        "التلقائي",
        "gd-exclude",
        "تجهيز الصفحة",
    ):
        assert needle in shell, needle
    assert "x-" not in shell
    assert (
        body.count('<span class="gd-bands" aria-hidden="true"></span>') == 3 + 1 + 1
    )  # tiles + tile shell + thumb shell
    assert '<span class="bk-thumb-mark is-doubt" hidden>' in body
    # the dialogs: re-run and delete (§3.13)
    assert "data-rerun-dialog" in body and f"حذف الكتاب «{book.title}»؟" in body
    assert "يُحذف الكتاب وملفه وكل صفحاته ونصوصها ومخطوطته وملفات إخراجه، ولا يمكن التراجع عن ذلك." in body
    # D65: no user-facing text says «الأدلة»
    assert "الأدلة" not in body


def test_guides_mode_states_uploaded_preparing_and_error(editor_client):
    book, _ = _awaiting(Book.Status.UPLOADED)
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert _config(body)["startAction"] == "extract"
    extract = _between(body, f'action="{reverse("books:start", args=[book.pk])}"', "</form>")
    assert "x-cloak" not in extract.split(">")[0] and ">استخراج الصفحات</span>" in extract
    assert 'x-text="emptyTitle">لم تُستخرج الصفحات بعد</h2>' in body and 'x-text="emptyText"' in body
    # while the pages are prepared: the chip counts them, «بدء المعالجة» waits, the empty state says so
    book2, _ = _awaiting(Book.Status.PROCESSING)
    body = editor_client.get(reverse("books:detail", args=[book2.pk])).content.decode()
    assert _config(body)["startAction"] == "startOcrDisabled"
    assert 'x-text="emptyTitle">تُستخرج الصفحات الآن</h2>' in body
    # the mode's chip and bar are the stage bar's «التخطيط» now (D76, §5.1)
    assert 'data-stage-bar data-rail data-current="pages"' in body and "نسبة الصفحات المُجهَّزة" not in body
    # every page failed: today's error banner and «إعادة استخراج الصفحات»
    book3, _ = _awaiting(Book.Status.ERROR, [Page.Status.ERROR])
    body = editor_client.get(reverse("books:detail", args=[book3.pk])).content.decode()
    assert _config(body)["startAction"] == "reextract" and ">إعادة استخراج الصفحات</span>" in body
    assert "x-show=\"status === 'error'\"" in body


def test_guides_view_on_a_started_book_and_the_plain_dashboard(editor_client):
    book, _ = _book(
        [(Page.Status.OCR_DONE, "final"), (Page.Status.REVIEWED, "final")], status=Book.Status.REVIEWING
    )
    url = reverse("books:detail", args=[book.pk])
    plain = editor_client.get(url).content.decode()
    # outside the mode: no mode markup at all; «⋯» «التخطيط» opens `/guides/` (D84)
    for needle in ("is-guides", "sheet-guides", "bookGuides", "gd-", "startOcr", "العودة إلى الصفحات"):
        assert needle not in plain, needle
    assert _config(plain)["guidesMode"] is False
    item = _between(plain, "data-guides-menu-item", "</a>")
    assert "<span>التخطيط</span>" in item and f'href="{url}guides/"' in plain
    body = editor_client.get(url + "guides/").content.decode()
    config = _config(body)
    assert config["guidesMode"] is True and config["layoutStage"] is False and config["startAction"] == "back"
    assert 'class="bk-dashboard is-guides' in body
    # «العودة إلى الصفحات», the re-read banner, today's «⋯» with «حذف الكتاب…» (and without «التخطيط»)
    back = _between(body, "data-guides-back", "</a>")
    assert "العودة إلى الصفحات" in back and "x-cloak" not in _between(
        body, "x-show=\"d.primary === 'back'\"", ">"
    )
    assert (
        "تغيير المناطق هنا يُعيد التعرّف على نص الصفحة. الصفحات المعتمدة وما فيه تصحيحات مراجعة لا تتغيّر."
        in body
    )
    menu = _between(body, 'class="menu menu-popover bk-menu"', "</template>")
    assert (  # in «التخطيط» of a started book the one re-run is «إعادة التخطيط…» (D101)
        menu.count("data-rerun-action=") == 1
        and 'data-rerun-action="preprocess"' in menu
        and "d.openRerun('preprocess', 'إعادة التخطيط')" in menu
        and "إعادة المعالجة" not in menu
        and "data-rerun-stage" not in menu
        and "حذف الكتاب…" in menu
        and "data-guides-menu-item" not in menu
    )
    assert "تجميع المخطوطة…" in menu and "bk-convert-host" in body
    assert "جُهّزت الصفحات واكتُشفت مناطقها" not in body


def test_guides_mode_for_a_proofreader_shows_the_layout_without_controls(client):
    book, _ = _awaiting(Book.Status.NEEDS_GUIDES, [Page.Status.PREPROCESSED])
    client.force_login(_user("reader", "proofreader"))
    body = client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert "is-guides" in body and "لكل الصفحات" not in body and "حذف الكتاب" not in body
    tpl = _between(body, '<template id="sheet-guides">', "</template>")
    assert "gd-grip" not in tpl and "gd-menu" not in tpl and "gd-savebar" not in tpl and "gd-lock" in tpl
    assert "d.primary === 'startOcr'" not in body and "gd-exclude" not in body


def test_books_list_waits_for_the_start_instead_of_a_full_bar(editor_client):
    _awaiting(Book.Status.NEEDS_GUIDES, [Page.Status.PREPROCESSED] * 2)
    body = editor_client.get(reverse("books:list")).content.decode()
    row = _between(body, '<li class="lb-card', '<details class="lb-more">')  # the book on the shelf
    assert "بانتظار «بدء المعالجة»" in row and 'role="progressbar"' not in row and "dot-warning" in row
    assert "تم التخطيط" in row


# ---------------------------------------------------------- «التخطيط» mode under Node, on the §3.11 fixtures


class _Tree(HTMLParser):
    """Rendered template HTML → a JSON tree {t, a, c} the Node harness builds its DOM from."""

    VOID = {"img", "input", "br", "hr", "meta", "link", "use", "source"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"t": "#frag", "a": {}, "c": []}
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {"t": tag, "a": {k: ("" if v is None else v) for k, v in attrs}, "c": []}
        self.stack[-1]["c"].append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1]["c"].append({"t": tag, "a": {k: ("" if v is None else v) for k, v in attrs}, "c": []})

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]["t"] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data.strip():
            self.stack[-1]["c"].append(data)


def _tree(html: str) -> dict:
    parser = _Tree()
    parser.feed(html)
    return parser.root


def _template(body: str, tpl_id: str) -> dict:
    inner = _between(body, f'<template id="{tpl_id}">', "</template>")[len(f'<template id="{tpl_id}">') :]
    return _tree(inner)


GUIDES_HARNESS = r"""
// A small DOM with a selector engine (tag, .class, #id, [attr], [attr="v"], descendant, comma lists): enough for
// books.js and processing.js to mount, patch and query the mode's templates, rendered by Django and passed in.
class CL { constructor(el){ this.el = el; this.s = new Set(); } add(...c){ c.forEach((x) => this.s.add(x)); } remove(...c){ c.forEach((x) => this.s.delete(x)); }
  toggle(c, f){ if (f === undefined) f = !this.s.has(c); if (f) this.s.add(c); else this.s.delete(c); return f; } contains(c){ return this.s.has(c); } toString(){ return [...this.s].join(' '); } }
class N { constructor(){ this.childNodes = []; this.parentNode = null; }
  appendChild(n){ if (n.tagName === '#FRAG') { [...n.childNodes].forEach((c) => this.appendChild(c)); return n; } if (n.parentNode) n.parentNode.removeChild(n); n.parentNode = this; this.childNodes.push(n); return n; }
  insertBefore(n, ref){ if (n.parentNode) n.parentNode.removeChild(n); const i = ref ? this.childNodes.indexOf(ref) : -1; n.parentNode = this; if (i < 0) this.childNodes.push(n); else this.childNodes.splice(i, 0, n); return n; }
  removeChild(n){ const i = this.childNodes.indexOf(n); if (i >= 0) this.childNodes.splice(i, 1); n.parentNode = null; return n; } }
class T extends N { constructor(d){ super(); this.nodeValue = d; } get textContent(){ return this.nodeValue; } }
const camel = (k) => k.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
class E extends N {
  constructor(tag){ super(); this.tagName = String(tag).toUpperCase(); this.classList = new CL(this); this.attrs = {}; this.dataset = {}; this.hidden = false; this.disabled = false; this.value = ''; this.listeners = {};
    const st = {}; st.setProperty = (k, v) => { st[k] = String(v); }; st.getPropertyValue = (k) => st[k] || ''; this.style = st; }
  set className(v){ this.classList = new CL(this); String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c)); } get className(){ return this.classList.toString(); }
  setAttribute(k, v){ v = String(v); if (k === 'class') this.className = v; else if (k === 'hidden') this.hidden = true; else if (k === 'disabled') this.disabled = true; else if (k === 'value') this.value = v; else if (k.startsWith('data-')) this.dataset[camel(k.slice(5))] = v; else this.attrs[k] = v; }
  getAttribute(k){ if (k === 'class') return this.className; if (k.startsWith('data-')) { const d = this.dataset[camel(k.slice(5))]; return d === undefined ? null : d; } if (k === 'name' || k === 'type') return k in this.attrs ? this.attrs[k] : null; return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k){ delete this.attrs[k]; }
  get children(){ return this.childNodes.filter((n) => n instanceof E); }
  set textContent(v){ this.childNodes.forEach((n) => { n.parentNode = null; }); this.childNodes = v === '' || v === null || v === undefined ? [] : [new T(String(v))]; }
  get textContent(){ return this.childNodes.map((n) => n.textContent).join(''); }
  get name(){ return this.attrs.name; } get type(){ return this.attrs.type || ''; }
  set type(v){ this.attrs.type = v; }
  addEventListener(t, fn){ (this.listeners[t] = this.listeners[t] || []).push(fn); }
  dispatch(t, ev){ let el = this; ev.target = ev.target || this; while (el) { (el.listeners[t] || []).forEach((fn) => fn(ev)); el = el.parentNode; } }
  focus(){ document.activeElement = this; }
  getBoundingClientRect(){ return this._rect || { top: 0, left: 0, width: 0, height: 0, bottom: 0, right: 0 }; }
  setPointerCapture(){}
  matches(sel){ return sel.split(',').some((one) => matchComplex(this, one.trim().split(/\s+/))); }
  closest(sel){ let el = this; while (el && el instanceof E) { if (el.matches(sel)) return el; el = el.parentNode; } return null; }
  querySelectorAll(sel){ const out = []; const walk = (n) => n.children.forEach((c) => { if (c.matches(sel)) out.push(c); walk(c); }); walk(this); return out; }
  querySelector(sel){ return this.querySelectorAll(sel)[0] || null; }
}
function matchCompound(el, c){
  const m = /^([a-zA-Z][\w-]*)?/.exec(c); let rest = c.slice(m[0].length);
  if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
  while (rest) {
    let t;
    if ((t = /^\.([\w-]+)/.exec(rest))) { if (!el.classList.contains(t[1])) return false; }
    else if ((t = /^#([\w-]+)/.exec(rest))) { if (el.attrs.id !== t[1]) return false; }
    else if ((t = /^\[([\w-]+)(?:="([^"]*)")?\]/.exec(rest))) { const v = el.getAttribute(t[1]); if (v === null || v === undefined || (t[2] !== undefined && String(v) !== t[2])) return false; }
    else return false;
    rest = rest.slice(t[0].length);
  }
  return true;
}
function matchComplex(el, parts){
  if (!matchCompound(el, parts[parts.length - 1])) return false;
  let i = parts.length - 2; let a = el.parentNode;
  while (i >= 0 && a && a instanceof E) { if (matchCompound(a, parts[i])) i -= 1; a = a.parentNode; }
  return i < 0;
}
const build = (node) => {
  if (typeof node === 'string') return new T(node);
  const el = new E(node.t);
  Object.entries(node.a || {}).forEach(([k, v]) => el.setAttribute(k, v));
  (node.c || []).forEach((c) => el.appendChild(build(c)));
  return el;
};
const inits = []; const reg = {}; const timers = []; const stores = {}; const events = []; const winListeners = {};
globalThis.window = globalThis;
globalThis.document = { hidden: false, activeElement: null, createElement: (t) => new E(t), createTextNode: (d) => new T(d),
  addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); }, getElementById: (id) => TEMPLATES[id] || null, querySelector: () => null, querySelectorAll: () => [] };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: (n, v) => { if (v !== undefined) { stores[n] = v; return v; } return stores[n]; } };
globalThis.CustomEvent = class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } };
globalThis.dispatchEvent = (ev) => { events.push([ev.type, ev.detail]); return true; };
globalThis.location = { pathname: '/books/25/', search: '', hash: '', assign: () => {}, reload: () => {} };
globalThis.history = { replaceState: (_s, _t, url) => { const i = url.indexOf('#'); location.hash = i >= 0 ? url.slice(i) : ''; } };
globalThis.addEventListener = (ev, fn) => { (winListeners[ev] = winListeners[ev] || []).push(fn); };
globalThis.removeEventListener = (ev, fn) => { winListeners[ev] = (winListeners[ev] || []).filter((f) => f !== fn); };
globalThis.requestAnimationFrame = (fn) => { fn(); return 1; };
globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
globalThis.clearTimeout = (h) => { if (h && timers[h - 1]) timers[h - 1].fn = null; };
const store = {}; globalThis.localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
globalThis.sessionStorage = { getItem: () => null, setItem: () => {} };
const fs = require('fs');
const DATA = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const FX = DATA.fixtures;
const TEMPLATES = {};
Object.entries(DATA.templates).forEach(([id, tree]) => { TEMPLATES[id] = { content: { cloneNode: () => build(tree) } }; });
for (const f of process.argv.slice(3)) eval(fs.readFileSync(f, 'utf8'));
inits.forEach((fn) => fn());
const toasts = []; window.Nassakh.toast = (m) => toasts.push(m);
const fire = async (ms) => { const due = timers.filter((t) => t.ms === ms && t.fn); timers.length = 0; for (const t of due) await t.fn(); await new Promise((r) => setImmediate(r)); return due.length; };
const settle = () => new Promise((r) => setImmediate(r));
const out = {};
const clone = (o) => JSON.parse(JSON.stringify(o));

// --- the server: the §3.11 fixtures, answered by URL and body; every request is recorded
const calls = [];
let conflict = false;
const reply = (status, data) => Promise.resolve({ ok: status >= 200 && status < 300, status, json: async () => clone(data) });
const blocks = FX['sheet_guides_blocks.json'];
const sheetBase = FX['sheets_guides.json'].pages[0];
const BLOCK_OF = { 1: 'computed', 2: 'no_footnote', 3: 'computed', 4: 'computed', 5: 'computed', 6: 'computed', 7: 'footnote_from_type' };
let stateFixture = 'book_guides_layout.json';
let startedBlocks = false;
const STARTED_OF = { 1: 'approved', 2: 'review', 3: 'derived', 4: 'derived', 5: 'derived', 6: 'derived', 7: 'derived_override_cut' };
globalThis.fetch = (url, opts = {}) => {
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({ url, method: opts.method || 'GET', body });
  const u = String(url);
  if (u.startsWith('/api/books/25/sheets/')) {
    const m = /from=(\d+)&to=(\d+)/.exec(u); const items = [];
    for (let n = Number(m[1]); n <= Number(m[2]); n += 1) items.push({ ...sheetBase, id: 811 + n, number: n, display_url: `/media/books/25/pages/000${n}/display.webp`, guides: clone(blocks[(startedBlocks ? STARTED_OF : BLOCK_OF)[n]]) });
    return reply(200, { pages: items, book_line_h_px: 40 });
  }
  if (u.startsWith('/api/books/25/guides/preview/')) return reply(200, FX['book_guides_preview.json'][body.stage === 'ocr' ? 'ocr' : 'layout'].response);
  if (u.startsWith('/api/books/25/guides/') && (opts.method || 'GET') === 'GET') {
    const st = clone(FX[stateFixture]);
    const m = /from=(\d+)&to=(\d+)/.exec(u);
    if (m) st.pages = st.pages.filter((p) => p.n >= Number(m[1]) && p.n <= Number(m[2]));
    return reply(200, st);
  }
  if (u.startsWith('/api/books/25/guides/')) {
    const A = FX['book_guides_apply.json'];
    if (conflict) return reply(409, A.conflict.response);
    if (body.undo) return reply(200, A.undo.response);
    return reply(200, A[body.stage === 'ocr' ? 'ocr' : 'layout'].response);
  }
  const pm = /^\/api\/pages\/(\d+)\/guides\/$/.exec(u);
  if (pm) {
    const P = FX['page_guides.json'];
    if (conflict) return reply(409, FX['errors.json'].started.response);
    if (body.replace !== undefined) return reply(200, P.undo.response);
    if (body.stage === 'ocr') return reply(200, P.ocr_save.response);
    if (body.set && body.set.page_number_zone) return reply(200, P.remove_page_number.response);
    return reply(200, P.merge.response);
  }
  return reply(404, {});
};

// --- the dashboard as the server renders it: shells and tiles from the templates, the stack in `.bk-layout`
const tilesOf = (layout) => layout.pages.map((e) => ({ id: e.id, number: e.n, status: e.s, status_label: '', text_state: 'none', is_excluded: e.x, error: e.s === 'error', width: 1000, height: 3000, thumb_url: e.s === 'preprocessed' ? `/t/${e.n}.webp` : null }));
function mountDashboard(cfgName, layoutName, extra = {}) {
  const cfg = clone(FX['dashboard_config.json'][cfgName]);
  const pages = tilesOf(FX[layoutName]);
  const root = new E('div'); const layout = new E('div'); layout.className = 'bk-layout';
  const stack = new E('div'); stack.setAttribute('data-sheet-stack', ''); const grid = new E('div'); grid.setAttribute('data-page-grid', '');
  const film = new E('div'); film.setAttribute('data-film-track', '');
  layout.appendChild(stack); layout.appendChild(grid); layout.appendChild(film); root.appendChild(layout);
  pages.forEach((p) => {
    const shell = build(DATA.templates['sheet-shell']).children[0]; shell.dataset.pageId = String(p.id); stack.appendChild(shell);
    const tile = build(DATA.templates['tile-shell']).children[0]; tile.dataset.pageId = String(p.id); grid.appendChild(tile);
  });
  const d = reg.bookDashboard(Object.assign(cfg, { progressUrl: '/api/books/25/progress/', sheetsUrl: '/api/books/25/sheets/', bookUrl: '/books/25/', canEdit: true, bookId: 25,
    active: false, status: 'needs_guides', statusLabel: 'تم التخطيط', stages: [], byStatus: { preprocessed: 7 }, total: 7, pages, urls: { page: '/books/25/pages/__n__/', review: '/books/25/review/__n__/', rerun: '/books/25/pages/__n__/rerun/', exclude: '/books/25/pages/__n__/exclude/' } }, extra));
  d.$el = root; d.$watch = () => {}; d.$nextTick = (fn) => fn(); d.$refs = {};
  d.init();
  const g = reg.bookGuides(); g.$el = layout; g.$nextTick = (fn) => fn(); g.init();
  return { d, g, root, layout, stack, grid, film };
}
const shellOf = (m, n) => m.stack.children.find((el) => el.dataset.number === String(n));
const figOf = (m, n) => shellOf(m, n).querySelector('.gd-page');
const tileOf = (m, n) => m.grid.children.find((el) => el.dataset.number === String(n));
const bandsOf = (fig) => fig.querySelectorAll('.gd-band').map((b) => [b.dataset.kind, b.style.left, b.style.top, b.style.width, b.style.height, b.querySelector('.gd-chip').textContent, b.querySelector('.gd-chip').tagName.toLowerCase(), Boolean(b.querySelector('.gd-chip').disabled)]);
const lineOf = (fig, kind) => { const l = fig.querySelector(`.guide-line.is-${kind}`); return l.hidden ? null : { top: l.style.top, handle: l.querySelector('.guide-handle').textContent, now: l.getAttribute('aria-valuenow'), text: l.getAttribute('aria-valuetext'), tab: l.getAttribute('tabindex'), dashed: l.classList.contains('is-dashed') }; };

(async () => {
  const G = window.NassakhGuides; const B = window.NassakhBooks;
  // --- pure helpers: gap middles, snapping (Alt off, tolerance), the gaps for ⇧↑ / ⇧↓, where an added line lands
  const rows = blocks.computed.rows;
  out.gaps = G.gapMiddles(rows);
  out.snap = { near: G.snapY(0.785, rows, 0.0107), far: G.snapY(0.795, rows, 0.0107), free: G.snapY(0.785, rows, 0.0107, true), tight: G.snapY(0.785, rows, 0.002) };
  out.step = [G.stepGap(0.7817, rows, 1), G.stepGap(0.7817, rows, -1), G.stepGap(0.95, rows, 1)];
  out.defaults = [G.defaultY('header', rows), G.defaultY('footnote', rows), G.defaultY('header', []), G.defaultY('footnote', [])];
  out.adjMove = G.adjacentBands(blocks.computed.bands, 'footnote', 0.8).map((b) => [b.kind, ...b.bbox]);
  out.adjAddHeader = G.adjacentBands(blocks.computed.bands, 'header', 0.06).map((b) => [b.kind, ...b.bbox]);
  out.adjAddFoot = G.adjacentBands(blocks.no_footnote.bands, 'footnote', 0.8).map((b) => [b.kind, ...b.bbox]);
  out.adjOff = G.adjacentBands(blocks.derived.bands, 'footnote', null).map((b) => [b.kind, ...b.bbox]);
  out.noNumber = G.withoutNumber(blocks.computed.bands).map((b) => [b.kind, ...b.bbox]);
  out.merge = [G.mergeBody({ footnote_line: 0.80001 }, ['header_cut', 'footnote_line']), G.mergeBody({}, [], true)];
  out.inverse = [G.inverse(FX['page_guides.json'].merge.response), G.inverse(FX['book_guides_apply.json'].layout.response), G.inverse(FX['book_guides_apply.json'].ocr.response)];
  out.ratio = [G.ratioOf('80.5'), G.ratioOf('٦٫٢'), G.ratioOf(''), G.ratioOf('6,25')];
  out.draft = [G.draftLines(blocks.computed, { header_cut: 0.062, footnote_line: 0.9 }), G.draftLines(blocks.no_footnote, { footnote_line: 0.8 }), G.draftLines(blocks.derived_override_cut, { header_cut: 0.07 })];
  out.compact = B.compactBands(blocks.derived.bands);

  // --- the form's range probe (§3.13) on byte fixtures: a found count, a missing one, two /Count values
  const pdf = (s) => `%PDF-1.4\n${s}\n%%EOF`;
  out.probe = [
    B.pdfPageCount(pdf('1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj 2 0 obj << /Type /Pages /Kids [3 0 R] /Count 555 >> endobj 3 0 obj << /Type /Page /Parent 2 0 R /Resources << /Font << >> >> >> endobj')),
    B.pdfPageCount(pdf('1 0 obj << /Type /ObjStm /N 5 /First 20 /Filter /FlateDecode >> stream xyz endstream endobj')),
    B.pdfPageCount(pdf('4 0 obj << /Type /Pages /Parent 2 0 R /Kids [5 0 R] /Count 40 >> endobj 2 0 obj << /Count 96 /Kids [4 0 R 6 0 R] /Type/Pages >> endobj 5 0 obj << /Type /Page /Count 7 >> endobj')),
  ];
  const f = reg.bookForm({ pagesPerSheet: '1', skipFirst: '186', skipLast: '362' });
  const bytes = Buffer.from(pdf('2 0 obj << /Type /Pages /Kids [] /Count 555 >> endobj'), 'latin1');
  await f.onFile({ target: { files: [{ name: 'الحوليات الليبية.pdf', size: bytes.length, slice: (a, b) => ({ arrayBuffer: async () => bytes.subarray(a, b) }) }] } });
  const lines = [f.fileName, f.sourcePages, f.range.text];
  f.skipLast = 369; lines.push(f.range.text, f.range.error);
  f.sourcePages = null; f.skipLast = 362; lines.push(f.range.text);
  f.skipFirst = 0; f.skipLast = 0; lines.push(f.range.text);
  f.sourcePages = 8; lines.push(f.range.text);
  f.sourcePages = 52; f.skipFirst = 2; f.skipLast = 2; f.pagesPerSheet = 2; lines.push(f.range.text);
  out.form = lines;

  // --- the layout book in «تم التخطيط»: the mode opens on the grid; the state arrives; bands on tiles and thumbs
  const L = mountDashboard('needs_guides', 'book_guides_layout.json');
  const d = L.d; const g = L.g;
  out.openView = d.view;
  // bookGuides' scope sits inside the dashboard's: no name of it may shadow one of the dashboard's
  out.collide = Object.keys(Object.getOwnPropertyDescriptors(g)).filter((k) => k in d && !['init', 'destroy'].includes(k) && !k.startsWith('$'));
  await fire(60); await settle(); // the sheets of the mounted pages (no observer: every sheet mounts)
  await settle();
  out.primaryLayout = d.primary;
  out.counts = { ...d.counts };
  out.tileBands = tileOf(L, 1).querySelector('.gd-bands').children.map((s) => [s.className, s.style.left, s.style.top, s.style.width, s.style.height]);
  out.tileAr = tileOf(L, 1).querySelector('.page-tile-thumb').style['--tile-ar'];
  out.doubtMarks = [1, 7].map((n) => [tileOf(L, n).classList.contains('is-doubt'), !tileOf(L, n).querySelector('.tile-mark-doubt').hidden]);
  out.thumbs = L.film.children.map((t) => [t.dataset.number, t.querySelector('.gd-bands').children.length, !t.querySelector('.bk-thumb-mark.is-doubt').hidden]);
  // the filters: the chip «تستحق نظرة» keeps page 7; «بضبط خاص» nothing yet; stored per book
  d.setFilter('doubt'); out.filterDoubt = [d.visibleNumbers(), store['nassakh.guidesFilter.25'], d.filteredOut];
  d.setFilter('override'); out.filterOverride = [d.filteredOut]; d.setFilter('all');
  out.stateCalls = calls.filter((c) => c.url.startsWith('/api/books/25/guides/')).map((c) => c.url);
  out.sheetCalls = calls.filter((c) => c.url.startsWith('/api/books/25/sheets/')).map((c) => c.url);
  // the viewer: V, a tile click, the page shown (the event the side panel follows)
  out.keys = [d.keyAction({ key: 'ر', code: 'KeyV' }, false), d.keyAction({ key: 'n', code: 'KeyN' }, false), d.keyAction({ key: 'c', code: 'KeyC' }, false)];
  d.onKey({ key: 'ر', code: 'KeyV', target: {}, preventDefault: () => {} });
  out.afterV = [d.view, store['nassakh.bookView'] === undefined];
  d.showPage(1, { instant: true });
  out.shown = events.filter((e) => e[0] === 'nassakh:page-shown').map((e) => e[1]).slice(-1)[0];
  out.pageId = g.pageId;
  // patchGuidesBody: the computed block of page 1, drawn in physical percentages; lines as sliders
  const fig1 = figOf(L, 1);
  out.fig1 = { editable: fig1.classList.contains('is-editable'), img: fig1.querySelector('.gd-img').getAttribute('src'), alt: fig1.querySelector('.gd-img').getAttribute('alt'), bands: bandsOf(fig1), header: lineOf(fig1, 'header'), footnote: lineOf(fig1, 'footnote'),
    grips: [fig1.querySelector('.gd-grip.is-header').hidden, fig1.querySelector('.gd-grip.is-footnote').hidden], sr: fig1.querySelector('.gd-sr').textContent, lock: fig1.querySelector('.gd-lock').hidden, savebar: fig1.querySelector('.gd-savebar').hidden };
  const fig7 = figOf(L, 7);
  out.head7 = { doubts: shellOf(L, 7).querySelector('.sheet-doubts').textContent, own: shellOf(L, 7).querySelector('.gd-own').hidden, auto: shellOf(L, 7).querySelector('.gd-auto').hidden, exclude: shellOf(L, 7).querySelector('.gd-exclude-label').textContent,
    next: shellOf(L, 7).querySelector('.gd-exclude input[name="next"]').value, action: shellOf(L, 7).querySelector('.gd-exclude').getAttribute('action') };
  out.fig7Foot = lineOf(fig7, 'footnote');

  // --- fixing one page (§3.12): a drag of page 1's footnote line snaps to the gap and is saved at once
  fig1._rect = { top: 0, left: 0, width: 333, height: 1000, bottom: 1000, right: 333 };
  const footLine = fig1.querySelector('.guide-line.is-footnote');
  footLine.dispatch('pointerdown', { button: 0, clientY: 781, pointerId: 1, preventDefault: () => {} });
  g.onPointerMove({ clientY: 786, altKey: false });
  out.dragPaint = { top: footLine.style.top, handle: footLine.querySelector('.guide-handle').textContent, body: bandsOf(fig1)[0].slice(0, 5), foot: bandsOf(fig1)[1].slice(0, 5) };
  calls.length = 0;
  g.onPointerUp({});
  await settle(); await settle();
  out.dragSave = calls.map((c) => [c.url, c.body]);
  out.dragToast = [g.toast.message, g.toast.hasAction, g.toast.visible];
  // the answer's block replaces the page's (the fixture answers for page 2's merge: the footnote at 0.8)
  out.afterSave = lineOf(fig1, 'footnote');
  // a toast action never runs by itself: showing it posts nothing, «تراجع» posts the inverse
  const before = calls.length; await settle();
  out.undoIdle = calls.length === before;
  calls.length = 0; g.runToastAction(); await settle(); await settle();
  out.undoPage = calls.map((c) => [c.url, c.body]);
  out.undoToast = [g.toast.message, g.toast.hasAction];
  // the merge fixture exactly: page 2 gains its footnote line (a grip adds it at the gap nearest 80 %)
  const fig2 = figOf(L, 2); fig2._rect = { top: 0, left: 0, width: 333, height: 1000 };
  out.grip2 = [fig2.querySelector('.gd-grip.is-footnote').hidden, fig2.querySelector('.gd-grip.is-footnote').textContent];
  calls.length = 0;
  fig2.querySelector('.gd-grip.is-footnote').dispatch('pointerdown', { button: 0, clientY: 900, preventDefault: () => {} });
  g.onPointerUp({}); await settle(); await settle();
  out.gripSave = calls.map((c) => c.body);
  calls.length = 0;
  await g.change(String(813), { set: { footnote_line: 0.8 } }, 'footnote'); await settle();
  out.mergeRequest = calls.map((c) => [c.url, c.body]);
  out.mergeFixture = FX['page_guides.json'].merge.request;
  // keys on a focused line: ↓ moves 0.2 %, ⇧↓ to the next gap; saved once the keys rest (700 ms)
  const line2 = fig2.querySelector('.guide-line.is-footnote');
  calls.length = 0; timers.length = 0;
  const kd = (key, shiftKey = false) => { const ev = { key, shiftKey, target: line2, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } }; L.stack.dispatch('keydown', ev); return ev.defaultPrevented; };
  out.keyHandled = [kd('ArrowDown'), line2.style.top];
  kd('ArrowDown', true); out.keyGap = line2.style.top;
  out.keyNoSaveYet = calls.length;
  await fire(700); await settle();
  out.keySave = calls.map((c) => c.body);
  // the band menu: «ليس رقم صفحة» on page 1
  calls.length = 0;
  const chipP = fig1.querySelectorAll('.gd-band').find((b) => b.dataset.kind === 'page_number').querySelector('.gd-chip');
  L.stack.dispatch('click', { target: chipP, preventDefault: () => {} });
  const menu = fig1.querySelector('.gd-menu');
  out.menu = [menu.hidden, menu.querySelectorAll('[data-act]').filter((i) => !i.hidden).map((i) => i.textContent), chipP.getAttribute('aria-expanded')];
  L.stack.dispatch('click', { target: menu.querySelector('[data-act="no-number"]'), preventDefault: () => {} });
  await settle(); await settle();
  out.noNumberRequest = calls.map((c) => c.body);
  out.menuClosed = menu.hidden;

  // --- all pages: the draft → preview → apply flow, and undo
  calls.length = 0; timers.length = 0;
  g.ctl.footnote = { on: true, pct: '80.5' }; g.onControl();
  out.draftSet = clone(g.draft);
  out.paused = figOf(L, g.pageNumber || d.current).classList.contains('is-paused');
  await fire(250); await settle();
  out.previewRequest = calls.map((c) => [c.url, c.body]);
  out.previewFixture = FX['book_guides_preview.json'].layout.request;
  out.previewText = [g.previewChanged, g.previewCut, g.cutPages, g.keptText, g.previewReocr, g.previewLocked];
  // Esc drops the draft (after an unsaved move and the menu)
  out.escape = [g.escape(), g.draft, g.ctl.footnote.on];
  // the running head for every page, applied, then undone from the toast
  calls.length = 0; timers.length = 0;
  g.ctl.header = { on: true, pct: '6.2' }; g.onControl(); await fire(250); await settle();
  calls.length = 0;
  await g.applyDraft(); await settle();
  out.applyRequest = calls.map((c) => [c.url, c.body]);
  out.applyFixture = FX['book_guides_apply.json'].layout.request;
  out.applied = { toast: [g.toast.message, g.toast.hasAction], book: g.book.source, ctl: clone(g.ctl), draft: g.draft, tileHead: tileOf(L, 1).querySelector('.gd-bands').children.map((s) => s.className)[0] };
  const idle = calls.length; await settle(); out.applyIdle = calls.length === idle;
  calls.length = 0; g.runToastAction(); await settle(); await settle();
  out.undoBook = calls.map((c) => c.body);
  out.undoBookFixture = FX['book_guides_apply.json'].undo.request;
  out.afterUndo = [g.toast.message, g.book.source, g.ctl.header.on];
  // started elsewhere: a 409 turns the banner on
  conflict = true; g.ctl.header = { on: true, pct: '6.2' }; g.onControl(); await fire(250); await settle(); await g.applyDraft(); await settle();
  out.conflict = d.startedElsewhere; conflict = false; g.dropDraft();
  // the end of preparation: «اكتمل التخطيط · 7 صفحات», no action
  d.active = true;
  d.apply({ total: 7, percent: 100, flags: 0, status: 'needs_guides', status_label: 'تم التخطيط', dot: 'dot-warning', by_status: { preprocessed: 7 }, active: false, layout_stage: true, waiting: true, pages: [] });
  out.doneToast = [d.doneToast.visible, d.doneText, d.doneToast.url];
  // dialogs: the re-run estimate of «إعادة التخطيط» (D101), the delete line
  d.openRerun('preprocess', 'إعادة التخطيط');
  out.rerunDialog = [d.rerunTitle, d.rerunText, d.rerunNote, d.rerunButton];
  d.onKey({ key: 'Escape', target: {}, preventDefault: () => {} }); out.dialogEsc = d.dialog.kind;
  d.openDelete(); out.deleteWork = d.deleteWork; d.closeDialog();

  // --- the chrome per state (§3.12): primary, chip, bar
  const S = (cfgName, extra) => mountDashboard(cfgName, 'book_guides_layout.json', extra).d;
  const pre = S('processing', { status: 'processing', active: true, byStatus: FX['progress.json'].processing.by_status, total: 6 });
  out.states = {
    uploaded: S('uploaded', { status: 'uploaded' }).primary,
    processing: [pre.primary, pre.statusText, pre.showBar, pre.preparedLine],
    needs_guides: d.primary,
    error: S('error', { status: 'error' }).primary,
    reader: S('needs_guides', { canEdit: false }).primary,
  };
  // a poll that says «المعالجة» began elsewhere
  pre.apply({ ...FX['progress.json'].started, pages: [] }); out.startedPoll = pre.startedElsewhere;
  // the mode opens on the viewer only with a #sheet-N address
  location.hash = '#sheet-3'; out.hashView = S('needs_guides').view; location.hash = '';

  // --- a started book (`?view=guides`): changes wait for the explicit save; locked pages have no handles
  stateFixture = 'book_guides_started.json'; startedBlocks = true;
  const M = mountDashboard('started_view_guides', 'book_guides_started.json', { status: 'reviewing', statusLabel: 'قيد المراجعة' });
  await fire(60); await settle(); await settle();
  out.startedPrimary = M.d.primary;
  const f1 = figOf(M, 1);
  out.locked = { lock: f1.querySelector('.gd-lock').hidden, text: f1.querySelector('.gd-lock-text').textContent, editable: f1.classList.contains('is-editable'), chipsDisabled: bandsOf(f1).filter((b) => b[6] === 'button').every((b) => b[7]), tab: lineOf(f1, 'header').tab };
  out.lockedReview = figOf(M, 2).querySelector('.gd-lock-text').textContent;
  M.d.showPage(3, { instant: true });
  calls.length = 0;
  M.g.change('814', { set: { footnote_line: 0.79 } }, 'footnote');
  const f3 = figOf(M, 3);
  out.pending = { calls: calls.length, savebar: f3.querySelector('.gd-savebar').hidden, dashed: lineOf(f3, 'footnote').dashed, top: lineOf(f3, 'footnote').top, pending: f3.classList.contains('is-pending') };
  await M.g.savePending('814'); await settle();
  out.ocrSave = calls.map((c) => [c.url, c.body]);
  out.ocrSaveFixture = FX['page_guides.json'].ocr_save.request;
  out.ocrToast = [M.g.toast.message, M.g.toast.hasAction];
  // the preview in «المعالجة» names the re-read and the locked pages; the apply toast has no undo
  calls.length = 0; timers.length = 0;
  M.g.ctl.header = { on: true, pct: '7' }; M.g.onControl(); await fire(250); await settle();
  out.ocrPreview = [calls.map((c) => c.body)[0], M.g.previewChanged, M.g.previewReocr, M.g.previewLocked, M.g.keptText, M.g.keptApply];
  await M.g.applyDraft(); await settle();
  out.ocrApply = [M.g.toast.message, M.g.toast.hasAction];
  // «إعادة التخطيط» of a started book reads the pages again too: the note names the models' time (D101)
  M.d.openRerun('preprocess', 'إعادة التخطيط');
  out.relayout = [M.d.rerunTitle, M.d.rerunText, M.d.rerunNote, M.d.rerunButton];
  // outside the mode: «إعادة المعالجة» with the dashboard's estimate (dashboard_config.json `started`)
  M.d.rerun = clone(FX['dashboard_config.json'].started.rerun);
  M.d.openRerun('ocr', 'إعادة المعالجة');
  out.reprocess = [M.d.rerunTitle, M.d.rerunText, M.d.rerunNote, M.d.rerunButton];
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_guides_mode_logic_under_node_on_the_contract_fixtures(editor_client, tmp_path):
    book, _ = _awaiting(Book.Status.NEEDS_GUIDES, [Page.Status.PREPROCESSED])
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    data = {
        "templates": {
            name: _template(body, name)
            for name in ("sheet-guides", "sheet-shell", "tile-shell", "thumb-shell")
        },
        "fixtures": {
            path.name: json.loads(path.read_text(encoding="utf-8")) for path in FIXTURES.glob("*.json")
        },
    }
    (tmp_path / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(GUIDES_HARNESS, encoding="utf-8")
    files = [str(JS / name) for name in ("ui.js", "keys.js", "books.js", "processing.js")]
    run = subprocess.run(
        ["node", str(harness), str(tmp_path / "data.json"), *files],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])

    # --- pure helpers (NassakhGuides): gap middles, snapping within the tolerance and never with ⌥, the gaps
    # of ⇧↑ / ⇧↓, where an added line lands, the two bands beside a moved line, the merge body and the undo
    # request
    assert out["gaps"] == [0.0683, 0.1034, 0.4367, 0.7817, 0.815, 0.902]
    assert out["snap"] == {"near": 0.7817, "far": 0.795, "free": 0.785, "tight": 0.785}
    assert out["step"] == [0.815, 0.4367, 0.95]
    assert out["defaults"] == [0.0683, 0.815, 0.06, 0.8]  # the gap after the first line; the gap nearest 80 %
    assert out["adjMove"] == [
        ["body", 0, 0, 1, 0.8],
        ["footnote", 0, 0.8, 1, 0.972],
        ["page_number", 0.46, 0.972, 0.53, 0.99],
    ]
    assert out["adjAddHeader"][:2] == [["running_header", 0, 0, 1, 0.06], ["body", 0, 0.06, 1, 0.781]]
    assert out["adjAddFoot"][:2] == [["body", 0, 0, 1, 0.8], ["footnote", 0, 0.8, 1, 0.972]]
    assert out["adjOff"][1] == ["body", 0, 0.062, 1, 0.972] and len(out["adjOff"]) == 3
    assert out["noNumber"] == [["body", 0, 0, 1, 0.781], ["footnote", 0, 0.781, 1, 1]]
    assert out["merge"] == [
        {"merge": True, "set": {"footnote_line": 0.8}, "unset": ["header_cut"], "reset": False},
        {"merge": True, "set": {}, "unset": [], "reset": True},
    ]
    apply_fx = data["fixtures"]["book_guides_apply.json"]
    assert out["inverse"] == [{"replace": None}, {"undo": apply_fx["layout"]["response"]["undo"]}, None]
    assert out["ratio"] == [0.805, 0.062, None, 0.0625]  # Eastern digits and «٫» read
    # the draft is drawn on the page on screen only where it applies (no own cut, no detected footnote)
    assert out["draft"] == [{"header": 0.062}, {"footnote": 0.8}, {}]
    assert out["compact"] == [
        ["h", 0, 0.062],
        ["b", 0.062, 0.782],
        ["f", 0.782, 0.972],
        ["p", 0.972, 0.99, 0.46, 0.53],
    ]

    # --- the form (§3.13): the range probe on byte fixtures (found, missing, two /Count values) and the line
    assert out["probe"] == [555, None, 96]
    assert out["form"] == [
        "الحوليات الليبية.pdf",
        555,
        "الملف 555 صفحة · تُستخرج الصفحات 187–193 (7 صفحات).",
        "لا تبقى صفحات: الملف 555 صفحة والتجاوز 186 + 369.",
        True,
        "يُعرف عدد صفحات الملف بعد رفعه؛ تُستخرج الصفحات بعد أول 186 وقبل آخر 362.",
        "تُستخرج كل صفحات الملف.",
        "الملف 8 صفحات · تُستخرج كلها.",
        "الملف 52 صفحة · تُستخرج الصفحات 3–50، وفي كل منها صفحتان (96 صفحة في الكتاب).",
    ]

    # --- the layout book: the mode opens on the grid; one api:book_guides call, one sheets range (guides=1)
    assert out["openView"] == "grid" and out["primaryLayout"] == "startOcr" and out["collide"] == []
    assert out["stateCalls"] == ["/api/books/25/guides/"]
    assert out["sheetCalls"] == ["/api/books/25/sheets/?from=1&to=7&guides=1"]
    assert out["counts"] == {"all": 7, "doubt": 1, "override": 0}
    # bands on the grid's tiles and the filmstrip (physical percentages of the prepared image), the amber mark
    assert out["tileBands"] == [
        ["gd-b region-body", "0%", "0%", "100%", "78.1%"],
        ["gd-b region-footnote", "0%", "78.1%", "100%", "19.1%"],
        ["gd-b region-page_number", "46%", "97.2%", "7%", "1.8%"],
    ]
    assert out["tileAr"] == "1000 / 3000" and out["doubtMarks"] == [[False, False], [True, True]]
    assert out["thumbs"] == [["1", 3, False], ["2", 2, False]] + [[str(n), 3, False] for n in range(3, 7)] + [
        ["7", 3, True]
    ]
    assert out["filterDoubt"] == [[7], "doubt", False] and out["filterOverride"] == [True]
    # V by physical key; N and C are not the mode's; the view is never remembered
    assert out["keys"] == ["toggleView", None, None] and out["afterV"] == ["sheets", True]
    assert out["shown"] == {"id": "812", "number": 1} and out["pageId"] == "812"
    # patchGuidesBody: bands with their chips (the body's a label), the footnote line as a slider, the grip
    # that adds the running head, the page's regions in reading order for screen readers
    fig = out["fig1"]
    assert (
        fig["editable"] is True
        and fig["img"].endswith("0001/display.webp?v=1727337600")
        and fig["alt"] == "الصفحة 1 بعد التجهيز"
    )
    assert fig["bands"] == [
        ["body", "0%", "0%", "100%", "78.1%", "متن", "span", False],
        ["footnote", "0%", "78.1%", "100%", "19.1%", "حاشية", "button", False],
        ["page_number", "46%", "97.2%", "7%", "1.8%", "رقم الصفحة", "button", False],
    ]
    assert fig["header"] is None
    assert fig["footnote"] == {
        "top": "78.1%",
        "handle": "حاشية 78.1%",
        "now": "78.1",
        "text": "بداية الحاشية عند 78.1%",
        "tab": "0",
        "dashed": False,
    }
    assert fig["grips"] == [False, True] and fig["sr"] == "مناطق الصفحة: متن، حاشية، رقم الصفحة"
    assert fig["lock"] is True and fig["savebar"] is True
    head = out["head7"]
    assert head["doubts"] == "حاشية من حجم الخط" and head["own"] is True and head["auto"] is True
    assert (
        head["exclude"] == "استثناء"
        and head["next"] == "/books/25/#sheet-7"
        and head["action"] == "/books/25/pages/7/exclude/"
    )

    # --- fixing one page: the drag moves the two bands live, snaps to the gap, saves at once with its undo
    assert out["dragPaint"] == {
        "top": "78.17%",
        "handle": "حاشية 78.2%",
        "body": ["body", "0%", "0%", "100%", "78.17%"],
        "foot": ["footnote", "0%", "78.17%", "100%", "19.03%"],
    }
    assert out["dragSave"] == [
        [
            "/api/pages/812/guides/",
            {"merge": True, "set": {"footnote_line": 0.7817}, "unset": [], "reset": False, "stage": "layout"},
        ]
    ]
    assert out["dragToast"] == ["حُفظ لهذه الصفحة", True, True]
    assert out["afterSave"]["top"] == "80%"  # the answer's block is drawn
    # the toast's «تراجع» never runs by itself; pressed, it posts the answer's inverse
    assert out["undoIdle"] is True
    assert out["undoPage"] == [["/api/pages/812/guides/", {"replace": None, "stage": "layout"}]]
    assert out["undoToast"] == ["أُلغي التغيير.", False]
    # «+ حاشية» adds the line in the gap nearest 80 %; the merge body equals the contract's
    assert out["grip2"] == [False, "+ حاشية"]
    assert out["gripSave"] == [
        {"merge": True, "set": {"footnote_line": 0.815}, "unset": [], "reset": False, "stage": "layout"}
    ]
    assert out["mergeRequest"] == [["/api/pages/813/guides/", out["mergeFixture"]]]
    # ↓ moves a focused line by 0.2 %, ⇧↓ to the next gap; saved once the keys rest
    assert out["keyHandled"] == [True, "80.2%"] and out["keyGap"] == "81.5%" and out["keyNoSaveYet"] == 0
    assert out["keySave"] == [
        {"merge": True, "set": {"footnote_line": 0.815}, "unset": [], "reset": False, "stage": "layout"}
    ]
    # the page number's chip offers «ليس رقم صفحة» only
    assert out["menu"] == [False, ["ليس رقم صفحة"], "true"] and out["menuClosed"] is True
    assert out["noNumberRequest"] == [data["fixtures"]["page_guides.json"]["remove_page_number"]["request"]]

    # --- all pages: draft → preview (the contract's body) → apply → undo; editing on the page waits meanwhile
    assert out["draftSet"] == {"set": {"footnote_line": 0.805}} and out["paused"] is True
    assert out["previewRequest"] == [["/api/books/25/guides/preview/", out["previewFixture"]]]
    assert out["previewText"] == [
        "يغيّر هذا صفحة واحدة.",
        "في صفحة واحدة يقطع خطٌّ سطرًا أو تغطّي منطقةٌ سطرًا من المتن:",
        [2],
        "",
        "",
        "",
    ]
    assert out["escape"] == [True, None, False]  # Esc drops the draft and the controls return to the book's
    assert out["applyRequest"] == [["/api/books/25/guides/", out["applyFixture"]]]
    applied = out["applied"]
    assert (
        applied["toast"] == ["طُبّق على 7 صفحات", True]
        and applied["book"] == "manual"
        and applied["draft"] is None
    )
    assert (
        applied["ctl"]["header"] == {"on": True, "pct": "6.2"}
        and applied["tileHead"] == "gd-b region-running_header"
    )
    assert out["applyIdle"] is True and out["undoBook"] == [out["undoBookFixture"]]
    assert out["afterUndo"] == ["أُلغي التغيير.", "auto", False]
    assert out["conflict"] is True  # a 409 `started` shows «بدأت المعالجة في نافذة أخرى»
    assert out["doneToast"] == [True, "اكتمل التخطيط · 7 صفحات", ""]
    assert out["rerunDialog"] == [
        "إعادة تخطيط الكتاب؟",
        "يُعاد تخطيط 7 صفحات.",
        "يُحتفظ بما ضُبط يدويًا لكل صفحة من تدوير وقصّ.",
        "إعادة التخطيط",
    ]
    assert out["dialogEsc"] == "" and out["deleteWork"] == ""

    # --- the states of §3.12: primary, chip, bar, the side panel's line; a poll from «المعالجة»; #sheet-N
    assert out["states"] == {
        "uploaded": "extract",
        "processing": ["startOcrDisabled", "قيد التخطيط · 3 من 6 صفحة", True, "جُهّزت 3 من 6 صفحات"],
        "needs_guides": "startOcr",
        "error": "reextract",
        "reader": "",
    }
    assert out["startedPoll"] is True and out["hashView"] == "sheets"

    # --- a started book (`?view=guides`): locked pages without handles; a change waits for the explicit save
    assert out["startedPrimary"] == "back"
    assert out["locked"] == {
        "lock": False,
        "text": "معتمدة: لا تتغيّر",
        "editable": False,
        "chipsDisabled": True,
        "tab": "-1",
    }
    assert out["lockedReview"] == "فيها تصحيحات مراجعة: لا تتغيّر"
    assert out["pending"] == {"calls": 0, "savebar": False, "dashed": True, "top": "79%", "pending": True}
    assert out["ocrSave"] == [["/api/pages/814/guides/", out["ocrSaveFixture"]]]
    assert out["ocrToast"] == ["يُعاد التعرّف على نص الصفحة 3.", False]
    assert out["ocrPreview"] == [
        data["fixtures"]["book_guides_preview.json"]["ocr"]["request"],
        "يغيّر هذا 3 صفحات.",
        "سيُعاد التعرّف على نص 3 صفحات؛ نحو دقيقة واحدة.",
        "صفحتان معتمدتان أو فيهما تصحيحات مراجعة لا تتغيّران.",
        "صفحة واحدة بضبط خاص تبقى كما هي",
        "تطبيقه عليها أيضًا",
    ]
    assert out["ocrApply"] == ["طُبّق على 3 صفحات؛ يُعاد التعرّف على نصّها.", False]  # no undo in «المعالجة»
    # the two book re-runs (D101): «إعادة التخطيط» in the mode, «إعادة المعالجة» outside it
    assert out["relayout"] == [
        "إعادة تخطيط الكتاب؟",
        "يُعاد تخطيط 5 صفحات، وتبقى صفحة واحدة معتمدة كما هي.",
        "يُحتفظ بما ضُبط يدويًا لكل صفحة من تدوير وقصّ، ثم يُعاد التعرّف عليها بالنماذج: "
        "نحو دقيقتين على هذا الجهاز.",
        "إعادة التخطيط",
    ]
    assert out["reprocess"] == [
        "إعادة معالجة الكتاب؟",
        "تُعاد معالجة 5 صفحات، وتبقى صفحة واحدة معتمدة كما هي.",
        "يُعاد التعرّف عليها بالنماذج: نحو دقيقتين على هذا الجهاز.",
        "إعادة المعالجة",
    ]


# ---------------------------------------------------------- 7b: the half-disc of a page one model read (D73)


def test_tile_and_thumb_shells_carry_the_reader_mark(editor_client):
    """The grid tile marks a page one model read with a small half-disc before its status dot (the danger
    tint for Tesseract alone, hidden otherwise); the tile and thumb shells carry it hidden for books.js."""

    def tile(readers):
        t = {"id": 5, "number": 3, "status": "ocr_done", "status_label": "تم التعرّف", "url": "/p/3/"}
        t["readers"] = readers
        html = render_to_string("books/_page_tile.html", {"t": t, "book": None, "role": "proofreader"})
        return _between(html, '<span class="page-tile-marks">', '<span class="dot')

    one, tesseract, two = tile("one"), tile("tesseract"), tile("two")
    assert (
        '<span class="mark-reader" title="قراءة واحدة"><span class="sr-only">قراءة واحدة</span></span>' in one
    )
    assert (
        '<span class="mark-reader is-tesseract" title="نص Tesseract وحده">'
        '<span class="sr-only">نص Tesseract وحده</span></span>' in tesseract
    )
    assert '<span class="mark-reader" hidden>' in two and tile("") == two
    book, _ = _book(
        [(Page.Status.OCR_DONE, "final"), (Page.Status.OCR_DONE, "final")], Book.Status.READY_FOR_REVIEW
    )
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    shell = _between(body, '<template id="tile-shell">', "</template>")
    assert '<span class="page-tile-marks"><span class="mark-reader" hidden>' in shell
    thumb = _between(body, '<template id="thumb-shell">', "</template>")
    assert '<span class="mark-reader" hidden aria-hidden="true"></span>' in thumb
    css = (ROOT / "static" / "src" / "components" / "review.css").read_text(encoding="utf-8")
    assert ".rv-thumb .mark-reader, .bk-thumb .mark-reader { position: absolute; top: 3px;" in css
    assert ".mark-reader[hidden] { display: none; }" in css


READER_RUN = r"""
(async () => {
  const cfg = clone(FX['dashboard_config.json'].started);
  const page = (id, n, readers, extra = {}) => Object.assign({ id, number: n, status: 'ocr_done', status_label: 'تم التعرّف', text_state: 'final', n_unresolved: 2, is_excluded: false, error: false, width: 1000, height: 1500, thumb_url: `/t/${n}.webp`, readers }, extra);
  const pages = [page(901, 1, 'one'), page(902, 2, 'tesseract'), page(903, 3, 'two'), page(904, 4, '', { status: 'reviewed', is_reviewed: true, n_unresolved: 0 })];
  const root = new E('div'); const layout = new E('div'); layout.className = 'bk-layout';
  const stack = new E('div'); stack.setAttribute('data-sheet-stack', ''); const grid = new E('div'); grid.setAttribute('data-page-grid', '');
  const film = new E('div'); film.setAttribute('data-film-track', '');
  layout.appendChild(stack); layout.appendChild(grid); layout.appendChild(film); root.appendChild(layout);
  pages.forEach((p) => {
    const shell = build(DATA.templates['sheet-shell']).children[0]; shell.dataset.pageId = String(p.id); stack.appendChild(shell);
    const tile = build(DATA.templates['tile-shell']).children[0]; tile.dataset.pageId = String(p.id); grid.appendChild(tile);
  });
  const d = reg.bookDashboard(Object.assign(cfg, { progressUrl: '/api/books/25/progress/', sheetsUrl: '/api/books/25/sheets/', bookUrl: '/books/25/', canEdit: true, bookId: 25,
    active: false, status: 'ready_for_review', statusLabel: 'بانتظار المراجعة', stages: [], byStatus: { ocr_done: 3, reviewed: 1 }, total: 4, pages, urls: { page: '/books/25/pages/__n__/', review: '/books/25/review/__n__/', rerun: '/books/25/pages/__n__/rerun/', exclude: '/books/25/pages/__n__/exclude/' } }));
  d.$el = root; d.$watch = () => {}; d.$nextTick = (fn) => fn(); d.$refs = {};
  d.init(); await settle();
  const tiles = () => grid.children.map((t) => { d.patchTile(t, d.page(t.dataset.pageId)); const m = t.querySelector('.mark-reader'); return [m.hidden, m.classList.contains('is-tesseract'), m.getAttribute('title'), m.querySelector('.sr-only').textContent]; });
  const thumbs = () => film.children.map((t) => { const m = t.querySelector('.mark-reader'); return [Number(t.dataset.number), m.hidden, m.classList.contains('is-tesseract'), t.getAttribute('title')]; });
  out.tiles = tiles();
  out.thumbs = thumbs();
  // the compact poll names the readers only for one reader or Tesseract: page 1 read again by both models,
  // page 3 now read by one
  d.apply({ total: 4, percent: 100, flags: 0, status: 'ready_for_review', status_label: 'بانتظار المراجعة', dot: 'dot-accent', by_status: {}, active: false,
    pages: [{ id: 901, number: 1, status: 'ocr_done', n_unresolved: 1 }, { id: 903, number: 3, status: 'ocr_done', n_unresolved: 2, readers: 'one' }] });
  await settle();
  out.polledTiles = tiles();
  out.polledThumbs = thumbs();
  // D1's contract tiles (review/fixtures/trust/tile.json): the full tile of page 901, then the compact poll
  const TT = DATA.trust;
  const full = TT['page_tile (page 901, full)'];
  const root2 = new E('div'); const grid2 = new E('div'); grid2.setAttribute('data-page-grid', ''); const film2 = new E('div'); film2.setAttribute('data-film-track', '');
  root2.appendChild(grid2); root2.appendChild(film2);
  const two = Object.assign({}, full, { id: 900, number: 1, readers: 'two', flag_labels: ['نص قد يكون ناقصًا'], n_unresolved: 14 });
  [two, full].forEach((p) => { const tile = build(DATA.templates['tile-shell']).children[0]; tile.dataset.pageId = String(p.id); grid2.appendChild(tile); });
  const d2 = reg.bookDashboard(Object.assign(clone(FX['dashboard_config.json'].started), { progressUrl: '/p', sheetsUrl: '/s', bookUrl: '/books/30/', canEdit: true, bookId: 30, active: false, status: 'ready_for_review', statusLabel: '', stages: [], byStatus: {}, total: 2, pages: [two, full], urls: { page: '/books/30/pages/__n__/', review: '/books/30/review/__n__/', rerun: '/r/__n__/', exclude: '/x/__n__/' } }));
  d2.$el = root2; d2.$watch = () => {}; d2.$nextTick = (fn) => fn(); d2.$refs = {}; d2.init(); await settle();
  const marks2 = () => grid2.children.map((t) => { d2.patchTile(t, d2.page(t.dataset.pageId)); return t.querySelector('.mark-reader').hidden; });
  out.contract = { tiles: marks2(), thumbs: film2.children.map((t) => t.querySelector('.mark-reader').hidden) };
  d2.apply({ total: 2, percent: 100, flags: 2, status: 'ready_for_review', status_label: '', dot: 'dot-accent', by_status: {}, active: false,
    pages: [TT['page_tile (page 900, compact: no readers key for two)'], Object.assign({}, TT['page_tile (page 901, compact)'], { n_unresolved: 0 })] });
  await settle();
  out.contractPolled = { tiles: marks2(), thumbs: film2.children.map((t) => [t.querySelector('.mark-reader').hidden, t.getAttribute('title')]) };
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""  # noqa: E501


def test_reader_mark_on_tiles_and_thumbs_under_node(editor_client, tmp_path):
    """books.js patches the half-disc on the grid tiles and the filmstrip's thumbs (D73) from the full tile at
    first paint and from the compact poll, which names the readers only for one reader or Tesseract."""
    book, _ = _book([(Page.Status.OCR_DONE, "final")], Book.Status.READY_FOR_REVIEW)
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    data = {
        "templates": {name: _template(body, name) for name in ("sheet-shell", "tile-shell", "thumb-shell")},
        "fixtures": {
            path.name: json.loads(path.read_text(encoding="utf-8")) for path in FIXTURES.glob("*.json")
        },
        "trust": json.loads(
            (ROOT / "review" / "fixtures" / "trust" / "tile.json").read_text(encoding="utf-8")
        ),
    }
    (tmp_path / "data.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(
        GUIDES_HARNESS[: GUIDES_HARNESS.index("(async () => {")] + READER_RUN, encoding="utf-8"
    )
    files = [str(JS / name) for name in ("ui.js", "keys.js", "books.js")]
    run = subprocess.run(
        ["node", str(harness), str(tmp_path / "data.json"), *files],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    one = [False, False, "قراءة واحدة", "قراءة واحدة"]
    tess = [False, True, "نص Tesseract وحده", "نص Tesseract وحده"]
    none = [True, False, None, "قراءة واحدة"]  # hidden; its screen-reader words wait for the next patch
    # tiles: [hidden, danger tint, title, screen-reader words]; thumbs: [number, hidden, danger tint, title]
    assert out["tiles"] == [one, tess, none, none]
    assert out["thumbs"] == [
        [1, False, False, "صفحة 1 — تم التعرّف · قراءة واحدة"],
        [2, False, True, "صفحة 2 — تم التعرّف · نص Tesseract وحده"],
        [3, True, False, "صفحة 3 — تم التعرّف"],
        [4, True, False, "صفحة 4 — تم التعرّف"],
    ]
    # the poll: page 1 now read by both models (no `readers`), page 3 by one
    assert out["polledTiles"] == [none, tess, one, none]
    assert [row[1] for row in out["polledThumbs"]] == [True, False, False, True]
    assert out["polledThumbs"][2][3] == "صفحة 3 — تم التعرّف · قراءة واحدة"
    # on D1's contract tiles: page 901 (one reader) keeps its mark through the compact poll, page 900 has none
    assert out["contract"] == {"tiles": [True, False], "thumbs": [True, False]}
    assert out["contractPolled"] == {
        "tiles": [True, False],
        "thumbs": [[True, "صفحة 1 — تم التعرّف"], [False, "صفحة 2 — تم التعرّف · قراءة واحدة"]],
    }


# ---------------------------------------------------------------- 7c: tiles lead to review, the stage bar


@pytest.mark.django_db
def test_tiles_and_sheets_lead_to_review_or_to_the_page_sheet(editor_client, client):
    book, _ = _book([(Page.Status.OCR_DONE, "final"), (Page.Status.LAYOUT_DONE, "provisional")])
    body = editor_client.get(reverse("books:detail", args=[book.pk])).content.decode()
    review = [_optional("review:page", book.pk, n) or f"/books/{book.pk}/review/{n}/" for n in (1, 2)]
    detail = [f"/books/{book.pk}/guides/#sheet-{n}" for n in (1, 2)]  # D84: the page's sheet
    grid = body[body.index('<div class="page-grid"') : body.index('<p class="bk-empty-filter')]
    tiles = grid.split('<div class="page-tile')[1:]
    # the link's address: review once the text is final, else the page's sheet (a new tab or ⌘-click)
    assert f'<a class="page-tile-link" href="{review[0]}"' in tiles[0]
    assert f'<a class="page-tile-link" href="{detail[1]}"' in tiles[1]
    # on hover (always on touch): «مراجعة», hidden until the text is final; no «تفاصيل المعالجة» (D84)
    assert (
        f'class="btn btn-sm page-tile-review" href="{review[0]}" aria-label="مراجعة الصفحة 1">مراجعة</a>'
        in tiles[0]
    )
    assert f'class="btn btn-sm page-tile-review" href="{review[1]}" hidden' in tiles[1]
    assert "page-tile-detail" not in tiles[0] and "#i-sliders" not in tiles[0]
    # the sheet's title leads to review too; nothing else sits in its head
    sheet = _shell(body, 0)
    assert f'<a class="sheet-title num" href="{review[0]}">' in sheet and "sheet-detail" not in sheet
    # D33's plain click still opens the viewer; a modifier-click follows the link
    js = (ROOT / "static" / "src" / "js" / "books.js").read_text(encoding="utf-8")
    assert "e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;" in js
    css = (ROOT / "static" / "src" / "components" / "books.css").read_text(encoding="utf-8")
    assert (
        ".page-tile:hover .page-tile-actions, .page-tile:focus-within .page-tile-actions { opacity: 1; }"
        in css
    )
    assert "@media (hover: none) { .page-tile-actions { opacity: 1; } }" in css
    # a proofreader: review, nothing technical
    client.force_login(_user("reader7c", "proofreader"))
    reader = client.get(reverse("books:detail", args=[book.pk])).content.decode()
    assert "page-tile-review" in reader and "page-tile-detail" not in reader and "sheet-detail" not in reader


DASH_7C = r"""
const reg = {}; const inits = []; const stores = {}; const toasts = [];
globalThis.window = globalThis;
globalThis.document = { hidden: false, addEventListener: (e, fn) => { if (e === 'alpine:init') inits.push(fn); }, querySelector: () => null, getElementById: () => null, activeElement: null };
globalThis.Alpine = { data: (n, f) => { reg[n] = f; }, store: (n, v) => { if (v !== undefined) stores[n] = v; return stores[n]; } };
const local = { 'nassakh.bookView': 'grid', 'nassakh.bookView7': 'sheets' };
globalThis.localStorage = { getItem: (k) => (k in local ? local[k] : null), setItem: (k, v) => { local[k] = String(v); } };
globalThis.sessionStorage = { getItem: () => null, setItem: () => {} };
const timers = []; globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; }; globalThis.clearTimeout = () => {};
let now = 50000; Date.now = () => now;
const listeners = {}; globalThis.addEventListener = (e, fn) => { listeners[e] = fn; }; globalThis.removeEventListener = () => {};
const channels = []; globalThis.BroadcastChannel = class { constructor(n) { this.name = n; channels.push(this); } postMessage() {} close() {} };
const fs = require('fs');
for (const f of ['keys.js', 'stages.js', 'books.js']) eval(fs.readFileSync(`${process.argv[2]}/${f}`, 'utf8'));
inits.forEach((fn) => fn());
const changed = []; window.NassakhStages.changed = (book) => changed.push(book);
const out = {};
const cfg = (extra) => Object.assign({ bookId: 7, bookUrl: '/books/7/', progressUrl: '/api/books/7/progress/', status: 'ocr', active: true, total: 8, percent: 38, byStatus: { ocr_done: 3, layout_done: 5 }, stages: [{ key: 'ocr_done', statuses: ['ocr_done', 'reviewed', 'assembled'] }], editorUrls: { layout: '/books/7/layout/' } }, extra || {});
const d = reg.bookDashboard(cfg());
// the view per book: this book's own, a book never opened keeps the last one chosen anywhere
out.view = [d.view, reg.bookDashboard(cfg({ bookId: 8 })).view];
d.setView('grid');
out.viewSaved = [local['nassakh.bookView7'], local['nassakh.bookView']];
// the tile's address (a compact poll entry carries no primary_url)
const p = (raw) => d.completePage(raw, null);
out.primary = [p({ id: 1, number: 1, status: 'ocr_done', text_state: 'final' }).primary_url, p({ id: 2, number: 2, status: 'layout_done', text_state: 'provisional' }).primary_url, p({ id: 3, number: 3, status: 'ocr_done', text_state: 'final', is_excluded: true }).primary_url, p({ id: 4, number: 4, status: 'ocr_done', text_state: 'final', primary_url: '/x/' }).primary_url];
// «?» opens the sheet (the Arabic layout's «؟» too), Esc closes it; the keys behind it wait
out.keys = [d.keyAction({ key: '?', code: 'Slash', shiftKey: true }, false), d.keyAction({ key: '؟', code: 'Slash', shiftKey: true }, false), d.keyAction({ key: '?', code: 'Slash', shiftKey: true }, true)];
d.$nextTick = (fn) => fn(); d.$refs = { sheetClose: { focus: () => { out.focused = 'close'; } } };
d.onKey({ key: '?', code: 'Slash', shiftKey: true, target: { tagName: 'DIV' }, preventDefault() {} });
const opened = d.sheetOpen;
d.onKey({ key: 'Escape', target: { tagName: 'DIV' }, preventDefault() {} });
out.sheet = [opened, d.sheetOpen];
// D76: the status folded into the stage bar's current step
out.patch = d.stagePatch;
d.syncStage(); out.store = stores.stages.live.ocr;
const idle = reg.bookDashboard(cfg({ active: false, status: 'reviewing' }));
out.idle = idle.stagePatch;
const failed = reg.bookDashboard(cfg({ active: false, status: 'error', errorHeadline: 'تعذّرت معالجة صفحة' }));
out.failed = failed.stagePatch;
const preparing = reg.bookDashboard(cfg({ guidesMode: true, layoutStage: true, status: 'processing', total: 7, percent: 43, byStatus: { preprocessed: 3, uploaded: 4 } }));
preparing.syncStage(); out.preparing = [preparing.stagePatch, stores.stages.live.pages];
// the «الكتاب» block links «عرض التغييرات» to the book page's changes tab
out.changes = d.changesUrl;
// the end of a run: the bar asks the server; the channel and the focus refresh the progress once (every 2 s)
d.stopEffects = () => {}; d.refreshFilmSoon = () => {};
d.onProcessingEnd(); out.changed = changed.slice();
const live = reg.bookDashboard(cfg({ active: false, status: 'reviewing' }));
let polls = 0; live.pollNow = () => { polls += 1; };
live.bindLive();
const ch = channels[channels.length - 1];
ch.onmessage({ data: { type: 'review', book: 9, page: 1 } });
ch.onmessage({ data: { type: 'review', book: 7, page: 1 } });
listeners.focus();
out.live = { polls, trailing: timers.length > 0 };
now += 2500; listeners.focus();
out.live.after = polls;
d.doneToast.count = 8; preparing.doneToast.count = 7;
out.doneText = [d.doneText, preparing.doneText];
console.log(JSON.stringify(out));
"""  # noqa: E501


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_dashboard_view_per_book_the_sheet_key_and_the_stage_bar_fold_under_node(tmp_path):
    harness = tmp_path / "dash7c.js"
    harness.write_text(DASH_7C, encoding="utf-8")
    run = subprocess.run(
        ["node", str(harness), str(ROOT / "static" / "src" / "js")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, run.stderr[-4000:]
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["view"] == ["sheets", "grid"] and out["viewSaved"] == ["grid", "grid"]
    assert out["primary"] == ["/books/7/review/1/", "/books/7/pages/2/", "/books/7/pages/3/", "/x/"]
    assert (
        out["keys"] == ["sheet", "sheet", None]
        and out["sheet"] == [True, False]
        and out["focused"] == "close"
    )
    assert out["patch"] == {
        "state": "active",
        "detail": "قيد المعالجة: 3 من 8",
        "count": "3/8",
        "percent": 38,
    }
    assert out["store"] == out["patch"] and out["idle"] is None
    assert out["failed"] == {"state": "attention", "detail": "تعذّرت معالجة صفحة"}
    preparing = {"state": "active", "detail": "قيد التخطيط: 3 من 7", "count": "3/7", "percent": 43}
    assert out["preparing"] == [preparing, preparing]
    assert out["changes"] == "/books/7/layout/?tab=changes"
    assert out["changed"] == [7]
    assert out["live"] == {"polls": 1, "trailing": True, "after": 2}
    assert out["doneText"] == [
        "اكتملت المعالجة · 8 صفحات",
        "اكتمل التخطيط · 7 صفحات",
    ]  # D77: the count helper
