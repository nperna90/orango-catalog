"""
build_catalog.py

ETL pipeline: Project Gutenberg's official bulk RDF dump -> SQLite (v1 DDL) -> gzip.

Pipeline: download_dump -> extract -> parse_rdf (per file) -> build_sqlite -> gzip_file.

Stdlib only -- tarfile, xml.etree.ElementTree, sqlite3, gzip, urllib, datetime,
logging. No rdflib/lxml/requests (see README.md + the orango app's 23.1 phase
RESEARCH.md for why: a targeted ElementTree walk over ~75k small, fixed-shape
RDF files is simpler and faster than general RDF graph construction).

Schema contract: catalog_meta.schema_version = 1. Any future column/type change
in `books` or `books_fts` is a version bump, never a silent edit -- the orango
app refuses/falls back on any schema_version it doesn't recognize.
"""

from __future__ import annotations

import datetime
import gzip
import logging
import os
import shutil
import sqlite3
import tarfile
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("build_catalog")

# Canonical URL -- NOT the stale feeds/catalog.rdf.bz2 (301s to a help page).
# See .planning/phases/22-librivox-catalog-audiobook-library/22-SPIKE-FINDINGS.md.
DUMP_URL = "https://www.gutenberg.org/cache/epub/feeds/rdf-files.tar.bz2"

SCHEMA_VERSION = 1

NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dcterms": "http://purl.org/dc/terms/",
    "pgterms": "http://www.gutenberg.org/2009/pgterms/",
    "dcam": "http://purl.org/dc/dcam/",
}

# <dcterms:hasFormat> MIME -> v1 DDL column name.
MIME_TO_FIELD = {
    "application/epub+zip": "epub_url",
    "image/jpeg": "cover_url",
    "audio/mpeg": "audio_mp3_url",
    "audio/ogg": "audio_ogg_url",
    "audio/mp4": "audio_m4b_url",
    "audio/x-m4b": "audio_m4b_url",
}

DDL = """
CREATE TABLE catalog_meta (
  id INTEGER PRIMARY KEY CHECK (id = 0),
  schema_version INTEGER NOT NULL,
  generated_at TEXT NOT NULL,
  pg_dump_date TEXT,
  row_count INTEGER NOT NULL
);
CREATE TABLE books (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  authors TEXT NOT NULL DEFAULT '',
  subjects TEXT NOT NULL DEFAULT '',
  bookshelves TEXT NOT NULL DEFAULT '',
  languages TEXT NOT NULL DEFAULT '',
  download_count INTEGER NOT NULL DEFAULT 0,
  media_type TEXT NOT NULL DEFAULT 'Text',
  epub_url TEXT,
  cover_url TEXT,
  audio_mp3_url TEXT,
  audio_ogg_url TEXT,
  audio_m4b_url TEXT
);
CREATE INDEX idx_books_download_count ON books(download_count DESC);
-- fts4 (not fts5): Android framework SQLite ships FTS3/FTS4 only -- fts5 has
-- no module on-device (verified against current AOSP external/sqlite
-- Android.bp: cflags enable FTS3/FTS3_BACKWARDS/FTS4, zero occurrences of
-- FTS5, on every API level). See 23.1-05 root-cause diagnosis.
-- content="books": external-content table, keeps the artifact small (index
-- only, text stays in books). The old FTS5-only content-rowid parameter is
-- dropped entirely -- fts4 has no such option; books.id IS books' rowid, so
-- the docid maps automatically.
-- tokenize=unicode61 "remove_diacritics=1": FTS4 tokenizer arg spelling
-- (name unquoted, each arg a quoted string) -- a different shape from FTS5's
-- single quoted-string tokenizer spec. =1 (not =2) for SQLite < 3.27
-- compatibility on older Android (minSdk 26 / Android 8.0).
CREATE VIRTUAL TABLE books_fts USING fts4(
  title, authors, subjects, bookshelves,
  content="books",
  tokenize=unicode61 "remove_diacritics=1"
);
"""


def download_dump(url: str, dest: str) -> str | None:
    """Fetch the PG bulk RDF dump to `dest`. Returns the Last-Modified header
    value (used as catalog_meta.pg_dump_date), or None if absent."""
    req = urllib.request.Request(url, headers={"User-Agent": "orango-catalog/1.0"})
    # timeout=60 is a per-socket-operation stall guard, not a total-transfer cap:
    # a stalled connection raises instead of hanging the CI runner until GitHub's
    # 6-hour job kill (the next weekly cron run retries).
    with urllib.request.urlopen(req, timeout=60) as response:  # noqa: S310 - trusted PG source
        pg_dump_date = response.headers.get("Last-Modified")
        with open(dest, "wb") as f:
            shutil.copyfileobj(response, f)
    return pg_dump_date


