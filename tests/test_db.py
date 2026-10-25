from app.db import Database
from app.indexer import TorrentFile, index_source

SOMBRA = "Biblioteca/Ruiz Zafón, Carlos - La sombra del viento.epub"
FUNDACION = "Biblioteca/Asimov, Isaac - Fundación.epub"


def test_removing_old_source_keeps_books_present_in_the_new_one(tmp_path):
    db = Database(tmp_path / "test.db")
    old = db.add_source("a" * 40, "magnet:?xt=urn:btih:" + "a" * 40, "v1", "manual")
    new = db.add_source("b" * 40, "magnet:?xt=urn:btih:" + "b" * 40, "v2", "manual")
    index_source(db, old, [TorrentFile(0, SOMBRA, 100), TorrentFile(1, FUNDACION, 100)], "Biblioteca")
    index_source(db, new, [TorrentFile(5, SOMBRA, 100)], "Biblioteca")
    assert db.count_books() == 2  # "La sombra del viento" no se duplica
    fundacion_id = db.search("fundacion")[0][0]["id"]

    assert db.delete_source(old) == [fundacion_id]

    assert db.count_books() == 1
    sombra = db.search("sombra")[0][0]
    assert db.file(sombra["formats"][0]["file_id"])["source_id"] == new
