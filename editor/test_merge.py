"""Tests of `editor.merge`, the page-by-page merge of review changes into an edited book (PHASE7_SPEC §5.6,
§8.4): tokens, diff3, every row of the decision table, splits, typed blocks, headings made on the book page,
a review role that makes a heading, an owner's footnote, stable ids, untouched blocks, idempotence, the base
splice and the two UX-test incidents (fixtures built once from the dev database). Pure: no database."""

from __future__ import annotations

import copy
import json
import pathlib

import pytest

from editor import document as doc
from editor import merge

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "merge"


def text(value, *marks):
    node = {"type": "text", "text": value}
    if marks:
        node["marks"] = [{"type": mark} for mark in marks]
    return node


def para(block_id, *content, lines=(), pages=(1,), **attrs):
    return {
        "type": "paragraph",
        "attrs": {
            "id": block_id,
            "sourcePages": list(pages),
            "sourceLineIds": list(lines),
            "reviewed": True,
            "suggestedRole": None,
            **attrs,
        },
        "content": [text(c) if isinstance(c, str) else c for c in content],
    }


def heading(block_id, value, lines=(), pages=(1,), level=1):
    return {
        "type": "heading",
        "attrs": {"level": level, "id": block_id, "sourcePages": list(pages), "sourceLineIds": list(lines)},
        "content": [text(value)],
    }


def note(note_id, value, lines=(), page=1, number=1):
    return {
        "type": "footnote",
        "attrs": {
            "id": note_id,
            "number": number,
            "marker": str(number),
            "sourcePage": page,
            "sourceLineIds": list(lines),
            "orphan": False,
        },
        "content": [text(value)] if isinstance(value, str) else value,
    }


def document(*blocks):
    return {
        "type": "doc",
        "attrs": {"bookId": 1},
        "content": [{"type": "title", "attrs": {"text": "ك"}}, *blocks],
    }


def kinds(items):
    return [(item["kind"], item["page"]) for item in items]


def texts(document_):
    return [doc.plain_text(node) for node in merge.body(document_)]


A = "كان الشيخ فقيها فاضلا زاهدا في الدنيا."
A2 = "كان الشيخ فقيهاً فاضلاً زاهداً في الدنيا."
B = "ورحل إلى المشرق فأقام به مدة ثم عاد إلى بلده."


# ====================================================================== tokens


def test_tokens_ignore_text_node_splits_null_attributes_and_edge_spaces():
    one = para("p1", "كان الشيخ  فقيها ", lines=[1])
    split = para("p1-2", " كان ", "الشيخ", " فقيها", lines=[9], reviewed=False, style=None, noteFor="1")
    split["attrs"]["breakBefore"] = None
    assert merge.keys(merge.tokens([one])) == merge.keys(merge.tokens([split]))
    assert merge.keys(merge.tokens([one])) == [
        ("¶", "paragraph", None, None),
        ("w", "كان", ()),
        (" ",),
        ("w", "الشيخ", ()),
        (" ",),
        ("w", "فقيها", ()),
    ]


def test_tokens_carry_marks_notes_page_marks_hard_breaks_and_structure():
    block = para(
        "p1",
        text("قال", "bold"),
        " أبو",
        text("حيان", "uncertain"),
        note("n1", "حاشية هنا", lines=[5]),
        {"type": "pageBreak", "attrs": {"page": 2, "printed": "2"}},
        {"type": "hardBreak"},
        "آخر",
        lines=[1],
        style="verse",
    )
    got = merge.keys(merge.tokens([block]))
    assert got[0] == ("¶", "paragraph", None, "verse")
    assert ("w", "قال", ("bold",)) in got and ("w", "حيان", ("uncertain",)) in got
    assert ("w", "أبو", ()) in got  # a mark change splits a word run
    assert ("fn", (("w", "حاشية", ()), (" ",), ("w", "هنا", ()))) in got
    assert ("pb", 2) in got and ("br",) in got
    head = merge.keys(merge.tokens([heading("h1", "عنوان", level=2)]))[0]
    assert head == ("¶", "heading", 2, None)
    # a note's volatile attributes (number, marker, orphan, sourcePage, ids) never enter its key
    other = copy.deepcopy(block)
    other["content"][3]["attrs"].update(number=7, marker="٧", orphan=True, sourcePage=9, id="n9")
    assert merge.keys(merge.tokens([other])) == got


