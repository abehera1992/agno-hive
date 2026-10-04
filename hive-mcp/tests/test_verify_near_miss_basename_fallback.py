"""Phase T3/T11 — verify_claims' _near_miss_hint basename fallback.

Proven live (T11, 2026-10-04): a claimed path missing an entire intermediate
directory level (API/storage-service/admin_api.py for the real
API/storage-service/router/admin_api.py) got NO useful hint -- the existing
fuzzy-sibling-match tier compared "admin_api.py" against the siblings of
API/storage-service/ (main.py, config.py, ...) and returned the nearest
STRING match (main.py), which is a real file but the WRONG one. This adds a
conservative fallback, tried only for the path's final segment and only when
exactly one file with that exact basename exists anywhere else in the
project -- the same one-candidate-only rule context.py's own
_find_by_basename fallback (used by get_file_content) already applies for
this identical mistake shape.
"""
from tools import context, verify


def test_missing_intermediate_directory_is_corrected_to_the_real_path(tmp_path, monkeypatch):
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    router_dir = tmp_path / "API" / "storage-service" / "router"
    router_dir.mkdir(parents=True)
    (router_dir / "admin_api.py").write_text("# real file", encoding="utf-8")
    (tmp_path / "API" / "storage-service" / "main.py").write_text("# unrelated", encoding="utf-8")

    hint = verify._near_miss_hint("API/storage-service/admin_api.py")

    assert "API/storage-service/router/admin_api.py" in hint
    assert "main.py" not in hint


def test_existing_sibling_typo_correction_is_unaffected(tmp_path, monkeypatch):
    """Regression guard: the original 'routers/ vs router/' directory-name-typo
    case (the reason this function exists at all) must still take the fuzzy
    sibling-match path, not the new basename fallback."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    router_dir = tmp_path / "API" / "inventory-service" / "router"
    router_dir.mkdir(parents=True)
    (router_dir / "items_api.py").write_text("# real file", encoding="utf-8")

    hint = verify._near_miss_hint("API/inventory-service/routers/items_api.py")

    assert "did you mean: router" in hint


def test_ambiguous_basename_is_not_suggested(tmp_path, monkeypatch):
    """Two files share the basename -- same conservatism as get_file_content's
    own fallback: do not guess which one, so no hint is produced."""
    monkeypatch.setattr(verify, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(context, "PROJECT_ROOT", tmp_path)
    (tmp_path / "API" / "service-a" / "router").mkdir(parents=True)
    (tmp_path / "API" / "service-a" / "router" / "admin_api.py").write_text("a", encoding="utf-8")
    (tmp_path / "API" / "service-b" / "router").mkdir(parents=True)
    (tmp_path / "API" / "service-b" / "router" / "admin_api.py").write_text("b", encoding="utf-8")

    hint = verify._near_miss_hint("API/storage-service/admin_api.py")

    assert hint == ""


def test_genuinely_nonexistent_file_still_gets_no_hint():
    """T11's OTHER error: a path that does not exist anywhere (sellers_api.py)
    must still be reported as a plain, unhinted NOT FOUND -- verification stays
    authoritative; the fallback must never manufacture a false correction for a
    claim that is genuinely fabricated."""
    hint = verify._near_miss_hint(
        "API/business-service/router/this_file_does_not_exist_anywhere_12345.py"
    )

    assert hint == ""
