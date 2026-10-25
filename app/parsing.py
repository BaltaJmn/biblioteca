"""Deducir título, autor y serie a partir de la ruta de un fichero.

Cubre las convenciones habituales de las colecciones de ebooks en español:

* ePubLibre:  ``Apellido, Nombre - [Serie 01] Título (r1.2).epub``
* Calibre:    ``Autor/Título (123)/Título - Autor.epub``
* Carpeta:    ``Autor/Título.epub``
* Simple:     ``Autor - Título.epub``
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

EBOOK_FORMATS = {
    "epub", "kepub", "mobi", "azw", "azw3", "pdf", "fb2", "txt", "rtf", "doc", "docx",
    "odt", "djvu", "cbz", "cbr", "lit", "htm", "html",
}
# Formatos que acepta el correo de "Send to Kindle" de Amazon.
KINDLE_FORMATS = {"epub", "pdf", "doc", "docx", "txt", "rtf", "htm", "html"}
FORMAT_PREFERENCE = [
    "epub", "kepub", "azw3", "mobi", "pdf", "fb2", "docx", "doc", "odt", "rtf", "txt",
    "djvu", "cbz", "cbr", "azw", "lit", "htm", "html",
]

_REVISION = re.compile(r"\s*[\(\[]\s*[rv]\d+(?:[.,]\d+)*\s*[\)\]]", re.I)
_TAGS = re.compile(
    r"\s*\[(?:es|esp|spa|en|eng|epub|mobi|pdf|retail|scan|ocr|spanish|español|castellano)\]",
    re.I,
)
_CALIBRE_ID = re.compile(r"\s*\(\d+\)$")
_SERIES_BRACKET = re.compile(r"\[\s*([^\[\]]+?)[\s#_.\-]*(\d{1,4}(?:[.,]\d+)?)\s*\]")
_SERIES_PAREN = re.compile(r"\(\s*([^()]+?)\s*,?\s*#\s*(\d{1,4}(?:[.,]\d+)?)\s*\)")
_YEAR = re.compile(r"\s*\((1[5-9]\d\d|20\d\d)\)")
_AUTHOR_SPLIT = re.compile(r"\s*(?:&|;|\s/\s)\s*")
_GENERIC_DIRS = {
    "libros", "ebooks", "ebook", "epub", "epubs", "books", "biblioteca", "library",
    "pdf", "mobi", "varios", "otros", "misc", "nuevos", "novedades",
}
_NOT_FIRST_NAMES = {"jr", "jr.", "sr", "sr.", "hijo", "padre"}


@dataclass
class ParsedName:
    title: str
    authors: list[str] = field(default_factory=list)
    series: str | None = None
    series_index: float | None = None
    year: int | None = None


def normalize(text: str) -> str:
    """Minúsculas, sin tildes y sin signos: base para comparar y deduplicar."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w]+", " ", text.lower())
    return re.sub(r"\s+", " ", text.replace("_", " ")).strip()


def book_key(authors: list[str], title: str) -> str:
    # Los tokens del autor se ordenan para que "Ruiz Zafón, Carlos" y
    # "Carlos Ruiz Zafón" generen la misma clave.
    author_tokens = sorted(set(normalize(" ".join(authors)).split()))
    return " ".join(author_tokens) + "|" + normalize(title)


def display_author(name: str) -> str:
    """``Apellido, Nombre`` -> ``Nombre Apellido``."""
    name = re.sub(r"\s+", " ", name).strip(" .,-")
    if name.count(",") == 1:
        last, first = (part.strip() for part in name.split(","))
        if first and last and first.lower() not in _NOT_FIRST_NAMES:
            return f"{first} {last}"
    return name


def split_authors(text: str) -> list[str]:
    authors = []
    for part in _AUTHOR_SPLIT.split(text or ""):
        name = display_author(part)
        if name and name not in authors:
            authors.append(name)
    return authors


def file_format(path: str) -> str | None:
    if "." not in path.rsplit("/", 1)[-1]:
        return None
    ext = path.rsplit(".", 1)[-1].lower()
    return ext if ext in EBOOK_FORMATS else None


def _clean(text: str) -> str:
    if " " not in text and "_" in text:
        text = text.replace("_", " ")
    text = _REVISION.sub("", text)
    text = _TAGS.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" .-_")


def _looks_like_author(name: str) -> bool:
    if not name or normalize(name) in _GENERIC_DIRS or re.search(r"\d", name):
        return False
    if "," in name:
        return True
    words = name.split()
    return 2 <= len(words) <= 5 and all(w[0].isupper() for w in words if len(w) > 3)


def _extract_series(title: str) -> tuple[str, str | None, float | None]:
    for pattern in (_SERIES_BRACKET, _SERIES_PAREN):
        match = pattern.search(title)
        if match and re.search(r"[^\W\d_]", match.group(1)):
            rest = (title[: match.start()] + " " + title[match.end():]).strip(" .-")
            if rest:
                index = float(match.group(2).replace(",", "."))
                return re.sub(r"\s+", " ", rest), match.group(1).strip(" ,.-"), index
    return title, None, None


def _build(title_raw: str, authors_raw: str, fallback: str) -> ParsedName:
    title, series, series_index = _extract_series(_clean(title_raw))
    year = None
    year_match = _YEAR.search(title)
    if year_match and _YEAR.sub("", title).strip():
        year = int(year_match.group(1))
        title = _YEAR.sub("", title).strip()
    title = title.strip(" .-") or fallback
    return ParsedName(
        title=title,
        authors=split_authors(_clean(authors_raw)),
        series=series,
        series_index=series_index,
        year=year,
    )


def parse_path(path: str, root: str | None = None) -> ParsedName:
    parts = [p for p in unicodedata.normalize("NFC", path).replace("\\", "/").split("/") if p]
    if root and len(parts) > 1 and parts[0] == root:
        parts = parts[1:]
    filename = parts[-1]
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    dirs = parts[:-1]

    # Biblioteca de Calibre: Autor/Título (id)/Título - Autor.ext
    if len(dirs) >= 2 and _CALIBRE_ID.search(dirs[-1]):
        return _build(_CALIBRE_ID.sub("", dirs[-1]), dirs[-2], stem)

    cleaned = _clean(stem)
    parent = _clean(dirs[-1]) if dirs else ""
    parent_author = parent if _looks_like_author(parent) else ""

    if " - " in cleaned:
        left, right = (s.strip() for s in cleaned.split(" - ", 1))
        if re.fullmatch(r"\d+", left):  # "01 - Título"
            return _build(right, parent_author, stem)
        if parent and normalize(parent) == normalize(right) != normalize(left):
            return _build(left, right, stem)  # "Título - Autor" dentro de la carpeta del autor
        if "," not in left and "," in parent_author and normalize(parent) != normalize(left):
            return _build(cleaned, parent_author, stem)
        return _build(right, left, stem)
    return _build(cleaned, parent_author, stem)
