"""Prueba completa sin Internet: un seeder BitTorrent local comparte una biblioteca
falsa, la app la indexa, descarga solo los libros pedidos y los "envía" a un
servidor SMTP local."""

import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from helpers import Seeder, login, make_epub, wait_for, wait_job, wait_source_ready

SOMBRA = "Ruiz Zafón, Carlos - [El cementerio de los libros olvidados 01] La sombra del viento (r1.3).epub"
FUNDACION = "Asimov, Isaac/Asimov, Isaac - Fundación"
ORWELL = "Orwell, George - 1984.mobi"


@pytest.fixture()
def env(tmp_path, data_dir, inbox):
    seed_root = tmp_path / "seed"
    lib = seed_root / "Biblioteca"
    make_epub(lib / SOMBRA, "La sombra del viento", "Carlos Ruiz Zafón", language=None)
    make_epub(lib / f"{FUNDACION}.epub", "Fundación", "Isaac Asimov", language="spa")
    (lib / f"{FUNDACION}.mobi").write_bytes(os.urandom(70_000))
    (lib / ORWELL).write_bytes(os.urandom(50_000))
    (lib / "Relleno").mkdir()
    for i in range(40):  # relleno para que el torrent tenga muchas piezas
        (lib / "Relleno" / f"Autor {i:02d} - Libro de relleno {i}.epub").write_bytes(os.urandom(60_000))
    (lib / "portada.jpg").write_bytes(b"no es un libro")
    seeder = Seeder(seed_root)

    from app.main import app

    with TestClient(app) as client:
        yield client, seeder, inbox, seed_root, data_dir


def test_full_flow(env):
    client, seeder, inbox, seed_root, data_dir = env

    # Sin sesión no se ve nada.
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/api/estado").status_code == 401
    r = client.post("/login", data={"password": "mala", "next": "/"}, follow_redirects=False)
    assert "error=1" in r.headers["location"]
    login(client)

    # Añadir la fuente e indexar.
    r = client.post("/ajustes/fuentes", data={"magnet": seeder.magnet}, follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"]
    status = wait_source_ready(client)
    # 3 libros reales + 40 de relleno (Fundación tiene 2 formatos; la portada no cuenta).
    assert status["books"] == 43
    # No se ha descargado ningún libro: solo el catálogo.
    assert not any((data_dir / "cache").iterdir())
    assert not any((data_dir / "staging").iterdir())

    # Búsqueda sin tildes y por autor.
    page = client.get("/", params={"q": "zafon sombra"}).text
    assert "La sombra del viento" in page and "El cementerio de los libros olvidados" in page
    assert "Fundación" in client.get("/", params={"autor": "Isaac Asimov"}).text
    assert "1 resultado" in client.get("/", params={"q": "fundacion"}).text
    assert "Libro de relleno" in client.get("/", params={"q": "relleno", "partial": "1"}).text

    db = client.app.state.db
    sombra = db.search("sombra")[0][0]
    fundacion = db.search("fundacion")[0][0]
    orwell = db.search("1984")[0][0]
    assert [f["format"] for f in fundacion["formats"]] == ["epub", "mobi"]
    assert orwell["kindle_format"] is None

    # Enviar al Kindle.
    client.post("/ajustes/kindles", data={"name": "Kindle de Ana", "email": "ana_123@kindle.com"})
    kindle = db.kindles()[0]
    r = client.post(f"/api/libros/{sombra['id']}/kindle", json={"kindle_id": kindle["id"]})
    assert r.status_code == 200, r.text
    job = wait_job(client, r.json()["job_id"])
    assert job["state"] == "done", job
    msg = wait_for(lambda: inbox.messages and inbox.messages[-1])
    assert msg["To"] == "ana_123@kindle.com"
    attachment = next(msg.iter_attachments())
    assert attachment.get_filename() == "La sombra del viento - Carlos Ruiz Zafon.epub"
    with zipfile.ZipFile(io.BytesIO(attachment.get_content())) as z:
        assert z.namelist()[0] == "mimetype"
        c1 = z.read("OEBPS/text/c1.xhtml").decode()
        opf = z.read("OEBPS/content.opf").decode()
    assert c1.startswith("<?xml") and 'href="c2.xhtml"' in c1 and "<img" not in c1
    assert ">es</dc:language>" in opf

    # Solo se ha guardado ese libro y no quedan datos temporales del torrent.
    cached = list((data_dir / "cache").iterdir())
    assert len(cached) == 1 and cached[0].read_bytes() == (seed_root / "Biblioteca" / SOMBRA).read_bytes()
    wait_for(lambda: not any((data_dir / "staging").iterdir()), timeout=10)

    # La ficha se completa con la sinopsis y la portada del EPUB.
    book = client.get(f"/libro/{sombra['id']}").text
    assert "Una novela sobre La sombra del viento." in book and "Editorial de Prueba" in book
    assert client.get(f"/portada/{sombra['id']}").headers["content-type"] == "image/jpeg"

    # Dos descargas a la vez del mismo torrent.
    mobi = next(f for f in fundacion["formats"] if f["format"] == "mobi")
    orwell_file = orwell["formats"][0]
    jobs = [
        client.post(f"/api/archivos/{f['file_id']}/preparar").json()["job_id"]
        for f in (mobi, orwell_file)
    ]
    for job_id in jobs:
        assert wait_job(client, job_id)["state"] == "done"
    r = client.get(f"/descargar/{mobi['file_id']}")
    assert r.content == (seed_root / "Biblioteca" / f"{FUNDACION}.mobi").read_bytes()
    assert "Fundacion%20-%20Isaac%20Asimov.mobi" in r.headers["content-disposition"]

    # Descarga sin JavaScript: redirige a una página que se refresca sola.
    epub_file = fundacion["formats"][0]
    r = client.get(f"/descargar/{epub_file['file_id']}", follow_redirects=False)
    assert r.status_code == 303
    job_id = r.headers["location"].split("job=")[1]
    wait_job(client, job_id)
    assert client.get(r.headers["location"]).content.startswith(b"PK")

    # Un libro solo en MOBI no se puede mandar al Kindle.
    r = client.post(f"/api/libros/{orwell['id']}/kindle", json={"kindle_id": kindle["id"]})
    assert r.status_code == 400

    # Quitar la fuente borra sus libros y la caché.
    source_id = status["sources"][0]["id"]
    client.post(f"/ajustes/fuentes/{source_id}/borrar")
    assert db.count_books() == 0
    assert not any((data_dir / "cache").iterdir())