def test_blockquotes_are_one_token_and_rebuild_whole():
    quote = {"type": "blockquote", "content": [para("p2", "قول", lines=[2])]}
    assert [t.key[0] for t in merge.tokens([quote])] == ["bq"]


# ====================================================================== diff3


def toks(*words):
    return merge.inline_tokens([text(" ".join(words))])


def plain(items):
    return " ".join(t.text for t in items if t.key[0] == "w")


def test_diff3_takes_each_sides_change_and_the_same_change_once():
    base = toks("أ", "ب", "ج", "د", "هـ")
    mine = toks("أ", "بب", "ج", "د", "هـ")
    theirs = toks("أ", "ب", "ج", "دد", "هـ")
    merged, conflicts = merge.diff3(base, mine, theirs)
    assert plain(merged) == "أ بب ج دد هـ" and not conflicts
    merged, conflicts = merge.diff3(base, mine, mine)
    assert plain(merged) == "أ بب ج د هـ" and not conflicts


def test_diff3_conflicts_keep_mine_and_are_reported():
    base, mine, theirs = toks("سنة", "1966"), toks("سنة", "1967"), toks("سنة", "1965")
    merged, conflicts = merge.diff3(base, mine, theirs)
    assert plain(merged) == "سنة 1967" and len(conflicts) == 1
    assert plain(conflicts[0].mine) == "1967" and plain(conflicts[0].theirs) == "1965"


def test_diff3_merges_two_changes_of_one_note_on_the_notes_own_words():
    base = [para("p1", "نص", note("n1", "حاشية قديمة جدا", lines=[5]), lines=[1])]
    mine = [para("p1", "نص", note("n1", "حاشية قديمة جدًّا", lines=[5]), lines=[1])]
    theirs = [para("p1", "نص", note("n1", "حاشيةٌ قديمة جدا", lines=[5]), lines=[1])]
    merged, conflicts = merge.diff3(merge.tokens(base), merge.tokens(mine), merge.tokens(theirs))
    assert not conflicts
    fn = [t for t in merged if t.key[0] == "fn"][0]
    assert plain(fn.inner) == "حاشيةٌ قديمة جدًّا"
    assert doc.block_text(fn.node) == "حاشيةٌ قديمة جدًّا" and doc.node_id(fn.node) == "n1"


# ====================================================================== the decision table


def one(mine, base, fresh, pages=(1,), **kw):
    items = merge.plan(
        document(*mine), document(*base) if base is not None else None, document(*fresh), pages, **kw
    )
    return items


def test_equal_to_fresh_is_no_item():
    assert one([para("p1", A, lines=[1])], [para("p1", "قديم", lines=[1])], [para("p1", A, lines=[1])]) == []


def test_mine_equal_to_base_is_a_take():
    [item] = one([para("p1", A, lines=[1])], [para("p1", A, lines=[1])], [para("p1", A2, lines=[1])])
    assert (item["kind"], item["default"], item["chip"], item["ask"]) == (
        "take",
        "theirs",
        "من المراجعة",
        False,
    )
    assert item["diff"] == [
        ["eq", "كان الشيخ"],
        ["del", "فقيها فاضلا زاهدا"],
        ["ins", "فقيهاً فاضلاً زاهداً"],
        ["eq", "في الدنيا."],
    ]
    assert item["block"] == "p1" and item["blocks"] == ["p1"] and item["base"] == "stored"


def test_fresh_equal_to_base_keeps_the_owners_edit():
    assert one([para("p1", A2, lines=[1])], [para("p1", A, lines=[1])], [para("p1", A, lines=[1])]) == []


