"""Convierte la lista de ficheros de un torrent en libros del catálogo."""

from __future__ import annotations

import logging
import posixpath
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .db import Database
from .epub import html_to_text
from .parsing import ParsedName, book_key, file_format, parse_path, split_authors

log = logging.getLogger("biblioteca.indexer")


@dataclass
class TorrentFile:
    index: int
    path: str
    size: int


@dataclass
class BookGroup:
    parsed: ParsedName
    files: list[tuple[TorrentFile, str]] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def _nfc(path: str) -> str:
    return unicodedata.normalize("NFC", path)


def find_calibre_db(files: list[TorrentFile]) -> TorrentFile | None:
    candidates = [f for f in files if posixpath.basename(f.path) == "metadata.db"]
    return min(candidates, key=lambda f: f.path.count("/"), default=None)


def read_calibre(db_path: Path, library_root: str) -> dict[str, dict]:
    """Metadatos de una biblioteca de Calibre indexados por ruta del fichero."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        books = conn.execute(
            """
            SELECT b.id, b.title, b.path, b.series_index, b.pubdate,
              (SELECT group_concat(a.name, ' & ') FROM books_authors_link l
                 JOIN authors a ON a.id = l.author WHERE l.book = b.id) AS authors,
              (SELECT s.name FROM books_series_link l
                 JOIN series s ON s.id = l.series WHERE l.book = b.id) AS series,
              (SELECT group_concat(t.name, ', ') FROM books_tags_link l
                 JOIN tags t ON t.id = l.tag WHERE l.book = b.id) AS tags,
              (SELECT p.name FROM books_publishers_link l
                 JOIN publishers p ON p.id = l.publisher WHERE l.book = b.id) AS publisher,
              (SELECT lg.lang_code FROM books_languages_link l
                 JOIN languages lg ON lg.id = l.lang_code WHERE l.book = b.id) AS language,
              (SELECT c.text FROM comments c WHERE c.book = b.id) AS description,
              (SELECT i.val FROM identifiers i WHERE i.book = b.id AND i.type = 'isbn') AS isbn
            FROM books b
            """
        ).fetchall()
        by_id = {r["id"]: r for r in books}
        result = {}
        for row in conn.execute("SELECT book, format, name FROM data"):
            book = by_id.get(row["book"])
            if book is None:
                continue
            path = posixpath.join(
                library_root, book["path"], f"{row['name']}.{row['format'].lower()}"
            )
            year = None
            if book["pubdate"] and book["pubdate"][:4].isdigit():
                year = int(book["pubdate"][:4])
                year = year if 1000 < year < 2100 else None  # Calibre usa 0101 como "sin fecha"
            result[_nfc(path)] = {
                "calibre_id": book["id"],
                "title": book["title"],
                "authors": split_authors(book["authors"] or ""),
                "series": book["series"],
                "series_index": book["series_index"] if book["series"] else None,
                "tags": book["tags"],
                "publisher": book["publisher"],
                "language": book["language"],
                "description": html_to_text(book["description"]),
                "isbn": book["isbn"],
                "year": year,
            }
        return result
    finally:
        conn.close()


def group_books(
    files: list[TorrentFile], root: str | None, calibre: dict[str, dict] | None = None
) -> list[BookGroup]:
    groups: dict[tuple, BookGroup] = {}
    for f in files:
        fmt = file_format(f.path)
        if fmt is None or f.size == 0:
            continue
        path = _nfc(f.path)
        meta = (calibre or {}).get(path)
        if meta:
            key: tuple = ("calibre", meta["calibre_id"])
            parsed = ParsedName(
                title=meta["title"],
                authors=meta["authors"],
                series=meta["series"],
                series_index=meta["series_index"],
                year=meta["year"],
            )
        else:
            directory, filename = posixpath.split(path)
            key = ("path", directory.lower(), filename.rsplit(".", 1)[0].lower())
            parsed = None
        group = groups.get(key)
        if group is None:
            group = groups[key] = BookGroup(parsed=parsed or parse_path(path, root), meta=meta or {})
        group.files.append((f, fmt))
    return list(groups.values())


def index_source(
    db: Database,
    source_id: int,
    files: list[TorrentFile],
    root: str | None,
    calibre: dict[str, dict] | None = None,
) -> int:
    groups = group_books(files, root, calibre)
    now = time.time()
    with db.connect() as conn:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS seen_files(file_index INTEGER PRIMARY KEY)")
        conn.execute("DELETE FROM seen_files")
        for g in groups:
            p, m = g.parsed, g.meta
            authors = " & ".join(p.authors)
            book_id = conn.execute(
                """
                INSERT INTO books(key, title, authors, series, series_index, language, year,
                                  publisher, tags, description, isbn, added_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    series = COALESCE(books.series, excluded.series),
                    series_index = COALESCE(books.series_index, excluded.series_index),
                    language = COALESCE(books.language, excluded.language),
                    year = COALESCE(books.year, excluded.year),
                    publisher = COALESCE(books.publisher, excluded.publisher),
                    tags = COALESCE(books.tags, excluded.tags),
                    description = COALESCE(books.description, excluded.description),
                    isbn = COALESCE(books.isbn, excluded.isbn)
                RETURNING id
                """,
                (
                    book_key(p.authors, p.title), p.title, authors, p.series, p.series_index,
                    m.get("language"), p.year, m.get("publisher"), m.get("tags"),
                    m.get("description"), m.get("isbn"), now,
                ),
            ).fetchone()[0]
            # Upsert para conservar los id de fichero (y su caché) al reindexar.
            conn.executemany(
                "INSERT INTO files(book_id, source_id, file_index, path, size, format)"
                " VALUES(?,?,?,?,?,?) ON CONFLICT(source_id, file_index) DO UPDATE SET"
                " book_id=excluded.book_id, path=excluded.path, size=excluded.size,"
                " format=excluded.format",
                [(book_id, source_id, f.index, f.path, f.size, fmt) for f, fmt in g.files],
            )
            conn.executemany(
                "INSERT OR IGNORE INTO seen_files VALUES(?)", [(f.index,) for f, _ in g.files]
            )
        conn.execute(
            "DELETE FROM files WHERE source_id=? AND file_index NOT IN (SELECT file_index FROM seen_files)",
            (source_id,),
        )
        conn.execute(
            "DELETE FROM books WHERE NOT EXISTS (SELECT 1 FROM files WHERE book_id=books.id)"
        )
        conn.execute("INSERT INTO books_fts(books_fts) VALUES('rebuild')")
        num_books = conn.execute(
            "SELECT count(DISTINCT book_id) FROM files WHERE source_id=?", (source_id,)
        ).fetchone()[0]
    log.info("Fuente %s indexada: %s libros", source_id, num_books)
    return num_books
