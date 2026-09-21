"""Forwarded member evidence is preserved LINE BY LINE, with no judgement about meaning.

Live runs of forward_member_answer showed the Coordinator retyping the Researcher's 24-file
list under different surrounding prose. Whole-string containment then appended the entire
member answer (list twice); later heuristics tried to decide which prose was harmless, and
that cannot be made reliable ("Everything in here is bleeding-edge and unsupported." has no
cue word and carries information).

The invariant now: EVERY line of a forwarded member answer is either represented in the final
answer or appended verbatim, and a carried list item is never repeated.

  * an item line (bare bullet/number/[FILE]/[DIR]/backticked path or identifier) is
    represented if the item appears in the answer as a whole token;
  * every other line is represented only if it appears verbatim (whitespace, backticks and
    asterisks aside) -- no vocabulary, no thresholds, no "is this prose harmless" decision;
  * blank lines and code-fence markers are ignored;
  * a member answer that is not a list (fewer than 3 item lines) is carried whole or appended
    whole; a list none of whose lines is present is appended whole.
"""
import re
from types import SimpleNamespace

import pytest

from swarm import team as team_mod
from swarm.team import (
    _BARE_ITEM_LINE_RE, _forwarded_gap, _make_forward_member_answer, _with_forwarded_evidence,
)

DIR = "API/inventory-service/router/"
FILES = [
    "__init__.py", "admin_gst_api.py", "auto_restock_config_api.py", "categories_api.py",
    "custom_attributes_api.py", "godowns_api.py", "gst_compliance_api.py", "hsn_api.py",
    "import_api.py", "inventory_api.py", "inventory_suppliers_api.py", "item_variants_api.py",
    "items_api.py", "parties_api.py", "payments_api.py", "purchase_order_api.py",
    "purchase_order_items_api.py", "smart_alerts_api.py", "stock_api.py",
    "stock_transactions_api.py", "supplier_items_api.py", "tally_import_api.py",
    "uom_api.py", "vouchers_api.py",
]
BULLETS = "\n".join(f"- `{f}`" for f in FILES)
INTRO1 = (f"The directory `{DIR}` contains 24 files, each corresponding to a different API "
          "endpoint or functionality within the inventory service. Here is the list of files:")
CLOSE1 = ("Each file likely defines routes and handlers for a specific aspect of the inventory "
          "service, such as managing items, categories, suppliers, and stock transactions.")
CLOSE2 = CLOSE1 + " If you need details about a specific API file, I can read its contents for you."
INTRO2 = (f"The `{DIR}` directory contains 24 files, each corresponding to a different API "
          "endpoint or functionality within the inventory service. Here is the raw output from "
          "the directory listing:")
HEADER2 = f"{DIR}  (24 items):"
INTRO3 = f"Here are the files in the `{DIR}` directory:"
CLOSE3 = "There are 24 files in total."

# The three REAL Researcher answers and the REAL Coordinator answers that followed them
# (before anything was appended), from the live validation runs.
RUN1_MEMBER = INTRO1 + "\n\n" + BULLETS + "\n\n" + CLOSE1
RUN2_MEMBER = (INTRO2 + f"\n\n```\n{HEADER2}\n" + "\n".join(f"[FILE] {f}" for f in FILES)
               + "\n```\n\n" + CLOSE2)
RUN3_MEMBER = INTRO3 + "\n\n" + BULLETS + "\n\n" + CLOSE3
RUN1_COORD = (f"The directory `{DIR}` contains the following 24 files, each corresponding to a "
              "different API endpoint or functionality within the inventory service:\n\n" + BULLETS)
RUN2_COORD = (f"The `{DIR}` directory contains the following 24 files, each corresponding to a "
              "different API endpoint or functionality within the inventory service:\n\n"
              + BULLETS + "\n\n" + CLOSE2)
RUN3_COORD = (f"Here are the files directly inside the `{DIR}` directory:\n\n" + BULLETS
              + "\n\n" + CLOSE3)


def _fwd(**members):
    return SimpleNamespace(_forwarded_members=members)


def _coord(items=FILES, fmt="- `{f}`", intro="Router files", path=True):
    body = "\n".join(fmt.format(f=f, n=i) for i, f in enumerate(items, 1))
    return intro + (f" in `{DIR}`" if path else "") + ":\n\n" + body


def _member(before="", after="", items=FILES, fmt="- `{f}`"):
    body = "\n".join(fmt.format(f=f, n=i) for i, f in enumerate(items, 1))
    return "\n\n".join(p for p in (before, body, after) if p)


