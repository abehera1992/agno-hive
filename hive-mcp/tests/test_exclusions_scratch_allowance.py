"""Regression/design test: is_excluded() must NOT reject hive-mcp's own scratch
directory, or get_file_content() (which calls is_excluded() before serving any
path -- see test_context_exclusions.py) would refuse to read back a scratch file
it just wrote, making the whole offload feature (tools/scratch.py) useless the
moment a model tries to read more than the preview.

Confirmed live 2026-08-14 while designing the feature, before writing scratch.py
itself: is_excluded()'s existing dot-directory rule ("Leading dot-directories are
skipped, but NOT dotfiles at the root") rejects ANY directory component starting
with "." -- .hive_scratch/ would hit this exactly the same as .git/ or a stray
.venv/, even though it's a hive-owned, intentionally-readable directory, not a
vendored/generated one. Needs an explicit, narrow exception -- not a project
EXCLUDE_ALLOW entry, since this isn't project-specific configuration, it's a
hive-mcp implementation detail that must always work regardless of project.
"""
from tools import exclusions


def test_scratch_directory_itself_is_not_excluded():
    assert exclusions.is_excluded(".hive_scratch") is False


def test_file_inside_scratch_directory_is_not_excluded():
    assert exclusions.is_excluded(".hive_scratch/20260814T120000-run_command-1234.txt") is False


def test_nested_path_inside_scratch_directory_is_not_excluded():
    assert exclusions.is_excluded(".hive_scratch/sub/whatever.txt") is False


def test_other_dot_directories_are_still_excluded():
    """The fix must be narrowly scoped to .hive_scratch specifically -- it must not
    accidentally reopen .git/, .venv/, or any other genuinely off-limits dot-dir."""
    assert exclusions.is_excluded(".git/config") is True
    assert exclusions.is_excluded(".venv/lib/site-packages/foo.py") is True


def test_a_directory_merely_starting_with_the_same_prefix_is_still_excluded():
    """.hive_scratch_evil/ (or similar) must not slip through via a loose prefix
    match -- only the exact directory name is allowed."""
    assert exclusions.is_excluded(".hive_scratchpad/x.txt") is True


def test_root_level_dotfile_behavior_is_unchanged():
    """Pre-existing behavior (dotfiles AT the root, like .gitignore, are readable)
    must not regress."""
    assert exclusions.is_excluded(".gitignore") is False
