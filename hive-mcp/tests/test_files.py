from tools import files


def _setup(tmp_path, monkeypatch, initial_text):
    # files.py does `from config import PROJECT_ROOT, WRITE_REVIEW` (direct name
    # import) — monkeypatching config.PROJECT_ROOT has NO effect here. Patch the
    # module-local bound names directly.
    monkeypatch.setattr(files, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(files, "WRITE_REVIEW", False)
    f = tmp_path / "sample.py"
    f.write_text(initial_text, encoding="utf-8")
    files._last_failed_call.clear()
    files._consecutive_failures.clear()
    return f


def _setup_scss(tmp_path, monkeypatch, initial_text):
    monkeypatch.setattr(files, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(files, "WRITE_REVIEW", False)
    f = tmp_path / "sample.module.scss"
    f.write_text(initial_text, encoding="utf-8")
    files._last_failed_call.clear()
    files._consecutive_failures.clear()
    return f


def test_apply_diff_failure_includes_near_match_hint(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "def foo():\n    status_filter = 'active'\n    return status_filter\n")

    result = files.apply_diff("sample.py", "status_filter = 'inactive'", "status_filter = 'archived'")

    assert "old_string not found" in result
    assert "status_filter = 'active'" in result  # the near-match hint shows the real line


def test_apply_diff_no_hint_when_nothing_close(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "def foo():\n    return 1\n")

    result = files.apply_diff("sample.py", "completely unrelated text with no resemblance", "x")

    assert "old_string not found" in result
    assert "Closest existing text" not in result


def test_apply_diff_second_identical_failure_hard_stops(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "def foo():\n    return 1\n")

    first = files.apply_diff("sample.py", "does not exist", "replacement")
    second = files.apply_diff("sample.py", "does not exist", "replacement")

    assert "old_string not found" in first
    assert "STOPPED" in second


def test_apply_diff_success_clears_failure_tracking(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "def foo():\n    return 1\n")

    files.apply_diff("sample.py", "does not exist", "replacement")       # fails, tracked
    files.apply_diff("sample.py", "return 1", "return 2")                # succeeds, clears tracking
    third = files.apply_diff("sample.py", "does not exist", "replacement")  # same failure again

    assert "STOPPED" not in third


def test_apply_diff_different_failure_after_first_is_not_treated_as_repeat(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, "def foo():\n    return 1\n")

    first = files.apply_diff("sample.py", "does not exist", "replacement")
    second = files.apply_diff("sample.py", "also does not exist", "other replacement")

    assert "STOPPED" not in second


def test_apply_diff_repeated_append_via_untouched_anchor_is_refused(tmp_path, monkeypatch):
    """Confirmed live 2026-08-05: a model appending a new mutation after an
    EXISTING, untouched sibling (updateParty) reused that same anchor across 5
    separate apply_diff calls -- each one succeeded (the anchor's own text never
    changes, so count==1 every time) and each one prepended another copy of the
    same block right after the anchor. 5 duplicate insertions landed before a
    human ever saw the file. The count>1 duplicate guard cannot catch this: it
    only looks at how many times old_string appears BEFORE this call, and the
    anchor here is deliberately something the insertion itself never touches.
    """
    anchor = "updateParty: builder.mutation(...),"
    _setup(tmp_path, monkeypatch, f"const api = {{\n  {anchor}\n\n  // next symbol\n}}\n")

    addition = "\n\n  deleteParty: builder.mutation(...),"
    first = files.apply_diff("sample.py", anchor, anchor + addition)
    second = files.apply_diff("sample.py", anchor, anchor + addition)  # same anchor, same call again

    assert "applied" in first
    assert "REFUSED" in second
    content = (tmp_path / "sample.py").read_text(encoding="utf-8")
    assert content.count("deleteParty") == 1  # only the first insertion landed


def test_apply_diff_bare_line_number_old_string_gets_specific_hint(tmp_path, monkeypatch):
    """Confirmed live 2026-08-05: a Coder mistook get_file_content's
    'LINENUM<TAB>content' citation-numbering for real file content and passed
    old_string values like '129\\n}' -- the line number itself, not the code
    after it. The generic near-match hint doesn't name this specific mistake;
    a model repeating it needs to be told exactly what's wrong."""
    _setup(tmp_path, monkeypatch, "def foo():\n    return 1\n")

    result = files.apply_diff("sample.py", "129\n}", "129\n}\nmore")

    assert "old_string not found" in result
    assert "LINENUM" in result or "line-number" in result or "line number" in result


def test_apply_diff_same_line_bare_line_number_also_gets_specific_hint(tmp_path, monkeypatch):
    """Confirmed live 2026-08-05, same run as the two-line case above: the FIRST
    apply_diff call in that run used the two-line form ('192\\n\\n193\\t.empty {...')
    and correctly got the hint, but the SECOND call rendered the same underlying
    mistake on a single line ('495  }' -- line number, spaces where a tab collapsed,
    then real content, no newline in between) and the original newline-only regex
    missed it entirely, silently falling back to the generic hint-less message."""
    _setup(tmp_path, monkeypatch, "def foo():\n    return 1\n")

    result = files.apply_diff("sample.py", "495  }", "495  }\nmore")

    assert "old_string not found" in result
    assert "LINENUM" in result or "line-number" in result or "line number" in result


def test_apply_diff_hallucinated_placeholder_comment_gets_specific_hint(tmp_path, monkeypatch):
    """Confirmed live 2026-08-06: two separate failed apply_diff attempts, in two
    different retries of the SAME task, used old_string='/* Add statusBadge class
    here */' and later old_string='// Add status badge style here' -- neither
    comment exists anywhere in the real file; both were invented as a plausible
    anchor rather than read."""
    _setup(tmp_path, monkeypatch, ".badge {\n  display: flex;\n}\n")

    result = files.apply_diff(
        "sample.py",
        "/* Add statusBadge class here */",
        "/* Add statusBadge class here */\n.statusBadge {}",
    )

    assert "old_string not found" in result
    assert "invented placeholder comment" in result


def test_apply_diff_hallucinated_line_comment_placeholder_also_gets_hint(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, ".badge {\n  display: flex;\n}\n")

    result = files.apply_diff(
        "sample.py",
        "// Add status badge style here",
        "// Add status badge style here\n.statusBadge {}",
    )

    assert "old_string not found" in result
    assert "invented placeholder comment" in result


def test_apply_diff_real_comment_does_not_trigger_placeholder_hint(tmp_path, monkeypatch):
    """The regex must not flag ordinary, real comments -- only instructional-sounding
    ones ("add X here", "TODO: implement") that this project's files never actually
    contain."""
    _setup(tmp_path, monkeypatch, ".badge {\n  display: flex;\n}\n")

    result = files.apply_diff(
        "sample.py",
        "// eslint-disable-next-line no-unused-vars",
        "// eslint-disable-next-line no-unused-vars\nx = 1",
    )

    assert "old_string not found" in result
    assert "invented placeholder comment" not in result


def test_apply_diff_hard_stops_after_many_distinct_failures(tmp_path, monkeypatch):
    """The exact-repeat STOPPED check (above) only catches the SAME old_string
    tried twice in a row. Confirmed live 2026-08-05: a Coder varied its guess
    on every single call ('129\\n}', '133\\n}', '134\\n}', ...) and ran past 90
    consecutive failed apply_diff calls against the same file because no two
    consecutive guesses were ever identical. This counter must trip regardless
    of whether the failing strings repeat."""
    _setup(tmp_path, monkeypatch, "def foo():\n    return 1\n")

    results = [
        files.apply_diff("sample.py", f"guess{i}", f"guess{i}\nmore")
        for i in range(6)
    ]

    assert all("HARD STOP" not in r for r in results[:5])
    assert "HARD STOP" in results[5]


def test_apply_diff_genuinely_new_content_after_same_anchor_is_allowed(tmp_path, monkeypatch):
    """The refusal must be specific to REPEATING the same net addition -- appending
    a second, DIFFERENT thing after the same anchor is a legitimate, common pattern
    (e.g. two separate apply_diff calls each adding a different new mutation right
    after the same reference point) and must not be blocked."""
    anchor = "updateParty: builder.mutation(...),"
    _setup(tmp_path, monkeypatch, f"const api = {{\n  {anchor}\n\n  // next symbol\n}}\n")

    first = files.apply_diff("sample.py", anchor, anchor + "\n\n  deleteParty: builder.mutation(...),")
    second = files.apply_diff("sample.py", anchor, anchor + "\n\n  archiveParty: builder.mutation(...),")

    assert "REFUSED" not in second
    content = (tmp_path / "sample.py").read_text(encoding="utf-8")
    assert "deleteParty" in content


def test_apply_diff_refuses_a_second_rule_under_an_existing_scss_selector(tmp_path, monkeypatch):
    """Confirmed live 2026-08-06: a citation-correction retry staged a second
    '.statusBadge { ... }' rule using DIFFERENT color variables right after an
    existing, already-correct one -- two rules, same selector, neither textually
    identical to the other, so the net-addition duplicate check (above) never
    fired. This is a NAME collision, not a text collision."""
    _setup_scss(
        tmp_path, monkeypatch,
        ".badge {\n  display: inline-flex;\n}\n\n"
        ".statusBadge {\n  background: index.$success-bg;\n  color: index.$success;\n}\n",
    )

    result = files.apply_diff(
        "sample.module.scss",
        ".badge {\n  display: inline-flex;\n}",
        ".badge {\n  display: inline-flex;\n}\n\n"
        ".statusBadge {\n  background: index.$accent-bg-muted;\n  color: index.$accent-deep;\n}",
    )

    assert "REFUSED" in result
    assert ".statusBadge" in result
    content = (tmp_path / "sample.module.scss").read_text(encoding="utf-8")
    assert content.count(".statusBadge {") == 1  # nothing landed


def test_apply_diff_allows_editing_the_existing_selector_in_place(tmp_path, monkeypatch):
    """The name-collision guard must not block a genuine in-place edit -- old_string
    already contains the selector being replaced, so it must be excluded from the
    "existing elsewhere" check or every real fix would be refused too."""
    _setup_scss(
        tmp_path, monkeypatch,
        ".statusBadge {\n  background: $success-bg;\n  color: $success;\n}\n",
    )

    result = files.apply_diff(
        "sample.module.scss",
        ".statusBadge {\n  background: $success-bg;\n  color: $success;\n}",
        ".statusBadge {\n  background: index.$success-bg;\n  color: index.$success;\n}",
    )

    assert "REFUSED" not in result
    content = (tmp_path / "sample.module.scss").read_text(encoding="utf-8")
    assert content.count(".statusBadge {") == 1
    assert "index.$success-bg" in content


def test_apply_diff_allows_a_genuinely_new_selector_name(tmp_path, monkeypatch):
    _setup_scss(tmp_path, monkeypatch, ".badge {\n  display: inline-flex;\n}\n")

    result = files.apply_diff(
        "sample.module.scss",
        ".badge {\n  display: inline-flex;\n}",
        ".badge {\n  display: inline-flex;\n}\n\n.statusBadge {\n  color: red;\n}",
    )

    assert "REFUSED" not in result


def test_apply_diff_name_collision_check_skips_non_scss_files(tmp_path, monkeypatch):
    """Scoped to .scss/.sass only -- a .foo { ... } -shaped string in some other
    language is not this check's business."""
    _setup(tmp_path, monkeypatch, ".statusBadge {\n  x = 1\n}\n")

    result = files.apply_diff(
        "sample.py",
        ".statusBadge {\n  x = 1\n}",
        ".statusBadge {\n  x = 1\n}\n\n.statusBadge {\n  y = 2\n}",
    )

    assert "REFUSED" not in result
