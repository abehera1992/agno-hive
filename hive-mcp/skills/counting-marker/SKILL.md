---
name: counting-marker
description: How to report any count, total, or "how many" — never write the number yourself; use the deterministic count mechanism.
---
Counts must be tool-filled, NEVER written by you (CRITICAL). You are FORBIDDEN from
writing any count / total / "how many" / "all" as a bare number you computed by
reading — reading and tallying is unreliable and treated as fabrication. Instead:

- If the count is over files in the repo: emit a COUNT MARKER and the system fills
  in the EXACT ripgrep count for you:
      [[COUNT pattern=`<ripgrep-regex>` glob=`<glob>`]]
  Example: 'There are [[COUNT pattern=`: *12\.0` glob=`**/gst_resolver.py`]] entries
  at 12%.' pattern = a ripgrep regex (backtick-delimited); glob = files to scan
  (e.g. **/gst_resolver.py, **/*.py). The system replaces the marker with the real
  number AFTER you finish — you never supply the digit, so the count cannot be
  wrong. Use ONE marker per distinct count.
- For a count of rows in a DATABASE table, use db_query (SELECT ... COUNT(*))
  instead — see the db-facts skill. Do NOT grep files for a value that lives in
  the DB.
- If you already ran count_matches / grep -c yourself and have the exact tool
  output, you may state that number directly. Otherwise ALWAYS use the marker —
  never guess.
- Research thoroughly first: a value may live in more than one place (e.g. a DB
  table AND a code fallback), so search across the WHOLE repo to confirm you
  found every occurrence before stating a total, and state which sources you
  checked. If the target is a big literal (a large dict/list/table/seed block),
  GREP it — do not scroll it and guess.
