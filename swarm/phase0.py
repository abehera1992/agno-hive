"""Phase 0 — observational instrumentation for the Context Intelligence experiment.

Establishes a BASELINE before any behaviour changes. Nothing here gates, rewrites, or
short-circuits anything: every entry point is wrapped so a bug in this module can cost
a telemetry record and never a task.

What it answers, per delegation:

  * how much context went INTO a member versus how much came back out
  * which files that member actually opened, and which it NAMED in its report
  * the two gaps between those sets, which are different failures:
        cited_not_read  -- named without opening it (inherited from a teammate, or
                           invented; only end-of-run resolution separates the two)
        read_not_cited  -- opened and then dropped from the report, which is the
                           relay loss measured at 9.1:1 on 2026-09-09

Design constraints, set with the reviewer and held to deliberately:

  * NO ContextPack, and no member-context injection. This phase only watches.
  * NO new MCP call per delegation. Cited paths are accumulated as plain strings
    during the run and resolved in ONE batch after the answer is final.
  * NO change to the audit-tag rule. The tag is required only on re-delegation
    (swarm/team.py's duplicate-delegation gate), so a first delegation has no
    authoritative target; a path token is read out of the prose instead and labelled
    `target_source="prose"` so an inferred target can never be read as a fact.
  * An unresolved citation is reported as `unresolved_path_rate`, NOT as a fabrication
    rate. A path this resolver cannot place may still exist -- the same
    unknown-is-not-missing rule the rest of the codebase already follows. Whether
    unresolved means fabricated is a question for the baseline, not an assumption
    built into the measurement.

Ambiguity is its own bucket. A bare basename (`models.py` -- eight files carry that
name in the project this was built against) cannot be resolved to one file, so it is
neither resolved nor unresolved and is excluded from the rate's denominator. The raw
token is preserved in the event so the classification can be re-examined later.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

# Distinct basenames resolved at end of run. One find_files call each, once, after the
# answer is final -- a run that cites more than this many different files gets a
# truncated denominator, recorded as `checked`, rather than a slow shutdown.
_MAX_RESOLVED_BASENAMES = 40
# Per-event list caps. These lists are for reading, not for storage: a member that
# opens 200 files makes the point just as well at 60.
_MAX_LISTED_PATHS = 60


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _extract_paths(text: str) -> list[str]:
    """Distinct path-shaped tokens in `text`, in order of appearance.

    Imported lazily from swarm.team rather than re-implemented: that module owns the
    pattern, it already handles this project's real syntax (route groups `(portal)`,
    dynamic segments `[id]`) after a guard that could not spell them told a model a
    real file did not exist, and a second copy here would drift from it silently.
    A lazy import also keeps swarm.team -> swarm.phase0 from becoming a cycle.
    """
    try:
        from swarm.team import _PATH_TOKEN_RE, _balanced_path
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for raw in _PATH_TOKEN_RE.findall(text or ""):
        tok = _balanced_path(raw.replace("\\", "/"))
        if tok and tok not in out:
            out.append(tok)
    return out


# Tools whose recorded `path` is a file this member actually OPENED. team.py's read
# record stores `relative_path or glob_pattern or pattern`, so the tool name is the
# only thing that separates "opened API/x/models.py" from "searched for createVoucher".
_FILE_OPENING_TOOLS = frozenset({"get_file_content"})
# Takes a path but names a DIRECTORY, so it belongs in neither bucket.
_DIRECTORY_TOOLS = frozenset({"list_directory", "list_directory_tree"})
# Reads a file set the record cannot name: get_files_batch passes `paths` (plural), so
# the stored path is "". Counted rather than dropped -- it is a real blind spot in
# files_read and a silent zero would misrepresent coverage.
_UNATTRIBUTED_READ_TOOLS = frozenset({"get_files_batch"})

# Extensions that make a token a plausible source file. Required for a citation to
# count as a path at all: it is what rejects `@router.get`, `e.g`, `builder.query` and
# `sa.UniqueConstraint`, none of which the previous any-word.word rule could tell from
# a filename.
_SOURCE_EXTS = frozenset({
    "py", "ts", "tsx", "js", "jsx", "mjs", "cjs", "vue", "svelte",
    "md", "rst", "txt", "json", "yaml", "yml", "toml", "ini", "cfg", "env",
    "sql", "scss", "sass", "css", "html", "htm", "xml",
    "sh", "bash", "zsh", "ps1", "dockerfile",
    "go", "rs", "java", "kt", "rb", "php", "cs", "c", "h", "cpp", "hpp", "swift",
})


def classify_citation(token: str) -> str:
    """"path" (has a directory), "basename" (ambiguous), or "not_a_path".

    Extension-first, deliberately. Requiring a separator instead would admit `and/or`
    while rejecting a real bare `vouchers_api.py`; the extension is the property that
    actually distinguishes a filename, and the separator then only decides whether it
    can be resolved to one file.
    """
    tok = (token or "").strip().strip(".,;:)('\"`")
    if not tok or "." not in tok:
        return "not_a_path"
    ext = tok.rsplit(".", 1)[-1].lower()
    if ext not in _SOURCE_EXTS:
        return "not_a_path"
    return "path" if "/" in tok.strip("/") else "basename"


def classify_read(entry: dict) -> tuple[str, str]:
    """(bucket, path) for one team.py read record.

    Buckets: "file" (really opened), "directory", "search" (a pattern, not a file),
    "unattributed" (a batch read whose paths the record does not carry), "empty".
    """
    tool = str((entry or {}).get("tool") or "")
    path = str((entry or {}).get("path") or "").strip()
    if tool in _UNATTRIBUTED_READ_TOOLS:
        return "unattributed", path
    if not path:
        return "empty", path
    if tool in _DIRECTORY_TOOLS:
        return "directory", path
    if tool in _FILE_OPENING_TOOLS:
        return "file", path
    return "search", path


def _is_ambiguous(path: str) -> bool:
    """A bare basename names no directory, so nothing can resolve it to one file."""
    return "/" not in path.strip("/")


class Phase0Run:
    """One run's telemetry. Every public method is best-effort and returns None."""

    def __init__(self, project_id: str, session_id: str | None, team_name: str | None,
                 read_only: bool) -> None:
        self.run_id = uuid.uuid4().hex[:12]
        self.project_id = project_id
        self.session_id = session_id
        self.team_name = team_name
        self.read_only = read_only
        self.started = time.monotonic()
        self.context_blocks: dict = {}
        self.delegations: list[dict] = []
        self.member_results: list[dict] = []
        self._seq = 0

    # ── during the run: append-only, no I/O ──────────────────────────────────────

    def record_context_blocks(self, blocks: dict) -> None:
        """The coordinator's EXISTING context blocks, measured one by one.

        Deliberately not "pack metrics": there is no pack in Phase 0. These are the
        real variables run_task_async already builds, sized individually so the same
        axis can be compared before and after a pack exists.
        """
        try:
            self.context_blocks = {
                k: v for k, v in (blocks or {}).items() if v is not None
            }
        except Exception:  # noqa: BLE001
            pass

    def record_delegation(self, member: str, task_text: str, audit_target: str,
                          result_kind: str, result_preview: str,
                          duration_ms: int) -> None:
        try:
            self._seq += 1
            if audit_target:
                target, source = audit_target, "audit"
                targets_all = [audit_target]
            else:
                # No authoritative target: the audit tuple is only required on a
                # RE-delegation, so a first delegation legitimately carries none.
                targets_all = _extract_paths(task_text)
                target = targets_all[0] if targets_all else None
                source = "prose" if target else "none"
            self.delegations.append({
                "type": "delegation",
                "run_id": self.run_id,
                "seq": self._seq,
                "member": member,
                "target": target,
                "target_source": source,
                "targets_all": targets_all[:_MAX_LISTED_PATHS],
                "member_input_chars": len(task_text or ""),
                # "async_generator" = the member actually ran; "str" = a gate refused
                # it and returned text instead. Recorded rather than interpreted.
                "result_kind": result_kind,
                "result_preview": (result_preview or "")[:120],
                "duration_ms": duration_ms,
            })
        except Exception:  # noqa: BLE001
            pass

    def record_member_result(self, member: str, content: str, read_delta: int,
                             reads: list[dict], elided: bool,
                             thin_report: bool) -> None:
        """`reads` is team.py's own read records ({tool, path, ...}), not a path list.

        Passing the raw entries keeps the file/search distinction here, in one place
        that the emitted record then shows its working for, rather than at the call
        site where it would be invisible.
        """
        try:
            files, dirs, searches = [], [], []
            unattributed = 0
            for entry in (reads or []):
                bucket, path = classify_read(entry)
                if bucket == "unattributed":
                    unattributed += 1
                elif bucket == "file" and path not in files:
                    files.append(path)
                elif bucket == "directory" and path not in dirs:
                    dirs.append(path)
                elif bucket == "search" and path not in searches:
                    searches.append(path)

            cited, cited_basenames, rejected = [], [], []
            for tok in _extract_paths(content):
                kind = classify_citation(tok)
                if kind == "path" and tok not in cited:
                    cited.append(tok)
                elif kind == "basename" and tok not in cited_basenames:
                    cited_basenames.append(tok)
                elif kind == "not_a_path" and tok not in rejected:
                    rejected.append(tok)

            read_set = set(files)
            cited_set = set(cited)
            self.member_results.append({
                "type": "member_result",
                "run_id": self.run_id,
                "member": member,
                "member_output_chars": len(content or ""),
                "output_elided": bool(elided),
                "thin_report": bool(thin_report),
                "read_chars_this_delegation": int(read_delta or 0),
                "files_read": files[:_MAX_LISTED_PATHS],
                "files_cited": cited[:_MAX_LISTED_PATHS],
                "cited_basenames": cited_basenames[:_MAX_LISTED_PATHS],
                # Two different losses, kept apart on purpose: naming what you did not
                # open, versus opening what you did not report. Both now compare
                # file-to-file, so neither is inflated by a search pattern or a dotted
                # code identifier the way the first controlled run's numbers were.
                "cited_not_read": [p for p in cited if p not in read_set][:_MAX_LISTED_PATHS],
                "read_not_cited": [p for p in files if p not in cited_set][:_MAX_LISTED_PATHS],
                # Everything the classifier decided against, so the decision can be
                # audited from the record instead of by re-running.
                "extractor": {
                    "search_patterns": searches[:_MAX_LISTED_PATHS],
                    "directories_listed": dirs[:_MAX_LISTED_PATHS],
                    "batch_reads_unattributed": unattributed,
                    "citations_rejected": rejected[:_MAX_LISTED_PATHS],
                    "counts": {
                        "files": len(files), "directories": len(dirs),
                        "searches": len(searches), "cited_paths": len(cited),
                        "cited_basenames": len(cited_basenames),
                        "rejected": len(rejected),
                    },
                },
            })
        except Exception:  # noqa: BLE001
            pass

    # ── after the answer is final: one batch of lookups, then emit ───────────────

    async def _resolve_cited_paths(self, hive_mcp_url, hive_mcp_tools) -> dict:
        """Resolve every cited path ONCE, at end of run.

        One find_files per distinct BASENAME, not per citation, and capped. A lookup
        that fails contributes nothing rather than counting as unresolved -- unknown is
        not missing, the rule _repo_find_files and _repo_match_count already state, and
        the one that keeps a measurement honest when the tool is simply unavailable.
        """
        concrete: list[str] = []
        ambiguous: list[str] = []
        for mr in self.member_results:
            for p in mr.get("files_cited") or []:
                if p not in concrete:
                    concrete.append(p)
            # Real filenames that name no directory. Classified upstream now, so this
            # bucket no longer collects `@router.get` and `builder.query`.
            for p in mr.get("cited_basenames") or []:
                if p not in ambiguous:
                    ambiguous.append(p)

        resolved: list[str] = []
        unresolved: list[str] = []
        unknown: list[str] = []
        if concrete and (hive_mcp_url or hive_mcp_tools):
            try:
                from swarm.team import _repo_find_files
                by_base: dict[str, list[str] | None] = {}
                for path in concrete:
                    base = path.rsplit("/", 1)[-1]
                    if base in by_base:
                        continue
                    if len(by_base) >= _MAX_RESOLVED_BASENAMES:
                        break
                    by_base[base] = await _repo_find_files(
                        f"**/{base}", hive_mcp_url, hive_mcp_tools)
                for path in concrete:
                    base = path.rsplit("/", 1)[-1]
                    found = by_base.get(base)
                    if base not in by_base or found is None:
                        unknown.append(path)      # not looked up, or lookup failed
                    elif any(c == path.strip("/") or c.endswith("/" + path.strip("/"))
                             for c in found):
                        resolved.append(path)
                    else:
                        unresolved.append(path)
            except Exception:  # noqa: BLE001
                unknown = list(concrete)
                resolved, unresolved = [], []
        else:
            unknown = list(concrete)

        denom = len(resolved) + len(unresolved)
        return {
            "checked": denom,
            "resolved": resolved[:_MAX_LISTED_PATHS],
            "unresolved": unresolved[:_MAX_LISTED_PATHS],
            # Preserved raw, per the reviewer's decision: excluded from the rate, not
            # discarded, so the classification itself can be re-examined.
            "ambiguous": ambiguous[:_MAX_LISTED_PATHS],
            "unknown": unknown[:_MAX_LISTED_PATHS],
            "counts": {
                "resolved": len(resolved), "unresolved": len(unresolved),
                "ambiguous": len(ambiguous), "unknown": len(unknown),
            },
            # NOT a fabrication rate. An unresolved citation is one this resolver could
            # not place; whether that means invented is what the baseline is for.
            "unresolved_path_rate": (len(unresolved) / denom) if denom else None,
        }

    async def finalize(self, answer_chars: int, read_chars_total: int,
                       member_result_chars: int, outcome: str,
                       hive_mcp_url=None, hive_mcp_tools=None,
                       commit: str = "") -> None:
        try:
            resolution = await self._resolve_cited_paths(hive_mcp_url, hive_mcp_tools)
            run_event = {
                "type": "run",
                "run_id": self.run_id,
                "project_id": self.project_id,
                "session_id": self.session_id,
                "team": self.team_name,
                "read_only": self.read_only,
                "commit": commit,
                "context_blocks": self.context_blocks,
                "delegations": len(self.delegations),
                "read_chars_total": int(read_chars_total or 0),
                "member_result_chars": int(member_result_chars or 0),
                "answer_chars": int(answer_chars or 0),
                "relay_ratio": (round(read_chars_total / member_result_chars, 2)
                                if member_result_chars else None),
                "path_resolution": resolution,
                "unresolved_path_rate": resolution.get("unresolved_path_rate"),
                "duration_s": round(time.monotonic() - self.started, 1),
                "outcome": outcome,
            }
            for ev in self.delegations + self.member_results + [run_event]:
                _emit(ev)
        except Exception as exc:  # noqa: BLE001
            print(f"[phase0] finalize skipped ({type(exc).__name__}: {exc})", flush=True)


