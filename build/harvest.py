"""Harvest the two source datasets the puzzle corpus is joined from.

CryptoDB (iacr.org/cryptodb) knows which papers appeared at CRYPTO, EUROCRYPT
and TCC but does not record ePrint identifiers.  The ePrint OAI-PMH feed knows
every ePrint identifier but does not record the publication venue.  We harvest
both and join them on the normalised title in `corpus.py`.

Both harvests are cached on disk so repeated builds do not re-hit IACR.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

USER_AGENT = "redact-iacr build script (+https://github.com/) - polite, cached"

CRYPTODB_URL = "https://iacr.org/cryptodb/data/conf.php"
OAI_URL = "https://eprint.iacr.org/oai"

VENUES = ("crypto", "eurocrypt", "tcc")

# CRYPTO from 1981, EUROCRYPT from 1982, TCC from 2004.  Asking CryptoDB for a
# year a venue did not run simply returns a page with no papers, so one
# generous range covers all three.
FIRST_YEAR = 1981

OAI_NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}

_PUB_TITLE_RE = re.compile(
    r'<span class="pub-title">\s*<a href="paper\.php\?pubkey=(\d+)">(.*?)</a>',
    re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _fetch(url: str, *, retries: int = 3, pause: float = 0.5) -> bytes:
    """GET a URL with a descriptive user agent and a small retry budget."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - network errors are all retryable here
            last = exc
            time.sleep(pause * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last}")


def harvest_cryptodb(cache_dir: Path, last_year: int, *, refresh: bool = False) -> list[dict]:
    """Return every CRYPTO/EUROCRYPT/TCC paper CryptoDB knows about."""
    cache_path = cache_dir / "cryptodb.json"
    if cache_path.exists() and not refresh:
        return json.loads(cache_path.read_text(encoding="utf-8"))

    papers: list[dict] = []
    for venue in VENUES:
        for year in range(FIRST_YEAR, last_year + 1):
            query = urllib.parse.urlencode({"venue": venue, "year": year})
            page = _fetch(f"{CRYPTODB_URL}?{query}").decode("utf-8", "replace")
            found = 0
            for pubkey, raw_title in _PUB_TITLE_RE.findall(page):
                title = html.unescape(_TAG_RE.sub("", raw_title)).strip()
                if not title:
                    continue
                papers.append(
                    {
                        "pubkey": pubkey,
                        "title": title,
                        "venue": venue.upper(),
                        "year": year,
                    }
                )
                found += 1
            print(f"  cryptodb {venue} {year}: {found} papers", flush=True)
            time.sleep(0.2)

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(papers, indent=1), encoding="utf-8")
    return papers


def harvest_eprint(cache_dir: Path, *, refresh: bool = False) -> list[dict]:
    """Return every record in the Cryptology ePrint Archive via OAI-PMH."""
    cache_path = cache_dir / "eprint.json"
    if cache_path.exists() and not refresh:
        return json.loads(cache_path.read_text(encoding="utf-8"))

    records: list[dict] = []
    url = f"{OAI_URL}?verb=ListRecords&metadataPrefix=oai_dc"
    while url:
        root = ElementTree.fromstring(_fetch(url))
        for record in root.iter(f"{{{OAI_NS['oai']}}}record"):
            parsed = _parse_oai_record(record)
            if parsed:
                records.append(parsed)
        print(f"  eprint: {len(records)} records", flush=True)

        token_el = root.find(".//oai:resumptionToken", OAI_NS)
        token = (token_el.text or "").strip() if token_el is not None else ""
        url = (
            f"{OAI_URL}?verb=ListRecords&resumptionToken={urllib.parse.quote(token)}"
            if token
            else ""
        )
        time.sleep(0.2)

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(records, indent=1), encoding="utf-8")
    return records


def _parse_oai_record(record: ElementTree.Element) -> dict | None:
    identifier_el = record.find(".//oai:header/oai:identifier", OAI_NS)
    if identifier_el is None or not identifier_el.text:
        return None
    # oai:eprint.iacr.org:2023/1234 -> 2023/1234
    eprint_id = identifier_el.text.rsplit(":", 1)[-1]
    if not re.fullmatch(r"\d{4}/\d+", eprint_id):
        return None

    # Deleted records carry a status attribute and no metadata block.
    header = record.find("oai:header", OAI_NS)
    if header is not None and header.get("status") == "deleted":
        return None

    def texts(tag: str) -> list[str]:
        return [
            el.text.strip()
            for el in record.iterfind(f".//dc:{tag}", OAI_NS)
            if el is not None and el.text and el.text.strip()
        ]

    titles = texts("title")
    if not titles:
        return None

    return {
        "id": eprint_id,
        "title": titles[0],
        "authors": texts("creator"),
        "abstract": (texts("description") or [""])[0],
        "keywords": texts("subject"),
    }
