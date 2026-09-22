# Redact IACR

A daily [Redactle](https://redactle.net)-style word game played over cryptology papers.
Every day one paper from **CRYPTO**, **EUROCRYPT**, **ASIACRYPT** or **TCC** is
pulled from the Cryptology ePrint Archive, blacked out in full, and you uncover it
one word at a time. You win when every word of the title is revealed, or when you
have guessed the full name of any one author.

You read the **real PDF**, rendered with pdf.js and covered with black
rectangles — equations, figures, tables and all — rather than a reconstruction
of it.

The site is fully static: a Python build script produces a hundred days of
puzzles ahead of time, and the browser plays them with no server involved.

## Quick start

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m build.main --plan --days 180   # decide the schedule (slow, once)
.venv/bin/python -m build.main                     # build the days around today
.venv/bin/python -m http.server 8000 --directory site
```

Then open <http://localhost:8000>.

Scheduling and building are separate on purpose. **Which paper falls on which
day is decided once** and committed to `schedule.json`; builds then read that
file. Re-deriving the schedule per build would be unstable twice over: the
shuffle is positional, so a build starting "today" hands the same paper to
every first day, and the pool grows as IACR publishes, which reshuffles every
later assignment.

`--plan` needs the network (IACR metadata, plus every newly scheduled PDF,
which it downloads to prove the paper extracts before fixing it in the
schedule). A plain build needs only PDFs, which are cached — so a routine
deploy fetches one new paper.

## Automatic deployment

[`.github/workflows/deploy.yml`](.github/workflows/deploy.yml) builds and
publishes to GitHub Pages on every push and daily at 03:20 UTC. Enable it under
**Settings → Pages → Source → GitHub Actions**.

The site serves a rolling window — two days back, three weeks ahead — rather
than the whole schedule, because only one day is ever playable. That keeps it
near 25 MB instead of growing without bound, and means tomorrow's puzzle is
already published before midnight, so the 00:00 UTC rollover is seamless even
if a run is late. A warm-cache rebuild takes about nine seconds.

`site/puzzles/` is therefore **not committed**: CI generates it and uploads it
straight to Pages, so the repository stays small.

When the schedule starts running out the deploy logs a CI warning. Run
[`.github/workflows/plan.yml`](.github/workflows/plan.yml) ("Extend the
schedule") from the Actions tab; it plans further ahead and commits the result.

Two GitHub limits worth knowing: Pages allows a 1 GB site and roughly 100 GB of
bandwidth a month, and a play costs about 1 MB (the PDF plus its boxes), so the
ceiling is on the order of 100,000 plays a month. Separately, GitHub disables
scheduled workflows after 60 days with no repository activity — if the game
ever goes quiet for that long, re-enable it from the Actions tab.

## How the corpus is assembled

The two IACR data sources each hold half of what the game needs, so the build
joins them:

| Source | Provides | Missing |
| --- | --- | --- |
| [CryptoDB](https://iacr.org/cryptodb/) | which papers appeared at CRYPTO / EUROCRYPT / ASIACRYPT / TCC, and their authors | ePrint identifiers |
| [ePrint OAI-PMH](https://eprint.iacr.org/oai) | every ePrint id, title, authors, abstract | publication venue |
| [Semantic Scholar](https://www.semanticscholar.org/product/api) | citation counts, shown on the result card | everything else |

They share no identifier, so `build/corpus.py` joins them on a normalised
title. That yields roughly **4,500 papers** that both appeared at one of the
four venues and are available on ePrint. From those, a seeded shuffle assigns
one paper per day.

Only the scheduled papers' PDFs are downloaded — about 100 per build, not the
whole archive. Metadata harvests and PDFs are cached under `cache/`, so
re-running the build is cheap.

### Only papers with a prolific author

A paper is scheduled only if at least one of its authors has **35 or more
papers** across CRYPTO, EUROCRYPT, ASIACRYPT and TCC (`--min-author-papers`).
The count comes from CryptoDB itself: every venue page links each author by
CryptoDB's author key, which stays the same across a person's papers however
their name is spelled on each, so counting keys counts people rather than name
variants. All of CryptoDB's papers at the venues count, not only the ones also
on ePrint.

About a hundred authors clear 35, and the papers they coauthored come to
roughly **2,400** of the joined corpus, or six and a half years of daily
puzzles. Unlike a citation bound, the rule does not skew the game old: a new
paper qualifies the day it appears.

Citation counts are still looked up, from Semantic Scholar, but only to show on
the result card and only for papers actually scheduled. They decide nothing, so
a lookup that cannot be completed leaves that day without a count rather than
stopping the plan. Semantic Scholar's free tier is roughly 100 requests per
five minutes, and setting `SEMANTIC_SCHOLAR_API_KEY` raises it. `/search/match`
returns its single best guess, which for a short or generic title can be a
different paper, so an exact title match is required.

## How papers are redacted

Nothing reconstructs the document. [`build/boxes.py`](build/boxes.py) only works
out *where* each redactable thing sits and *what guess* uncovers it, and emits
rectangles in PDF points:

```
word    one rectangle per word, revealed by guessing that word
math    one rectangle per formula, revealed by any identifier inside it
```

PyMuPDF reports coordinates with a top-left origin, which is exactly what
pdf.js's viewport uses at scale 1, so the client only multiplies by its render
scale.

A few decisions worth knowing about:

- **Maths is found by font.** TeX sets it in dedicated families (`CMMI`,
  `CMSY`, `MSBM`, `LMMath`, …) while body text uses a text face. Without this
  every symbol in the paper would be free, since symbols are not words.
- **Formulas are revealed by their identifiers.** Guessing `negl`, `poly` or
  `lambda` uncovers every formula containing that identifier. A formula that is
  pure notation (`ℓ, k, n`) has nothing guessable in it, so it gets no box —
  otherwise it would be a rectangle no guess could ever reach.
- **Hyphenation is resolved statistically.** `modulus-\nto-noise` and
  `pro-\ngrams` are indistinguishable line-locally. The build decides using the
  rest of the paper: if both fragments occur as standalone words elsewhere the
  hyphen was real, otherwise the word was broken and *both* rectangles answer
  to the joined word. Without this, broken words would be unguessable.
- **Ligatures are expanded.** `puriﬁcation` is one glyph run in the PDF but the
  word you would type is "purification"; both letters of `ﬁ` share the
  ligature's rectangle.
- **Rectangles are rounded outwards** and padded by a point, so they still
  cover their glyphs once the client scales them up.
- **No text layer is rendered**, deliberately — one would put the answer in the
  DOM for Ctrl+F to find.
- **A guess matches the word's inflections.** `require` uncovers *requires*,
  *required* and *requiring*, and counts them as one guess. The client groups
  the paper's vocabulary by stem at load, using **Porter's step 1 only** —
  plural, third person, past tense, gerund. Full Porter would also fold
  `general` into `gener` and `university` into `univers`, merging words that
  mean different things and uncovering text the player never guessed. One
  addition to Porter: a trailing `e` is dropped from the stem, since step 1 is
  otherwise inconsistent about silent `e` (it strips it from *assumed* but
  keeps it on *assume*, and leaves *hashes* as `hashe`).

## Puzzle files

Each day ships as `site/puzzles/<date>.json` (metadata plus box geometry) and
`site/puzzles/pdf/<date>.pdf` (the paper itself). Box keys are indices into one
shared key table, since words repeat constantly; that alone is most of the
difference between a 700 KB payload and a 350 KB one.

Nothing is obfuscated. Three weeks are published ahead of time, so anyone who
opens tomorrow's PDF can read the answer early.

pdf.js is vendored under `site/vendor/` (345 KB plus a 1.4 MB worker), so the
site has no CDN dependency and works offline.

## Extending the schedule

```sh
.venv/bin/python -m build.main --plan --days 365            # cover a year from today
.venv/bin/python -m build.main --plan --days 365 --refresh  # ... and re-harvest IACR first
```

Planning is **append-only**: a day already in `schedule.json` is never
reassigned, and papers already scheduled are excluded from the draw, so nothing
repeats and no published day changes. `--refresh` re-harvests IACR metadata,
worth doing once a year as new proceedings appear.

Useful build flags: `--all` builds every day in the schedule rather than the
window, `--today` overrides the date for testing, and `--force` rebuilds days
that already exist.

## Layout

```
schedule.json  committed: which paper runs on which day, append-only
.github/
  workflows/   daily build + Pages deploy; manual schedule extension
build/
  harvest.py   CryptoDB + ePrint OAI-PMH harvesting, disk-cached
  corpus.py    title-normalised join, seeded daily schedule
  boxes.py     PDF -> redaction rectangles (maths by font, hyphenation, keys)
  main.py      orchestrator CLI (--plan to schedule, default to build)
site/
  index.html   markup
  styles.css   styling
  app.js       game logic — pdf.js viewer, redaction overlay, guessing, stats
  vendor/      pdf.js
  puzzles/     generated: index.json, one JSON + one PDF per day
```

## Credits

Papers are © their authors and are served from the
[Cryptology ePrint Archive](https://eprint.iacr.org); most carry a Creative
Commons licence. Venue data from [CryptoDB](https://iacr.org/cryptodb/).
Rendering by [pdf.js](https://mozilla.github.io/pdf.js/).
The game is a homage to [Redactle](https://redactle.net) by John Lawler.
