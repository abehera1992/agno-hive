"""Phase Z23 (2026-09-28) -- verify_claims' labeled-line-to-path pairing loop
(the "**File:** `x`, **Line:** N" prose form, distinct from the compact `path:N`
form _FILE_LINE_RE handles) always preferred a backward-preceding path over a
closer forward one, and unlike _find_nearby_quote/_find_anchor_symbols it had no
_citation_bounds clamp and no markdown-heading boundary -- so it could reach all
the way back across an entire unrelated section and steal that section's own
citation's path.

Root cause, isolated from the real Z22 T13a incident (2026-09-28): a corrected
answer's Database Tables section ended "...File: `.../migration.py`, Line 26-39",
then a NEW "### Frontend Hooks" section wrote, fully unambiguously, "`useGet
VouchersQuery` at line 15 in `.../page.tsx`" -- the path is right there in the
same sentence. The backward-nearest search still found `migration.py` (116 chars
back, inside the 200-char window) and paired "line 15" to it, because the forward
fallback only ever ran when NO backward candidate existed at all, never when a
nearer forward one did. verify_claims reported a false MISMATCH on a citation
that was exactly right.

A second, independent gap compounded this on the real path: `_BACKTICK_PATH_RE`'s
character class excluded parentheses, so a Next.js App Router route-group path
like `app/(portal)/business/.../page.tsx` -- an idiomatic, real pattern -- was
never recognised as a path anywhere in this file at all (not a labeled-line
candidate, not a `_resolve_path` hint). Both are fixed together: the regex now
allows `()`, and the pairing loop reuses the same `_citation_bounds` clamp,
markdown-heading boundary, and nearest-wins comparison that quote/anchor pairing
already use -- no new mechanism, just applying the proven one consistently.
"""
from tools import verify


def _rg_noop(tok, **k):
    return []


class _FakeRgFiles:
    """Stand-in for `subprocess.run(["rg", "--files", ...])` -- same convention as
    test_verify_content_location.py -- this dev machine does not have `rg` on PATH."""
    def __init__(self, paths: list[str]):
        self.stdout = "\n".join(paths)


def _mock_rg_files(monkeypatch, paths: list[str]):
    monkeypatch.setattr(verify.shutil, "which", lambda name: "rg")
    monkeypatch.setattr(verify.subprocess, "run", lambda *a, **k: _FakeRgFiles(paths))


def _write(tmp_path, rel_path: str, lines: dict[int, str], total: int = 60):
    """A file whose real content matches specific 1-indexed line numbers, filler
    elsewhere -- same convention as test_verify_citation_boundaries.py."""
    p = tmp_path / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    content = [f"// filler {i}" for i in range(1, total + 1)]
    for lineno, text in lines.items():
        content[lineno - 1] = text
    p.write_text("\n".join(content), encoding="utf-8")


# ---- Test 1 -- existing valid citation continues to PASS -------------------

def test_a_normal_labeled_citation_with_no_neighbours_still_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _write(tmp_path, "foo.py", {5: "def handler():"})

    report = verify.verify_claims(
        "The `handler` function is defined in `foo.py` at line 5."
    )

    # Scoped to the CITATIONS section this phase touches -- SYMBOLS depends on a
    # real `rg` binary being on PATH, which this dev machine does not have (same
    # limitation test_verify_content_location.py documents).
    assert "MISMATCH" not in report
    assert "AMBIGUOUS  foo.py" not in report
    assert "LINE 5" in report and "foo.py" in report


# ---- Test 2 -- a genuine mismatch is still caught ---------------------------

def test_a_genuinely_wrong_labeled_citation_is_still_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _write(tmp_path, "foo.py", {5: "def handler():", 40: "def other():"})

    report = verify.verify_claims(
        "The `handler` function is defined in `foo.py` at line 40."
    )

    assert "MISMATCH" in report
    assert "actually appears at line(s) 5" in report


# ---- Test 3 -- shared basename, correct full path: unaffected --------------

def test_shared_basename_with_correct_full_path_is_not_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _write(tmp_path, "app/foo/page.tsx", {10: "export const Foo = () => null;"})
    _write(tmp_path, "app/bar/page.tsx", {10: "export const Bar = () => null;"})

    report = verify.verify_claims(
        "`Foo` is defined in `app/foo/page.tsx` at line 10."
    )

    assert "MISMATCH" not in report
    assert "AMBIGUOUS" not in report