def extract(dump_path: str, work_dir: str) -> None:
    """Extract the tar.bz2 dump to disk FIRST, then callers walk the plain .rdf
    files from `work_dir`. Never stream-parse tar members one at a time while
    still inside the bz2 layer (Pitfall 3 -- serializes two CPU-bound steps)."""
    with tarfile.open(dump_path, "r:bz2") as tar:
        # filter="data" refuses ../ traversal, absolute paths, links outside the
        # archive, and device/special members (tar-slip) -- this archive is fetched
        # fresh from the network every week, in a CI job holding a contents:write
        # token. Also pins the 3.12+ extraction semantics explicitly (the default
        # filter changes in Python 3.14).
        tar.extractall(work_dir, filter="data")


def _extract_id(root: ET.Element) -> int | None:
    ebook = root.find("pgterms:ebook", NS)
    if ebook is None:
        return None
    about = ebook.get(f"{{{NS['rdf']}}}about", "")  # e.g. "ebooks/26471"
    digits = "".join(ch for ch in about.rsplit("/", 1)[-1] if ch.isdigit())
    return int(digits) if digits else None


def _extract_rdf_value_list(elements: list[ET.Element]) -> list[str]:
    """For a list of elements each shaped `<X><rdf:Description><rdf:value>...`
    (dcterms:subject, pgterms:bookshelf, dcterms:language all share this
    shape), collect the non-empty rdf:value text from each. Missing/empty
    values are silently skipped -- never raises on a thin record."""
    values = []
    for el in elements:
        value_el = el.find("rdf:Description/rdf:value", NS)
        if value_el is not None and value_el.text:
            values.append(value_el.text.strip())
    return values


def parse_rdf(rdf_bytes: bytes) -> dict[str, Any] | None:
    """Targeted ElementTree walk extracting the v1 DDL fields from one book's
    RDF/XML. Defensive per-field fallback (Assumption A3): a thin/legacy record
    with missing elements returns empty-string/None/0 defaults rather than
    raising. Returns None only when the record has no discoverable id (an
    unusable row) or the bytes don't parse as XML at all -- callers log and
    skip those, they never abort the whole build."""
    try:
        root = ET.fromstring(rdf_bytes)
    except ET.ParseError as e:
        logger.warning("parse_rdf: XML parse error: %s", e)
        return None

    ebook = root.find("pgterms:ebook", NS)
    if ebook is None:
        logger.warning("parse_rdf: no pgterms:ebook element found")
        return None

    book_id = _extract_id(root)
    if book_id is None:
        logger.warning("parse_rdf: could not determine book id")
        return None

    title_el = ebook.find("dcterms:title", NS)
    title = (title_el.text or "").strip() if title_el is not None else ""
    if not title:
        logger.warning("parse_rdf(%s): missing title", book_id)

    authors = []
    for creator in ebook.findall("dcterms:creator", NS):
        name_el = creator.find("pgterms:agent/pgterms:name", NS)
        if name_el is not None and name_el.text:
            authors.append(name_el.text.strip())
    authors_str = "; ".join(authors)

    subjects_str = "; ".join(_extract_rdf_value_list(ebook.findall("dcterms:subject", NS)))
    bookshelves_str = "; ".join(_extract_rdf_value_list(ebook.findall("pgterms:bookshelf", NS)))
    languages_str = ",".join(_extract_rdf_value_list(ebook.findall("dcterms:language", NS)))

    media_type = "Text"
    type_el = ebook.find("dcterms:type", NS)
    if type_el is not None:
        value_el = type_el.find("rdf:Description/rdf:value", NS)
        if value_el is not None and value_el.text:
            media_type = value_el.text.strip()

    download_count = 0
    downloads_el = ebook.find("pgterms:downloads", NS)
    if downloads_el is not None and downloads_el.text:
        try:
            download_count = int(downloads_el.text.strip())
        except ValueError:
            logger.warning(
                "parse_rdf(%s): non-numeric downloads value %r", book_id, downloads_el.text
            )

    formats: dict[str, str] = {}
    for has_format in ebook.findall("dcterms:hasFormat", NS):
        file_el = has_format.find("pgterms:file", NS)
        if file_el is None:
            continue
        url = file_el.get(f"{{{NS['rdf']}}}about")
        if not url:
            continue
        format_value_el = file_el.find("dcterms:format/rdf:Description/rdf:value", NS)
        mime = (
            format_value_el.text.strip()
            if format_value_el is not None and format_value_el.text
            else None
        )
        field = MIME_TO_FIELD.get(mime) if mime else None
        if field and field not in formats:
            formats[field] = url

    return {
        "id": book_id,
        "title": title,
        "authors": authors_str,
        "subjects": subjects_str,
        "bookshelves": bookshelves_str,
        "languages": languages_str,
        "download_count": download_count,
        "media_type": media_type,
        "epub_url": formats.get("epub_url"),
        "cover_url": formats.get("cover_url"),
        "audio_mp3_url": formats.get("audio_mp3_url"),
        "audio_ogg_url": formats.get("audio_ogg_url"),
        "audio_m4b_url": formats.get("audio_m4b_url"),
    }


