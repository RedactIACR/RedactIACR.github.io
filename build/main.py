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
downloads to prove the paper is usable) and commits each scheduled PDF under
`papers/`. Building reads only those committed PDFs and never downloads:
ePrint puts its PDFs behind a Cloudflare bot challenge, so CI cannot fetch them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

from .boxes import ExtractionError, extract_boxes, pack_pages, word_tokens
from .citations import Citations, LookupFailed
from .corpus import author_roster, by_roster, join_venues, parse_roster, shuffled_pool
from .harvest import VENUES, Blocked, _fetch, harvest_cryptodb, harvest_eprint

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
OUT = ROOT / "site" / "puzzles"
LOCK = ROOT / "schedule.json"
ROSTER = ROOT / "authors.txt"
# The PDF of every scheduled paper, committed next to schedule.json so that a
# build never has to reach eprint.iacr.org.
PAPERS = ROOT / "papers"


class MissingPDF(Exception):
    """A scheduled day whose PDF is not committed under papers/."""


def _pdf_name(eprint_id: str) -> str:
    return f"{eprint_id.replace('/', '-')}.pdf"


def load_pdf(eprint_id: str) -> bytes:
    """The committed PDF of a scheduled paper."""
    path = PAPERS / _pdf_name(eprint_id)
    if not path.exists():
        raise MissingPDF(f"{path.relative_to(ROOT)} is not committed")
    return path.read_bytes()


def fetch_pdf(eprint_id: str, *, download: bool = True) -> bytes | None:
    """A candidate's PDF: committed, cached by an earlier plan, or downloaded.

    None if it is on neither disk and `download` is off.
    """
    name = _pdf_name(eprint_id)
    for path in (PAPERS / name, CACHE / "pdf" / name):
        if path.exists():
            return path.read_bytes()
    if not download:
        return None
    data = _fetch(f"https://eprint.iacr.org/{eprint_id}.pdf")
    if not data.startswith(b"%PDF"):
        raise ExtractionError("response was not a PDF")
    (CACHE / "pdf").mkdir(parents=True, exist_ok=True)
    (CACHE / "pdf" / name).write_bytes(data)
    time.sleep(0.4)  # be a good citizen towards eprint.iacr.org
    return data