# ---- Test 4 -- shared basename, genuinely ambiguous bare citation ----------

def test_shared_basename_with_no_disambiguating_path_is_reported_ambiguous(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _write(tmp_path, "app/foo/page.tsx", {10: "export const Foo = () => null;"})
    _write(tmp_path, "app/bar/page.tsx", {10: "export const Bar = () => null;"})
    _mock_rg_files(monkeypatch, ["app/foo/page.tsx", "app/bar/page.tsx"])

    report = verify.verify_claims("See `page.tsx` at line 10.")

    assert "AMBIGUOUS" in report
    assert "share that name" in report


# ---- Test 7 (Z22 regression) -- the exact incident shape -------------------

def test_z22_forward_path_in_a_new_section_is_not_stolen_by_the_prior_sections_citation(
        tmp_path, monkeypatch):
    """The exact Z22 T13a shape: a Database Tables section citing a migration
    file, immediately followed by a NEW ### heading and a Frontend Hooks section
    whose own citation gives its path in the same sentence, through a Next.js
    route-group directory (parens)."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _write(tmp_path, "API/inventory-service/migrations/versions/f6a7b8c9.py",
           {26: "op.create_table("})
    _write(tmp_path,
           "Client/EcommClient-Web/ekamweb/src/app/(portal)/business/inventory/vouchers/page.tsx",
           {15: "const { data } = useGetVouchersQuery();",
            16: "const [create] = useCreateVoucherMutation();"})

    answer = (
        "### Database Tables\n\n"
        "- **gstr1_export_log**\n"
        "   - File: `API/inventory-service/migrations/versions/f6a7b8c9.py`, Line 26-39\n\n"
        "### Frontend Hooks\n"
        "The vouchers module uses the following frontend hooks:\n\n"
        "- `useGetVouchersQuery` at line 15 in "
        "`Client/EcommClient-Web/ekamweb/src/app/(portal)/business/inventory/vouchers/page.tsx`\n"
        "- `useCreateVoucherMutation` at line 16 in "
        "`Client/EcommClient-Web/ekamweb/src/app/(portal)/business/inventory/vouchers/page.tsx`\n"
    )

    report = verify.verify_claims(answer)

    assert "MISMATCH" not in report, report
    # The migration file's own citation must still be checked against ITSELF,
    # not silently dropped by the boundary/heading clamp.
    assert "migrations/versions/f6a7b8c9.py" in report


def test_z22_migration_file_citation_still_checked_on_its_own_merits(tmp_path, monkeypatch):
    """Companion to the regression above: if the migration file's OWN cited range
    were wrong, that must still be caught -- the boundary clamp must not make the
    prior citation unfalsifiable while fixing the later one."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(verify, "_rg", _rg_noop)
    _write(tmp_path, "API/inventory-service/migrations/versions/f6a7b8c9.py",
           {5: "op.create_table("})  # NOT at line 26-39
    _write(tmp_path,
           "Client/EcommClient-Web/ekamweb/src/app/(portal)/business/inventory/vouchers/page.tsx",
           {15: "const { data } = useGetVouchersQuery();"})

    answer = (
        "### Database Tables\n\n"
        "- File: `API/inventory-service/migrations/versions/f6a7b8c9.py`, Line 26\n\n"
        "### Frontend Hooks\n"
        "- `useGetVouchersQuery` at line 15 in "
        "`Client/EcommClient-Web/ekamweb/src/app/(portal)/business/inventory/vouchers/page.tsx`\n"
    )

    report = verify.verify_claims(answer)

    # The migration citation's own line is genuinely wrong -- it must still be
    # flagged as BAD (no line 26 in a 5-line-content file), while the unrelated
    # page.tsx citation right after it stays clean.
    assert "f6a7b8c9.py" in report and ("BAD" in report or "MISMATCH" in report)


# ---- Backtick-path regex: Next.js route-group parens ------------------------

def test_backtick_path_regex_now_matches_a_route_group_path():
    m = verify._BACKTICK_PATH_RE.search(
        "See `Client/.../app/(portal)/business/inventory/vouchers/page.tsx` for details."
    )
    assert m is not None
    assert m.group(1) == "Client/.../app/(portal)/business/inventory/vouchers/page.tsx"
