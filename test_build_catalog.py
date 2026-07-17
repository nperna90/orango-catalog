"""
test_build_catalog.py

Pytest coverage for build_catalog.py:
- RDF field extraction from 3 inline fixtures (a Sound/audiobook record, a minimal
  Text-book record, and a malformed/thin legacy record) covering the v1 DDL fields.
- SQLite (v1 DDL) + FTS5 build behavior: schema_version, row_count, MATCH queries,
  and diacritic-insensitive search (tokenize='unicode61 remove_diacritics 2').

Fixture provenance: the Sound fixture's shape (namespaces, dcterms:type Sound
classification, dcterms:hasFormat audio blocks) is abbreviated from the live
26471.rdf sample captured in
.planning/phases/22-librivox-catalog-audiobook-library/22-SPIKE-FINDINGS.md §3.
"""

import sqlite3

from build_catalog import build_sqlite, parse_rdf

# --- Fixture 1: a Sound (audiobook) record, abbreviated from the live 26471.rdf
# sample in 22-SPIKE-FINDINGS.md §3 (Spoon River Anthology). ---
FIXTURE_SOUND = """<?xml version="1.0" encoding="utf-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
   xmlns:dcterms="http://purl.org/dc/terms/"
   xmlns:pgterms="http://www.gutenberg.org/2009/pgterms/"
   xmlns:dcam="http://purl.org/dc/dcam/">
  <pgterms:ebook rdf:about="ebooks/26471">
    <dcterms:title>Spoon River Anthology</dcterms:title>
    <dcterms:creator>
      <pgterms:agent rdf:about="2009/agents/1234">
        <pgterms:name>Masters, Edgar Lee</pgterms:name>
      </pgterms:agent>
    </dcterms:creator>
    <dcterms:subject>
      <rdf:Description>
        <rdf:value>American poetry</rdf:value>
        <dcam:memberOf rdf:resource="http://purl.org/dc/terms/LCSH"/>
      </rdf:Description>
    </dcterms:subject>
    <dcterms:language>
      <rdf:Description>
        <rdf:value rdf:datatype="http://purl.org/dc/terms/RFC4646">en</rdf:value>
      </rdf:Description>
    </dcterms:language>
    <dcterms:type>
      <rdf:Description rdf:nodeID="N60ea75ec3f4f4034bbf4804b77e53662">
        <dcam:memberOf rdf:resource="http://purl.org/dc/terms/DCMIType"/>
        <rdf:value>Sound</rdf:value>
      </rdf:Description>
    </dcterms:type>
    <pgterms:downloads rdf:datatype="http://www.w3.org/2001/XMLSchema#nonNegativeInteger">30062</pgterms:downloads>
    <dcterms:hasFormat>
      <pgterms:file rdf:about="https://www.gutenberg.org/files/26471/ogg/26471-01.ogg">
        <dcterms:format>
          <rdf:Description>
            <rdf:value rdf:datatype="http://purl.org/dc/terms/IMT">audio/ogg</rdf:value>
          </rdf:Description>
        </dcterms:format>
      </pgterms:file>
    </dcterms:hasFormat>
    <dcterms:hasFormat>
      <pgterms:file rdf:about="https://www.gutenberg.org/files/26471/mp3/26471-01.mp3">
        <dcterms:format>
          <rdf:Description>
            <rdf:value rdf:datatype="http://purl.org/dc/terms/IMT">audio/mpeg</rdf:value>
          </rdf:Description>
        </dcterms:format>
      </pgterms:file>
    </dcterms:hasFormat>
  </pgterms:ebook>
</rdf:RDF>
""".encode("utf-8")