def _jsonl_path() -> Path | None:
    """Where the durable record goes.

    Defaults under the repo's existing `data/` directory -- the same runtime location
    swarm/db.py already puts its SQLite files in -- rather than inventing a new storage
    convention. PHASE0_TELEMETRY_PATH overrides it for a deployment that keeps state
    elsewhere.
    """
    override = os.getenv("PHASE0_TELEMETRY_PATH", "").strip()
    if override:
        return Path(override)
    return _repo_root() / "data" / "phase0" / "delegations.jsonl"


def _emit(event: dict) -> None:
    """One record to stdout (journald, survives a SIGKILLed worker's exit) and one
    appended to the JSONL artifact (durable across log rotation). Either may fail
    without affecting the other, and neither may affect the run."""
    line = None
    try:
        line = json.dumps(event, default=str)
        print(f"[phase0] {line}", flush=True)
    except Exception:  # noqa: BLE001
        return
    try:
        path = _jsonl_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        print(f"[phase0] jsonl write skipped ({type(exc).__name__}: {exc})", flush=True)


def enabled() -> bool:
    """Off unless explicitly enabled. Instrumentation that nobody switched on should
    cost nothing at all, and a baseline run is a deliberate act."""
    return os.getenv("PHASE0_TELEMETRY", "").strip().lower() in ("1", "true", "yes", "on")


def start_run(project_id: str, session_id: str | None, team_name: str | None,
              read_only: bool) -> "Phase0Run | None":
    if not enabled():
        return None
    try:
        run = Phase0Run(project_id, session_id, team_name, read_only)
        print(f"[phase0] run {run.run_id} started "
              f"(project={project_id}, team={team_name}, read_only={read_only})",
              flush=True)
        return run
    except Exception:  # noqa: BLE001
        return None
