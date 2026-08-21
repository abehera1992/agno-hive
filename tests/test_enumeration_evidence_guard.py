"""Tests: an enumeration answer must be backed by an actual directory listing.

Measured 2026-08-21 across four probes that needed enumeration: `list_directory`
was called ZERO times in every one, including a probe whose prompt named the tool
outright. What shipped instead:

  * "the directory contains exactly one file: `__init__.py`"      (it holds six)
  * "the router files that do exist are: items, vouchers, categories"  (24 exist)
  * "the entire frontend layer for Parties is missing"            (it exists:
    useGetPartiesQuery / useCreatePartyMutation, inventoryApi.ts:933-935)

This ranks above the stalls it sits beside: a stall fails loudly and the watchdog
catches it, whereas this ships a confident, well-formatted, wrong inventory. In
the Parties case `verify_claims` printed the contradicting evidence (FOUND Parties
-> inventoryApi.ts:244) in its own report and the answer shipped regardless.

A tool hook cannot catch it — the failure is the model ANSWERING without calling
anything, and answering is not a tool call. Hence an answer-time guard, mirroring
the db-evidence guard exactly.
"""
import pytest

from swarm.team import _ENUM_TOOLS, _is_enumeration_task


# ── trigger: the real failing prompts must match ──────────────────────────────

@pytest.mark.parametrize("task", [
    "Read API/business-service/router/ and list every Python file in it.",
    "Call list_directory on API/inventory-service/router/ and report the EXACT number",
    "How many files are in the router directory?",
    "name what router files DO exist in that directory",   # T11's real wording
    "enumerate the services under API/",
    "what is in the directory API/inventory-service/router/",
])
def test_real_enumeration_prompts_trigger(task):
    if not _is_enumeration_task(task):
        pytest.fail(f"missed an enumeration prompt: {task!r}")


@pytest.mark.parametrize("task", [
    "Which line defines the sku_prefix column, and what is its type?",
    "What HTTP status does _check_party_limit raise?",
    "Does a column named gstin exist on any model? Cite the line.",
    "Add a comment at the top of models.py saying reviewed.",
    "Compare the backend endpoints with the frontend hooks.",
    "Does the file API/inventory-service/router/gst_api.py exist?",
])
def test_non_enumeration_prompts_do_not_trigger(task):
    """Narrowness matters as much as coverage. Every one of these PASSED the battery
    on its own path; a false positive here spends a whole extra pipeline turn."""
    assert not _is_enumeration_task(task), f"false positive on: {task!r}"


# ── the tool set ──────────────────────────────────────────────────────────────

def test_the_enum_tools_are_the_ones_that_actually_enumerate():
    assert _ENUM_TOOLS == {"list_directory", "list_directory_tree", "find_files"}


def test_reading_one_file_does_not_count_as_enumerating():
    """The exact 2026-08-21 failure: a run read `__init__.py`, found it empty, and
    concluded the directory holds one file. Reading is not listing."""
    assert "get_file_content" not in _ENUM_TOOLS
    assert "search_files" not in _ENUM_TOOLS


# ── the counting path it depends on ───────────────────────────────────────────

def test_count_read_calls_accepts_the_enum_tool_filter():
    """The guard is only cheap because _count_read_calls already takes a tool filter
    (added 2026-08-20 for the db-evidence guard) — no new counting machinery."""
    import inspect

    from swarm.team import _count_read_calls

    assert "tool_names" in inspect.signature(_count_read_calls).parameters


def test_the_guard_mirrors_the_db_evidence_guard():
    """Structural pin: retry once, then a hard disclaimer if the retry ALSO produced no
    enumeration. Silently accepting a non-compliant retry is the exact bug that had to
    be fixed in the retry-adjudication work (5748e28)."""
    import inspect

    from swarm import team

    src = inspect.getsource(team._verified_answer)
    guard = src[src.index("Enumeration-evidence check"):]

    assert "_is_enumeration_task(task)" in guard
    assert "_adopt_retry(\"enumeration\"" in guard
    assert "NOT VERIFIED BY A DIRECTORY LISTING" in guard


def test_the_disclaimer_names_the_real_measured_error():
    """A disclaimer that says "may be inaccurate" gets skimmed. This one carries the
    measured magnitude, because that is what makes a reader check."""
    import inspect

    from swarm import team

    src = inspect.getsource(team._verified_answer)

    assert "recalled rather than" in src
    assert "6-8x" in src


# ── file-contents questions must NOT demand a directory listing ───────────────

@pytest.mark.parametrize("task", [
    "How many @router endpoints are defined in API/inventory-service/router/uom_api.py?",
    "list every function defined in swarm/team.py",
    "how many tests are in tests/test_verify.py",
])
def test_a_file_contents_question_is_not_an_enumeration_task(task):
    """Live false positive, 2026-08-21. The uom_api.py prompt matched on "how many"
    and the guard demanded list_directory/find_files evidence — but this asks what is
    IN A FILE, where get_file_content is correct and sufficient and a directory listing
    proves nothing. A correct answer (3 endpoints, exact methods and paths, read from
    the file) shipped carrying "NOT VERIFIED BY A DIRECTORY LISTING".

    Not merely noisy: the guard also forces a RETRY, so a false positive spends a
    pipeline turn and risks the documented case where an un-grounded re-run overwrites
    a correct answer."""
    assert not _is_enumeration_task(task)


def test_a_directory_question_still_counts_even_when_a_filename_appears():
    """T11's real shape — a single-file existence check AND a directory enumeration in
    one prompt. The filename must not suppress the directory half."""
    task = ("Does the file API/inventory-service/router/gst_api.py exist? If no, say so "
            "plainly and name what router files DO exist in that directory.")
    assert _is_enumeration_task(task)


def test_a_bare_extension_mention_is_not_a_file_target():
    """"report the EXACT number of .py files" names no file — requiring a name part
    before the dot is what keeps this a directory question."""
    task = "Call list_directory on API/inventory-service/router/ and report the EXACT number of .py files"
    assert _is_enumeration_task(task)