# --- Fixture 2: a minimal Text book (no subjects/bookshelves — exercises the
# "normal but sparse" path, distinct from the deliberately-malformed fixture below). ---
FIXTURE_TEXT = """<?xml version="1.0" encoding="utf-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
   xmlns:dcterms="http://purl.org/dc/terms/"
   xmlns:pgterms="http://www.gutenberg.org/2009/pgterms/"
   xmlns:dcam="http://purl.org/dc/dcam/">
  <pgterms:ebook rdf:about="ebooks/98">
    <dcterms:title>A Tale of Two Cities</dcterms:title>
    <dcterms:creator>
      <pgterms:agent rdf:about="2009/agents/25">
        <pgterms:name>Dickens, Charles</pgterms:name>
      </pgterms:agent>
    </dcterms:creator>
    <dcterms:type>
      <rdf:Description rdf:nodeID="Ntext0001">
        <dcam:memberOf rdf:resource="http://purl.org/dc/terms/DCMIType"/>
        <rdf:value>Text</rdf:value>
      </rdf:Description>
    </dcterms:type>
    <pgterms:downloads rdf:datatype="http://www.w3.org/2001/XMLSchema#nonNegativeInteger">54321</pgterms:downloads>
    <dcterms:hasFormat>
      <pgterms:file rdf:about="https://www.gutenberg.org/ebooks/98.epub3.images">
        <dcterms:format>
          <rdf:Description>
            <rdf:value rdf:datatype="http://purl.org/dc/terms/IMT">application/epub+zip</rdf:value>
          </rdf:Description>
        </dcterms:format>
      </pgterms:file>
    </dcterms:hasFormat>
    <dcterms:hasFormat>
      <pgterms:file rdf:about="https://www.gutenberg.org/cache/epub/98/pg98.cover.medium.jpg">
        <dcterms:format>
          <rdf:Description>
            <rdf:value rdf:datatype="http://purl.org/dc/terms/IMT">image/jpeg</rdf:value>
          </rdf:Description>
        </dcterms:format>
      </pgterms:file>
    </dcterms:hasFormat>
  </pgterms:ebook>
</rdf:RDF>
""".encode("utf-8")

# --- Fixture 3: a deliberately malformed/thin legacy record — no subjects, no
# bookshelves, no language, no downloads, no dcterms:type, no hasFormat blocks at
# all. Only an id + title survive. Must NOT crash (Assumption A3). ---
FIXTURE_MALFORMED = """<?xml version="1.0" encoding="utf-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
   xmlns:dcterms="http://purl.org/dc/terms/"
   xmlns:pgterms="http://www.gutenberg.org/2009/pgterms/"
   xmlns:dcam="http://purl.org/dc/dcam/">
  <pgterms:ebook rdf:about="ebooks/99999">
    <dcterms:title>An Old Thin Legacy Record</dcterms:title>
  </pgterms:ebook>
</rdf:RDF>
""".encode("utf-8")


class TestParseRdfSoundBook:
    """parse_rdf on the 26471.rdf-derived Sound sample."""

    def test_extracts_id_title_and_authors(self):
        record = parse_rdf(FIXTURE_SOUND)
        assert record is not None
        assert record["id"] == 26471
        assert record["title"] == "Spoon River Anthology"
        assert "Masters, Edgar Lee" in record["authors"]

    def test_classifies_as_sound_media_type(self):
        record = parse_rdf(FIXTURE_SOUND)
        assert record["media_type"] == "Sound"

    def test_extracts_at_least_one_audio_format_url(self):
        record = parse_rdf(FIXTURE_SOUND)
        assert record["audio_mp3_url"] or record["audio_ogg_url"]
        assert record["audio_mp3_url"] == "https://www.gutenberg.org/files/26471/mp3/26471-01.mp3"
        assert record["audio_ogg_url"] == "https://www.gutenberg.org/files/26471/ogg/26471-01.ogg"


class TestParseRdfTextBook:
    """parse_rdf on a minimal Text-book fixture."""

    def test_classifies_as_text_media_type(self):
        record = parse_rdf(FIXTURE_TEXT)
        assert record["media_type"] == "Text"

    def test_extracts_epub_and_cover_urls(self):
        record = parse_rdf(FIXTURE_TEXT)
        assert record["epub_url"] == "https://www.gutenberg.org/ebooks/98.epub3.images"
        assert record["cover_url"] == "https://www.gutenberg.org/cache/epub/98/pg98.cover.medium.jpg"

    def test_download_count_is_an_int(self):
        record = parse_rdf(FIXTURE_TEXT)
        assert isinstance(record["download_count"], int)
        assert record["download_count"] == 54321


