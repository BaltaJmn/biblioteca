"""Catálogo en SQLite con búsqueda de texto completo (FTS5)."""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
    id INTEGER PRIMARY KEY,
    infohash TEXT NOT NULL UNIQUE,
    magnet TEXT NOT NULL,
    name TEXT,
    origin TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    num_files INTEGER NOT NULL DEFAULT 0,
    num_books INTEGER NOT NULL DEFAULT 0,
    total_size INTEGER NOT NULL DEFAULT 0,
    added_at REAL NOT NULL,
    indexed_at REAL
);
CREATE TABLE IF NOT EXISTS books(
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    authors TEXT NOT NULL DEFAULT '',
    series TEXT,
    series_index REAL,
    language TEXT,
    year INTEGER,
    publisher TEXT,
    tags TEXT,
    description TEXT,
    isbn TEXT,
    has_cover INTEGER NOT NULL DEFAULT 0,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    file_index INTEGER NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    format TEXT NOT NULL,
    UNIQUE(source_id, file_index)
);
CREATE INDEX IF NOT EXISTS files_book ON files(book_id);
CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
    title, authors, series, tags,
    content='books', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);
CREATE TABLE IF NOT EXISTS kindles(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS feed_seen(
    infohash TEXT PRIMARY KEY,
    seen_at REAL NOT NULL
);
"""

PAGE_SIZE = 24


def fts_query(text: str) -> str | None:
    tokens = re.findall(r"\w+", text, re.UNICODE)
    if not tokens:
        return None
    return " ".join(f'"{t}"*' for t in tokens)


class Database:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---- fuentes (torrents) -------------------------------------------------

    def sources(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM sources ORDER BY added_at DESC").fetchall()

    def source(self, source_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()

    def source_by_hash(self, infohash: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM sources WHERE infohash=?", (infohash,)
            ).fetchone()

    def add_source(self, infohash: str, magnet: str, name: str | None, origin: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO sources(infohash, magnet, name, origin, added_at) VALUES(?,?,?,?,?)",
                (infohash, magnet, name, origin, time.time()),
            )
            return cur.lastrowid

    def update_source(self, source_id: int, **fields) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{k}=?" for k in fields)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE sources SET {assignments} WHERE id=?", (*fields.values(), source_id)
            )

    def delete_source(self, source_id: int) -> list[int]:
        """Borra la fuente y los libros que solo estaban en ella (devuelve sus id)."""
        with self.connect() as conn:
            conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
            orphans = [
                r[0]
                for r in conn.execute(
                    "DELETE FROM books WHERE NOT EXISTS"
                    " (SELECT 1 FROM files WHERE book_id=books.id) RETURNING id"
                )
            ]
            conn.execute("INSERT INTO books_fts(books_fts) VALUES('rebuild')")
        return orphans

    def feed_seen(self, infohash: str) -> bool:
        with self.connect() as conn:
            return (
                conn.execute("SELECT 1 FROM feed_seen WHERE infohash=?", (infohash,)).fetchone()
                is not None
            )

    def mark_feed_seen(self, infohash: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO feed_seen(infohash, seen_at) VALUES(?,?)",
                (infohash, time.time()),
            )

    # ---- libros -------------------------------------------------------------

    def count_books(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT count(*) FROM books").fetchone()[0]

    def search(
        self,
        text: str = "",
        author: str = "",
        series: str = "",
        page: int = 1,
    ) -> tuple[list[dict], int]:
        clauses = []
        if q := fts_query(text):
            clauses.append(f"({q})")
        if q := fts_query(author):
            clauses.append(f"authors : ({q})")
        if q := fts_query(series):
            clauses.append(f"series : ({q})")
        offset = (max(page, 1) - 1) * PAGE_SIZE
        with self.connect() as conn:
            if clauses:
                match = " AND ".join(clauses)
                total = conn.execute(
                    "SELECT count(*) FROM books_fts WHERE books_fts MATCH ?", (match,)
                ).fetchone()[0]
                order = "b.series_index, b.title" if series and not text else "rank"
                rows = conn.execute(
                    "SELECT b.*, bm25(books_fts, 10.0, 6.0, 3.0, 1.0) AS rank FROM books_fts"
                    " JOIN books b ON b.id = books_fts.rowid WHERE books_fts MATCH ?"
                    f" ORDER BY {order} LIMIT ? OFFSET ?",
                    (match, PAGE_SIZE, offset),
                ).fetchall()
            else:
                total = conn.execute("SELECT count(*) FROM books").fetchone()[0]
                rows = conn.execute(
                    "SELECT * FROM books ORDER BY added_at DESC, id DESC LIMIT ? OFFSET ?",
                    (PAGE_SIZE, offset),
                ).fetchall()
            books = [dict(r) for r in rows]
            self._attach_formats(conn, books)
        return books, total

    def book(self, book_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
            if row is None:
                return None
            book = dict(row)
            self._attach_formats(conn, [book])
            return book

    def _attach_formats(self, conn: sqlite3.Connection, books: list[dict]) -> None:
        from .parsing import FORMAT_PREFERENCE, KINDLE_FORMATS

        if not books:
            return
        by_id = {b["id"]: b for b in books}
        for b in books:
            b["formats"] = []
        placeholders = ",".join("?" * len(by_id))
        # Un fichero por formato: el de la fuente más reciente.
        rows = conn.execute(
            "SELECT f.book_id, f.format, f.id, f.size"
            " FROM files f JOIN sources s ON s.id = f.source_id"
            f" WHERE f.book_id IN ({placeholders})"
            " ORDER BY s.added_at DESC",
            tuple(by_id),
        ).fetchall()
        seen = set()
        for r in rows:
            if (r["book_id"], r["format"]) in seen:
                continue
            seen.add((r["book_id"], r["format"]))
            by_id[r["book_id"]]["formats"].append(
                {"format": r["format"], "file_id": r["id"], "size": r["size"]}
            )
        rank = {f: i for i, f in enumerate(FORMAT_PREFERENCE)}
        for b in books:
            b["formats"].sort(key=lambda f: rank.get(f["format"], 99))
            b["kindle_format"] = next(
                (f["format"] for f in b["formats"] if f["format"] in KINDLE_FORMATS), None
            )

    def files_for_book(self, book_id: int, fmt: str) -> list[sqlite3.Row]:
        """Ficheros candidatos de un libro y formato, de la fuente más reciente a la más antigua."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT f.*, s.infohash FROM files f JOIN sources s ON s.id=f.source_id"
                " WHERE f.book_id=? AND f.format=? ORDER BY s.added_at DESC",
                (book_id, fmt),
            ).fetchall()

    def file(self, file_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT f.*, s.infohash FROM files f JOIN sources s ON s.id=f.source_id"
                " WHERE f.id=?",
                (file_id,),
            ).fetchone()

    def enrich_book(self, book_id: int, **fields) -> None:
        """Rellena solo los campos que aún estén vacíos."""
        fields = {k: v for k, v in fields.items() if v not in (None, "")}
        if not fields:
            return
        assignments = ", ".join(
            f"{k}=?" if k == "has_cover" else f"{k}=COALESCE(NULLIF({k}, ''), ?)" for k in fields
        )
        with self.connect() as conn:
            conn.execute(f"UPDATE books SET {assignments} WHERE id=?", (*fields.values(), book_id))

    # ---- kindles --------------------------------------------------------------

    def kindles(self) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM kindles ORDER BY name")]

    def kindle(self, kindle_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM kindles WHERE id=?", (kindle_id,)).fetchone()

    def add_kindle(self, name: str, email: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO kindles(name, email) VALUES(?, ?)"
                " ON CONFLICT(email) DO UPDATE SET name=excluded.name",
                (name, email),
            )

    def delete_kindle(self, kindle_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM kindles WHERE id=?", (kindle_id,))
