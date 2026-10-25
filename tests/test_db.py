from app.db import Database
from app.indexer import TorrentFile, index_source

SOMBRA = "Biblioteca/Ruiz Zafón, Carlos - La sombra del viento.epub"
FUNDACION = "Biblioteca/Asimov, Isaac - Fundación.epub"


def test_removing_old_source_keeps_books_cached_in_the_new_one(tmp_path):
    db = Database(tmp_path / "test.db")
    old = db.add_source("a" * 40, "magnet:?xt=urn:btih:" + "a" * 40, "v1", "manual")
    new = db.add_source("b" * 40, "magnet:?xt=urn:btih:" + "b" * 40, "v2", "manual")
    index_source(db, old, [TorrentFile(0, SOMBRA, 100), TorrentFile(1, FUNDACION, 100)], "Biblioteca")
    index_source(db, new, [TorrentFile(5, SOMBRA, 100)], "Biblioteca")
    with db.connect() as conn:
        rows = conn.execute("SELECT path, id FROM files WHERE source_id=?", (old,)).fetchall()
    old_files = {r["path"]: r["id"] for r in rows}
    db.add_cached(old_files[SOMBRA], "/cache/sombra.epub", 100)
    db.add_cached(old_files[FUNDACION], "/cache/fundacion.epub", 100)

    to_delete = db.delete_source(old)

    # "Fundación" solo estaba en la fuente antigua: desaparece con su caché.
    assert to_delete == ["/cache/fundacion.epub"]
    assert db.count_books() == 1
    # "La sombra del viento" sigue en caché, ahora asociada al fichero de la fuente nueva.
    book = db.search("sombra")[0][0]
    assert book["formats"][0]["cached"] is True
    assert db.cached_path(book["formats"][0]["file_id"]) == "/cache/sombra.epub"
