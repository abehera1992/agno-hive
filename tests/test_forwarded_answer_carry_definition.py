"""What counts as a forwarded member answer being "carried" by the final answer.

Found in the first live validation of forward_member_answer: the Researcher returned an
intro, 24 file names and a closing sentence; the Coordinator retyped the 24 names under a
different intro and with no closing sentence. Whole-string containment said "not carried"
and the runtime appended the ENTIRE member answer, so the list appeared twice.

The list is the information; the prose around it is framing. A forwarded answer is carried
when it appears verbatim (whitespace, backticks and asterisks aside) or, if it is
list-shaped, when every item appears. Anything else -- a missing item, ordinary prose that
was reworded, an annotated list -- still gets the member's text appended in full, so the
exact-forwarding fallback is unchanged.
"""
from types import SimpleNamespace

from swarm import team as team_mod
from swarm.team import (
    _forwarded_text_carried, _list_items_if_list_shaped, _make_forward_member_answer,
    _with_forwarded_evidence,
)

FILES = [
    "__init__.py", "admin_gst_api.py", "auto_restock_config_api.py", "categories_api.py",
    "custom_attributes_api.py", "godowns_api.py", "gst_compliance_api.py", "hsn_api.py",
    "import_api.py", "inventory_api.py", "inventory_suppliers_api.py", "item_variants_api.py",
    "items_api.py", "parties_api.py", "payments_api.py", "purchase_order_api.py",
    "purchase_order_items_api.py", "smart_alerts_api.py", "stock_api.py",
    "stock_transactions_api.py", "supplier_items_api.py", "tally_import_api.py",
    "uom_api.py", "vouchers_api.py",
]
# The Researcher's real answer from the live run: intro, 24 items, closing sentence.
MEMBER_LIST = (
    "The directory `API/inventory-service/router/` contains 24 files, each corresponding to "
    "a different API endpoint or functionality within the inventory service. Here is the "
    "list of files:\n\n" + "\n".join(f"- `{f}`" for f in FILES)
    + "\n\nEach file likely defines routes and handlers for a specific aspect of the "
      "inventory service, such as managing items, categories, suppliers, and stock "
      "transactions."
)


def _fwd(**members):
    return SimpleNamespace(_forwarded_members=members)


def _count(text, needle="__init__.py"):
    return text.count(needle)


# ── the cases from the live run ──────────────────────────────────────────────────────────

def test_exact_carry_of_the_whole_member_answer_appends_nothing():
    content = "Here you go:\n\n" + MEMBER_LIST + "\n\nAnything else?"
    assert _with_forwarded_evidence(content, _fwd(researcher=MEMBER_LIST)) == content


def test_list_reproduced_under_different_prose_is_not_appended_again():
    """The exact live case: a different intro, the same 24 items, no closing sentence."""
    content = ("The directory `API/inventory-service/router/` contains the following 24 "
               "files, each for a different endpoint:\n\n"
               + "\n".join(f"- `{f}`" for f in FILES))
    out = _with_forwarded_evidence(content, _fwd(researcher=MEMBER_LIST))
    assert out == content
    assert _count(out) == 1
    assert "FORWARDED FROM THE MEMBERS" not in out


def test_partial_list_reproduction_still_appends_the_full_member_answer():
    content = "The router files:\n" + "\n".join(f"- `{f}`" for f in FILES[:20])
    out = _with_forwarded_evidence(content, _fwd(researcher=MEMBER_LIST))
    assert out.startswith(content) and "FORWARDED FROM THE MEMBERS" in out
    appended = out.split("### From researcher\n", 1)[1]
    for missing in FILES[20:]:
        assert missing in appended
    assert appended == MEMBER_LIST.strip()          # full text, unedited


def test_a_single_missing_item_is_enough_to_append():
    content = "\n".join(f"- {f}" for f in FILES if f != "vouchers_api.py")
    out = _with_forwarded_evidence(content, _fwd(researcher=MEMBER_LIST))
    assert "FORWARDED FROM THE MEMBERS" in out
    assert "vouchers_api.py" in out.split("### From researcher\n", 1)[1]


def test_four_item_list_with_two_mentioned_is_appended():
    member = "\n".join(["- a_one.py", "- b_two.py", "- c_three.py", "- d_four.py"])
    out = _with_forwarded_evidence("Found a_one.py and b_two.py.", _fwd(researcher=member))
    assert "FORWARDED FROM THE MEMBERS" in out
    assert "c_three.py" in out and "d_four.py" in out


def test_unrelated_answer_gets_the_member_answer_appended():
    out = _with_forwarded_evidence("I could not determine that.", _fwd(researcher=MEMBER_LIST))
    assert out.startswith("I could not determine that.")
    assert out.endswith(MEMBER_LIST.strip())


def test_the_list_never_appears_twice_and_a_second_pass_changes_nothing():
    retyped = "Router files:\n" + "\n".join(f"{i}. {f}" for i, f in enumerate(FILES, 1))
    once = _with_forwarded_evidence(retyped, _fwd(researcher=MEMBER_LIST))
    assert once == retyped and _count(once, "vouchers_api.py") == 1
    # The fallback path is idempotent too -- the retry sites apply this a second time.
    appended = _with_forwarded_evidence("Nothing useful.", _fwd(researcher=MEMBER_LIST))
    assert _with_forwarded_evidence(appended, _fwd(researcher=MEMBER_LIST)) == appended
    assert _count(appended) == 1