def build_sqlite(
    records: list[dict[str, Any]], out_path: str, pg_dump_date: str | None = None
) -> None:
    """Create `out_path` with the exact v1 DDL, insert every record (all
    columns specified explicitly), populate books_fts with a single one-time
    INSERT...SELECT (external-content, read-only artifact -- no live triggers
    needed), write the catalog_meta row, and assert PRAGMA integrity_check."""
    if os.path.exists(out_path):
        os.remove(out_path)

    conn = sqlite3.connect(out_path)
    try:
        conn.executescript(DDL)
        conn.executemany(
            """
            INSERT INTO books (
                id, title, authors, subjects, bookshelves, languages,
                download_count, media_type, epub_url, cover_url,
                audio_mp3_url, audio_ogg_url, audio_m4b_url
            ) VALUES (
                :id, :title, :authors, :subjects, :bookshelves, :languages,
                :download_count, :media_type, :epub_url, :cover_url,
                :audio_mp3_url, :audio_ogg_url, :audio_m4b_url
            )
            """,
            records,
        )
        conn.execute(
            "INSERT INTO books_fts(rowid, title, authors, subjects, bookshelves) "
            "SELECT id, title, authors, subjects, bookshelves FROM books"
        )
        generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO catalog_meta (id, schema_version, generated_at, pg_dump_date, row_count) "
            "VALUES (0, ?, ?, ?, ?)",
            (SCHEMA_VERSION, generated_at, pg_dump_date, len(records)),
        )
        conn.commit()

        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"PRAGMA integrity_check failed: {integrity}")
    finally:
        conn.close()


def gzip_file(db_path: str) -> str:
    """Gzip-compress (level 9) `db_path` to `{db_path}.gz`, returning the gz path."""
    gz_path = db_path + ".gz"
    with open(db_path, "rb") as f_in, gzip.open(gz_path, "wb", compresslevel=9) as f_out:
        shutil.copyfileobj(f_in, f_out)
    return gz_path


def main() -> None:
    work_dir = "work"
    dump_path = "rdf-files.tar.bz2"
    os.makedirs(work_dir, exist_ok=True)

    logger.info("Downloading %s ...", DUMP_URL)
    pg_dump_date = download_dump(DUMP_URL, dump_path)

    logger.info("Extracting %s ...", dump_path)
    extract(dump_path, work_dir)

    records: list[dict[str, Any]] = []
    skipped = 0
    for dirpath, _dirnames, filenames in os.walk(work_dir):
        for filename in filenames:
            if not filename.endswith(".rdf"):
                continue
            path = os.path.join(dirpath, filename)
            with open(path, "rb") as f:
                rdf_bytes = f.read()
            record = parse_rdf(rdf_bytes)
            if record is None:
                skipped += 1
                continue
            records.append(record)

    logger.info("Parsed %d usable records (%d skipped/unusable)", len(records), skipped)

    # If the gz artifact balloons past the ~15-25 MB target, the lever is to
    # drop non-EPUB Text rows or prune less-used format columns -- do NOT
    # implement pre-emptively, only if a real build shows a size overrun.
    build_sqlite(records, "catalog.db", pg_dump_date=pg_dump_date)
    gz_path = gzip_file("catalog.db")
    gz_size_mb = os.path.getsize(gz_path) / (1024 * 1024)
    logger.info("Wrote %s (%.1f MB) with %d rows", gz_path, gz_size_mb, len(records))


if __name__ == "__main__":
    main()