class TestParseRdfMalformedThinRecord:
    """parse_rdf must never crash on a thin/legacy record — defaults, not exceptions."""

    def test_does_not_crash_and_returns_a_record(self):
        record = parse_rdf(FIXTURE_MALFORMED)
        assert record is not None

    def test_still_has_a_usable_id_and_title(self):
        record = parse_rdf(FIXTURE_MALFORMED)
        assert record["id"] == 99999
        assert record["title"] == "An Old Thin Legacy Record"

    def test_missing_subjects_and_language_default_to_empty_string(self):
        record = parse_rdf(FIXTURE_MALFORMED)
        assert record["subjects"] == ""
        assert record["languages"] == ""

    def test_missing_downloads_and_type_use_sane_defaults(self):
        record = parse_rdf(FIXTURE_MALFORMED)
        assert record["download_count"] == 0
        assert record["media_type"] == "Text"

    def test_missing_format_urls_are_none_not_raised(self):
        record = parse_rdf(FIXTURE_MALFORMED)
        assert record["epub_url"] is None
        assert record["cover_url"] is None
        assert record["audio_mp3_url"] is None


# --- build_sqlite tests: a small hand-built record list, independent of parse_rdf,
# with one accented title to exercise diacritic-insensitive FTS. ---
SAMPLE_RECORDS = [
    {
        "id": 1,
        "title": "Émile ou De l'éducation",  # "Émile ou De l'éducation"
        "authors": "Rousseau, Jean-Jacques",
        "subjects": "Education",
        "bookshelves": "",
        "languages": "fr",
        "download_count": 100,
        "media_type": "Text",
        "epub_url": None,
        "cover_url": None,
        "audio_mp3_url": None,
        "audio_ogg_url": None,
        "audio_m4b_url": None,
    },
    {
        "id": 2,
        "title": "Plain Book Title",
        "authors": "Someone, Author",
        "subjects": "",
        "bookshelves": "",
        "languages": "en",
        "download_count": 5,
        "media_type": "Text",
        "epub_url": None,
        "cover_url": None,
        "audio_mp3_url": None,
        "audio_ogg_url": None,
        "audio_m4b_url": None,
    },
]


class TestBuildSqlite:
    def test_opens_read_only_with_schema_version_and_row_count(self, tmp_path):
        out_path = tmp_path / "catalog.db"
        build_sqlite(SAMPLE_RECORDS, str(out_path))

        uri = out_path.as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            cur = conn.execute("SELECT schema_version, row_count FROM catalog_meta WHERE id = 0")
            schema_version, row_count = cur.fetchone()
            assert schema_version == 1
            assert row_count == len(SAMPLE_RECORDS)
        finally:
            conn.close()

    def test_fts_match_on_title_token_returns_the_right_row(self, tmp_path):
        out_path = tmp_path / "catalog.db"
        build_sqlite(SAMPLE_RECORDS, str(out_path))

        conn = sqlite3.connect(str(out_path))
        try:
            cur = conn.execute("SELECT rowid FROM books_fts WHERE books_fts MATCH 'Plain'")
            rows = [r[0] for r in cur.fetchall()]
            assert rows == [2]
        finally:
            conn.close()

    def test_fts_is_diacritic_insensitive(self, tmp_path):
        out_path = tmp_path / "catalog.db"
        build_sqlite(SAMPLE_RECORDS, str(out_path))

        conn = sqlite3.connect(str(out_path))
        try:
            # Unaccented query "Emile" must match the accented indexed title
            # "Émile ..." -- proves tokenize='unicode61 remove_diacritics 2'.
            cur = conn.execute("SELECT rowid FROM books_fts WHERE books_fts MATCH 'Emile'")
            rows = [r[0] for r in cur.fetchall()]
            assert rows == [1]
        finally:
            conn.close()

    def test_passes_integrity_check(self, tmp_path):
        out_path = tmp_path / "catalog.db"
        build_sqlite(SAMPLE_RECORDS, str(out_path))

        conn = sqlite3.connect(str(out_path))
        try:
            cur = conn.execute("PRAGMA integrity_check")
            assert cur.fetchone()[0] == "ok"
        finally:
            conn.close()
