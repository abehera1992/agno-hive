---
name: external-web-research
description: When and how to use web_search/web_fetch — a shared URL, an unfamiliar library, an external technology to evaluate — and the hard rule that a failure to find an INTERNAL project file is never a reason to search the public web.
---
WEB rule: If web_search and web_fetch are available (check connected MCP
tools), use them when:
  - The user shares a URL in their prompt → call web_fetch(url) immediately
    before doing anything else.
  - The user mentions a GitHub repo by URL or name → web_fetch(github_url)
    to read the README and understand the project.
  - The codebase references an unfamiliar library, tool, or service →
    web_search(name) then web_fetch on the best result.
  - The user asks about a new technology or asks you to evaluate something
    external → web_search first.
  - Codebase context is insufficient because the topic itself is EXTERNAL (a
    library, tool, API, or service not defined in this project) →
    web_search to fill that specific external gap.
  Always prefer local file tools (find_files, search_files) for project
  questions. Use web tools only for external context.
  NEVER a case for web_search/web_fetch: failing to locate an INTERNAL
  project file, component, or symbol after repeated
  find_files/search_files/get_file_content attempts. Confirmed live
  2026-08-11: after repeatedly guessing a wrong path for a real internal
  frontend file, a run pivoted to web_search('eKam platform GitHub
  repository') and similar queries — searching the public web for a
  private, internal codebase question, which can never return a useful
  result. A failure to find something internal means try a different
  find_files/search_files query, or say "not found — file not located" — it
  is never a reason to search the web.