def test_both_changed_cleanly_is_merged():
    mine = [para("p1", B.replace("بلده", "بلاده"), lines=[1])]
    [item] = one(mine, [para("p1", B, lines=[1])], [para("p1", B.replace("المشرق", "الشرق"), lines=[1])])
    assert (item["kind"], item["default"], item["chip"]) == ("merged", "merged", "مع تعديلك")
    assert item["diff"] == [
        ["eq", "ورحل إلى"],
        ["del", "المشرق"],
        ["ins", "الشرق"],
        ["eq", "فأقام به مدة ثم عاد …"],
    ]
    [node] = item["work"]["merged"]
    assert (
        doc.block_text(node) == "ورحل إلى الشرق فأقام به مدة ثم عاد إلى بلاده." and doc.node_id(node) == "p1"
    )


def test_both_made_the_same_change_with_the_owners_other_edits_is_no_item():
    # D79: a fix everywhere, then the book page's «استبدال الكل»: mine holds review's change already (and the
    # owner's own edit elsewhere in the block), so the merged result is mine and the page settles
    base = [para("p1", B, lines=[1])]
    mine = [para("p1", B.replace("المشرق", "الشرق").replace("بلده", "بلاده"), lines=[1])]
    assert one(mine, base, [para("p1", B.replace("المشرق", "الشرق"), lines=[1])]) == []


def test_both_changed_the_same_words_is_a_conflict_on_mine():
    [item] = one(
        [para("p1", "وله كتب في الفقه واللغة سنة 1967.", lines=[1])],
        [para("p1", "وله كتب في الفقه واللغة سنة 1966.", lines=[1])],
        [para("p1", "وله كتب في الفقه واللغة سنة 1965.", lines=[1])],
    )
    assert (item["kind"], item["default"], item["ask"]) == ("conflict", "mine", True)
    assert item["conflicts"] == [{"mine": "… الفقه واللغة سنة 1967.", "theirs": "… الفقه واللغة سنة 1965."}]
    assert item["help"] == merge.HELP_CONFLICT and item["diff"][-2:] == [["del", "1967."], ["ins", "1965."]]


def test_no_base_and_a_difference_is_a_choice_starting_on_mine():
    [item] = one([para("p1", A, lines=[1])], None, [para("p1", A2, lines=[1])])
    assert (item["kind"], item["default"], item["chip"], item["ask"]) == ("choose", "mine", "للمقارنة", True)
    assert item["help"] == merge.HELP_CHOOSE and item["base"] == "none"


def test_a_new_block_with_a_known_base_is_an_insert_after_its_anchor():
    base = [para("p1", A, lines=[1])]
    fresh = [para("p1", A, lines=[1]), para("p2", "فقرة جديدة.", lines=[2], pages=(2,))]
    [item] = one([para("p1", A, lines=[1])], base, fresh, pages=(2,))
    assert (item["kind"], item["default"], item["chip"]) == ("insert", "theirs", "فقرة جديدة")
    assert item["work"]["anchor"] == ["after", 1] and item["block"] == "p1" and item["blocks"] == []


def test_a_new_block_without_a_base_is_an_insert_only_on_an_added_page():
    fresh = [para("p1", A, lines=[1]), para("p2", "فقرة جديدة.", lines=[2], pages=(2,))]
    [item] = one([para("p1", A, lines=[1])], None, fresh, pages=(2,), added=(2,))
    assert item["kind"] == "insert" and item["base"] == "none"
    # §2.6: a legacy merge kept only the first block's lines, so a lone fresh block may duplicate text
    [item] = one([para("p1", A, lines=[1])], None, fresh, pages=(2,))
    assert (item["kind"], item["default"]) == ("choose", "mine")


