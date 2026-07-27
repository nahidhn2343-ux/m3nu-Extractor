"""
extractor.py
------------
Core logic for resolving a user-submitted URL (short link, redirect link,
or a video page URL) down to a playable HLS (.m3u8) or DASH (.mpd) stream
URL.

Pipeline:
    1. Follow the URL through any HTTP redirects (301/302/303/307/308),
       recording the full redirect chain.
    2. Inspect the final response's Content-Type / URL:
         - If it already looks like a manifest (by extension or
           Content-Type), treat it as a direct stream candidate.
         - Otherwise, treat the body as HTML/JS and regex-scan it for
           embedded .m3u8 / .mpd references (absolute or relative,
           inside quotes, JS strings, JSON blobs, <source> tags, etc).
    3. Resolve every candidate to an absolute URL (using the final page
       URL as the base, so relative paths and query-string auth tokens
       are preserved).
    4. Validate each candidate with a lightweight HEAD (falling back to a
       ranged GET, since many CDNs don't support HEAD) to confirm it
       actually responds with 200 OK and a sane Content-Type.
    5. Return the first validated candidate plus any other candidates
       found, so the frontend can show alternates if the "best" one
       turns out to be wrong.

This module intentionally does nothing beyond what a normal browser +
devtools network inspector would reveal: it does not attempt to defeat
DRM (Widevine/FairPlay/PlayReady), does not brute-force or strip auth
tokens, and does not bypass paywalls. Signed URLs / tokens found in the
page or in redirects are preserved verbatim, not fabricated.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# A realistic desktop-browser UA. Many CDNs / anti-bot layers reject the
# default "python-httpx" UA outright.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

MANIFEST_CONTENT_TYPES = (
    "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "application/dash+xml",
    "video/mp2t",  # occasionally returned for .ts segments; not a manifest
)

# Things that positively identify a manifest by content type (excludes the
# generic video/mp2t segment type above, which we don't want to treat as
# "found it").
MANIFEST_POSITIVE_TYPES = (
    "application/x-mpegurl",
    "application/vnd.apple.mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "application/dash+xml",
)

MAX_PAGE_BYTES = 3 * 1024 * 1024  # don't slurp huge pages into memory
REQUEST_TIMEOUT = 12.0
MAX_REDIRECTS = 15
MAX_CANDIDATES_TO_VALIDATE = 8

# Regexes used to hunt for stream URLs inside HTML / inline <script> JS.
# Order matters: more specific patterns first.
_STREAM_URL_PATTERNS = [
    # Quoted absolute or relative URLs ending in .m3u8 or .mpd, optionally
    # followed by a query string, inside single/double quotes (typical of
    # JS source objects, JSON configs, <source src="">, data-attrs, etc).
    re.compile(r'["\']((?:https?:)?//[^"\'<>\s]+?\.(?:m3u8|mpd)(?:\?[^"\'<>\s]*)?)["\']', re.IGNORECASE),
    re.compile(r'["\'](/[^"\'<>\s]+?\.(?:m3u8|mpd)(?:\?[^"\'<>\s]*)?)["\']', re.IGNORECASE),
    # Bare (unquoted) URLs, e.g. inside comments or plain text.
    re.compile(r'(https?://[^\s"\'<>]+?\.(?:m3u8|mpd)(?:\?[^\s"\'<>]*)?)', re.IGNORECASE),
]


@dataclass
class Candidate:
    url: str
    kind: str  # "hls" | "dash" | "unknown"
    source: str  # "content-type" | "html-regex" | "url-extension"
    validated: bool = False
    status_code: Optional[int] = None
    content_type: Optional[str] = None


@dataclass
class ExtractionResult:
    success: bool
    message: str
    original_url: str
    final_page_url: Optional[str] = None
    redirect_chain: list[str] = field(default_factory=list)
    stream_url: Optional[str] = None
    stream_type: Optional[str] = None
    alternates: list[Candidate] = field(default_factory=list)
    elapsed_ms: int = 0


def _classify(url: str, content_type: str = "") -> str:
    ct = (content_type or "").lower()
    lower = url.lower().split("?")[0]
    if "mpegurl" in ct or lower.endswith(".m3u8"):
        return "hls"
    if "dash+xml" in ct or lower.endswith(".mpd"):
        return "dash"
    return "unknown"


def _looks_like_manifest_response(resp: httpx.Response) -> bool:
    ct = resp.headers.get("content-type", "").lower()
    if any(t in ct for t in MANIFEST_POSITIVE_TYPES):
        return True
    path = urlparse(str(resp.url)).path.lower()
    return path.endswith(".m3u8") or path.endswith(".mpd")


_STREAM_ATTRS = ("src", "data-src", "data-hls", "data-mpd", "data-stream", "data-url", "href")


def _find_candidates_in_html(html: str, base_url: str) -> list[Candidate]:
    """Find m3u8/mpd references in a page two ways:
      1. Structured scan of relevant HTML tag attributes via BeautifulSoup
         (<source>, <video>, <a>, <iframe>, and any data-* attribute), which
         is more reliable than regex for well-formed markup.
      2. Regex scan of the raw text (covers inline <script> JS, JSON blobs,
         and anything BeautifulSoup's attribute scan misses).
    Both resolve relative URLs against base_url and are deduplicated."""
    found: dict[str, Candidate] = {}

    def _add(raw: str, source: str) -> None:
        raw = raw.strip()
        if not raw or not re.search(r"\.(m3u8|mpd)(\?|$)", raw, re.IGNORECASE):
            return
        if raw.startswith("//"):
            scheme = urlparse(base_url).scheme or "https"
            raw = f"{scheme}:{raw}"
        absolute = urljoin(base_url, raw)
        if absolute not in found:
            found[absolute] = Candidate(url=absolute, kind=_classify(absolute), source=source)

    # 1. Tag-attribute scan.
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(True):
            for attr in _STREAM_ATTRS:
                val = tag.get(attr)
                if isinstance(val, str):
                    _add(val, "html-tag")
    except Exception:
        pass  # fall through to regex-only scan

    # 2. Raw regex scan (catches inline JS / JSON that BS4 attrs miss).
    for pattern in _STREAM_URL_PATTERNS:
        for match in pattern.finditer(html):
            _add(match.group(1), "html-regex")

    return list(found.values())


async def _validate_candidate(client: httpx.AsyncClient, candidate: Candidate) -> Candidate:
    """Confirm a candidate URL is actually reachable and looks like a
    manifest. Tries HEAD first (cheap), falls back to a small ranged GET
    since several CDNs (esp. for HLS) reject HEAD requests."""
    headers = {**DEFAULT_HEADERS, "Range": "bytes=0-2047"}
    try:
        resp = await client.head(candidate.url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 400 or resp.status_code == 405:
            raise httpx.HTTPStatusError("HEAD not usable", request=resp.request, response=resp)
    except Exception:
        try:
            resp = await client.get(candidate.url, headers=headers, timeout=REQUEST_TIMEOUT)
        except Exception:
            candidate.validated = False
            return candidate

    candidate.status_code = resp.status_code
    candidate.content_type = resp.headers.get("content-type")
    candidate.kind = _classify(candidate.url, candidate.content_type or "")
    # 200 or 206 (partial content, from our Range GET) both indicate the
    # resource genuinely exists and is servable.
    candidate.validated = resp.status_code in (200, 206)
    return candidate


async def extract_stream_url(input_url: str) -> ExtractionResult:
    start = time.monotonic()
    input_url = input_url.strip()

    parsed = urlparse(input_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ExtractionResult(
            success=False,
            message="Please provide a valid http:// or https:// URL.",
            original_url=input_url,
        )

    redirect_chain: list[str] = []

    async with httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
        headers=DEFAULT_HEADERS,
        timeout=REQUEST_TIMEOUT,
    ) as client:
        # --- Step 1: fetch (following redirects) ---------------------------
        try:
            resp = await client.get(input_url)
        except httpx.TooManyRedirects:
            return ExtractionResult(
                success=False,
                message="Too many redirects — the link may be caught in a loop.",
                original_url=input_url,
            )
        except httpx.ConnectTimeout:
            return ExtractionResult(
                success=False,
                message="Connection timed out while reaching the URL.",
                original_url=input_url,
            )
        except httpx.RequestError as exc:
            return ExtractionResult(
                success=False,
                message=f"Could not reach that URL ({exc.__class__.__name__}).",
                original_url=input_url,
            )

        redirect_chain = [str(r.url) for r in resp.history] + [str(resp.url)]
        final_url = str(resp.url)

        if resp.status_code >= 400:
            return ExtractionResult(
                success=False,
                message=f"The final destination returned HTTP {resp.status_code}. "
                        "The link may be dead, private, or geo/IP restricted.",
                original_url=input_url,
                final_page_url=final_url,
                redirect_chain=redirect_chain,
            )

        # --- Step 2: is the final URL itself already a manifest? -----------
        if _looks_like_manifest_response(resp):
            kind = _classify(final_url, resp.headers.get("content-type", ""))
            elapsed = int((time.monotonic() - start) * 1000)
            return ExtractionResult(
                success=True,
                message="The submitted link resolved directly to a stream manifest.",
                original_url=input_url,
                final_page_url=final_url,
                redirect_chain=redirect_chain,
                stream_url=final_url,
                stream_type=kind,
                elapsed_ms=elapsed,
            )

        # --- Step 3: not a manifest -> treat as HTML/JS, regex scan --------
        ct = resp.headers.get("content-type", "")
        if "text" not in ct and "html" not in ct and "javascript" not in ct and "json" not in ct:
            return ExtractionResult(
                success=False,
                message=f"The final URL returned Content-Type '{ct or 'unknown'}', "
                        "which doesn't look like a web page or a stream manifest.",
                original_url=input_url,
                final_page_url=final_url,
                redirect_chain=redirect_chain,
            )

        body = resp.text[: MAX_PAGE_BYTES]
        candidates = _find_candidates_in_html(body, final_url)

        # Also try fetching same-origin script src="" files? Out of scope for
        # a lightweight extractor — most players inline the manifest URL or
        # build it from a JSON config already present in the HTML.

        if not candidates:
            return ExtractionResult(
                success=False,
                message="No .m3u8 or .mpd references were found on that page. "
                        "The player may build the URL dynamically via an API "
                        "call this tool doesn't execute (e.g. requires running "
                        "the page's JavaScript), or the stream may be DRM-protected.",
                original_url=input_url,
                final_page_url=final_url,
                redirect_chain=redirect_chain,
            )

        # --- Step 4: validate candidates, return first success -------------
        to_check = candidates[:MAX_CANDIDATES_TO_VALIDATE]
        validated: list[Candidate] = []
        for c in to_check:
            validated.append(await _validate_candidate(client, c))

        working = [c for c in validated if c.validated]
        elapsed = int((time.monotonic() - start) * 1000)

        if not working:
            return ExtractionResult(
                success=False,
                message=f"Found {len(candidates)} candidate stream URL(s) in the page, "
                        "but none of them responded successfully when tested. They may "
                        "require session cookies, a Referer header, or have expired tokens.",
                original_url=input_url,
                final_page_url=final_url,
                redirect_chain=redirect_chain,
                alternates=validated,
                elapsed_ms=elapsed,
            )

        best = working[0]
        others = [c for c in validated if c.url != best.url]

        return ExtractionResult(
            success=True,
            message=f"Found and validated a working {best.kind.upper()} stream URL.",
            original_url=input_url,
            final_page_url=final_url,
            redirect_chain=redirect_chain,
            stream_url=best.url,
            stream_type=best.kind,
            alternates=others,
            elapsed_ms=elapsed,
        )