def _appended(content, member, key="researcher"):
    """The text the runtime appends after `content` for one forwarded member ('' if none)."""
    out = _with_forwarded_evidence(content, _fwd(**{key: member}))
    assert out.startswith(content)
    return out[len(content):]


def _lines_appended(content, member):
    """The member lines appended in 'lines' mode, or None if another mode applied."""
    tail = _appended(content, member)
    marker = "(only the lines the answer above does not contain)\n"
    return tail.split(marker, 1)[1].split("\n") if marker in tail else None


def _tok(text, name):
    """How many times `name` appears in `text` as a whole token (not inside a longer name)."""
    return len(re.findall(rf"(?<![\w./-]){re.escape(name)}(?![\w-])", text))


def _every_line_represented(final, member):
    """The invariant: each non-blank, non-fence member line is in `final` (items by whole
    token, everything else verbatim)."""
    have = re.sub(r"\s+", " ", re.sub(r"[`*]", "", final))
    for raw in member.splitlines():
        line = raw.strip()
        if not line or re.match(r"^(?:`{3,}|~{3,})[\w+-]*$", line):
            continue
        m = _BARE_ITEM_LINE_RE.match(line)
        if m:
            assert re.search(rf"(?<![\w./-]){re.escape(m.group('item'))}(?![\w-])", final), line
        else:
            assert re.sub(r"\s+", " ", re.sub(r"[`*]", "", line)).strip() in have, line


# ── complete carry ───────────────────────────────────────────────────────────────────────

def test_exact_full_text_containment_appends_nothing():
    for member in (RUN1_MEMBER, RUN2_MEMBER, RUN3_MEMBER, "The auth service signs tokens."):
        content = "Here you go:\n\n" + member + "\n\nAnything else?"
        assert _with_forwarded_evidence(content, _fwd(researcher=member)) == content


def test_every_member_line_carried_by_a_reformatted_answer_appends_nothing():
    member = f"{INTRO3}\n\n{BULLETS}\n\n{CLOSE3}"
    content = f"{INTRO3}\n" + "\n".join(f"{i}. {f}" for i, f in enumerate(FILES, 1)) + f"\n{CLOSE3}"
    assert _forwarded_gap(member, content) == ("carried", [])
    assert _with_forwarded_evidence(content, _fwd(researcher=member)) == content


@pytest.mark.parametrize("member,coord,expected_lines", [
    (RUN1_MEMBER, RUN1_COORD, [INTRO1, CLOSE1]),
    (RUN2_MEMBER, RUN2_COORD, [INTRO2, HEADER2]),
    (RUN3_MEMBER, RUN3_COORD, [INTRO3]),
], ids=["run1", "run2", "run3"])
def test_real_live_runs_list_appears_once_and_only_the_missing_lines_are_appended(
        member, coord, expected_lines):
    out = _with_forwarded_evidence(coord, _fwd(researcher=member))
    assert _tok(out, "__init__.py") == 1 and _tok(out, "vouchers_api.py") == 1
    assert _lines_appended(coord, member) == expected_lines
    assert len(out) - len(coord) < len(member)                # never a whole-answer copy
    _every_line_represented(out, member)


# ── missing prose ────────────────────────────────────────────────────────────────────────

def test_list_carried_but_intro_omitted_appends_the_intro_verbatim():
    member = _member("Here are the router files, grouped by domain:", "")
    assert _lines_appended(_coord(), member) == ["Here are the router files, grouped by domain:"]


def test_list_carried_but_closing_omitted_appends_the_closing_verbatim():
    member = _member("", "That is every file in the directory.")
    assert _lines_appended(_coord(), member) == ["That is every file in the directory."]


def test_list_carried_but_caveat_omitted_appends_the_caveat_verbatim():
    member = _member("", "Note: three of these are deprecated.")
    assert _lines_appended(_coord(), member) == ["Note: three of these are deprecated."]


# Vocabulary must not affect correctness: cue words or none, content-bearing or decorative,
# every absent prose line is appended.
PROSE = [
    "Everything in here is bleeding-edge and unsupported.",          # no cue word
    "Each file likely defines routes and handlers for a specific aspect of the service.",
    "Only the Python files are listed; hidden files are excluded.",  # cue words
    "Note: three of these are deprecated.",
    "There are 30 files in total.",
    "There are 24 files in total.",                                  # restates the count
    "You should read stock_api.py first.",
    "Thanks!",
    "Here you go:",
    "Most were recently modified, however the last two were not.",
]


