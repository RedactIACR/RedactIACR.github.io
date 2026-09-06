"""Look up how often a paper has been cited, via Semantic Scholar.

Neither CryptoDB nor the ePrint feed records citations, so the count comes from
the Semantic Scholar Academic Graph. Lookups are matched on the title and
cached on disk, because the schedule only needs a paper's count once.

Why not OpenAlex: it moved to a credit model, and the anonymous allowance is
1000 credits a day against 10 credits per request — a hundred lookups, which
does not cover even one planning run. Semantic Scholar's free tier is a shared
pool of roughly 100 requests per five minutes, which is slow but sufficient,
and `SEMANTIC_SCHOLAR_API_KEY` raises it if one is set.

Two distinctions matter to the caller:

* A paper the graph does not know is `None`, not zero. The scheduler treats
  unknown as ineligible, since an unverifiable count cannot be shown to clear
  the bar.
* A lookup that could not be completed raises `LookupFailed`. That must never
  be recorded as `None`: doing so would bar a paper from the schedule
  permanently on the strength of a transient network failure.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .corpus import normalise_title

MATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search/match"
USER_AGENT = "redact-iacr build script (+https://github.com/) - polite, cached"

# The anonymous pool allows on the order of 100 requests per five minutes.
# The endpoint's own latency is 4-8s, which already paces us well below that,
# so this only adds a margin; it grows on its own if we are ever throttled.
DELAY = 1.5


class LookupFailed(RuntimeError):
    """The citation count could not be established (throttled, offline, ...)."""


class Citations:
    """Cached title -> citation count lookups."""

    def __init__(self, cache_dir: Path) -> None:
        self.delay = DELAY
        self.path = cache_dir / "citations.json"
        self.counts: dict[str, int | None] = {}
        if self.path.exists():
            try:
                self.counts = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.counts = {}
        self._dirty = 0
        self._key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.counts, indent=0), encoding="utf-8")
        self._dirty = 0

    def get(self, title: str) -> int | None:
        key = normalise_title(title)
        if not key:
            return None
        if key not in self.counts:
            self.counts[key] = self._fetch(title, key)
            self._dirty += 1
            if self._dirty >= 20:      # checkpoint, so a long plan is resumable
                self.save()
        return self.counts[key]

    def _fetch(self, title: str, key: str) -> int | None:
        query = urllib.parse.quote(title[:300])
        url = f"{MATCH_URL}?query={query}&fields=title,citationCount"
        headers = {"User-Agent": USER_AGENT}
        if self._key:
            headers["x-api-key"] = self._key

        payload = None
        for attempt in range(6):
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=45) as response:
                    payload = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    # The documented "Title match not found" answer.
                    time.sleep(self.delay)
                    return None
                if exc.code not in (429, 500, 502, 503, 504):
                    raise LookupFailed(f"HTTP {exc.code} for {title[:60]!r}") from exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                wait = float(retry_after) if (retry_after or "").isdigit() else min(90.0, 5.0 * 2 ** attempt)
                self.delay = min(10.0, self.delay * 1.3)   # ease off for good
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                time.sleep(min(30.0, 3.0 * 2 ** attempt))

        if payload is None:
            raise LookupFailed(f"no answer for {title[:60]!r}")

        time.sleep(self.delay)
        if payload.get("error"):
            return None

        # /search/match returns its single best guess, which for a short or
        # generic title can be a different paper: insist on the exact title.
        for paper in payload.get("data") or []:
            if normalise_title(paper.get("title") or "") == key:
                return int(paper.get("citationCount") or 0)
        return None
