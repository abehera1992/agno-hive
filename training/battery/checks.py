"""Checkers: answer text in, (passed, detail) out.

Every checker states WHY it passed or failed in `detail`, because a bare boolean in a
score table is exactly what let a wrong verdict stand unexamined for a whole session
(subset19's T13a was scored a citation failure on five citations that were correct;
nothing in the score line said what had been compared).

`kind` separates COMPUTED verdicts -- a set difference against the filesystem, which is
as certain as the repo itself -- from HEURISTIC ones, where the right answer is a
stance ("this premise is false") and matching is necessarily fuzzy. The two are
reported separately and never added into one number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Verdict:
    passed: bool
    detail: str
    kind: str = "computed"     # "computed" | "heuristic"


def _norm(text: str) -> str:
    return (text or "").lower()


_NEGATION = re.compile(
    r"\b(no|not|never|isn'?t|aren'?t|does\s+not|do\s+not|doesn'?t|don'?t|without|"
    r"rather than|instead of|nothing|nowhere|incorrect|false premise|mistaken)\b", re.I)


# A sentence or line boundary. A period only ends a sentence when what follows looks
# like a new one -- otherwise "(e.g. `storageApi.ts`)" would be cut at the "e.g." and
# lose the "There is no ..." that governs it.
_SENT_BREAK = re.compile(r"\n|(?<=[.!?;:])\s+(?=[A-Z0-9])")


def _clause_before(text: str, pos: int) -> str:
    """The run-up to `pos` within its own clause -- not a fixed character window.

    A flat 80-character lookbehind was the first attempt and it broke enumeration
    scoring immediately: battery2's E5 listed all nine routes correctly, and
    `/vouchers/stock-transfer` was rejected as "negated" because the PREVIOUS list item
    ended "(no party, no tax)". Ordinary prose is full of the word "no"; what matters
    is whether the negation actually governs this mention, and a list item on its own
    line is not governed by the line above it.

    Fixing a false PASS by creating a false FAIL is not progress, so the scope stops at
    the nearest line or sentence boundary.
    """
    start = 0
    for m in _SENT_BREAK.finditer(text, 0, pos):
        start = m.end()
    return text[start:pos]


def _affirms(text: str, term: str) -> bool:
    """Is `term` asserted as fact, rather than named in order to be denied?

    The distinction is the whole point of a false-premise trap and the first version
    of this missed it completely: it counted ANY mention as affirmation, so the ideal
    answer -- "It does not use MongoDB; the stock ledger is a SQLAlchemy model" --
    scored as falling for the trap. Rejecting a premise requires naming the premise.
    The self-test caught it before a single run was scored.

    An occurrence counts as affirmed only when no negation appears in the run-up to
    it. Crude, and deliberately so: this is reported as a heuristic verdict and never
    added to the computed ones.
    """
    occurrences = list(re.finditer(re.escape(term), text, re.I))
    if not occurrences:
        return False
    return any(not _NEGATION.search(_clause_before(text, m.start()))
               for m in occurrences)


def names_all(expected, label="items", allow_missing=0):
    """Answer must name every expected item -- and mean it.

    Mentioning a name is not claiming it exists. battery2's X2 answered "There is no
    top-level API slice (e.g. `storageApi.ts`) ... No Hooks for Upload/Download Exist"
    about a file that exists and exports nine hooks. The answer was wrong, and this
    checker scored it CORRECT, because the string "storageApi.ts" was present -- inside
    the sentence denying it.

    A false PASS is the most dangerous result a scorer can produce: a false FAIL gets
    investigated, a false PASS is filed as success and never looked at again. So an
    item counts only when at least one mention is not inside a negation, reusing the
    same _affirms helper the false-premise checker uses. That helper exists because
    rejects_premise had this identical bug pointed the other way -- it treated any
    mention as agreement. Same confusion, opposite sign, two different functions.
    """
    def check(answer: str) -> Verdict:
        a = answer or ""
        missing = [e for e in expected if not _affirms(a, e)]
        ok = len(missing) <= allow_missing
        return Verdict(
            ok,
            "named %d/%d %s%s" % (len(expected) - len(missing), len(expected), label,
                                  ("; missing: " + ", ".join(missing[:8])) if missing else ""),
        )
    return check


def states_count(n: int, nouns: tuple[str, ...]):
    """Answer must state the number, adjacent to one of the nouns it counts.

    Adjacency matters: a bare "24" anywhere in a long answer is not a claim about the
    file count, and crediting it would let a wrong answer pass on a coincidence.
    """
    pats = [re.compile(r"(?<![\w.:/-])%d\b[^.\n]{0,40}\b%s" % (n, re.escape(no)), re.I)
            for no in nouns] + \
           [re.compile(r"\b%s\b[^.\n]{0,40}(?<![\w.:/-])%d(?!\d)" % (re.escape(no), n), re.I)
            for no in nouns]

    def check(answer: str) -> Verdict:
        hit = any(p.search(answer or "") for p in pats)
        return Verdict(hit, "states %d near %s: %s" % (n, "/".join(nouns), "yes" if hit else "NO"))
    return check


_DENIAL = re.compile(
    r"\b(does not exist|doesn't exist|no such (file|router|module|service|directory)|"
    r"not present|not found|there is no|no \w+ (file|router|module) (named|called)|"
    r"absent|does not contain)\b"
    # "No notifications-service directory exists under API/" -- the natural way to
    # write this denial, and the self-test caught the original regex rejecting it.
    r"|\bno\b[^.\n]{0,60}\b(exists?|present|there)\b", re.I)


def denies_existence(token: str):
    """The answer must say the thing is absent -- and must not assert it exists.

    Both halves are required. An answer that hedges ("I could not locate X, though it
    may be at ...") is not a denial, and one that describes X's contents while also
    saying it wasn't found is self-contradictory; neither should score as correct.
    """
    def check(answer: str) -> Verdict:
        a = answer or ""
        denied = bool(_DENIAL.search(a))
        # an assertion that it DOES exist: the token cited with a path or line
        asserts = bool(re.search(re.escape(token) + r"[^\n]{0,40}:\d+", a, re.I))
        ok = denied and not asserts
        return Verdict(ok, "denial=%s contradicting-assertion=%s" % (denied, asserts),
                       kind="heuristic")
    return check


def rejects_premise(false_terms: tuple[str, ...], true_terms):
    """A false-premise trap: the answer must not affirm the false thing, and should
    name what is actually there.

    Heuristic by nature. Scored on two observable signals rather than on tone: does the
    answer assert the false term as fact, and does it name the real one.
    """
    def check(answer: str) -> Verdict:
        a = answer or ""
        affirmed = [t for t in false_terms if _affirms(a, t)]
        # Word-boundary, not substring: "rest" inside "restock"/"restore" is all over
        # an inventory codebase and would credit an answer that never said REST.
        # ANY of several correct alternatives counts. Demanding one literal token
        # scored battery1's F1 as a failure when it had rejected the trap perfectly:
        # "does not use MongoDB ... relies entirely on PostgreSQL" -- correct, but the
        # checker was looking for the word "sqlalchemy".
        wanted = (true_terms,) if isinstance(true_terms, str) else tuple(true_terms)
        hit = [w for w in wanted
               if re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", a, re.I)]
        ok = (not affirmed) and bool(hit)
        return Verdict(
            ok,
            "affirmed-false=%s named-true%s=%s" % (affirmed or "none", wanted, hit or "none"),
            kind="heuristic",
        )
    return check


_CANNOT = re.compile(
    r"\b(cannot be determined|can't be determined|not determinable|no way to (tell|know)|"
    r"not (available|present|recorded|measured) in (the )?(code|codebase|repository|repo)|"
    r"would require (runtime|monitoring|profiling|production)|"
    r"not something the codebase|no data (in|available)|"
    # How the swarm actually declined on battery1 U1 -- a correct, honest answer that
    # the first version of this regex scored as a failure to decline.
    r"not hardcoded in the (code|codebase|repo)|none of these files contain|"
    r"would need to check the (production |live )?(monitoring|dashboard|grafana)|"
    r"not (defined|recorded|stored) (anywhere )?in the (code|codebase|repository|repo))\b",
    re.I)


def declines_as_undeterminable(must_not_invent: tuple[str, ...] = ()):
    """An underdetermined question: the honest answer is that the repo cannot say.

    `must_not_invent` catches the specific fabrication shape this trap targets -- a
    concrete figure offered as if measured.
    """
    def check(answer: str) -> Verdict:
        a = answer or ""
        declined = bool(_CANNOT.search(a))
        invented = [p for p in must_not_invent if re.search(p, a, re.I)]
        ok = declined and not invented
        return Verdict(ok, "declined=%s invented=%s" % (declined, invented or "none"),
                       kind="heuristic")
    return check


def cites(rel_file: str, line: int, tolerance: int = 3):
    """Answer must cite this file at (about) this line."""
    base = rel_file.split("/")[-1]

    def check(answer: str) -> Verdict:
        # "models.py:428" AND "`models.py` at line 428" -- the second is how a model
        # actually writes it, and requiring the colon form scored a perfectly correct
        # citation as a miss (battery1 S1: "defined in `API/inventory-service/
        # models.py` at line 428", which is exactly right).
        hits = [int(m.group(1)) for m in re.finditer(
            re.escape(base) + r"[^\n]{0,40}?(?::|\bline[s]?\s+|\bat\s+)(\d{1,6})",
            answer or "", re.I)]
        near = [h for h in hits if abs(h - line) <= tolerance]
        return Verdict(bool(near),
                       "cited %s at %s (want %d+/-%d)" % (base, hits or "nothing", line, tolerance))
    return check


def all_of(*checks):
    """Every sub-check must pass; details are concatenated so a failure says which."""
    def check(answer: str) -> Verdict:
        vs = [c(answer) for c in checks]
        kind = "heuristic" if any(v.kind == "heuristic" for v in vs) else "computed"
        return Verdict(all(v.passed for v in vs),
                       " | ".join(("+" if v.passed else "-") + v.detail for v in vs), kind)
    return check
