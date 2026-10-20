"""T36 Track A -- _count_contradicts_own_list false positive on a repeated list.

Root cause, confirmed against the live T6 answer (session d553634c-8d7e-4a16-ab8e-
4591c4c250cc, 2026-10-19 battery, recovered verbatim from this conversation's own
transcript): the additive answer-adoption architecture glued a second (and partial
third) copy of the SAME 24-file list onto the end of the first one with no paragraph
break between them -- the restated intro sentence landed on the SAME LINE as the
first list's last bullet ("...`vouchers_api.py`The directory ... contains 24 Python
files. Here is the complete list:"). `_count_contradicts_own_list`'s scan only ever
breaks on a line that fails `_LIST_LINE_RE`; that merged line still starts with a
bullet, so it never broke, and the scan walked straight into the restated copy and
counted 4 more of its items before real prose finally ended it -- 28 for a genuinely
correct, complete 24-file answer.

The fix (`_list_item_identity` + the `distinct` tally in `_count_contradicts_own_list`)
dedupes list items by normalized identity within the same contiguous block: if every
item beyond the stated count is a duplicate of one already counted, the list restates
ONE inventory, not two disagreeing ones, and this is not reported. A genuinely wrong
count (no duplication) or a genuinely missing item still fires exactly as before --
the raw `actual`/`concrete` signals this function already used are untouched.
"""
from swarm.team import _count_contradicts_own_list

_REAL_FILES = [
    "__init__.py", "admin_gst_api.py", "auto_restock_config_api.py",
    "categories_api.py", "custom_attributes_api.py", "godowns_api.py",
    "gst_compliance_api.py", "hsn_api.py", "import_api.py", "inventory_api.py",
    "inventory_suppliers_api.py", "item_variants_api.py", "items_api.py",
    "parties_api.py", "payments_api.py", "purchase_order_api.py",
    "purchase_order_items_api.py", "smart_alerts_api.py", "stock_api.py",
    "stock_transactions_api.py", "supplier_items_api.py", "tally_import_api.py",
    "uom_api.py", "vouchers_api.py",
]
assert len(_REAL_FILES) == 24


def _bulleted(files):
    return "\n".join(f"- `{f}`" for f in files)


# ── Case 1: the exact live T6 shape -- malformed, no-newline merge ─────────────

def test_t6_real_answer_malformed_merge_is_not_flagged():
    """The exact reconstructed T6 answer: 24 real files, then a second full copy
    whose intro sentence is glued onto the first list's last bullet with no
    newline, then a third copy truncated after 4 items by unbroken prose.

    Before the fix this scores actual=28 against stated=24 -- the live false
    positive. After the fix, distinct == stated (24) and this is not reported.
    """
    answer = (
        "The directory `API/inventory-service/router/` contains the following "
        "24 files:\n\n"
        + _bulleted(_REAL_FILES)
        # No newline here -- the real, malformed merge.
        + "The directory `API/inventory-service/router/` contains 24 Python "
        "files. Here is the complete list:\n\n"
        + _bulleted(_REAL_FILES[:3])
        + "\n- `categories_api.py\n\n"   # truncated 4th item, missing closing backtick
        "The directory `API/inventory-service/router/` contains 24 Python "
        "files. Here is the complete list:\n\n"
        + _bulleted(_REAL_FILES)
    )
    assert _count_contradicts_own_list(answer) is None


# ── Case 2: a genuinely wrong count, no repetition at all ──────────────────────

def test_genuinely_wrong_count_with_no_repetition_still_flagged():
    answer = (
        "The directory `API/inventory-service/router/` contains 24 files:\n\n"
        + _bulleted(_REAL_FILES[:20])   # 20 distinct, real files -- genuinely short
    )
    result = _count_contradicts_own_list(answer)
    assert result is not None
    stated, actual, intro, concrete = result
    assert stated == 24
    assert actual == 20


# ── Case 3: a missing file, reported honestly even once duplicates are dropped ─

def test_missing_file_still_flagged_after_dedup():
    # 23 distinct files (one missing) stated as 24 -- distinct(23) != stated(24).
    answer = (
        "The directory `API/inventory-service/router/` contains 24 files:\n\n"
        + _bulleted(_REAL_FILES[:23])
    )
    result = _count_contradicts_own_list(answer)
    assert result is not None
    stated, actual, intro, concrete = result
    assert stated == 24
    assert actual == 23


# ── Case 4: two distinct lists answering different sub-questions must not be ───
# ── silently merged into a false "they agree" by the same dedup logic ──────────

def test_distinct_subquestion_lists_are_not_merged_by_dedup():
    """T2-shape (2026-08-26 live case, preserved): a stated 7 followed by 6 real,
    non-duplicate bullets. No item repeats, so dedup changes nothing here -- this
    must still fire exactly as it did before this track's fix.
    """
    answer = (
        "The following 7 endpoints have no corresponding hook:\n\n"
        "- `/api/a`\n- `/api/b`\n- `/api/c`\n- `/api/d`\n- `/api/e`\n- `/api/f`"
    )
    result = _count_contradicts_own_list(answer)
    assert result is not None
    stated, actual, intro, concrete = result
    assert stated == 7
    assert actual == 6


# ── Case 5: padded-filler detection (pre-existing third case) is unchanged ──────

def test_padded_filler_detection_unchanged():
    answer = (
        "The vouchers module has 5 backend-only endpoints:\n\n"
        "- `/vouchers/post`\n- `/vouchers/cancel`\n- `/vouchers/grn`\n"
        "- `/vouchers/credit-note`\n"
        "- (No hook for internal endpoints) - all internal endpoints are "
        "intentionally not exposed.\n"
    )
    result = _count_contradicts_own_list(answer)
    assert result is not None
    stated, actual, intro, concrete = result
    assert stated == 5
    assert actual == 5
    assert concrete == 4


# ── Case 6: a correct inventory cleanly repeated twice (proper paragraph break) ─
# ── already did not fire before this track -- confirm the fix does not change ──
# ── that.                                                                       ─

def test_cleanly_separated_repeat_still_not_flagged():
    answer = (
        "The directory contains the following 24 files:\n\n"
        + _bulleted(_REAL_FILES)
        + "\n\nTo restate, the directory contains the following 24 files:\n\n"
        + _bulleted(_REAL_FILES)
    )
    assert _count_contradicts_own_list(answer) is None