def test_a_block_the_owner_deleted_stays_deleted_unless_review_changed_it():
    base = [para("p1", A, lines=[1]), para("p2", "فقرة.", lines=[2])]
    assert one([para("p1", A, lines=[1])], base, base) == []
    [item] = one([para("p1", A, lines=[1])], base, [base[0], para("p2", "فقرةٌ.", lines=[2])])
    assert (item["kind"], item["default"], item["help"]) == ("conflict", "mine", merge.HELP_DELETED)
    assert item["conflicts"] == [{"mine": "", "theirs": "فقرةٌ."}] and item["diff"] == [["ins", "فقرةٌ."]]


def test_a_block_gone_from_the_fresh_text_is_removed_or_a_conflict():
    base = [para("p1", A, lines=[1]), para("p2", "فقرة.", lines=[2], pages=(2,))]
    [item] = one(base, base, base[:1], pages=(2,))
    assert (item["kind"], item["default"], item["chip"]) == ("remove", "theirs", "تُحذف")
    assert item["work"]["theirs"] == [] and item["diff"] == [["del", "فقرة."]]
    edited = [base[0], para("p2", "فقرة عدّلها المحرر.", lines=[2], pages=(2,))]
    [item] = one(edited, base, base[:1], pages=(2,))
    assert (item["kind"], item["default"]) == ("conflict", "mine")


def test_pages_the_base_does_not_know_are_merged_as_without_a_base():
    base = document(para("p1", A, lines=[1]))
    base["attrs"][merge.UNKNOWN_PAGES] = [1]
    [item] = merge.plan(document(para("p1", A, lines=[1])), base, document(para("p1", A2, lines=[1])), [1])
    assert item["kind"] == "choose" and item["base"] == "none"


def test_only_the_pages_asked_for_are_compared():
    base = [para("p1", A, lines=[1]), para("p2", B, lines=[2], pages=(2,))]
    fresh = [para("p1", A2, lines=[1]), para("p2", B + " تمّ", lines=[2], pages=(2,))]
    assert kinds(one(base, base, fresh, pages=(2,))) == [("take", 2)]


# ====================================================================== structure


def test_a_split_paragraph_meets_the_one_fresh_paragraph_and_takes_the_change_in_its_half():
    # convert.js copies the source lines to both halves of a split
    base = [para("p1", "أول الفقرة ثم وسطها ثم آخرها.", lines=[1, 2])]
    mine = [para("p1", "أول الفقرة ثم", lines=[1, 2]), para("p1-2", "وسطها ثم آخرها.", lines=[1, 2])]
    fresh = [para("p1", "أول الفقرة ثم وسطها ثم آخرُها.", lines=[1, 2])]
    [item] = one(mine, base, fresh)
    assert item["kind"] == "merged" and item["blocks"] == ["p1", "p1-2"]
    new, stats = merge.apply(document(*mine), [item])
    assert texts(new) == ["أول الفقرة ثم", "وسطها ثم آخرُها."]
    assert [doc.node_id(n) for n in merge.body(new)] == ["p1", "p1-2"] and stats["merged"] == 1


def test_a_block_the_owner_typed_stays_where_it_is():
    typed = para("e1", "عنوان كتبه المحرر", lines=[], pages=())
    base = [para("p1", A, lines=[1])]
    mine = [typed, para("p1", A, lines=[1])]
    items = one(mine, base, [para("p1", A2, lines=[1])])
    new, _stats = merge.apply(document(*mine), items)
    assert merge.body(new)[0] is typed and texts(new) == ["عنوان كتبه المحرر", A2]


def test_a_heading_made_on_the_book_page_stays_while_a_word_of_the_page_is_taken():
    base = [para("p1", "الفصل الأول", lines=[1]), para("p2", A, lines=[2])]
    mine = [heading("p1", "الفصل الأول", lines=[1]), para("p2", A, lines=[2])]
    fresh = [para("p1", "الفصل الأول", lines=[1]), para("p2", A2, lines=[2])]
    items = one(mine, base, fresh)
    assert kinds(items) == [("take", 1)]
    edited = document(*mine)
    new, _stats = merge.apply(edited, items)
    assert merge.body(new)[0] is merge.body(edited)[0]  # the heading is not even rebuilt
    assert [n["type"] for n in merge.body(new)] == ["heading", "paragraph"] and texts(new)[1] == A2


