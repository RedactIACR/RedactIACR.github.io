"""Build the daily puzzle schedule.

    python -m build.main --days 100

Harvests IACR metadata, joins it to find CRYPTO/EUROCRYPT/TCC papers that are
also on ePrint, picks one paper per day, and downloads and extracts only the
PDFs it actually scheduled.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import time
from pathlib import Path

from .boxes import ExtractionError, extract_boxes, pack_pages, word_tokens
from .corpus import build_schedule, join_venues
from .harvest import _fetch, harvest_cryptodb, harvest_eprint

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
OUT = ROOT / "site" / "puzzles"


def fetch_pdf(eprint_id: str, *, refresh: bool = False) -> bytes:
    pdf_dir = CACHE / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    path = pdf_dir / f"{eprint_id.replace('/', '-')}.pdf"
    if path.exists() and not refresh:
        return path.read_bytes()
    data = _fetch(f"https://eprint.iacr.org/{eprint_id}.pdf")
    if not data.startswith(b"%PDF"):
        raise ExtractionError("response was not a PDF")
    path.write_bytes(data)
    time.sleep(0.4)  # be a good citizen towards eprint.iacr.org
    return data


def build_puzzle(date: str, paper: dict) -> dict:
    """Ship the paper itself, plus where every redactable thing sits on it."""
    pdf_bytes = fetch_pdf(paper["id"])
    document = extract_boxes(pdf_bytes)
    pages, keys = pack_pages(document["pages"])

    pdf_dir = OUT / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    (pdf_dir / f"{date}.pdf").write_bytes(pdf_bytes)

    return {
        "date": date,
        "id": paper["id"],
        "venue": paper["venue"],
        "year": paper["year"],
        "url": f"https://eprint.iacr.org/{paper['id']}",
        "pdf": f"pdf/{date}.pdf",
        "titleText": paper["title"],
        "titleWords": word_tokens(paper["title"]),
        "authorsText": paper["authors"],
        "keys": keys,
        "pages": pages,
        "stats": {
            "words": document["words"],
            "formulas": document["formulas"],
            "pages": document["page_count"],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=100, help="days of puzzles to build")
    parser.add_argument("--start", default="", help="first puzzle date (default: today, UTC)")
    parser.add_argument("--seed", type=int, default=20260901, help="schedule shuffle seed")
    parser.add_argument("--refresh", action="store_true", help="re-harvest IACR metadata")
    parser.add_argument("--keep", action="store_true", help="keep existing puzzle files")
    args = parser.parse_args()

    start = dt.date.fromisoformat(args.start) if args.start else dt.datetime.now(dt.UTC).date()
    CACHE.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    print("harvesting CryptoDB (CRYPTO / EUROCRYPT / TCC)...")
    cryptodb = harvest_cryptodb(CACHE, start.year + 1, refresh=args.refresh)
    print(f"  {len(cryptodb)} conference papers")

    print("harvesting ePrint via OAI-PMH...")
    eprint = harvest_eprint(CACHE, refresh=args.refresh)
    print(f"  {len(eprint)} ePrint records")

    papers = join_venues(cryptodb, eprint)
    print(f"joined: {len(papers)} conference papers are also on ePrint")

    existing = _existing_ids() if args.keep else {}
    if not args.keep:
        for stale in OUT.glob("*.json"):
            stale.unlink()
        shutil.rmtree(OUT / "pdf", ignore_errors=True)

    schedule = build_schedule(papers, start, args.days, args.seed, set(existing.values()))
    print(f"scheduling {len(schedule)} days from {schedule[0][0]} to {schedule[-1][0]}")

    index: list[dict] = []
    skipped: list[str] = []
    queue = list(schedule)
    spare = _spares(papers, schedule, existing)

    for date, paper in queue:
        iso = date.isoformat()
        while True:
            try:
                puzzle = build_puzzle(iso, paper)
                break
            except (ExtractionError, RuntimeError) as exc:
                # A handful of ePrint PDFs are scans, malformed, or withdrawn.
                # Swap in a replacement rather than losing the day.
                skipped.append(f"{paper['id']}: {exc}")
                if not spare:
                    raise SystemExit(f"ran out of replacement papers at {iso}")
                paper = spare.pop()

        (OUT / f"{iso}.json").write_text(
            json.dumps(puzzle, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        index.append({"date": iso, "id": paper["id"]})
        print(
            f"  {iso}  {paper['venue']} {paper['year']}  {paper['id']}  "
            f"{puzzle['stats']['words']} words  {puzzle['stats']['formulas']} formulas"
        )

    # Index every puzzle file present, not just the ones this run produced, so
    # that `--keep` extends the schedule instead of orphaning earlier days.
    dates = sorted(path.stem for path in OUT.glob("*.json") if path.name != "index.json")
    (OUT / "index.json").write_text(
        json.dumps(
            {
                "v": 1,
                "built": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                "start": dates[0],
                "end": dates[-1],
                "days": len(dates),
                "venues": ["CRYPTO", "EUROCRYPT", "TCC"],
                "dates": dates,
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    print(f"\nwrote {len(index)} puzzles; index now covers {len(dates)} days in {OUT}")
    if skipped:
        print(f"replaced {len(skipped)} unusable papers:")
        for line in skipped:
            print(f"  - {line}")


def _existing_ids() -> dict[str, str]:
    """Map date -> ePrint id for puzzles a previous build already produced."""
    found: dict[str, str] = {}
    for path in sorted(OUT.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            found[path.stem] = json.loads(path.read_text(encoding="utf-8"))["id"]
        except Exception:  # noqa: BLE001 - a corrupt file just gets rebuilt
            continue
    return found


def _spares(papers: list[dict], schedule, existing: dict[str, str]) -> list[dict]:
    used = {paper["id"] for _, paper in schedule} | set(existing.values())
    return [paper for paper in papers if paper["id"] not in used][:400]


if __name__ == "__main__":
    main()
