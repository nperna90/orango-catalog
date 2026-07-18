# orango-catalog

A static [Project Gutenberg](https://www.gutenberg.org/) book catalog, rebuilt weekly and published as a single downloadable file. This repo is the entire ETL (extract-transform-load) pipeline behind the offline book search in [orango](https://orango.app), a privacy-first radio · podcast · ebook reader app for Android.

## What this repo does

Every Sunday at 03:00 UTC, a scheduled GitHub Action:

1. Downloads Project Gutenberg's official bulk catalog dump (`rdf-files.tar.bz2`, ~75,000 books' worth of RDF/XML metadata).
2. Extracts the fields orango's Libri Discover screen needs: title, authors, subjects, bookshelves, languages, download counts, and direct file URLs (EPUB, cover image, and — for the ~1,150 public-domain audiobooks Project Gutenberg hosts — MP3/OGG/M4B audio).
3. Builds ONE compact SQLite database with a full-text search (FTS4) index over title/author/subject/bookshelf.
4. Gzip-compresses it (target: ~15-25 MB) and publishes it to this repo's [Releases](../../releases) page, on a single rolling `catalog` tag.

The result is always available at a stable URL:

```
https://github.com/nperna90/orango-catalog/releases/latest/download/catalog.db.gz
```

No API, no accounts, no server-side code beyond the GitHub Action itself.

## Why this exists — the privacy rationale

orango is built with a zero-servers, zero-accounts, zero-tracking promise. Prior to this repo, Libri's book search queried [gutendex.com](https://gutendex.com), a third-party hobby API that (per direct experience) is often slow or unavailable.

Rather than stand up a server orango would have to operate — which would violate that promise — this repo turns the problem into a **static file**:

- The orango app makes ONE anonymous HTTPS `GET` request to a public GitHub Releases URL. No API key, no request parameters that identify the user, no account, no telemetry — no server orango operates is involved at any point. GitHub Releases is a static-file CDN, not a service orango runs or controls.
- GitHub Releases cannot run server-side logic against the request — there is nothing here that *could* track a user even if someone wanted it to.
- The entire pipeline that produces the file is public and auditable in this very repo: anyone can read `build_catalog.py`, read the workflow that runs it, and verify for themselves that the artifact only ever contains public, already-public-domain Project Gutenberg metadata.
- If GitHub Releases were ever unavailable, orango silently falls back to querying gutendex.com directly, exactly as it did before this repo existed. Nothing about the app's privacy posture regresses if this repo goes away.

In short: this repo trades "always-fresh, third-party-hosted, servers-owned-by-someone-else" for "weekly-refreshed, statically-hosted, fully-auditable." That tradeoff favors orango's privacy pillars.

## The artifact

`catalog.db.gz` gunzips to a SQLite database with this schema (schema_version 1):

```sql
CREATE TABLE catalog_meta (
  id INTEGER PRIMARY KEY CHECK (id = 0),
  schema_version INTEGER NOT NULL,     -- = 1 for this contract
  generated_at TEXT NOT NULL,          -- ISO-8601 UTC, Action run timestamp
  pg_dump_date TEXT,                   -- Last-Modified of rdf-files.tar.bz2 at fetch time
  row_count INTEGER NOT NULL
);

CREATE TABLE books (
  id INTEGER PRIMARY KEY,              -- Gutenberg ebook id
  title TEXT NOT NULL,
  authors TEXT NOT NULL DEFAULT '',    -- "Name; Name2" (display names, "; "-joined)
  subjects TEXT NOT NULL DEFAULT '',   -- "; "-joined
  bookshelves TEXT NOT NULL DEFAULT '',-- "; "-joined
  languages TEXT NOT NULL DEFAULT '',  -- comma-joined RAW RFC4646 codes as carried by the
                                       -- dump: mostly ISO 639-1 ("en") but also 639-2/3
                                       -- ("enm", "grc") -- consumers must exact-match
                                       -- against the delimited list, never substring
  download_count INTEGER NOT NULL DEFAULT 0,
  media_type TEXT NOT NULL DEFAULT 'Text',  -- "Text" | "Sound"
  epub_url TEXT,
  cover_url TEXT,
  audio_mp3_url TEXT,                  -- Sound rows only
  audio_ogg_url TEXT,
  audio_m4b_url TEXT
);
CREATE INDEX idx_books_download_count ON books(download_count DESC);

-- fts4, not fts5: Android framework SQLite ships FTS3/FTS4 only, so the
-- index must be queryable on-device at minSdk 26.
CREATE VIRTUAL TABLE books_fts USING fts4(
  title, authors, subjects, bookshelves,
  content="books",
  tokenize=unicode61 "remove_diacritics=1"
);
```

`schema_version` is the cross-repo contract with the orango app: any future column/type change is a version bump, never a silent edit. The app refuses to trust an artifact whose schema version it doesn't recognize, and falls back to gutendex.com.

## Files in this repo

- `build_catalog.py` — the ETL pipeline (stdlib-only Python: `tarfile`, `xml.etree.ElementTree`, `sqlite3`, `gzip`).
- `test_build_catalog.py` — pytest coverage of the RDF parsing and SQLite/FTS build logic.
- `.github/workflows/build-catalog.yml` — the weekly scheduled build + publish workflow.
- `requirements-dev.txt` — dev-only dependency (`pytest`); `build_catalog.py` itself has zero third-party dependencies.

## Who this powers

This catalog powers offline/local book search inside [orango](https://orango.app)'s Libri (ebook reader) pillar. It is not a general-purpose product — it exists solely to keep orango's Discover search fast and reliable independent of any third-party API's uptime.