def test_a_review_role_that_makes_a_heading_splits_the_chapter():
    base = [heading("h1", "الفصل الأول", lines=[1]), para("p2", "عنوان جديد نص الفقرة.", lines=[2, 3])]
    fresh = [
        heading("h1", "الفصل الأول", lines=[1]),
        heading("h2", "عنوان جديد", lines=[2]),
        para("p3", "نص الفقرة.", lines=[3]),
    ]
    [item] = one(base, base, fresh)
    assert item["kind"] == "take"
    new, _stats = merge.apply(document(*base), [item])
    assert [c.title for c in doc.chapters_of(new)] == ["الفصل الأول", "عنوان جديد"]


def test_a_footnote_the_owner_inserted_is_kept_while_a_word_is_taken():
    base = [para("p1", A, lines=[1])]
    mine = [para("p1", "كان الشيخ فقيها", note("ne1", "حاشية المحرر"), " فاضلا زاهدا في الدنيا.", lines=[1])]
    fresh = [para("p1", A.replace("الدنيا", "الدنيا والآخرة"), lines=[1])]
    [item] = one(mine, base, fresh)
    assert item["kind"] == "merged"
    [node] = item["work"]["merged"]
    assert doc.block_text(node) == "كان الشيخ فقيها￼ فاضلا زاهدا في الدنيا والآخرة."
    assert [n["type"] for n in node["content"]] == ["text", "footnote", "text"]
    assert doc.node_id(node["content"][1]) == "ne1"


def test_a_one_to_one_take_keeps_mines_id_and_page_flags():
    mine = [para("p1-2", A, lines=[1], breakBefore=True, keepWithNext=True)]
    [item] = one(mine, [para("p1", A, lines=[1])], [para("p1", A2, lines=[1])])
    [node] = item["work"]["theirs"]
    assert doc.node_id(node) == "p1-2" and node["attrs"]["breakBefore"] is True
    assert node["attrs"]["keepWithNext"] is True and node["attrs"]["sourceLineIds"] == [1]


# ====================================================================== apply


def test_untouched_blocks_are_the_same_objects_and_all_mine_changes_nothing():
    mine = document(para("p1", A, lines=[1]), para("p2", B, lines=[2], pages=(2,)))
    fresh = document(para("p1", A2, lines=[1]), para("p2", B, lines=[2], pages=(2,)))
    items = merge.plan(mine, copy.deepcopy(mine), fresh, [1, 2])
    new, stats = merge.apply(mine, items)
    assert new["content"][2] is mine["content"][2] and new["content"][0] is mine["content"][0]
    assert stats == {"taken": 1, "merged": 0, "kept": 0, "changed": ["p1"], "written": True}
    same, stats = merge.apply(mine, items, {item["id"]: "mine" for item in items})
    assert same is mine and stats["written"] is False and stats["kept"] == 1
    before = json.dumps(mine, sort_keys=True)
    merge.apply(mine, items)
    assert json.dumps(mine, sort_keys=True) == before  # the input is never changed


def test_apply_refuses_a_choice_the_item_does_not_offer():
    items = one([para("p1", A, lines=[1])], [para("p1", A, lines=[1])], [para("p1", A2, lines=[1])])
    with pytest.raises(ValueError):
        merge.apply(document(para("p1", A, lines=[1])), items, {items[0]["id"]: "merged"})