def build_puzzle(date: str, paper: dict, pdf_bytes: bytes) -> dict:
    """Ship the paper itself, plus where every redactable thing sits on it."""
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

    print(f"harvesting CryptoDB ({' / '.join(v.upper() for v in VENUES)})...")
    cryptodb = harvest_cryptodb(CACHE, start.year + 1, refresh=args.refresh)
    print(f"  {len(cryptodb)} conference papers")

    print("harvesting ePrint via OAI-PMH...")
    eprint = harvest_eprint(CACHE, refresh=args.refresh)
    print(f"  {len(eprint)} ePrint records")

    papers = join_venues(cryptodb, eprint)
    print(f"joined: {len(papers)} conference papers are also on ePrint")

    if not ROSTER.exists():
        raise SystemExit(
            f"no author roster at {ROSTER}\n"
            f"Run: python -m build.main --authors > {ROSTER.name}, then edit it."
        )
    roster = parse_roster(ROSTER.read_text(encoding="utf-8"))
    if not roster:
        raise SystemExit(f"the author roster at {ROSTER} lists nobody")
    papers = by_roster(papers, roster)
    print(f"  {len(papers)} are by one of the {len(roster)} authors on {ROSTER.name}")

    wanted = [
        (start + dt.timedelta(days=i)).isoformat()
        for i in range(args.days)
        if (start + dt.timedelta(days=i)).isoformat() not in lock["days"]
    ]
    if not wanted:
        print(f"schedule already covers {args.days} days from {start}")
        return

    queue = shuffled_pool(papers, args.seed, taken)
    citations = Citations(CACHE)
    print(f"planning {len(wanted)} new days ({wanted[0]} .. {wanted[-1]})")

    added = 0
    blocked = False
    # Candidates passed over only because their PDF could not be downloaded,
    # in draw order: what to fetch by hand if ePrint refuses the script.
    by_hand = []
    for iso in wanted:
        while queue:
            paper = queue.pop(0)
            try:
                pdf = fetch_pdf(paper["id"], download=not blocked)
                if pdf is None:
                    by_hand.append(paper["id"])
                    continue
                # Building it here is the point: a day only enters the
                # schedule once its PDF is proven to extract.
                build_puzzle(iso, paper, pdf)
            except Blocked as exc:
                _warn(f"{exc}; planning on from the PDFs already on disk only")
                blocked = True
                by_hand.append(paper["id"])
                continue
            except (ExtractionError, RuntimeError) as exc:
                print(f"  skip {paper['id']}: {exc}")
                continue

            PAPERS.mkdir(exist_ok=True)
            (PAPERS / _pdf_name(paper["id"])).write_bytes(pdf)

            # The count is shown on the result card but decides nothing, so a
            # lookup that cannot be completed leaves the day without one
            # rather than stopping the plan.
            try:
                cited = citations.get(paper["title"])
            except LookupFailed as exc:
                print(f"  no citation count for {paper['id']}: {exc}")
                cited = None

            lock["days"][iso] = {
                "id": paper["id"], "venue": paper["venue"], "year": paper["year"],
                "title": paper["title"], "authors": paper["authors"],
                "citations": cited,
            }
            print(f"  {iso}  {paper['venue']} {paper['year']}  {paper['id']}  {cited} cites")
            added += 1
            # Save as we go. Planning is slow enough to be interrupted, and a
            # run that loses every decided day leaves the schedule with holes.
            save_lock(lock)
            citations.save()
            break
        else:
            if not blocked:
                raise SystemExit(f"ran out of usable papers at {iso}")
            break

    citations.save()

    lock["seed"] = args.seed
    lock["generated"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    save_lock(lock)
    print(f"\nschedule now covers {len(lock['days'])} days, {added} added -> {LOCK}")

    unplanned = len(wanted) - added
    if unplanned:
        _warn(f"{unplanned} of the {len(wanted)} days asked for are still unplanned")
        print(f"\nePrint only serves PDFs to browsers. Save these into {CACHE / 'pdf'}/ "
              f"as YYYY-NNN.pdf, then plan again:")
        for eprint_id in by_hand[:unplanned]:
            print(f"  https://eprint.iacr.org/{eprint_id}.pdf")


def authors(args) -> None:
    """Print the roster of authors eligible to carry a puzzle.

    Deliberately written to stdout rather than over `authors.txt`: the file is
    meant to be edited by hand, and a regeneration that silently reinstated
    everyone struck off it would undo that work. Redirect it, then diff.
    """
    year = dt.datetime.now(dt.UTC).year
    print(f"harvesting CryptoDB ({' / '.join(v.upper() for v in VENUES)})...", file=sys.stderr)
    cryptodb = harvest_cryptodb(CACHE, year + 1, refresh=args.refresh)
    rows = author_roster(cryptodb, args.min_author_papers)

    print("# Authors eligible to carry a puzzle: a paper is scheduled only if at least")
    print("# one of its authors is listed here.")
    print("#")
    print(f"# Generated with: python -m build.main --authors --min-author-papers {args.min_author_papers}")
    print(f"# {len(rows)} authors, from {len(cryptodb)} papers at "
          f"{' / '.join(v.upper() for v in VENUES)}.")
    print("#")
    print("# Columns: CryptoDB author key (iacr.org/cryptodb/author.php?authorkey=N),")
    print("# papers at those venues, name. Delete or comment out a line to bar that")
    print("# author. Only the key is read; the count is informational and goes stale.")
    print("#")
    for key, count, name in rows:
        print(f"{key:>7}  {count:>4}  {name}")
    print(f"\nwrote {len(rows)} authors", file=sys.stderr)


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

    # Today is the only day that actually has to exist. Without this the build
    # succeeds on the strength of other days in the window and publishes a site
    # that tells every visitor there is no puzzle.
    if not args.all and today.isoformat() not in lock["days"]:
        raise SystemExit(
            f"schedule has no paper for today ({today}). "
            f"Run: python -m build.main --plan --days 180"
        )

    missing = [
        (today + dt.timedelta(days=offset)).isoformat()
        for offset in range(0, args.horizon + 1)
        if (today + dt.timedelta(days=offset)).isoformat() not in lock["days"]
    ]
    if missing:
        _warn(f"schedule runs out on {max(lock['days'])}; {len(missing)} of the next "
              f"{args.horizon} days are unplanned - run: python -m build.main --plan")

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
        try:
            puzzle = build_puzzle(iso, paper, load_pdf(paper["id"]))
        except (MissingPDF, ExtractionError) as exc:
            # Only today has to exist. A later day that cannot be built must
            # not hold back today's deploy; there is time to fix it.
            if iso == today.isoformat():
                raise SystemExit(f"cannot build today's puzzle ({iso}, {paper['id']}): {exc}")
            _warn(f"skipped {iso} ({paper['id']}): {exc}")
            # Never leave an older build of another paper under this date.
            (OUT / f"{iso}.json").unlink(missing_ok=True)
            (OUT / "pdf" / f"{iso}.pdf").unlink(missing_ok=True)
            continue
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
                "venues": [venue.upper() for venue in VENUES],
                "dates": dates,
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\nbuilt {built} new puzzles; site now serves {len(dates)} days ({dates[0]} .. {dates[-1]})")


def _warn(note: str) -> None:
    # Surface it as a CI annotation, not just a line in a long log.
    print(f"::warning::{note}" if os.environ.get("GITHUB_ACTIONS") else f"warning: {note}")


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
    parser.add_argument("--authors", action="store_true",
                        help="print the eligible-author roster to stdout instead of building")
    parser.add_argument("--days", type=int, default=180, help="days to plan ahead (--plan)")
    parser.add_argument("--start", default="", help="first day to plan (--plan, default: today UTC)")
    parser.add_argument("--seed", type=int, default=20260901, help="schedule shuffle seed (--plan)")
    parser.add_argument("--refresh", action="store_true", help="re-harvest IACR metadata (--plan)")
    parser.add_argument("--min-author-papers", type=int, default=20,
                        help="lower bound on papers at the venues for the roster "
                             "(--authors); which of those authors count is then "
                             "whatever authors.txt still lists")
    parser.add_argument("--back", type=int, default=2, help="days before today to keep serving")
    parser.add_argument("--horizon", type=int, default=21, help="days ahead to build")
    parser.add_argument("--today", default="", help="override today's date (testing)")
    parser.add_argument("--all", action="store_true", help="build every day in the schedule")
    parser.add_argument("--force", action="store_true", help="rebuild days that already exist")
    args = parser.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    if args.authors:
        authors(args)
    elif args.plan:
        plan(args)
    else:
        build(args)


if __name__ == "__main__":
    main()
