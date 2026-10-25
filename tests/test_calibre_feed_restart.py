import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import feed
from helpers import Seeder, login, make_epub, wait_for, wait_job, wait_source_ready

CALIBRE_SCHEMA = """
CREATE TABLE books(id INTEGER PRIMARY KEY, title TEXT, path TEXT, series_index REAL, pubdate TEXT);
CREATE TABLE authors(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_authors_link(book INTEGER, author INTEGER);
CREATE TABLE series(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_series_link(book INTEGER, series INTEGER);
CREATE TABLE tags(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_tags_link(book INTEGER, tag INTEGER);
CREATE TABLE publishers(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE books_publishers_link(book INTEGER, publisher INTEGER);
CREATE TABLE languages(id INTEGER PRIMARY KEY, lang_code TEXT);
CREATE TABLE books_languages_link(book INTEGER, lang_code INTEGER);
CREATE TABLE comments(book INTEGER, text TEXT);
CREATE TABLE identifiers(book INTEGER, type TEXT, val TEXT);
CREATE TABLE data(book INTEGER, format TEXT, name TEXT);
"""


def make_calibre_library(root: Path) -> None:
    lib = root / "Calibre"
    make_epub(
        lib / "Carlos Ruiz Zafon/La sombra del viento (1)/La sombra del viento - Carlos Ruiz Zafon.epub",
        "La sombra del viento", "Carlos Ruiz Zafón", language="es",
    )
    (lib / "Isaac Asimov/Fundacion (2)").mkdir(parents=True)
    (lib / "Isaac Asimov/Fundacion (2)/Fundacion - Isaac Asimov.azw3").write_bytes(b"x" * 40_000)
    (lib / "Isaac Asimov/Fundacion (2)/cover.jpg").write_bytes(b"y" * 10_000)
    conn = sqlite3.connect(lib / "metadata.db")
    conn.executescript(CALIBRE_SCHEMA)
    conn.executescript(
        """
        INSERT INTO books VALUES (1, 'La sombra del viento', 'Carlos Ruiz Zafon/La sombra del viento (1)', 1, '2001-04-01'),
                                 (2, 'Fundación', 'Isaac Asimov/Fundacion (2)', 1, '0101-01-01');
        INSERT INTO authors VALUES (1, 'Carlos Ruiz Zafón'), (2, 'Isaac Asimov');
        INSERT INTO books_authors_link VALUES (1, 1), (2, 2);
        INSERT INTO series VALUES (1, 'El cementerio de los libros olvidados'), (2, 'Fundación');
        INSERT INTO books_series_link VALUES (1, 1), (2, 2);
        INSERT INTO tags VALUES (1, 'Novela'), (2, 'Misterio');
        INSERT INTO books_tags_link VALUES (1, 1), (1, 2);
        INSERT INTO publishers VALUES (1, 'Planeta');
        INSERT INTO books_publishers_link VALUES (1, 1);
        INSERT INTO languages VALUES (1, 'spa');
        INSERT INTO books_languages_link VALUES (1, 1);
        INSERT INTO comments VALUES (1, '<div><p>Barcelona, 1945.</p><p>Un niño y un libro maldito.</p></div>');
        INSERT INTO identifiers VALUES (1, 'isbn', '9788408163435');
        INSERT INTO data VALUES (1, 'EPUB', 'La sombra del viento - Carlos Ruiz Zafon'),
                                (2, 'AZW3', 'Fundacion - Isaac Asimov');
        """
    )
    conn.commit()
    conn.close()


def test_calibre_library_and_restart(tmp_path, data_dir, inbox):
    seed_root = tmp_path / "seed"
    make_calibre_library(seed_root)
    seeder = Seeder(seed_root, folder="Calibre")

    from app.main import app

    with TestClient(app) as client:
        login(client)
        client.post("/ajustes/fuentes", data={"magnet": seeder.magnet})
        status = wait_source_ready(client)
        assert status["books"] == 2
        db = client.app.state.db
        book = db.search("sombra")[0][0]
        # Metadatos sacados de metadata.db, no del nombre del fichero.
        assert book["authors"] == "Carlos Ruiz Zafón"
        assert book["series"] == "El cementerio de los libros olvidados"
        assert book["description"] == "Barcelona, 1945.\n\nUn niño y un libro maldito."
        assert book["isbn"] == "9788408163435" and book["publisher"] == "Planeta"
        assert book["tags"] == "Novela, Misterio" and book["year"] == 2001
        fundacion = db.search("fundacion")[0][0]
        assert fundacion["title"] == "Fundación" and fundacion["year"] is None
        assert fundacion["formats"][0]["format"] == "azw3"
        # metadata.db se descargó solo para leerlo: no queda nada en disco.
        assert not any((data_dir / "cache").iterdir())
        assert not any(p.name.startswith("tmp") for p in data_dir.iterdir())

    # Tras reiniciar no se vuelve a pedir nada al enjambre y todo sigue funcionando.
    with TestClient(app) as client:
        login(client)
        status = client.get("/api/estado").json()
        assert status["books"] == 2 and status["sources"][0]["status"] == "ready"
        client.post("/ajustes/kindles", data={"name": "Kindle", "email": "yo@kindle.com"})
        kindle = client.app.state.db.kindles()[0]
        job_id = client.post(
            f"/api/libros/{book['id']}/kindle", json={"kindle_id": kindle["id"]}
        ).json()["job_id"]
        assert wait_job(client, job_id)["state"] == "done"
        wait_for(lambda: inbox.messages)
        assert (data_dir / "session.state").exists()


def test_extract_magnets_from_html():
    page = """
    <a href="magnet:?xt=urn:btih:e099bdd9f3c323632a9fa395e4143e140dd5c298&amp;dn=Biblioteca&amp;tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337">v1</a>
    otra vez: magnet:?xt=urn:btih:E099BDD9F3C323632A9FA395E4143E140DD5C298&dn=Repetido
    nueva: magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Nueva<br>
    rota: magnet:?xt=urn:btih:zzz
    """
    magnets = feed.extract_magnets(page)
    assert len(magnets) == 2
    assert "&dn=Biblioteca&tr=" in magnets[0]


@pytest.mark.parametrize("value, expected", [("spa", "es"), ("es-ES", "es"), ("klingon", "es"), (None, "es"), ("en", "en")])
def test_normalize_language(value, expected):
    from app.epub import normalize_language

    assert normalize_language(value, "es") == expected


def test_book_without_peers_fails_cleanly(tmp_path, data_dir, monkeypatch):
    monkeypatch.setenv("TORRENT_PEER_TIMEOUT", "3")
    seed_root = tmp_path / "seed"
    make_epub(seed_root / "Biblioteca/Autor, Uno - Libro solitario.epub", "Libro solitario", "Uno Autor", "es")
    seeder = Seeder(seed_root)

    from app.main import app

    with TestClient(app) as client:
        login(client)
        client.post("/ajustes/fuentes", data={"magnet": seeder.magnet})
        wait_source_ready(client)
        seeder.session.remove_torrent(seeder.handle)  # el único seeder desaparece
        book = client.app.state.db.search("solitario")[0][0]
        job_id = client.post(f"/api/archivos/{book['formats'][0]['file_id']}/preparar").json()["job_id"]
        job = wait_job(client, job_id)
        assert job["state"] == "error"
        assert "Nadie comparte" in job["message"]
        assert not any((data_dir / "staging").iterdir())
