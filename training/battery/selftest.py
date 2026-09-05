"""Does each checker accept a correct answer and reject a wrong one?

A checker that can never pass is worse than no checker: it reports a real answer as a
failure and sends the whole effort chasing a model problem that is not there. A checker
that can never FAIL is the mirror image and is how a guard ships inert. Both were live
failures in this codebase within one day, so the task set proves each direction before
it is used to score anything.

The "correct" answers here are synthesised from groundtruth.py, so this also proves the
ground truth is expressible as an answer a model could actually write.

    python -m training.battery.selftest
"""

from __future__ import annotations

from . import groundtruth as G
from .tasks import TASKS, INV, BUS, AUTH, STO, SLICES

# A plausible correct answer per task, built from the same facts the checker reads.
def _perfect() -> dict[str, str]:
    def files(d):
        fs = G.py_files_in(d)
        return "The directory %s contains %d Python files: %s." % (d, len(fs), ", ".join(fs))

    def routes(f):
        rs = G.routes_in(f)
        return ("%s defines %d endpoints: " % (f, len(rs))
                + "; ".join("%s %s" % (m, p) for m, p in rs))

    hooks = G.hooks_in(SLICES + "/storage/storageApi.ts")
    rbac_n = len(G.hooks_in(SLICES + "/rbac/rbacApi.ts"))
    last = G.git_last_commit(INV + "/vouchers_api.py")
    cnt = G.git_commit_count(INV + "/vouchers_api.py")
    cancel = [m for m, p in G.routes_in(INV + "/vouchers_api.py") if p.endswith("/cancel")]

    return {
        "E1": files(INV), "E2": files(BUS), "E3": files(AUTH), "E4": files(STO),
        "E5": routes(INV + "/vouchers_api.py"), "E6": routes(INV + "/items_api.py"),
        "E7": "It exports %d hooks: %s." % (len(hooks), ", ".join(hooks)),
        "S1": "Defined at API/inventory-service/models.py:%d." % G.symbol_line(
            "API/inventory-service/models.py", "class StockLedger"),
        "S2": "It defines %d endpoints." % len(G.routes_in(INV + "/uom_api.py")),
        "S3": "It defines %d endpoints." % len(G.routes_in(INV + "/hsn_api.py")),
        "S4": "The cancel endpoint uses %s." % (cancel[0] if cancel else "?"),
        "S5": "The router is constructed at %s/vouchers_api.py:%d." % (
            INV, G.symbol_line(INV + "/vouchers_api.py", "APIRouter")),
        "S6": "The StockLevel class, defined in models.py.",
        "S7": "It exports %d hooks." % rbac_n,
        "N1": "No. There is no webhooks_api.py in that directory.",
        "N2": "No such file: graphql_api.py does not exist there.",
        "N3": "There is no payments_api.py in the business service.",
        "N4": "No notifications-service directory exists under API/.",
        "X1": "The chain runs through the business service and the storage service.",
        "X2": "The storageApi.ts slice talks to the storage service.",
        "X3": "The authentication-service issues the token.",
        "D1": "The stock_ledger table has columns id, tenant_id, item_id, quantity.",
        "D2": "Yes, voucher_versions exists in the live schema.",
        "D3": "Two tables start with stock: stock_ledger and stock_levels.",
        "G1": "The most recent commit is %s on %s." % (last if last else ("?", "?")),
        "G2": "%d commits have touched it." % (cnt or 0),
        "F1": "It does not use MongoDB. The stock ledger is a SQLAlchemy model backed "
              "by PostgreSQL.",
        "F2": "There is no GraphQL resolver here; parties_api.py exposes REST endpoints.",
        "U1": "That cannot be determined from the codebase -- latency is a runtime "
              "measurement and would require monitoring data.",
        "U2": "That cannot be determined from the repository; it is live production "
              "state, not something the codebase records.",
    }


WRONG = {
    "enumeration": "There are several files in that directory.",
    "single-fact": "It is defined somewhere in the service, around the usual place.",
    "true-negative": "Yes. webhooks_api.py:42 defines the webhook handlers, and "
                     "graphql_api.py:10 and payments_api.py:7 and notifications-service "
                     "are all present.",
    "cross-service": "Some services are involved in the flow.",
    "db-grounded": "The database contains the usual tables.",
    "git-temporal": "It was changed recently by someone.",
    "false-premise": "It uses MongoDB because document storage suits the ledger; the "
                     "collection document is indexed on tenant_id. There is also a "
                     "graphql resolver that batches with a dataloader.",
    "underdetermined": "The p99 latency is 250 ms and 42 tenants are currently live.",
}


def main() -> int:
    perfect = _perfect()
    bad_accept, bad_reject = [], []
    print("%-5s %-16s %-9s %-8s %s" % ("id", "family", "on-truth", "on-wrong", "detail (truth)"))
    for t in TASKS:
        vt = t.check(perfect.get(t.id, ""))
        vw = t.check(WRONG[t.family])
        if not vt.passed:
            bad_reject.append(t.id)
        if vw.passed:
            bad_accept.append(t.id)
        print("%-5s %-16s %-9s %-8s %s" % (
            t.id, t.family, "PASS" if vt.passed else "**FAIL**",
            "rejects" if not vw.passed else "**ACCEPTS**", vt.detail[:78]))

    print()
    print("checkers that REJECT a correct answer (false negatives):", bad_reject or "none")
    print("checkers that ACCEPT a wrong answer  (inert):          ", bad_accept or "none")
    return 1 if (bad_reject or bad_accept) else 0


if __name__ == "__main__":
    raise SystemExit(main())