def test_inserts_go_after_the_anchor_before_the_successor_or_at_the_chapter_end():
    mine = document(heading("h1", "ف", lines=[1]), para("p3", "ثالثة.", lines=[3], pages=(3,)))
    fresh = document(
        heading("h1", "ف", lines=[1]),
        para("p2", "ثانية.", lines=[2], pages=(2,)),
        para("p3", "ثالثة.", lines=[3], pages=(3,)),
        para("p4", "رابعة.", lines=[4], pages=(4,)),
    )
    items = merge.plan(mine, copy.deepcopy(mine), fresh, [2, 4])
    assert [(i["kind"], i["work"]["anchor"]) for i in items] == [
        ("insert", ["after", 1]),
        ("insert", ["after", 2]),
    ]
    new, _stats = merge.apply(mine, items)
    assert texts(new) == ["ف", "ثانية.", "ثالثة.", "رابعة."]
    lone = document(para("e1", "مكتوب", pages=()))
    items = merge.plan(lone, document(), document(para("p9", "وحيدة.", lines=[9], pages=(9,))), [9])
    assert items[0]["work"]["anchor"][0] == "end"
    assert texts(merge.apply(lone, items)[0]) == ["مكتوب", "وحيدة."]


def test_approval_only_pages_flip_reviewed_only_when_the_text_is_written():
    mine = document(para("p1", A, lines=[1]), para("p2", B, lines=[2], pages=(2,), reviewed=False))
    fresh = document(para("p1", A2, lines=[1]), para("p2", B, lines=[2], pages=(2,)))
    items = merge.plan(mine, copy.deepcopy(mine), fresh, [1])
    new, _stats = merge.apply(mine, items, approvals=[2], reviewed=[1, 2])
    assert new["content"][2]["attrs"]["reviewed"] is True and mine["content"][2]["attrs"]["reviewed"] is False
    same, _stats = merge.apply(mine, [], approvals=[2], reviewed=[1, 2])
    assert same is mine


def test_new_ids_never_clash_with_the_rest_of_the_document():
    mine = document(para("p2", "أخرى", lines=[7], pages=(3,)), para("p1", A, lines=[1]))
    fresh = document(
        para("p2", "أخرى", lines=[7], pages=(3,)), para("p1", A, lines=[1]), para("p2", "جديدة", lines=[2])
    )
    items = merge.plan(mine, copy.deepcopy(mine), fresh, [1])
    new, _stats = merge.apply(mine, items)
    ids = [doc.node_id(node) for node in merge.body(new)]
    assert len(ids) == len(set(ids)) == 3


def test_after_an_apply_and_the_base_splice_a_new_plan_has_no_items():
    base = document(
        heading("h1", "ف", lines=[1]),
        para("p2", A, lines=[2]),
        para("p3", B, lines=[3], pages=(2,)),
        para("p4", "وله كتب سنة 1966.", lines=[4], pages=(2,)),
    )
    mine = copy.deepcopy(base)
    mine["content"][3]["content"][0]["text"] = B.replace("بلده", "بلاده")
    mine["content"][4]["content"][0]["text"] = "وله كتب سنة 1967."
    fresh = copy.deepcopy(base)
    fresh["content"][2]["content"][0]["text"] = A2
    fresh["content"][3]["content"][0]["text"] = B.replace("المشرق", "الشرق")
    fresh["content"][4]["content"][0]["text"] = "وله كتب سنة 1965."
    items = merge.plan(mine, base, fresh, [1, 2])
    assert [i["kind"] for i in items] == ["take", "merged", "conflict"]
    for choice in ("mine", "theirs"):
        new, _stats = merge.apply(mine, items, {items[2]["id"]: choice})
        spliced = merge.splice_base(base, fresh, [1, 2])
        assert merge.plan(new, spliced, fresh, [1, 2]) == []