@pytest.mark.parametrize("sentence", PROSE)
@pytest.mark.parametrize("where", ["before", "after"])
def test_any_absent_prose_line_is_appended_whatever_it_says(sentence, where):
    member = _member(before=sentence) if where == "before" else _member(after=sentence)
    assert _lines_appended(_coord(), member) == [sentence]
    _every_line_represented(_with_forwarded_evidence(_coord(), _fwd(researcher=member)), member)


@pytest.mark.parametrize("sentence", PROSE)
def test_a_prose_line_the_answer_already_contains_is_not_appended(sentence):
    member = _member(after=sentence)
    assert _with_forwarded_evidence(_coord() + "\n\n" + sentence, _fwd(researcher=member)) \
        == _coord() + "\n\n" + sentence


def test_each_absent_prose_line_is_appended_and_a_carried_one_is_not():
    member = _member("First intro line.", "Closing line one.\nClosing line two.")
    content = _coord() + "\n\nClosing line one."
    assert _lines_appended(content, member) == ["First intro line.", "Closing line two."]


def test_appended_lines_keep_the_members_order():
    member = "Alpha intro.\n" + BULLETS + "\nBeta middle.\nGamma end."
    assert _lines_appended(_coord(), member) == ["Alpha intro.", "Beta middle.", "Gamma end."]


# ── missing items ────────────────────────────────────────────────────────────────────────

def test_one_missing_item_is_appended_alone_and_carried_items_are_not_repeated():
    content = _coord(items=[f for f in FILES if f != "vouchers_api.py"])
    out = _with_forwarded_evidence(content, _fwd(researcher=RUN3_MEMBER))
    assert _lines_appended(content, RUN3_MEMBER) == [INTRO3, "- `vouchers_api.py`", CLOSE3]
    assert _tok(out, "vouchers_api.py") == 1 and _tok(out, "__init__.py") == 1
    _every_line_represented(out, RUN3_MEMBER)


def test_multiple_missing_items_are_all_appended_and_none_repeated():
    content = _coord(items=FILES[:20])
    appended = _lines_appended(content, RUN3_MEMBER)
    assert [ln for ln in appended if ln.startswith("- ")] == [f"- `{f}`" for f in FILES[20:]]
    out = _with_forwarded_evidence(content, _fwd(researcher=RUN3_MEMBER))
    assert all(_tok(out, f) == 1 for f in FILES)
    _every_line_represented(out, RUN3_MEMBER)


@pytest.mark.parametrize("dropped", range(24))
def test_dropping_any_single_item_never_loses_it(dropped):
    content = _coord(items=[f for i, f in enumerate(FILES) if i != dropped])
    for member in (RUN1_MEMBER, RUN2_MEMBER, RUN3_MEMBER):
        out = _with_forwarded_evidence(content, _fwd(researcher=member))
        assert _tok(out, FILES[dropped]) == 1
        _every_line_represented(out, member)


def test_missing_items_and_missing_prose_together_lose_nothing():
    member = _member("Intro.", "Closing.")
    content = _coord(items=FILES[::2])
    out = _with_forwarded_evidence(content, _fwd(researcher=member))
    _every_line_represented(out, member)
    assert all(_tok(out, f) == 1 for f in FILES)


def test_an_answer_that_carries_none_of_a_list_gets_the_whole_member_text():
    for content in ("I could not determine that.", "Something entirely unrelated."):
        out = _with_forwarded_evidence(content, _fwd(researcher=RUN2_MEMBER))
        assert out.endswith("### From researcher\n" + RUN2_MEMBER.strip())
        assert "only the lines" not in out


# ── matching correctness ─────────────────────────────────────────────────────────────────

def test_items_match_whole_tokens_only():
    member = "\n".join(["- items_api.py", "- parties_api.py", "- stock_api.py"])
    for content in ("inventory_items_api.py all_parties_api.py xstock_api.py",
                    "items_api.py.bak parties_api.py stock_api.pyc"):
        assert _forwarded_gap(member, content)[0] != "carried"
    assert _forwarded_gap(member, "items_api.py, parties_api.py, and stock_api.py.") == ("carried", [])


def test_items_api_does_not_match_inventory_items_api():
    member = "\n".join(["- items_api.py", "- parties_api.py", "- stock_api.py"])
    mode, lines = _forwarded_gap(member, "inventory_items_api.py parties_api.py stock_api.py")
    assert mode == "lines" and lines == ["- items_api.py"]


