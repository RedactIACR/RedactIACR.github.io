"""Locate every redactable thing in an ePrint PDF, in PDF coordinates.

The game renders the real PDF with pdf.js and lays black rectangles over the
words, so nothing here reconstructs the document — it only says *where* things
are and *what guess* uncovers them.

Two kinds of box come out:

    word    one rectangle per word, revealed by guessing that word
    math    one rectangle per formula, revealed by any identifier inside it

Maths is found by font: TeX sets it in dedicated families (CMMI, CMSY, MSBM,
LMMath, …) while body text uses a text face. Without this, every symbol in the
paper would be given away for free, since symbols are not words.

Coordinates are PDF points with a top-left origin, which is what PyMuPDF
reports and what pdf.js's viewport uses at scale 1, so the client only has to
multiply by its render scale.
"""

from __future__ import annotations

import re
import unicodedata

import pymupdf

_MATH_FONT_RE = re.compile(
    r"""(?:^|\b)(
        CMMI | CMSY | CMEX | CMBSY | CMMIB
      | MSAM | MSBM
      | RSFS | EUFM | EUFB | EUSM | EUSB | EURM
      | LMMath | LMSY | LMMI | LMEX
      | (?:r?tx|px)(?:mi|sy|ex)
      | .*Math(?:ematical)?(?:Italic|Symbol)?
    )""",
    re.I | re.X,
)
_SUBSET_PREFIX_RE = re.compile(r"^[A-Z]{6}\+")

# Characters that belong to a formula even when set in the text font, e.g. the
# "2" of $2^{128}$ or the parentheses of $f(x)$.
_MATH_GLUE = set("0123456789()[]{}|+-*/=<>^_,.'′−·×")

_WORD_RE = re.compile(r"[0-9A-Za-zÀ-ɏ]+")
_LETTERS_RE = re.compile(r"[^A-Za-zÀ-ɏ]+")
_LEADING_DIGITS_RE = re.compile(r"^\d+")

_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
    "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
}

_GLYPH_KEYWORDS = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta",
    "ε": "epsilon", "ϵ": "epsilon", "ζ": "zeta", "η": "eta",
    "θ": "theta", "κ": "kappa", "λ": "lambda", "μ": "mu",
    "ν": "nu", "ξ": "xi", "π": "pi", "ρ": "rho",
    "σ": "sigma", "τ": "tau", "φ": "phi", "ϕ": "phi",
    "χ": "chi", "ψ": "psi", "ω": "omega",
    "Γ": "gamma", "Δ": "delta", "Θ": "theta", "Λ": "lambda",
    "Π": "pi", "Σ": "sigma", "Φ": "phi", "Ψ": "psi",
    "Ω": "omega",
}


class ExtractionError(RuntimeError):
    """Raised when a PDF cannot be turned into a playable puzzle."""


def _is_math_font(name: str) -> bool:
    return bool(_MATH_FONT_RE.match(_SUBSET_PREFIX_RE.sub("", name or "")))


def normalise(text: str) -> str:
    for ligature, replacement in _LIGATURES.items():
        text = text.replace(ligature, replacement)
    text = unicodedata.normalize("NFC", text)
    return text.replace("’", "'").replace("‘", "'")


def word_tokens(text: str) -> list[str]:
    """Lowercase word tokens of a metadata string, as the player would type."""
    return [word.lower() for word in _WORD_RE.findall(normalise(text))]


def math_keywords(text: str) -> list[str]:
    """Identifiers that should reveal a formula.

    Two letters minimum, so `negl`, `poly` and `Zp` are guessable but the "2k"
    of an exponent is not. A formula with no keywords is pure notation; those
    get no box at all rather than one no guess could reach.
    """
    keywords = {
        _LEADING_DIGITS_RE.sub("", word).lower()
        for word in _WORD_RE.findall(text)
        if len(_LETTERS_RE.sub("", word)) > 1
    }
    keywords.update(_GLYPH_KEYWORDS[ch] for ch in text if ch in _GLYPH_KEYWORDS)
    return sorted(k for k in keywords if k)


# ------------------------------------------------------------------ glyphs

