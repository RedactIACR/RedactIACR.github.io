"""Build the daily puzzles.

    python -m build.main --plan --days 180    # decide which paper runs on which day
    python -m build.main                      # build the days around today

Scheduling and building are deliberately separate. Which paper falls on which
day is decided once and written to `schedule.json`, which is committed; builds
then read that file. Re-deriving the schedule on every build would be unstable
in two ways: the shuffle is positional, so a build starting "today" would hand
the same paper to every first day, and the pool grows as IACR publishes, which
reshuffles every later assignment too.

Planning needs the network (IACR metadata plus every scheduled PDF, which it
downloads to prove the paper is usable). Building needs only the PDFs, which
are cached, so routine deploys touch eprint.iacr.org once per new day.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path

from .boxes import ExtractionError, extract_boxes, pack_pages, word_tokens
from .citations import Citations, LookupFailed
from .corpus import join_venues, shuffled_pool
from .harvest import _fetch, harvest_cryptodb, harvest_eprint

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
OUT = ROOT / "site" / "puzzles"
LOCK = ROOT / "schedule.json"


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
        "citations": paper.get("citations"),
        "keys": keys,
        "pages": pages,
        "stats": {
            "words": document["words"],
            "formulas": document["formulas"],
            "pages": document["page_count"],
        },
    }


# ----------------------------------------------------------------- schedule

def load_lock() -> dict:
    if not LOCK.exists():
        return {"v": 1, "seed": None, "days": {}}
    return json.loads(LOCK.read_text(encoding="utf-8"))


def save_lock(lock: dict) -> None:
    lock["days"] = dict(sorted(lock["days"].items()))
    LOCK.write_text(json.dumps(lock, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def plan(args) -> None:
    """Extend the schedule, without ever moving a day that is already fixed."""
    lock = load_lock()
    taken = {entry["id"] for entry in lock["days"].values()}
    start = dt.date.fromisoformat(args.start) if args.start else dt.datetime.now(dt.UTC).date()

    print("harvesting CryptoDB (CRYPTO / EUROCRYPT / TCC)...")
    cryptodb = harvest_cryptodb(CACHE, start.year + 1, refresh=args.refresh)
    print(f"  {len(cryptodb)} conference papers")

    print("harvesting ePrint via OAI-PMH...")
    eprint = harvest_eprint(CACHE, refresh=args.refresh)
    print(f"  {len(eprint)} ePrint records")

    papers = join_venues(cryptodb, eprint)
    print(f"joined: {len(papers)} conference papers are also on ePrint")

    wanted = [
        (start + dt.timedelta(days=i)).isoformat()
        for i in range(args.days)
        if (start + dt.timedelta(days=i)).isoformat() not in lock["days"]
    ]
    if not wanted:
        print(f"schedule already covers {args.days} days from {start}")
        return

    # Walk the whole shuffled pool: the citation filter rejects most papers,
    # so a small fixed draw would run dry.
    queue = shuffled_pool(papers, args.seed, taken)
    citations = Citations(CACHE)
    print(f"planning {len(wanted)} new days ({wanted[0]} .. {wanted[-1]})")
    if args.min_citations:
        print(f"  requiring at least {args.min_citations} citations (Semantic Scholar)")

    added, thin, unknown = 0, 0, 0
    for iso in wanted:
        while queue:
            paper = queue.pop(0)

            try:
                cited = citations.get(paper["title"]) if args.min_citations else 0
            except LookupFailed as exc:
                # Guessing here would silently exclude good papers, so stop and
                # keep what has been decided so far.
                citations.save()
                save_lock(lock)
                raise SystemExit(f"citation lookup failed: {exc}\nRe-run to resume.")
            if args.min_citations:
                if cited is None:
                    # Unknown is not the same as zero, but an unverifiable
                    # count cannot be said to clear the bar.
                    unknown += 1
                    continue
                if cited < args.min_citations:
                    thin += 1
                    continue

            try:
                # Building it here is the point: a day only enters the
                # schedule once its PDF is proven to extract.
                build_puzzle(iso, paper)
            except (ExtractionError, RuntimeError) as exc:
                print(f"  skip {paper['id']}: {exc}")
                continue
            lock["days"][iso] = {
                "id": paper["id"], "venue": paper["venue"], "year": paper["year"],
                "title": paper["title"], "authors": paper["authors"],
                "citations": cited,
            }
            print(f"  {iso}  {paper['venue']} {paper['year']}  {paper['id']}  {cited} cites")
            added += 1
            break
        else:
            raise SystemExit(
                f"ran out of usable papers at {iso} "
                f"(rejected {thin} under-cited, {unknown} unknown to Semantic Scholar)"
            )

    citations.save()
    if args.min_citations:
        print(f"  rejected {thin} papers under {args.min_citations} citations, "
              f"{unknown} not found in Semantic Scholar")

    lock["seed"] = args.seed
    lock["generated"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    save_lock(lock)
    print(f"\nschedule now covers {len(lock['days'])} days, {added} added -> {LOCK}")


# -------------------------------------------------------------------- build

def build(args) -> None:
    """Emit the puzzle files for the days around today."""
    lock = load_lock()
    if not lock["days"]:
        raise SystemExit(f"no schedule at {LOCK}; run with --plan first")

    today = dt.date.fromisoformat(args.today) if args.today else dt.datetime.now(dt.UTC).date()
    if args.all:
        wanted = sorted(lock["days"])
    else:
        window = {
            (today + dt.timedelta(days=offset)).isoformat()
            for offset in range(-args.back, args.horizon + 1)
        }
        wanted = sorted(window & set(lock["days"]))

    if not wanted:
        raise SystemExit(
            f"schedule has nothing for {today}; its last day is {max(lock['days'])}"
        )

    missing = [
        (today + dt.timedelta(days=offset)).isoformat()
        for offset in range(0, args.horizon + 1)
        if (today + dt.timedelta(days=offset)).isoformat() not in lock["days"]
    ]
    if missing:
        note = (f"schedule runs out on {max(lock['days'])}; {len(missing)} of the next "
                f"{args.horizon} days are unplanned - run: python -m build.main --plan")
        # Surface it as a CI annotation, not just a line in a long log.
        print(f"::warning::{note}" if os.environ.get("GITHUB_ACTIONS") else f"warning: {note}")

    OUT.mkdir(parents=True, exist_ok=True)
    keep = set(wanted)

    # Only today's puzzle is ever served, so days outside the window are dead
    # weight on a Pages site with a size budget.
    for stale in OUT.glob("*.json"):
        if stale.name != "index.json" and stale.stem not in keep:
            stale.unlink()
    for stale in (OUT / "pdf").glob("*.pdf"):
        if stale.stem not in keep:
            stale.unlink()

    built = 0
    for iso in wanted:
        paper = lock["days"][iso]
        # Re-use an existing file only if it is the paper now scheduled: a
        # re-plan can reassign a day, and testing for mere existence would
        # leave yesterday's paper published under today's date.
        if not args.force and _built_id(iso) == paper["id"] and (OUT / "pdf" / f"{iso}.pdf").exists():
            continue
        puzzle = build_puzzle(iso, paper)
        (OUT / f"{iso}.json").write_text(
            json.dumps(puzzle, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        built += 1
        print(
            f"  {iso}  {paper['venue']} {paper['year']}  {paper['id']}  "
            f"{puzzle['stats']['words']} words  {puzzle['stats']['formulas']} formulas"
        )

    dates = sorted(path.stem for path in OUT.glob("*.json") if path.name != "index.json")
    (OUT / "index.json").write_text(
        json.dumps(
            {
                "v": 1,
                "built": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                # Day one of the whole schedule, not of this window: the puzzle
                # number has to keep counting as the served window rolls on.
                "epoch": min(lock["days"]),
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
    print(f"\nbuilt {built} new puzzles; site now serves {len(dates)} days ({dates[0]} .. {dates[-1]})")


def _built_id(iso: str) -> str | None:
    """The ePrint id of an already-built puzzle file, if it is readable."""
    path = OUT / f"{iso}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))["id"]
    except Exception:  # noqa: BLE001 - a corrupt file just gets rebuilt
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--plan", action="store_true", help="extend schedule.json instead of building")
    parser.add_argument("--days", type=int, default=180, help="days to plan ahead (--plan)")
    parser.add_argument("--start", default="", help="first day to plan (--plan, default: today UTC)")
    parser.add_argument("--seed", type=int, default=20260901, help="schedule shuffle seed (--plan)")
    parser.add_argument("--refresh", action="store_true", help="re-harvest IACR metadata (--plan)")
    parser.add_argument("--min-citations", type=int, default=50,
                        help="skip papers cited fewer times than this (--plan; 0 disables)")
    parser.add_argument("--back", type=int, default=2, help="days before today to keep serving")
    parser.add_argument("--horizon", type=int, default=21, help="days ahead to build")
    parser.add_argument("--today", default="", help="override today's date (testing)")
    parser.add_argument("--all", action="store_true", help="build every day in the schedule")
    parser.add_argument("--force", action="store_true", help="rebuild days that already exist")
    args = parser.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    if args.plan:
        plan(args)
    else:
        build(args)


if __name__ == "__main__":
    main()