@pytest.mark.parametrize("fmt", ["- `{f}`", "* {f}", "- {f}", "• {f}", "{n}. {f}", "{n}) {f}",
                                 "[FILE] {f}", "`{f}`", "{f}", "  -   {f}  ", "\t{f}"])
def test_bullet_number_tag_backtick_and_whitespace_variants_are_all_items(fmt):
    member = _member(items=FILES, fmt=fmt)
    assert _forwarded_gap(member, _coord()) == ("carried", [])
    assert _forwarded_gap(_member(), _coord(fmt=fmt)) == ("carried", [])


def test_dir_and_file_tags_and_crlf_and_blank_lines_are_supported():
    member = "\r\n\r\n".join(["[DIR] static/", "[DIR] router/", "[FILE] main.py", "[FILE] app_config.py"])
    assert _forwarded_gap(member, "static/ router/ main.py app_config.py") == ("carried", [])
    mode, lines = _forwarded_gap(member, "static/ router/ main.py")
    assert mode == "lines" and lines == ["[FILE] app_config.py"]


@pytest.mark.parametrize("fence", ["```", "```text", "```plaintext", "~~~", "````"])
def test_code_fence_markers_are_ignored_never_appended(fence):
    member = f"{fence}\n" + "\n".join(f"[FILE] {f}" for f in FILES[:5]) + f"\n{fence[:3]}"
    assert _forwarded_gap(member, "\n".join(FILES[:4]))[1] == ["[FILE] " + FILES[4]]
    assert _forwarded_gap(member, "\n".join(FILES[:5])) == ("carried", [])


def test_prose_that_merely_mentions_a_file_is_not_an_item():
    member = _member(after="See items_api.py for the details.")
    mode, lines = _forwarded_gap(member, _coord())
    assert (mode, lines) == ("lines", ["See items_api.py for the details."])
    # ...and mentioning the file in the answer does not count as carrying the sentence
    assert _forwarded_gap(member, _coord() + "\nitems_api.py")[1] == ["See items_api.py for the details."]


def test_a_bare_token_line_that_is_really_a_message_is_still_carried_or_appended():
    member = "\n".join(["- a_one.py", "- b_two.py", "- c_three.py", "- IMPORTANT-read-first"])
    assert _forwarded_gap(member, "a_one.py b_two.py c_three.py") == ("lines", ["- IMPORTANT-read-first"])
    assert _forwarded_gap(member, "a_one.py b_two.py c_three.py IMPORTANT-read-first")[0] == "carried"


# ── non-list responses stay safe ─────────────────────────────────────────────────────────

def test_ordinary_prose_is_carried_whole_or_appended_whole():
    member = ("The auth service signs tokens with HS256 and rotates the key every 24 hours. "
              "Refresh tokens live in Redis under auth_refresh keys.")
    assert _forwarded_gap(member, "Summary: " + member) == ("carried", [])
    paraphrase = "Tokens are HS256-signed and rotate daily; refresh tokens are in Redis."
    assert _forwarded_gap(member, paraphrase) == ("full", [])
    assert _appended(paraphrase, member).endswith("### From researcher\n" + member)


def test_prose_with_two_or_fewer_item_lines_is_not_treated_as_a_list():
    member = "Intro sentence.\n- a_b.py\n- c_d.py\nClosing sentence."
    assert _forwarded_gap(member, "a_b.py c_d.py Intro sentence.") == ("full", [])
    assert _forwarded_gap(member, "Intro sentence.\n- a_b.py\n- c_d.py\nClosing sentence.")[0] == "carried"
    assert _appended("nothing", member).endswith(member)


def test_annotated_items_are_non_item_lines_and_are_never_reduced_to_names():
    member = "\n".join(["- a_one.py: handles auth", "- b_two.py: handles billing",
                        "- c_three.py: handles stock"])
    assert _forwarded_gap(member, "a_one.py b_two.py c_three.py") == ("full", [])
    assert _appended("a_one.py b_two.py c_three.py", member).endswith(member)
    mixed = "\n".join(["- a_one.py", "- b_two.py", "- c_three.py", "- d_four.py: handles auth"])
    assert _forwarded_gap(mixed, "a_one.py b_two.py c_three.py d_four.py") == \
        ("lines", ["- d_four.py: handles auth"])


# ── multiple members ─────────────────────────────────────────────────────────────────────