def _page_glyphs(page: pymupdf.Page) -> list[list[dict]]:
    """Return the page as lines of glyphs, each carrying its own rectangle.

    Ligatures are expanded so that "puriﬁcation" tokenises as the word it is;
    both letters of "ﬁ" share the ligature's rectangle, which is close enough
    to cover it.
    """
    lines: list[list[dict]] = []
    for block in page.get_text("rawdict", sort=True)["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            glyphs: list[dict] = []
            for span in line["spans"]:
                is_math = _is_math_font(span["font"])
                for char in span["chars"]:
                    text = normalise(char["c"])
                    if not text or ord(text[0]) < 32:
                        continue
                    for piece in _LIGATURES.get(text, text):
                        glyphs.append({"c": piece, "bbox": char["bbox"], "math": is_math})
            if glyphs:
                lines.append(glyphs)
    return lines


def _union(glyphs: list[dict]) -> list[int]:
    """Bounding rectangle, in whole points and rounded outwards.

    Integers keep the payload small; rounding outwards (and padding by a
    point) guarantees the black rectangle still covers the glyphs it hides,
    which half-point precision would not once the client scales it up.
    """
    x0 = min(g["bbox"][0] for g in glyphs)
    y0 = min(g["bbox"][1] for g in glyphs)
    x1 = max(g["bbox"][2] for g in glyphs)
    y1 = max(g["bbox"][3] for g in glyphs)
    left, top = int(x0), int(y0)
    return [left, top, int(x1 + 1) - left, int(y1 + 1) - top]


def _mark_math(glyphs: list[dict]) -> None:
    """Absorb digits and operators sitting inside a formula."""
    flags = [g["math"] for g in glyphs]
    for index, glyph in enumerate(glyphs):
        if flags[index] or glyph["c"] not in _MATH_GLUE:
            continue
        before = index > 0 and flags[index - 1]
        after = index + 1 < len(glyphs) and glyphs[index + 1]["math"]
        if before or after:
            flags[index] = True
    for glyph, flag in zip(glyphs, flags):
        glyph["math"] = flag


# ------------------------------------------------------------------- boxes

def _line_boxes(glyphs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split one line into word tokens and formula runs."""
    _mark_math(glyphs)

    words: list[dict] = []
    formulas: list[dict] = []

    index = 0
    while index < len(glyphs):
        if glyphs[index]["math"]:
            start = index
            while index < len(glyphs) and glyphs[index]["math"]:
                index += 1
            run = glyphs[start:index]
            # Trailing sentence punctuation belongs to the sentence.
            while run and run[-1]["c"] in ",.;:":
                run.pop()
            if run:
                text = "".join(g["c"] for g in run)
                keywords = math_keywords(text)
                if keywords:
                    formulas.append({"bbox": _union(run), "keys": keywords, "text": text})
            continue

        if _WORD_RE.match(glyphs[index]["c"]):
            start = index
            while index < len(glyphs) and not glyphs[index]["math"] and _WORD_RE.match(glyphs[index]["c"]):
                index += 1
            run = glyphs[start:index]
            words.append({"bbox": _union(run), "text": "".join(g["c"] for g in run)})
            continue

        index += 1

    return words, formulas


def _resolve_hyphenation(pages: list[dict]) -> None:
    """Give both halves of a word TeX broke across lines the same guess key.

    "modulus-\\nto-noise" and "pro-\\ngrams" are indistinguishable line-locally
    but distinguishable statistically: if both fragments occur as standalone
    words elsewhere the hyphen was real, otherwise the word was broken and the
    two boxes should answer to the joined word.
    """
    frequency: dict[str, int] = {}
    for page in pages:
        for line in page["lines"]:
            for word in line["words"]:
                frequency[word["text"].lower()] = frequency.get(word["text"].lower(), 0) + 1

    for page in pages:
        lines = page["lines"]
        for index, line in enumerate(lines[:-1]):
            if not line["hyphenated"] or not line["words"]:
                continue
            following = lines[index + 1]
            if not following["words"]:
                continue

            left, right = line["words"][-1], following["words"][0]
            # Each fragment vouches for itself once; require other evidence.
            real = (
                frequency.get(left["text"].lower(), 0) > 1
                and frequency.get(right["text"].lower(), 0) > 1
            )
            if not real:
                joined = (left["text"] + right["text"]).lower()
                left["key"] = joined
                right["key"] = joined


def extract_boxes(pdf_bytes: bytes) -> dict:
    """Return the page geometry and redaction boxes for one paper."""
    try:
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"cannot open PDF: {exc}") from exc

    if document.is_encrypted and not document.authenticate(""):
        raise ExtractionError("PDF is encrypted")

    pages: list[dict] = []
    for page in document:
        if page.rotation:
            # pdf.js applies rotation in its viewport; PyMuPDF reports
            # pre-rotation boxes, so the two would disagree.
            raise ExtractionError(f"page {page.number} is rotated")

        lines: list[dict] = []
        for glyphs in _page_glyphs(page):
            words, formulas = _line_boxes(glyphs)
            for word in words:
                word["key"] = word["text"].lower()
            lines.append(
                {
                    "words": words,
                    "formulas": formulas,
                    "hyphenated": glyphs[-1]["c"] == "-",
                }
            )
        pages.append({"w": round(page.rect.width, 1), "h": round(page.rect.height, 1), "lines": lines})

    _resolve_hyphenation(pages)

    total_words = sum(len(l["words"]) for p in pages for l in p["lines"])
    if total_words < 400:
        raise ExtractionError(f"too little text to be a real paper ({total_words} words)")

    return {
        "pages": pages,
        "words": total_words,
        "formulas": sum(len(l["formulas"]) for p in pages for l in p["lines"]),
        "page_count": document.page_count,
    }


def pack_pages(pages: list[dict]) -> tuple[list[dict], list[str]]:
    """Flatten to the wire format: boxes referencing a shared key table.

    Words repeat constantly, so storing an index into one table of keys rather
    than the key itself is a large saving across a whole paper.
    """
    table: dict[str, int] = {}

    def key_id(key: str) -> int:
        if key not in table:
            table[key] = len(table)
        return table[key]

    packed: list[dict] = []
    for page in pages:
        words: list = []
        formulas: list = []
        for line in page["lines"]:
            for word in line["words"]:
                words.append(word["bbox"] + [key_id(word["key"])])
            for formula in line["formulas"]:
                formulas.append(formula["bbox"] + [[key_id(k) for k in formula["keys"]]])
        packed.append({"w": page["w"], "h": page["h"], "words": words, "math": formulas})

    keys = [""] * len(table)
    for key, index in table.items():
        keys[index] = key
    return packed, keys
