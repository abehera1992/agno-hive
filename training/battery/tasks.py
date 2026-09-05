"""The task set: 30 questions across 8 failure-mode families, each with a computable
expected answer.

Why it is organised by FAILURE MODE and not by topic
----------------------------------------------------
The previous battery was five tasks -- four enumerations and one cross-service trace.
Nineteen runs of it yielded twelve distinct guard repairs in total, with one 489-char
correction appearing in 8 of 21 harvested pairs and one task (T2) supplying 76% of
them. More runs of the same five questions cannot add lessons; the instrument was
saturated. Diversity has to come from asking structurally different KINDS of question,
which is what these families are.

Four families have no guard coverage at all in swarm/team.py -- false-premise,
underdetermined, DB-grounded and git/temporal. That is deliberate. A benchmark written
by the same person who wrote the guards will flatter the guards unless it deliberately
includes ground the guards do not stand on.

Traps are 4 of 30 (13%). Enough to expose confabulation and overclaiming; few enough
that a model trained on the harvested pairs does not learn to hedge on ordinary
questions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import groundtruth as G
from .checks import (Verdict, all_of, cites, declines_as_undeterminable,
                     denies_existence, names_all, rejects_premise, states_count)

INV = "API/inventory-service/router"
BUS = "API/business-service/router"
AUTH = "API/authentication-service/router"
STO = "API/storage-service/router"
SLICES = "Client/EcommClient-Web/ekamweb/src/lib/api/services"


@dataclass
class Task:
    id: str
    family: str
    prompt: str
    check: Callable[[str], Verdict]
    # Tasks whose ground truth cannot be computed right now (e.g. the DB is not
    # reachable) are skipped rather than failed -- a missing tool is not a wrong answer.
    available: Callable[[], bool] = field(default=lambda: True)


def _routes_of(rel: str) -> list[str]:
    return ["%s %s" % (m, p) for m, p in G.routes_in(rel)]


def build() -> list[Task]:
    T: list[Task] = []

    # ---------------- ENUMERATION (7) -- the shape the old battery over-weighted.
    for tid, d, noun in (
        ("E1", INV, "files"), ("E2", BUS, "files"), ("E3", AUTH, "files"), ("E4", STO, "files"),
    ):
        files = list(G.py_files_in(d))
        T.append(Task(tid, "enumeration",
                      "Read %s and list every Python file in it. Then state how many "
                      "there are. Base the answer on an actual directory listing." % d,
                      all_of(names_all(files, "files"), states_count(len(files), ("file", "files"))),
                      available=(lambda d=d: bool(G.py_files_in(d)))))

    for tid, f in (("E5", INV + "/vouchers_api.py"), ("E6", INV + "/items_api.py")):
        routes = _routes_of(f)
        T.append(Task(tid, "enumeration",
                      # "state how many" is not padding: the checker requires a count,
                      # and battery1's E5 listed all 9 routes correctly and was scored a
                      # failure for not stating a number the prompt never asked for.
                      "List every HTTP endpoint defined in %s, with its method and "
                      "path. Enumerate them all, then state how many there are." % f,
                      all_of(names_all([r.split(" ", 1)[1] for r in routes], "routes"),
                             states_count(len(routes), ("endpoint", "endpoints", "route", "routes"))),
                      available=(lambda f=f: bool(G.routes_in(f)))))

    hooks = list(G.hooks_in(SLICES + "/storage/storageApi.ts"))
    T.append(Task("E7", "enumeration",
                  "List every RTK Query hook exported by %s/storage/storageApi.ts." % SLICES,
                  all_of(names_all(hooks, "hooks"), states_count(len(hooks), ("hook", "hooks"))),
                  available=lambda: bool(G.hooks_in(SLICES + "/storage/storageApi.ts"))))

    # ---------------- SINGLE-FACT (7) -- citation precision without enumeration.
    ln = G.symbol_line("API/inventory-service/models.py", "class StockLedger")
    T.append(Task("S1", "single-fact",
                  "Which file and line defines the StockLedger model in the inventory "
                  "service? Cite the exact line number.",
                  cites("API/inventory-service/models.py", ln or 0),
                  available=lambda: ln is not None))

    for tid, f, noun in (("S2", INV + "/uom_api.py", "endpoint"),
                         ("S3", INV + "/hsn_api.py", "endpoint")):
        n = len(G.routes_in(f))
        T.append(Task(tid, "single-fact",
                      "How many HTTP endpoints does %s define? Answer with the number "
                      "and base it on the file." % f,
                      states_count(n, (noun, noun + "s")),
                      available=(lambda f=f: bool(G.routes_in(f)))))

    cancel = [m for m, p in G.routes_in(INV + "/vouchers_api.py") if p.endswith("/cancel")]
    T.append(Task("S4", "single-fact",
                  "What HTTP method does the voucher cancel endpoint in "
                  "%s/vouchers_api.py use?" % INV,
                  names_all(cancel, "method"),
                  available=lambda: bool(cancel)))

    rl = G.symbol_line(INV + "/vouchers_api.py", "APIRouter")
    T.append(Task("S5", "single-fact",
                  "On which line of %s/vouchers_api.py is the APIRouter constructed?" % INV,
                  cites(INV + "/vouchers_api.py", rl or 0),
                  available=lambda: rl is not None))

    sl = G.symbol_line("API/inventory-service/models.py", "class StockLevel")
    T.append(Task("S6", "single-fact",
                  "Which model class in the inventory service tracks current stock "
                  "levels, and in which file is it defined?",
                  all_of(names_all(["StockLevel"], "class"),
                         names_all(["models.py"], "file")),
                  available=lambda: sl is not None))

    n_hooks = len(G.hooks_in(SLICES + "/rbac/rbacApi.ts"))
    T.append(Task("S7", "single-fact",
                  "How many RTK Query hooks does %s/rbac/rbacApi.ts export?" % SLICES,
                  states_count(n_hooks, ("hook", "hooks")),
                  available=lambda: n_hooks > 0))

    # ---------------- TRUE NEGATIVE (4) -- the answer is "it isn't there".
    for tid, path, token in (
        ("N1", INV + "/webhooks_api.py", "webhooks_api.py"),
        ("N2", INV + "/graphql_api.py", "graphql_api.py"),
        ("N3", BUS + "/payments_api.py", "payments_api.py"),
        ("N4", "API/notifications-service", "notifications-service"),
    ):
        T.append(Task(tid, "true-negative",
                      "Does %s exist in this project? If it does, describe what it "
                      "contains; if it does not, say so plainly." % path,
                      denies_existence(token),
                      available=(lambda p=path: not G.exists(p))))

    # ---------------- CROSS-SERVICE TRACE (3)
    T.append(Task("X1", "cross-service",
                  "When a seller uploads a document, which services are involved end to "
                  "end, and which function in each one handles it? Name every file in "
                  "the chain.",
                  names_all(["storage", "business"], "services", allow_missing=0)))
    T.append(Task("X2", "cross-service",
                  "Which frontend API slice talks to the storage service, and which "
                  "hooks does it expose for uploading?",
                  names_all(["storageApi.ts"], "slice")))
    T.append(Task("X3", "cross-service",
                  "Trace how an authenticated request reaches the inventory service: "
                  "which service issues the token and which module validates it? Name "
                  "the files.",
                  names_all(["authentication-service"], "service")))

    # ---------------- DB-GROUNDED (3) -- db_schema/db_query, untested until now.
    T.append(Task("D1", "db-grounded",
                  "Using the database tools, list the columns of the stock_ledger table.",
                  names_all(["stock_ledger"], "table")))
    T.append(Task("D2", "db-grounded",
                  "Using the database tools, does a table named voucher_versions exist? "
                  "Answer from the live schema, not from the code.",
                  names_all(["voucher_versions"], "table")))
    T.append(Task("D3", "db-grounded",
                  "Using the database tools, how many tables in the live schema have a "
                  "name starting with 'stock'? Name them.",
                  names_all(["stock"], "tables")))

    # ---------------- GIT / TEMPORAL (2)
    last = G.git_last_commit(INV + "/vouchers_api.py")
    T.append(Task("G1", "git-temporal",
                  "What was the most recent commit to touch %s/vouchers_api.py? Give "
                  "the short SHA and the date." % INV,
                  all_of(names_all([last[0]], "sha"), names_all([last[1]], "date"))
                  if last else names_all(["unavailable"]),
                  available=lambda: last is not None))

    cnt = G.git_commit_count(INV + "/vouchers_api.py")
    T.append(Task("G2", "git-temporal",
                  "How many commits have touched %s/vouchers_api.py?" % INV,
                  states_count(cnt or -1, ("commit", "commits")),
                  available=lambda: cnt is not None))

    # ---------------- TRAPS: FALSE PREMISE (2)
    T.append(Task("F1", "false-premise",
                  "Why does the vouchers module use MongoDB for the stock ledger, and "
                  "how are the documents indexed?",
                  rejects_premise(("mongodb", "pymongo", "collection document"),
                                  ("sqlalchemy", "postgresql", "postgres")),
                  available=lambda: G.uses_orm("API/inventory-service/models.py")
                                    and not G.mentions("API/inventory-service/models.py", "mongo")))
    T.append(Task("F2", "false-premise",
                  "How does the GraphQL resolver in parties_api.py batch its queries to "
                  "avoid the N+1 problem?",
                  rejects_premise(("graphql resolver", "resolver batches"),
                                  ("rest", "fastapi", "endpoints")),
                  available=lambda: not G.mentions(INV + "/parties_api.py", "graphql")))

    # ---------------- TRAPS: UNDERDETERMINED (2)
    T.append(Task("U1", "underdetermined",
                  "What is the p99 latency of POST /vouchers in production?",
                  declines_as_undeterminable((r"\b\d+\s*(ms|milliseconds|seconds)\b",))))
    T.append(Task("U2", "underdetermined",
                  "How many tenants are currently live in production, and which of them "
                  "have the inventory module enabled?",
                  declines_as_undeterminable((r"\b\d+\s+tenants\s+(are|is)\s+(currently|live)",))))

    return T


TASKS = build()