def test_each_forwarded_member_is_handled_independently():
    other = "The reviewer found a race condition in the stock update path."
    out = _with_forwarded_evidence(_coord(), _fwd(researcher=RUN3_MEMBER, reviewer=other))
    assert out.startswith(_coord())
    assert "### From researcher (only the lines the answer above does not contain)" in out
    assert "### From reviewer\n" + other in out
    assert out.count("**FORWARDED FROM THE MEMBERS") == 1
    assert out.index("### From researcher") < out.index("### From reviewer")
    assert out.count("__init__.py") == 1


def test_a_carried_member_is_left_alone_while_another_is_appended():
    a = "\n".join(["- a_one.py", "- a_two.py", "- a_three.py"])
    b = "\n".join(["- b_one.py", "- b_two.py", "- b_three.py"])
    out = _with_forwarded_evidence("a_one.py a_two.py a_three.py b_one.py",
                                   _fwd(alpha=a, beta=b))
    assert "### From alpha" not in out
    assert _lines_appended("a_one.py a_two.py a_three.py b_one.py", b) == ["- b_two.py", "- b_three.py"]
    assert "FORWARDED FROM THE MEMBERS" not in _with_forwarded_evidence(
        "a_one.py a_two.py a_three.py b_one.py b_two.py b_three.py", _fwd(alpha=a, beta=b))


# ── idempotence ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("content,member", [
    (RUN1_COORD, RUN1_MEMBER), (RUN2_COORD, RUN2_MEMBER), (RUN3_COORD, RUN3_MEMBER),
    (_coord(items=FILES[:10]), RUN3_MEMBER), ("Nothing useful.", RUN2_MEMBER),
    ("Nothing useful.", "A prose answer that is not a list."),
], ids=["run1", "run2", "run3", "partial", "unrelated-list", "unrelated-prose"])
def test_applying_it_twice_never_appends_a_line_twice(content, member):
    team = _fwd(researcher=member)
    once = _with_forwarded_evidence(content, team)
    assert _with_forwarded_evidence(once, team) == once
    assert _with_forwarded_evidence(_with_forwarded_evidence(once, team), team) == once


# ── regression: guards and logging ───────────────────────────────────────────────────────

def test_empty_and_canned_content_are_still_returned_unchanged():
    team = _fwd(researcher=RUN3_MEMBER)
    assert _with_forwarded_evidence("", team) == ""
    canned = team_mod._BUDGET_EXHAUSTED_ANSWER
    assert _with_forwarded_evidence(canned, team) == canned
    leaked = '<tool_call>{"name": "get_file_content", "arguments": {}}</tool_call>'
    assert _with_forwarded_evidence(leaked, team) == leaked


def test_nothing_forwarded_or_nothing_missing_returns_the_same_object_content():
    assert _with_forwarded_evidence("The answer.", _fwd()) == "The answer."
    assert _with_forwarded_evidence("The answer.", SimpleNamespace()) == "The answer."
    assert _with_forwarded_evidence("The answer.", None) == "The answer."


def test_the_log_distinguishes_partial_carry_from_not_carried(capsys):
    _with_forwarded_evidence(RUN3_COORD, _fwd(researcher=RUN3_MEMBER))
    partial = capsys.readouterr().out
    assert "forwarded list(s) carried but some of the member's lines are absent" in partial
    assert "not carried by the final answer" not in partial
    _with_forwarded_evidence("Unrelated.", _fwd(researcher=RUN3_MEMBER))
    full = capsys.readouterr().out
    assert "not carried by the final answer -- appending verbatim: ['researcher']" in full
    _with_forwarded_evidence(RUN3_MEMBER, _fwd(researcher=RUN3_MEMBER))
    assert capsys.readouterr().out == ""                      # fully carried: silent


def test_the_obsolete_heuristic_machinery_is_gone():
    for name in ("_QUALIFYING_WORDS", "_inert_prose_identifiers", "_header_counts", "_list_shape",
                 "_MAX_FRAMING_CHARS", "_MAX_FRAMING_LINES", "_carry_check", "_forwarded_text_carried",
                 "_list_items_if_list_shaped"):
        assert not hasattr(team_mod, name), name


# ── prompt / tool description ────────────────────────────────────────────────────────────

def test_forward_instructions_tell_the_coordinator_not_to_list_forwarded_items_again():
    text = " ".join(team_mod._FORWARD_INSTRUCTIONS)
    assert "do not list them again yourself" in text and "name every item" in text


def test_the_tool_description_says_it_returns_a_receipt_not_the_text():
    assert "receipt" in (_make_forward_member_answer({}, {}).description or "")