def test_each_forwarded_member_is_judged_independently():
    other = "The reviewer found a race condition in the stock update path and nothing else."
    content = "Files:\n" + "\n".join(f"- `{f}`" for f in FILES)
    out = _with_forwarded_evidence(content, _fwd(researcher=MEMBER_LIST, reviewer=other))
    assert out.startswith(content)
    assert "### From reviewer" in out and other in out
    assert "### From researcher" not in out            # carried by its items

    only_reviewer = "The reviewer found a race condition in the stock update path and nothing else."
    out2 = _with_forwarded_evidence(only_reviewer, _fwd(researcher=MEMBER_LIST, reviewer=other))
    assert "### From researcher" in out2 and "### From reviewer" not in out2


# ── ordinary prose is never treated as a list ────────────────────────────────────────────

def test_reworded_prose_is_still_appended():
    member = ("The auth service signs tokens with HS256 and rotates the key every 24 hours. "
              "Refresh tokens live in Redis under auth_refresh keys.")
    paraphrase = "Tokens are HS256-signed and the key rotates daily; refresh tokens are in Redis."
    out = _with_forwarded_evidence(paraphrase, _fwd(researcher=member))
    assert member in out and "FORWARDED FROM THE MEMBERS" in out
    assert _list_items_if_list_shaped(member) is None


def test_prose_that_merely_mentions_files_is_not_a_list():
    member = "Look at items_api.py, parties_api.py and stock_api.py; they hold the routes."
    assert _list_items_if_list_shaped(member) is None
    out = _with_forwarded_evidence("items_api.py parties_api.py stock_api.py",
                                   _fwd(researcher=member))
    assert member in out


# ── formatting differences do not cause a needless append ────────────────────────────────

def test_bullet_backtick_number_and_tag_differences_do_not_force_an_append():
    formats = ["- `{f}`", "* {f}", "{n}. {f}", "[FILE] {f}", "  -   {f}  ", "`{f}`"]
    for fmt in formats:
        content = "Files:\n" + "\n".join(
            fmt.format(f=f, n=i) for i, f in enumerate(FILES, 1))
        assert _with_forwarded_evidence(content, _fwd(researcher=MEMBER_LIST)) == content, fmt


def test_backtick_only_difference_on_verbatim_prose_is_carried():
    member = "The gate returns ALREADY DONE when a target repeats."
    content = "Summary: The gate returns `ALREADY DONE` when a target repeats."
    assert _with_forwarded_evidence(content, _fwd(researcher=member)) == content
    # a case change is a real difference and is NOT forgiven
    lowered = "Summary: the gate returns ALREADY DONE when a target repeats."
    assert "FORWARDED FROM THE MEMBERS" in _with_forwarded_evidence(lowered, _fwd(researcher=member))


# ── the list check is deliberately narrow ────────────────────────────────────────────────

def test_an_annotated_list_is_not_reduced_to_its_names():
    """Dropping the annotations would lose information, so this is not list-shaped."""
    member = "\n".join(f"- items_api.py: {d}" for d in ("items", "a", "b", "c"))
    assert _list_items_if_list_shaped(member) is None
    assert "items" in _with_forwarded_evidence("items_api.py", _fwd(researcher=member))


def test_a_line_pairing_a_file_with_text_disqualifies_a_list():
    assert _list_items_if_list_shaped(MEMBER_LIST + "\nNote: parties_api.py is deprecated.") is None


def test_too_much_surrounding_prose_disqualifies_a_list():
    many = MEMBER_LIST + "\n" + "\n".join(f"Observation {i}: something specific." for i in range(4))
    assert _list_items_if_list_shaped(many) is None
    assert _list_items_if_list_shaped(MEMBER_LIST + "\n" + ("x" * 500)) is None


def test_the_live_answer_is_list_shaped_with_all_24_items():
    assert _list_items_if_list_shaped(MEMBER_LIST) == FILES


def test_fewer_than_three_items_is_not_a_list():
    assert _list_items_if_list_shaped("- a_b.py\n- c_d.py") is None


def test_an_item_must_appear_as_a_whole_token():
    member = "\n".join(["- items_api.py", "- parties_api.py", "- stock_api.py"])
    inside_longer_names = "inventory_items_api.py all_parties_api.py xstock_api.py"
    out = _with_forwarded_evidence(inside_longer_names, _fwd(researcher=member))
    assert "FORWARDED FROM THE MEMBERS" in out
    exact = "items_api.py, parties_api.py, and stock_api.py."
    assert _with_forwarded_evidence(exact, _fwd(researcher=member)) == exact


def test_sentence_fragments_are_not_bare_items():
    for line in ("Done.", "utils", "e.g.", "Here is the list of files:"):
        assert _list_items_if_list_shaped("\n".join([line] * 6)) is None


def test_carried_helper_agrees_with_the_public_behaviour():
    assert _forwarded_text_carried(MEMBER_LIST, "\n".join(FILES)) is True
    assert _forwarded_text_carried(MEMBER_LIST, "\n".join(FILES[:-1])) is False
    assert _forwarded_text_carried(MEMBER_LIST, "nothing here") is False


# ── the empty/canned guard from the previous fix is untouched ────────────────────────────

def test_empty_and_canned_content_are_still_returned_unchanged():
    team = _fwd(researcher=MEMBER_LIST)
    assert _with_forwarded_evidence("", team) == ""
    canned = team_mod._BUDGET_EXHAUSTED_ANSWER
    assert _with_forwarded_evidence(canned, team) == canned


# ── prompt: the instruction no longer conflicts with "name every item" ───────────────────

def test_forward_instructions_tell_the_coordinator_not_to_list_forwarded_items_again():
    text = " ".join(team_mod._FORWARD_INSTRUCTIONS)
    assert "do not list them again yourself" in text
    assert "name every item" in text          # names the rule it overrides


def test_the_tool_description_says_it_returns_a_receipt_not_the_text():
    tool = _make_forward_member_answer({}, {})
    assert "receipt" in (tool.description or "")
