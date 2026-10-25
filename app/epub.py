"""Utilidades EPUB: leer metadatos/portada y dejar el fichero listo para Kindle."""

from __future__ import annotations

import html
import io
import logging
import posixpath
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

log = logging.getLogger("biblioteca.epub")

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
# Idiomas que acepta el conversor de Send to Kindle (lista de kindle-epub-fix).
KINDLE_LANGUAGES = {
    "af", "gsw", "ar", "eu", "nb", "br", "ca", "zh", "kw", "co", "da", "nl", "stq", "en",
    "fi", "fr", "fy", "gl", "de", "gu", "hi", "is", "ga", "it", "ja", "lb", "mr", "ml",
    "gv", "frr", "nn", "pl", "pt", "oc", "rm", "sco", "gd", "es", "sv", "ta", "cy",
}
LANGUAGE_ALIASES = {
    "spa": "es", "esp": "es", "español": "es", "espanol": "es", "castellano": "es",
    "spanish": "es", "eng": "en", "english": "en", "cat": "ca", "glg": "gl", "gal": "gl",
    "eus": "eu", "baq": "eu", "fra": "fr", "fre": "fr", "por": "pt", "ita": "it",
    "deu": "de", "ger": "de",
}
HTML_EXTENSIONS = (".html", ".xhtml", ".htm")


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)</(?:p|div|h\d|li|blockquote)\s*>", "\n\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() or None


def normalize_language(value: str | None, default: str) -> str:
    code = (value or "").strip().lower().replace("_", "-")
    code = LANGUAGE_ALIASES.get(code, code).split("-")[0]
    code = LANGUAGE_ALIASES.get(code, code)
    return code if code in KINDLE_LANGUAGES else default


def _opf_path(zf: zipfile.ZipFile) -> str:
    container = ET.fromstring(zf.read("META-INF/container.xml"))
    rootfile = container.find(".//c:rootfile", NS)
    if rootfile is None:
        raise ValueError("EPUB sin rootfile")
    return rootfile.attrib["full-path"]


def read_metadata(path: Path) -> dict:
    """Descripción, idioma, editorial, año y portada (bytes) de un EPUB."""
    result: dict = {}
    try:
        with zipfile.ZipFile(path) as zf:
            opf_name = _opf_path(zf)
            opf = ET.fromstring(zf.read(opf_name))
            meta = opf.find("opf:metadata", NS)
            if meta is not None:
                def first(tag: str) -> str | None:
                    el = meta.find(f"dc:{tag}", NS)
                    return el.text.strip() if el is not None and el.text else None

                result["description"] = html_to_text(first("description"))
                result["language"] = first("language")
                result["publisher"] = first("publisher")
                date = first("date") or ""
                if m := re.match(r"(\d{4})", date):
                    result["year"] = int(m.group(1))
                subjects = [el.text.strip() for el in meta.findall("dc:subject", NS) if el.text]
                result["tags"] = ", ".join(subjects) or None
            cover = _find_cover(opf, opf_name)
            if cover and cover in zf.namelist():
                result["cover"] = zf.read(cover)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, ValueError) as exc:
        log.info("No se pudieron leer metadatos de %s: %s", path.name, exc)
    return result


def _find_cover(opf: ET.Element, opf_name: str) -> str | None:
    items = opf.findall("opf:manifest/opf:item", NS)
    images = [i for i in items if i.attrib.get("media-type", "").startswith("image/")]
    chosen = next((i for i in images if "cover-image" in i.attrib.get("properties", "")), None)
    if chosen is None:
        meta = opf.find("opf:metadata/opf:meta[@name='cover']", NS)
        cover_id = meta.attrib.get("content") if meta is not None else None
        chosen = next((i for i in images if i.attrib.get("id") == cover_id), None)
    if chosen is None:
        chosen = next(
            (i for i in images if "cover" in (i.attrib.get("id", "") + i.attrib.get("href", "")).lower()),
            None,
        )
    if chosen is None:
        return None
    base = posixpath.dirname(opf_name)
    return posixpath.normpath(posixpath.join(base, unquote(chosen.attrib["href"])))


def make_thumbnail(data: bytes, dest: Path) -> bool:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")
            img.thumbnail((360, 540))
            img.save(dest, "JPEG", quality=82, optimize=True)
        return True
    except Exception as exc:  # imagen corrupta o formato raro: no es grave
        log.info("Portada no válida: %s", exc)
        return False


def fix_for_kindle(src: Path, dest: Path, default_language: str = "es") -> None:
    """Copia el EPUB corrigiendo los fallos que hacen que Amazon lo rechace.

    Basado en kindle-epub-fix: declaración de codificación en los XHTML,
    enlaces a ``fichero#id-del-body``, ``<img>`` sin ``src`` e idioma válido."""
    with zipfile.ZipFile(src) as zin:
        entries = [(info, zin.read(info.filename)) for info in zin.infolist()]
        opf_name = _opf_path(zin)

    texts: dict[str, str] = {}
    for info, data in entries:
        name = info.filename
        if name.lower().endswith(HTML_EXTENSIONS + (".ncx", ".opf")):
            try:
                texts[name] = data.decode("utf-8")
            except UnicodeDecodeError:
                pass

    body_ids = {}
    for name, text in texts.items():
        if name.lower().endswith(HTML_EXTENSIONS):
            if m := re.search(r"<body[^>]*\sid=[\"']([^\"']+)[\"']", text):
                body_ids[posixpath.basename(name)] = m.group(1)

    for name, text in list(texts.items()):
        original = text
        for filename, body_id in body_ids.items():
            text = text.replace(f"{filename}#{body_id}", filename)
        if name.lower().endswith(HTML_EXTENSIONS):
            if not text.lstrip().startswith("<?xml"):
                text = '<?xml version="1.0" encoding="utf-8"?>\n' + text.lstrip()
            text = re.sub(r"<img(?![^>]*\bsrc\s*=)[^>]*>", "", text)
        if name == opf_name:
            text = _fix_language(text, default_language)
        if text != original:
            texts[name] = text

    with zipfile.ZipFile(dest, "w") as zout:
        # "mimetype" debe ir primero y sin comprimir.
        mimetype = next((d for i, d in entries if i.filename == "mimetype"), b"application/epub+zip")
        zout.writestr(zipfile.ZipInfo("mimetype"), mimetype, compress_type=zipfile.ZIP_STORED)
        for info, data in entries:
            if info.filename == "mimetype" or info.is_dir():
                continue
            payload = texts[info.filename].encode("utf-8") if info.filename in texts else data
            zout.writestr(info.filename, payload, compress_type=zipfile.ZIP_DEFLATED)


def _fix_language(opf: str, default: str) -> str:
    pattern = re.compile(r"(<(?:dc:)?language[^>]*>)([^<]*)(</(?:dc:)?language>)", re.I)
    if m := pattern.search(opf):
        fixed = normalize_language(m.group(2), default)
        return opf[: m.start()] + m.group(1) + fixed + m.group(3) + opf[m.end():]
    tag = f'<dc:language xmlns:dc="{NS["dc"]}">{default}</dc:language>'
    return re.sub(r"(</(?:opf:)?metadata>)", tag + r"\1", opf, count=1)
