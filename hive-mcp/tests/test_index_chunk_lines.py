"""Regression tests: indexed chunks must carry real line numbers.

Confirmed 2026-08-03 via a live groundedness test against the ekam project: asked to
cite the line of a class definition, the swarm answered from a LightRAG-retrieved
chunk with no line info in it at all, and fabricated a plausible-looking number --
off by a consistent ~155 lines across three separate citations in the same answer.
The chunk text is the model's only view of the file when it answers from retrieval
instead of a live get_file_content() call; without a real line anchor embedded in
that text, it had nothing honest to cite and guessed instead.

Fix: _py_chunks() and _text_chunks() now embed a "Lines: N-M" header in every chunk,
sourced from the AST node's own lineno/end_lineno (for Python) or a newline count at
the chunk's character offset (for generic text).
"""
from pathlib import Path

from tools import index


def test_py_chunks_class_and_function_carry_real_line_numbers(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "PROJECT_ROOT", tmp_path)

    f = tmp_path / "sample.py"
    f.write_text(
        "\n".join([
            '"""Module docstring."""',      # line 1
            "",                              # line 2
            "",                              # line 3
            "class Party(Base):",            # line 4
            '    """L1 entity."""',          # line 5
            "    name = None",                # line 6
            "",                               # line 7
            "",                               # line 8
            "def helper():",                  # line 9
            '    """Do a thing."""',          # line 10
            "    return 1",                   # line 11
        ]),
        encoding="utf-8",
    )

    chunks = index._py_chunks(f)

    class_chunk = next(c for c in chunks if "Name: Party" in c)
    assert "Lines: 4-6" in class_chunk

    func_chunk = next(c for c in chunks if "Name: helper" in c)
    assert "Lines: 9-11" in func_chunk

    module_chunk = next(c for c in chunks if "Type: module" in c)
    assert "Lines: 1-1" in module_chunk


def test_text_chunks_carry_approximate_line_ranges(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(index, "_CHUNK_SIZE", 20)  # force multiple small chunks

    f = tmp_path / "notes.md"
    # 5 lines, well under 20 chars each so chunk boundaries land predictably
    f.write_text("aaaa\nbbbb\ncccc\ndddd\neeee\n", encoding="utf-8")

    chunks = index._text_chunks(f)
    assert len(chunks) >= 2
    for c in chunks:
        assert "Lines: " in c.splitlines()[2]

    first_header = chunks[0].splitlines()[2]
    assert first_header.startswith("Lines: 1-")
