"""AGNOHive code indexer — walks a repo, chunks files, inserts into LightRAG.

Usage:
    python -m indexer.cli --path /path/to/repo --project-id ekam
    python -m indexer.cli --path /path/to/repo --project-id ekam --force
"""
import argparse
import asyncio
from pathlib import Path

from .parser import iter_files, file_hash, chunk_file
from . import tracker


async def run(repo_path: Path, project_id: str, force: bool = False) -> None:
    from lightrag_mcp.rag import get_rag

    print(f"[indexer] scanning {repo_path} ...")
    files = iter_files(repo_path)
    current = {str(f.relative_to(repo_path)): file_hash(f) for f in files}

    old = {} if force else tracker.load(project_id)
    changed, deleted = tracker.diff(old, current)

    if not changed and not deleted:
        print(f"[indexer] '{project_id}': nothing changed.")
        return

    print(f"[indexer] '{project_id}': {len(changed)} to index, {len(deleted)} deleted")

    rag = get_rag(project_id)

    for i, rel in enumerate(changed, 1):
        chunks = chunk_file(repo_path / rel, repo_path)
        if not chunks:
            continue
        for chunk in chunks:
            await rag.ainsert(chunk)
        print(f"  [{i}/{len(changed)}] {rel} ({len(chunks)} chunk{'s' if len(chunks) > 1 else ''})")

    if deleted:
        print(f"  note: {len(deleted)} deleted files skipped (LightRAG does not support removal yet)")

    tracker.save(project_id, current)
    print(f"[indexer] done — {len(changed)} files indexed for '{project_id}'.")


def main() -> None:
    p = argparse.ArgumentParser(description="AGNOHive code indexer")
    p.add_argument("--path", required=True, help="Path to the repository root")
    p.add_argument("--project-id", required=True, help="Project namespace in LightRAG/Qdrant")
    p.add_argument("--force", action="store_true", help="Reindex all files, ignoring cached state")
    args = p.parse_args()

    repo_path = Path(args.path).resolve()
    if not repo_path.is_dir():
        print(f"Error: '{repo_path}' is not a directory")
        return

    asyncio.run(run(repo_path, args.project_id, args.force))


if __name__ == "__main__":
    main()
