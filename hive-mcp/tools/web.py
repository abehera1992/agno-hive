"""Web search and fetch tools for hive-mcp.

Uses the client machine's network — no ZGX involvement.
Gated by WEB_SEARCH_ENABLED=true in the hive-mcp environment.

Tools:
    web_search(query, max_results)  — DuckDuckGo full-text search, returns titles + URLs + snippets
    web_fetch(url, max_chars)       — fetch a URL and return clean readable text
                                      GitHub repos: returns README via GitHub API
                                      PDFs: extracts real text via pypdf (2026-08-15 — previously
                                            fell through to the generic branch and returned raw
                                            binary bytes decoded as text, unusable garbage)
                                      All other pages: strips nav/scripts/ads via BeautifulSoup
"""
import base64
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import config

_DISABLED = (
    "Web search is disabled. Set WEB_SEARCH_ENABLED=true in the hive-mcp environment "
    "(docker run -e WEB_SEARCH_ENABLED=true ...) to enable."
)

_GITHUB_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git|/.*)?$"
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_html(html: str, max_chars: int) -> str:
    """Strip scripts/styles/nav and return readable plain text."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return html[:max_chars]

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header",
                     "aside", "noscript", "iframe", "form"]):
        tag.decompose()

    # Prefer main content containers
    for selector in ["main", "article", '[role="main"]', ".content", "#content", ".post"]:
        block = soup.select_one(selector)
        if block:
            return block.get_text(separator="\n", strip=True)[:max_chars]

    return soup.get_text(separator="\n", strip=True)[:max_chars]


def _github_readme(owner: str, repo: str) -> str | None:
    """Fetch README from GitHub API (no auth required for public repos)."""
    try:
        import httpx
        resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/readme",
            headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "hive-mcp/1.0",
            },
            timeout=10,
            follow_redirects=True,
        )
        if resp.status_code == 200:
            data = resp.json()
            content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            return content
        return None
    except Exception:
        return None


def _github_repo_summary(owner: str, repo: str) -> str:
    """Fetch repo metadata + README from GitHub API."""
    try:
        import httpx
        headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "hive-mcp/1.0"}

        meta_resp = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}",
            headers=headers, timeout=10, follow_redirects=True,
        )
        meta = meta_resp.json() if meta_resp.status_code == 200 else {}

        parts = []
        if meta:
            parts.append(
                f"Repository: {owner}/{repo}\n"
                f"Description: {meta.get('description', 'N/A')}\n"
                f"Language: {meta.get('language', 'N/A')}\n"
                f"Stars: {meta.get('stargazers_count', 0):,}\n"
                f"Topics: {', '.join(meta.get('topics', []))}\n"
            )

        readme = _github_readme(owner, repo)
        if readme:
            parts.append(f"README:\n\n{readme}")

        return "\n".join(parts) if parts else f"Could not retrieve {owner}/{repo}"
    except Exception as exc:
        return f"GitHub fetch failed: {exc}"


def _extract_pdf_text(pdf_bytes: bytes, max_chars: int) -> str:
    """Extract readable text from PDF bytes via pypdf. Never raises -- a corrupt,
    encrypted, or scanned-image-only PDF returns a clear message instead of an
    exception (matching web_fetch's own "return a string, always" contract) or,
    the pre-2026-08-15 behavior this replaces, silently unreadable binary garbage."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return "PDF extraction unavailable — pypdf not installed."

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:
        return f"Could not open PDF (corrupt or unsupported format): {exc}"

    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            return "PDF is password-protected — cannot extract text."

    parts = []
    total = 0
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            continue
        if text.strip():
            parts.append(text)
            total += len(text)
        if total >= max_chars:
            break

    if not parts:
        return "PDF contains no extractable text (likely scanned images with no OCR layer)."

    return "\n\n".join(parts)[:max_chars]


# ── MCP tools ─────────────────────────────────────────────────────────────────

def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web using DuckDuckGo and return titles, URLs, and summaries.

    Use this when:
    - The user asks about an unfamiliar library, tool, or technology
    - The user shares a topic that needs official documentation or examples
    - The codebase references something the team doesn't have grounded context on
    - You need to find the GitHub repo or homepage for a named project

    After searching, call web_fetch(url) on the most relevant result to get full content.

    Args:
        query:       Search query string
        max_results: Number of results to return (default 5, max 10)
    """
    if not config.WEB_SEARCH_ENABLED:
        return _DISABLED
    try:
        from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=min(max_results, 10)):
                results.append(
                    f"Title:   {r.get('title', '')}\n"
                    f"URL:     {r.get('href', '')}\n"
                    f"Summary: {r.get('body', '')}"
                )
        if not results:
            return f"No results found for: {query}"
        return f"{len(results)} result(s) for '{query}':\n\n" + "\n\n---\n\n".join(results)
    except Exception as exc:
        return f"web_search failed: {exc}"


def web_fetch(url: str, max_chars: int = 8000) -> str:
    """
    Fetch a URL and return its readable text content.

    Handles:
    - GitHub repos (github.com/owner/repo) — returns repo metadata + README via GitHub API
    - GitHub raw files                      — returns raw file content directly
    - PDFs                                  — extracts real text via pypdf
    - Documentation sites, blog posts       — strips nav/scripts/ads, returns main content
    - Any other HTTP URL                    — returns cleaned plain text

    Use this when:
    - The user shares a link in their prompt — fetch it immediately before answering
    - web_search returned a promising URL — fetch it to get full content
    - The user mentions a GitHub repo by URL or name — fetch to understand the project
    - You need official docs, changelog, or API reference for a library

    Args:
        url:       Full URL to fetch (must start with http:// or https://)
        max_chars: Max characters to return from the page (default 8000)
    """
    if not config.WEB_SEARCH_ENABLED:
        return _DISABLED

    if not url.startswith(("http://", "https://")):
        return f"Invalid URL — must start with http:// or https://: {url}"

    # GitHub repo pages — use API for structured README + metadata
    gh_match = _GITHUB_RE.match(url)
    if gh_match and "/blob/" not in url and "/raw/" not in url:
        owner, repo = gh_match.group(1), gh_match.group(2)
        result = _github_repo_summary(owner, repo)
        return result[:max_chars]

    # All other URLs — httpx fetch + BeautifulSoup extraction
    try:
        import httpx
        resp = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; hive-mcp/1.0)"},
            timeout=15,
            follow_redirects=True,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")

        # Check both content-type AND the URL's own extension -- some servers send
        # application/octet-stream for a PDF instead of application/pdf, and the
        # extension is a reliable enough second signal not to fall through to the
        # generic branch (which would decode the raw PDF bytes as text -- garbage).
        if "pdf" in content_type or url.lower().split("?")[0].endswith(".pdf"):
            return _extract_pdf_text(resp.content, max_chars)

        if "html" in content_type:
            return _clean_html(resp.text, max_chars)
        else:
            # Raw text, markdown, JSON, etc.
            return resp.text[:max_chars]
    except Exception as exc:
        return f"web_fetch failed for {url}: {exc}"
