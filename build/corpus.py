"""Join CryptoDB venue data with ePrint identifiers and pick the daily schedule."""

from __future__ import annotations

import datetime as dt
import random
import re
import unicodedata

# Inline LaTeX that shows up in ePrint titles: $x^2$, \mathbb{F}, {AES}, \'e ...
_MATH_SPAN_RE = re.compile(r"\$[^$]*\$")
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+\s*")
_NON_WORD_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def normalise_title(title: str) -> str:
    """Collapse a title to a comparison key that survives typographic drift."""
    text = unicodedata.normalize("NFKD", title)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _MATH_SPAN_RE.sub(" ", text)
    text = _LATEX_CMD_RE.sub(" ", text)
    text = text.replace("\u2013", " ").replace("\u2014", " ")
    text = text.lower()
    text = _NON_WORD_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def join_venues(cryptodb: list[dict], eprint: list[dict]) -> list[dict]:
    """Return ePrint papers that are known to have appeared at one of the venues.

    CryptoDB and ePrint share no identifier, so the join is on the normalised
    title.  Titles that collide are resolved towards the ePrint record posted
    closest to (and not after) the conference year, which is how preprints
    actually behave.
    """
    by_title: dict[str, list[dict]] = {}
    for record in eprint:
        key = normalise_title(record["title"])
        if key:
            by_title.setdefault(key, []).append(record)

    joined: dict[str, dict] = {}
    for paper in cryptodb:
        key = normalise_title(paper["title"])
        candidates = by_title.get(key)
        if not candidates:
            continue

        venue_year = paper["year"]
        best = min(
            candidates,
            key=lambda rec: (
                int(rec["id"][:4]) > venue_year,  # prefer preprints posted before the conf
                abs(int(rec["id"][:4]) - venue_year),
                rec["id"],
            ),
        )

        # An ePrint paper can be matched by several CryptoDB entries (e.g. an
        # invited-talk duplicate).  Keep the earliest venue appearance.
        existing = joined.get(best["id"])
        if existing and (existing["year"], existing["venue"]) <= (venue_year, paper["venue"]):
            continue

        joined[best["id"]] = {
            "id": best["id"],
            "title": best["title"],
            "authors": best["authors"],
            "abstract": best["abstract"],
            "keywords": best["keywords"],
            "venue": paper["venue"],
            "year": venue_year,
        }

    return sorted(joined.values(), key=lambda rec: rec["id"])


def build_schedule(
    papers: list[dict],
    start: dt.date,
    days: int,
    seed: int,
    exclude_ids: set[str] | None = None,
) -> list[tuple[dt.date, dict]]:
    """Assign one paper per day, deterministically for a given seed.

    `exclude_ids` lets a later build extend the schedule without repeating a
    paper that an earlier build already used.
    """
    pool = [p for p in papers if p["id"] not in (exclude_ids or set())]
    if len(pool) < days:
        raise SystemExit(
            f"only {len(pool)} eligible papers available for {days} days of puzzles"
        )

    rng = random.Random(seed)
    rng.shuffle(pool)
    return [(start + dt.timedelta(days=i), pool[i]) for i in range(days)]
