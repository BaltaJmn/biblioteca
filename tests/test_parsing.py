import pytest

from app.parsing import book_key, display_author, parse_path


@pytest.mark.parametrize(
    "path, title, authors, series, index",
    [
        (
            "Biblioteca/Ruiz Zafón, Carlos - [El cementerio de los libros olvidados 01] La sombra del viento (r1.3).epub",
            "La sombra del viento", ["Carlos Ruiz Zafón"], "El cementerio de los libros olvidados", 1,
        ),
        (
            "Biblioteca/Asimov, Isaac/Asimov, Isaac - Fundación.epub",
            "Fundación", ["Isaac Asimov"], None, None,
        ),
        (
            "Biblioteca/Isaac Asimov/Yo, robot (12)/Yo, robot - Isaac Asimov.epub",
            "Yo, robot", ["Isaac Asimov"], None, None,
        ),
        (
            "Biblioteca/Gabriel García Márquez/Cien años de soledad.epub",
            "Cien años de soledad", ["Gabriel García Márquez"], None, None,
        ),
        (
            "Biblioteca/Pratchett, Terry & Gaiman, Neil - Buenos presagios.mobi",
            "Buenos presagios", ["Terry Pratchett", "Neil Gaiman"], None, None,
        ),
        (
            "Biblioteca/Orwell, George - 1984.epub",
            "1984", ["George Orwell"], None, None,
        ),
        (
            "Biblioteca/Sanderson, Brandon - El imperio final (Nacidos de la bruma, #1).epub",
            "El imperio final", ["Brandon Sanderson"], "Nacidos de la bruma", 1,
        ),
        (
            "Biblioteca/Libros/Cervantes, Miguel de/Don Quijote de la Mancha.epub",
            "Don Quijote de la Mancha", ["Miguel de Cervantes"], None, None,
        ),
    ],
)
def test_parse_path(path, title, authors, series, index):
    parsed = parse_path(path, root="Biblioteca")
    assert parsed.title == title
    assert parsed.authors == authors
    assert parsed.series == series
    assert parsed.series_index == index


def test_display_author():
    assert display_author("Ruiz Zafón, Carlos") == "Carlos Ruiz Zafón"
    assert display_author("King, Jr., Martin Luther") == "King, Jr., Martin Luther"
    assert display_author("Platón") == "Platón"


def test_book_key_ignores_author_order_and_accents():
    assert book_key(["Carlos Ruiz Zafón"], "La sombra del viento") == book_key(
        ["Ruiz Zafon, Carlos"], "La Sombra del Viento"
    )
