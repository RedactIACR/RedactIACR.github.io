# Redact IACR

A daily [Redactle](https://redactle.net)-style word game played over cryptology papers.
Every day one paper from **CRYPTO**, **EUROCRYPT** or **TCC** is pulled from the
Cryptology ePrint Archive, blacked out in full, and you uncover it one word at a
time. You win when every word of the title is revealed.

You read the **real PDF**, rendered with pdf.js and covered with black
rectangles — equations, figures, tables and all — rather than a reconstruction
of it.

The site is fully static: a Python build script produces a hundred days of
puzzles ahead of time, and the browser plays them with no server involved.

## Quick start

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m build.main --days 100      # ~15 minutes on a cold cache
.venv/bin/python -m http.server 8000 --directory site
```

Then open <http://localhost:8000>.

Deploy by serving `site/` as static files — GitHub Pages, Netlify, S3, anything.
There is no build step for the frontend and no runtime dependency.

## How the corpus is assembled

The two IACR data sources each hold half of what the game needs, so the build
joins them:

| Source | Provides | Missing |
| --- | --- | --- |
| [CryptoDB](https://iacr.org/cryptodb/) | which papers appeared at CRYPTO / EUROCRYPT / TCC | ePrint identifiers |
| [ePrint OAI-PMH](https://eprint.iacr.org/oai) | every ePrint id, title, authors, abstract | publication venue |

They share no identifier, so `build/corpus.py` joins them on a normalised
title. That yields roughly **3,300 papers** that both appeared at one of the
three venues and are available on ePrint. From those, a seeded shuffle assigns
one paper per day.

Only the scheduled papers' PDFs are downloaded — about 100 per build, not the
whole archive. Metadata harvests and PDFs are cached under `cache/`, so
re-running the build is cheap.

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

## Puzzle files

Each day ships as `site/puzzles/<date>.json` (metadata plus box geometry) and
`site/puzzles/pdf/<date>.pdf` (the paper itself). Box keys are indices into one
shared key table, since words repeat constantly; that alone is most of the
difference between a 700 KB payload and a 350 KB one.

Nothing is obfuscated. A hundred days are built ahead of time, so anyone who
opens tomorrow's PDF can read the answer early.

pdf.js is vendored under `site/vendor/` (345 KB plus a 1.4 MB worker), so the
site has no CDN dependency and works offline.

## Extending the schedule

Puzzle days run out 100 days after the build. To add more without disturbing
days already published:

```sh
.venv/bin/python -m build.main --days 100 --start 2026-12-11 --keep
```

`--keep` preserves existing puzzle files and excludes their papers from the new
draw, so no paper repeats. Drop `--keep` to regenerate from scratch. Use
`--refresh` to re-harvest IACR metadata (worth doing once a year, as new
conference proceedings appear).

## Layout

```
build/
  harvest.py   CryptoDB + ePrint OAI-PMH harvesting, disk-cached
  corpus.py    title-normalised join, seeded daily schedule
  boxes.py     PDF -> redaction rectangles (maths by font, hyphenation, keys)
  main.py      orchestrator CLI
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