def test_item_ids_follow_the_item_and_not_its_place_in_the_plan():
    base = document(
        heading("h1", "ف", lines=[1]),
        para("p2", A, lines=[2]),
        para("p3", B, lines=[3], pages=(2,)),
        para("p4", "وله كتب سنة 1966.", lines=[4], pages=(2,)),
    )
    mine = copy.deepcopy(base)
    mine["content"][3]["content"][0]["text"] = B.replace("بلده", "بلاده")
    mine["content"][4]["content"][0]["text"] = "وله كتب سنة 1967."
    fresh = copy.deepcopy(base)
    fresh["content"][2]["content"][0]["text"] = A2
    fresh["content"][3]["content"][0]["text"] = B.replace("المشرق", "الشرق")
    fresh["content"][4]["content"][0]["text"] = "وله كتب سنة 1965."
    first = merge.plan(mine, base, fresh, [1, 2])
    assert [i["kind"] for i in first] == ["take", "merged", "conflict"]
    assert len({i["id"] for i in first}) == 3 and all(len(i["id"]) == 13 for i in first)
    assert [i["id"] for i in merge.plan(mine, base, fresh, [1, 2])] == [i["id"] for i in first]
    # the owner types review's words into the first paragraph: its item goes, the others keep their ids
    edited = copy.deepcopy(mine)
    edited["content"][2]["content"][0]["text"] = A2
    again = merge.plan(edited, base, fresh, [1, 2])
    assert [(i["kind"], i["id"]) for i in again] == [(i["kind"], i["id"]) for i in first[1:]]
    new, _stats = merge.apply(edited, again, {first[2]["id"]: "theirs"})  # the choice kept by id
    assert texts(new)[2:] == [B.replace("المشرق", "الشرق").replace("بلده", "بلاده"), "وله كتب سنة 1965."]
    # a paragraph whose text changed again is a new item: its choice goes back to the default
    edited["content"][4]["content"][0]["text"] = "وله كتب سنة 1968."
    third = merge.plan(edited, base, fresh, [1, 2])
    assert third[0]["id"] == first[1]["id"] and third[1]["id"] not in {i["id"] for i in first}


# ====================================================================== the base


def test_splice_base_replaces_the_pages_taken_in_page_order():
    base = document(
        para("p1", "أولى", lines=[1]),
        para("p2", "ثانية", lines=[2], pages=(2,)),
        para("p3", "ثالثة تمتد", lines=[3, 4], pages=(3, 4)),
        para("p5", "خامسة", lines=[5], pages=(5,)),
    )
    fresh = document(
        para("p1", "أولى جديدة", lines=[1]),
        para("p2", "ثانية جديدة", lines=[2], pages=(2,)),
        para("p3", "ثالثة تمتد جديدة", lines=[3, 4], pages=(3, 4)),
        para("p5", "خامسة جديدة", lines=[5], pages=(5,)),
    )
    spliced = merge.splice_base(base, fresh, [2, 4])
    assert texts(spliced) == ["أولى", "ثانية جديدة", "ثالثة تمتد جديدة", "خامسة"]
    assert spliced["content"][1] is base["content"][1] and texts(base)[1] == "ثانية"
    legacy = merge.splice_base(None, fresh, [2], unknown=[2, 5])
    assert texts(legacy) == texts(fresh) and legacy["attrs"][merge.UNKNOWN_PAGES] == [5]
    assert merge.splice_base(legacy, fresh, [5])["attrs"][merge.UNKNOWN_PAGES] == []


# ====================================================================== the two incidents (§8.4 gate 2)


@pytest.mark.parametrize("name", ["incident_23_8.json", "incident_26_3.json"])
def test_each_incident_gives_exactly_one_take_and_keeps_every_book_page_edit(name):
    fixture = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    lines = {int(k): tuple(v) for k, v in fixture["lines"].items()}
    items = merge.plan(fixture["mine"], fixture["base"], fixture["fresh"], [fixture["page"]], lines=lines)
    assert [(i["kind"], i["page"]) for i in items] == [("take", fixture["page"])]
    assert [{"kind": i["kind"], "page": i["page"], "diff": i["diff"]} for i in items] == fixture["expect"][
        "items"
    ]
    new, stats = merge.apply(fixture["mine"], items)
    changed = [
        i
        for i, (a, b) in enumerate(zip(doc.content_of(fixture["mine"]), doc.content_of(new), strict=True))
        if a is not b
    ]
    assert len(changed) == 1 and stats["taken"] == 1
    spliced = merge.splice_base(fixture["base"], fixture["fresh"], [fixture["page"]], lines=lines)
    assert merge.plan(new, spliced, fixture["fresh"], [fixture["page"]], lines=lines) == []
