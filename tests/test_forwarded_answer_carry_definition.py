"""What counts as a forwarded member answer being "carried" by the final answer.

Two live runs of forward_member_answer showed the Coordinator retyping the Researcher's
24-file list under different surrounding prose. Whole-string containment called the member
answer "not carried" and appended all of it, so the list appeared twice.

A forwarded answer is carried when it appears verbatim (whitespace, backticks and asterisks
aside) or when it is LIST-SHAPED and the answer carries the list. The primary invariant is
that nothing a member said may silently disappear, so "list-shaped" is a strict definition:

  * at least 3 contiguous item lines, ignoring blank lines and Markdown code fences;
  * a header line (a path and/or "N items") only directly against the list, with a correct N;
  * prose only before the first / after the last item, in a bounded amount;
  * every prose line INERT: no digit but the item count, no number word, no source file,
    no negation / caveat / quantity / obligation / change word, and any path or identifier it
    names must appear in the answer too.

Anything else is not a list, and the member's text is appended in full.
"""
import re
from types import SimpleNamespace

import pytest

from swarm import team as team_mod
from swarm.team import (
    _MAX_FRAMING_CHARS, _MAX_FRAMING_LINES, _forwarded_text_carried, _header_counts,
    _list_items_if_list_shaped, _list_shape, _make_forward_member_answer,
    _with_forwarded_evidence,
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
CLOSE1 = ("Each file likely defines routes and handlers for a specific aspect of the inventory "
          "service, such as managing items, categories, suppliers, and stock transactions.")
CLOSE2 = CLOSE1 + " If you need details about a specific API file, I can read its contents for you."
BULLETS = "\n".join(f"- `{f}`" for f in FILES)

# The two REAL Researcher answers from the live validation runs, byte for byte.
RUN1_MEMBER = (
    f"The directory `{DIR}` contains 24 files, each corresponding to a different API endpoint "
    "or functionality within the inventory service. Here is the list of files:\n\n"
    + BULLETS + "\n\n" + CLOSE1)
RUN2_MEMBER = (
    f"The `{DIR}` directory contains 24 files, each corresponding to a different API endpoint "
    "or functionality within the inventory service. Here is the raw output from the directory "
    f"listing:\n\n```\n{DIR}  (24 items):\n"
    + "\n".join(f"[FILE] {f}" for f in FILES) + "\n```\n\n" + CLOSE2)
# The two REAL Coordinator answers that followed them (before anything was appended).
RUN1_COORD = (
    f"The directory `{DIR}` contains the following 24 files, each corresponding to a "
    "different API endpoint or functionality within the inventory service:\n\n" + BULLETS)
RUN2_COORD = (
    f"The `{DIR}` directory contains the following 24 files, each corresponding to a "
    "different API endpoint or functionality within the inventory service:\n\n"
    + BULLETS + "\n\n" + CLOSE2)


def _fwd(**members):
    return SimpleNamespace(_forwarded_members=members)


def _coord(items=FILES, fmt="- `{f}`", path=True, intro="Router files"):
    body = "\n".join(fmt.format(f=f, n=i) for i, f in enumerate(items, 1))
    return f"{intro}" + (f" in `{DIR}`" if path else "") + ":\n\n" + body


def _member(prose_before="", prose_after="", items=FILES, fmt="- `{f}`"):
    body = "\n".join(fmt.format(f=f, n=i) for i, f in enumerate(items, 1))
    return "\n\n".join(p for p in (prose_before, body, prose_after) if p)


def _appends(content, member):
    """True if the runtime appends the member's text to `content`."""
    return "FORWARDED FROM THE MEMBERS" in _with_forwarded_evidence(
        content, _fwd(researcher=member))


def _prose(n):
    """One inert prose line of exactly n characters."""
    text = ("alpha beta gamma delta " * (n // 10 + 2))[:n].rstrip()
    return text + "x" * (n - len(text))


# ── POSITIVE: real fixtures ──────────────────────────────────────────────────────────────

def test_run1_real_member_answer_is_list_shaped_and_carried_by_the_real_coordinator_answer():
    assert _list_items_if_list_shaped(RUN1_MEMBER) == FILES
    assert not _appends(RUN1_COORD, RUN1_MEMBER)
    assert _with_forwarded_evidence(RUN1_COORD, _fwd(researcher=RUN1_MEMBER)) == RUN1_COORD


def test_run2_real_member_answer_with_code_fences_is_list_shaped_and_carried():
    assert _list_items_if_list_shaped(RUN2_MEMBER) == FILES
    assert not _appends(RUN2_COORD, RUN2_MEMBER)
    assert _with_forwarded_evidence(RUN2_COORD, _fwd(researcher=RUN2_MEMBER)) == RUN2_COORD


def test_the_real_answers_prose_lengths_are_within_the_cap_with_the_header_not_counted():
    for member, expected in ((RUN1_MEMBER, 348), (RUN2_MEMBER, 452)):
        prose = [ln.strip() for ln in member.splitlines()
                 if ln.strip() and not re.match(r"^(`{3}|- |\[FILE\])", ln.strip())
                 and _header_counts(ln.strip()) is None]
        assert sum(map(len, prose)) == expected <= _MAX_FRAMING_CHARS
    assert _MAX_FRAMING_CHARS == 460 and _MAX_FRAMING_LINES == 3


def test_each_real_member_answer_is_carried_by_the_other_runs_coordinator_answer():
    assert not _appends(RUN2_COORD, RUN1_MEMBER)
    assert not _appends(RUN1_COORD, RUN2_MEMBER)


def test_exact_member_text_appends_nothing():
    for member in (RUN1_MEMBER, RUN2_MEMBER):
        content = "Here you go:\n\n" + member + "\n\nAnything else?"
        assert _with_forwarded_evidence(content, _fwd(researcher=member)) == content


# ── POSITIVE: formats ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fmt", ["- `{f}`", "* {f}", "- {f}", "• {f}", "{n}. {f}", "{n}) {f}",
                                 "[FILE] {f}", "`{f}`", "{f}", "  -   {f}  ", "\t{f}"])
def test_bullet_number_tag_backtick_and_whitespace_variants_are_list_shaped(fmt):
    member = _member("Router files:", "That is all.", fmt=fmt)
    assert _list_items_if_list_shaped(member) == FILES, fmt
    assert not _appends(_coord(), member), fmt


def test_dir_and_file_tagged_items_are_supported():
    items = ["static/", "router/", "main.py", "app_config.py"]
    member = "\n".join(["[DIR] static/", "[DIR] router/", "[FILE] main.py", "[FILE] app_config.py"])
    assert _list_items_if_list_shaped(member) == items
    assert not _appends("static/ router/ main.py app_config.py", member)


def test_blank_lines_between_items_and_crlf_line_endings_are_fine():
    member = "\r\n\r\n".join(f"- {f}" for f in FILES[:6])
    assert _list_items_if_list_shaped(member) == FILES[:6]
    assert _list_items_if_list_shaped("\n\n".join(f"- {f}" for f in FILES[:6])) == FILES[:6]


@pytest.mark.parametrize("fence", ["```", "```text", "```plaintext", "``` ", "~~~", "````"])
def test_standalone_code_fence_markers_are_ignored_whatever_their_language_tag(fence):
    member = f"{fence.strip()}\n" + "\n".join(f"[FILE] {f}" for f in FILES[:5]) + f"\n{fence.strip()[:3]}"
    assert _list_items_if_list_shaped(member) == FILES[:5]


def test_a_header_directly_against_the_list_is_structure_not_prose():
    for header in (f"{DIR}  (24 items):", f"`{DIR}`:", "(24 items)"):
        member = f"```\n{header}\n" + "\n".join(f"[FILE] {f}" for f in FILES) + "\n```"
        assert _list_items_if_list_shaped(member) == FILES, header
    trailing = "\n".join(f"- {f}" for f in FILES) + f"\n{DIR}  (24 items)"
    assert _list_items_if_list_shaped(trailing) == FILES


def test_a_bare_path_line_is_an_item_so_it_must_be_carried_not_ignored():
    member = f"{DIR}\n" + "\n".join(f"- {f}" for f in FILES)
    assert _list_items_if_list_shaped(member) == [DIR] + FILES
    assert _appends("\n".join(FILES), member)                 # path not mentioned -> append
    assert not _appends(f"{DIR}\n" + "\n".join(FILES), member)


def test_different_intro_and_closing_prose_with_every_item_present_is_not_appended():
    member = RUN2_MEMBER
    for content in (
        _coord(intro="Sure. The files are"),
        _coord(intro="Here they are") + "\n\nLet me know if you want more.",
        "\n".join(FILES) + f"\n(all under {DIR})",
    ):
        assert not _appends(content, member), content


def test_inert_closing_prose_is_dropped_by_design():
    """Pins the accepted trade-off: a sentence with no cue word is decoration."""
    assert not _appends(_coord(), RUN1_MEMBER)          # closing sentence not reproduced


# ── NEGATIVE: the list itself ────────────────────────────────────────────────────────────

def test_one_missing_item_appends_the_full_member_answer():
    content = _coord(items=[f for f in FILES if f != "vouchers_api.py"])
    out = _with_forwarded_evidence(content, _fwd(researcher=RUN2_MEMBER))
    assert out.startswith(content)
    assert out.endswith(RUN2_MEMBER.strip())
    assert "vouchers_api.py" in out.split("### From researcher\n", 1)[1]


@pytest.mark.parametrize("dropped", range(24))
def test_dropping_any_single_item_is_always_detected(dropped):
    content = _coord(items=[f for i, f in enumerate(FILES) if i != dropped])
    for member in (RUN1_MEMBER, RUN2_MEMBER):
        out = _with_forwarded_evidence(content, _fwd(researcher=member))
        assert out.endswith(member.strip()), FILES[dropped]        # nothing silently lost
        assert FILES[dropped] in out.split("### From researcher\n", 1)[1]


def test_multiple_missing_items_append_the_full_member_answer():
    out = _with_forwarded_evidence(_coord(items=FILES[:12]), _fwd(researcher=RUN1_MEMBER))
    assert out.endswith(RUN1_MEMBER.strip())
    for f in FILES[12:]:
        assert f in out.split("### From researcher\n", 1)[1]


def test_unrelated_answer_appends_the_full_member_answer():
    out = _with_forwarded_evidence("I could not determine that.", _fwd(researcher=RUN2_MEMBER))
    assert out.startswith("I could not determine that.") and out.endswith(RUN2_MEMBER.strip())


def test_an_item_must_appear_as_a_whole_token():
    member = "\n".join(["- items_api.py", "- parties_api.py", "- stock_api.py"])
    assert _appends("inventory_items_api.py all_parties_api.py xstock_api.py", member)
    assert _appends("items_api.py.bak parties_api.py stock_api.pyc", member)
    assert not _appends("items_api.py, parties_api.py, and stock_api.py.", member)


def test_a_bare_token_line_that_looks_like_an_item_but_is_a_message_must_still_be_carried():
    """A line that merely LOOKS like a filename is treated as an item, so if the Coordinator
    drops it the member's text is appended rather than the line being silently lost."""
    member = "\n".join(["- a_one.py", "- b_two.py", "- c_three.py", "- IMPORTANT-read-first"])
    assert _list_items_if_list_shaped(member)[-1] == "IMPORTANT-read-first"
    assert _appends("a_one.py b_two.py c_three.py", member)
    assert not _appends("a_one.py b_two.py c_three.py IMPORTANT-read-first", member)


# ── NEGATIVE: not a list ─────────────────────────────────────────────────────────────────

def test_fewer_than_three_items_is_not_a_list():
    assert _list_shape("- a_b.py\n- c_d.py") is None
    assert _appends("a_b.py", "- a_b.py\n- c_d.py")


def test_plain_prose_is_never_a_list_and_needs_exact_containment():
    member = ("The auth service signs tokens with HS256 and rotates the key every 24 hours. "
              "Refresh tokens live in Redis under auth_refresh keys.")
    assert _list_shape(member) is None
    assert _appends("Tokens are HS256-signed and the key rotates daily; refresh tokens are in Redis.", member)
    assert not _appends("Summary: " + member, member)


def test_prose_that_merely_mentions_files_is_not_a_list():
    member = "Look at items_api.py, parties_api.py and stock_api.py; they hold the routes."
    assert _list_shape(member) is None
    assert _appends("items_api.py parties_api.py stock_api.py", member)


def test_annotated_item_lines_are_not_reduced_to_their_names():
    member = "\n".join(f"- items_api.py: {d}" for d in ("items", "a", "b", "c"))
    assert _list_shape(member) is None
    annotated_names = "\n".join(f"- {f} - handles {n}" for f, n in
                                [("a_one.py", "auth"), ("b_two.py", "billing"), ("c_three.py", "stock")])
    assert _list_shape(annotated_names) is None
    assert _appends("a_one.py b_two.py c_three.py", annotated_names)


def test_prose_interleaved_between_items_disqualifies_the_list():
    member = "\n".join(["- alpha_one.py", "- Party: person record", "- beta_two.py",
                        "- gamma_three.py", "- delta_four.py"])
    assert _list_shape(member) is None
    assert _appends("alpha_one.py beta_two.py gamma_three.py delta_four.py", member)
    inserted = "\n".join(FILES[:12] + ["That was the first half."] + FILES[12:])
    assert _list_shape(inserted) is None


def test_a_header_among_the_items_or_away_from_them_disqualifies_the_list():
    among = "\n".join([f"- {f}" for f in FILES[:6]] + [f"{DIR}  (24 items):"] + [f"- {f}" for f in FILES[6:]])
    assert _list_shape(among) is None
    away = f"{DIR}  (24 items):\nSome intro text here.\n" + "\n".join(f"- {f}" for f in FILES)
    assert _list_shape(away) is None


def test_two_headers_or_a_wrong_count_in_the_header_disqualify_the_list():
    body = "\n".join(f"- {f}" for f in FILES)
    assert _list_shape(f"{DIR}\n(24 items):\n{body}") is None
    assert _list_shape(f"{DIR}  (30 items):\n{body}") is None
    assert _list_shape(f"{DIR}  (24 items):\n{body}") is not None


# ── NEGATIVE: information in the framing prose must never disappear ─────────────────────

MEANINGFUL_FRAMING = {
    "qualification (only/excluded)": ("Only the Python files are listed; hidden files are excluded.", ""),
    "caveat after the list": ("", "Note: three of these are deprecated."),
    "contrast": ("", "The list is complete, however the last entries are not routers."),
    "negation": ("", "None of these files define models."),
    "contraction of not": ("", "This listing doesn't include subdirectories."),
    "count that is not the item count": ("", "There are 30 files in total, 24 shown."),
    "count word": ("The last three are experimental.", ""),
    "incomplete claim": ("", "There are some more files further down."),
    "warning": ("", "Warning: parties_api.py is unfinished."),
    "obligation": ("", "You should read stock_api.py first."),
    "change over time": ("", "Most were recently modified."),
    "source file named in prose": ("", "The routers are mounted from main.py."),
    "wrong count in the intro": ("The directory contains 30 files. Here is the list:", ""),
}


@pytest.mark.parametrize("name", sorted(MEANINGFUL_FRAMING))
def test_meaningful_framing_disqualifies_the_list_and_the_member_text_is_appended_whole(name):
    before, after = MEANINGFUL_FRAMING[name]
    member = _member(before, after)
    assert _list_shape(member) is None, name
    out = _with_forwarded_evidence(_coord(), _fwd(researcher=member))
    assert out.endswith(member.strip()), name
    for sentence in (before, after):
        if sentence:
            assert sentence in out.split("### From researcher\n", 1)[1], name


def test_framing_that_names_an_identifier_the_answer_does_not_carry_is_appended():
    member = _member(f"The routers live under `{DIR}`, mounted at /inventory/v2/.")
    assert _list_shape(member) is not None                        # inert on its own
    assert _appends(_coord(path=True), member)                    # /inventory/v2/ not carried
    assert not _appends(_coord(path=True) + "\n(mounted at /inventory/v2/)", member)


def test_framing_at_the_cap_is_accepted_and_one_character_more_is_not():
    at, over = _prose(_MAX_FRAMING_CHARS), _prose(_MAX_FRAMING_CHARS + 1)
    assert _list_shape(_member(at)) is not None
    assert _list_shape(_member(over)) is None
    assert _appends(_coord(), _member(over)) and not _appends(_coord(), _member(at))


def test_the_cap_is_on_prose_in_total_across_lines():
    half = _MAX_FRAMING_CHARS // 2
    assert _list_shape(_member(_prose(half), _prose(_MAX_FRAMING_CHARS - half))) is not None
    assert _list_shape(_member(_prose(half), _prose(_MAX_FRAMING_CHARS - half + 1))) is None


def test_prose_line_count_boundary():
    lines = [f"Alpha beta gamma {chr(97 + i) * 4}." for i in range(_MAX_FRAMING_LINES + 1)]
    ok = "\n".join(lines[:_MAX_FRAMING_LINES]) + "\n" + BULLETS
    too_many = "\n".join(lines) + "\n" + BULLETS
    assert _list_shape(ok) is not None
    assert _list_shape(too_many) is None


def test_long_framing_prose_with_a_complete_list_still_appends_everything():
    long_intro = " ".join(["The directory holds the API routers for the inventory service."] * 12)
    member = _member(long_intro)
    assert len(long_intro) > _MAX_FRAMING_CHARS
    out = _with_forwarded_evidence(_coord(), _fwd(researcher=member))
    assert out.endswith(member.strip())


def test_items_must_dominate_the_framing_lines_three_to_one():
    member = "Alpha beta.\nGamma delta.\n- a_one.py\n- b_two.py\n- c_three.py"
    assert _list_shape(member) is None                   # 3 items vs 2 prose lines
    assert _list_shape("Alpha beta.\n- a_one.py\n- b_two.py\n- c_three.py") is not None


# ── multiple forwarded members ───────────────────────────────────────────────────────────

def test_each_forwarded_member_is_judged_independently():
    other = "The reviewer found a race condition in the stock update path and nothing else."
    out = _with_forwarded_evidence(_coord(), _fwd(researcher=RUN2_MEMBER, reviewer=other))
    assert out.startswith(_coord()) and "### From reviewer" in out and other in out
    assert "### From researcher" not in out                       # carried by its items

    reviewer_only = other
    out2 = _with_forwarded_evidence(reviewer_only, _fwd(researcher=RUN2_MEMBER, reviewer=other))
    assert "### From researcher" in out2 and "### From reviewer" not in out2
    assert out2.index("### From researcher") == out2.rindex("### From researcher")


def test_two_list_members_are_each_checked_against_their_own_items():
    a = "\n".join(["- a_one.py", "- a_two.py", "- a_three.py"])
    b = "\n".join(["- b_one.py", "- b_two.py", "- b_three.py"])
    out = _with_forwarded_evidence("a_one.py a_two.py a_three.py b_one.py", _fwd(alpha=a, beta=b))
    assert "### From beta" in out and "### From alpha" not in out
    both = _with_forwarded_evidence("a_one.py a_two.py a_three.py b_one.py b_two.py b_three.py",
                                    _fwd(alpha=a, beta=b))
    assert "FORWARDED FROM THE MEMBERS" not in both


# ── regression behaviour ─────────────────────────────────────────────────────────────────

def test_the_list_never_appears_twice_and_a_second_pass_changes_nothing():
    retyped = _coord(fmt="{n}. {f}")
    once = _with_forwarded_evidence(retyped, _fwd(researcher=RUN2_MEMBER))
    assert once == retyped and once.count("vouchers_api.py") == 1
    appended = _with_forwarded_evidence("Nothing useful.", _fwd(researcher=RUN2_MEMBER))
    assert _with_forwarded_evidence(appended, _fwd(researcher=RUN2_MEMBER)) == appended
    assert appended.count("__init__.py") == 1


def test_backtick_only_difference_on_verbatim_prose_is_carried_but_case_is_not_forgiven():
    member = "The gate returns ALREADY DONE when a target repeats."
    assert not _appends("Summary: The gate returns `ALREADY DONE` when a target repeats.", member)
    assert _appends("Summary: the gate returns ALREADY DONE when a target repeats.", member)


def test_carried_helper_agrees_with_the_public_behaviour():
    assert _forwarded_text_carried(RUN1_MEMBER, _coord()) is True
    assert _forwarded_text_carried(RUN1_MEMBER, _coord(items=FILES[:-1])) is False
    assert _forwarded_text_carried(RUN1_MEMBER, "nothing here") is False
    assert _forwarded_text_carried(RUN1_MEMBER, _coord(path=False)) is False   # framing names DIR


def test_empty_and_canned_content_are_still_returned_unchanged():
    team = _fwd(researcher=RUN2_MEMBER)
    assert _with_forwarded_evidence("", team) == ""
    canned = team_mod._BUDGET_EXHAUSTED_ANSWER
    assert _with_forwarded_evidence(canned, team) == canned


# ── prompt / tool description ────────────────────────────────────────────────────────────

def test_forward_instructions_tell_the_coordinator_not_to_list_forwarded_items_again():
    text = " ".join(team_mod._FORWARD_INSTRUCTIONS)
    assert "do not list them again yourself" in text and "name every item" in text


def test_the_tool_description_says_it_returns_a_receipt_not_the_text():
    assert "receipt" in (_make_forward_member_answer({}, {}).description or "")
