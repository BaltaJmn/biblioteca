"""Aplicación web: buscador, ficha de libro, descargas, Kindle y ajustes."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .config import Settings
from .db import PAGE_SIZE, Database
from .feed import check_feed, feed_loop
from .kindle import EMAIL_RE, SendError, ascii_filename, send_email
from .service import Library
from .torrents import TorrentManager

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("biblioteca")

BASE_DIR = Path(__file__).parent
COOKIE = "biblioteca_session"
PUBLIC_PATHS = ("/login", "/static/", "/healthz")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    db = Database(settings.db_path)
    torrents = TorrentManager(settings, db)
    await torrents.start()
    for magnet in settings.initial_magnets:
        try:
            torrents.add_source(magnet, origin="env")
        except ValueError as exc:
            log.error("Magnet de MAGNETS no válido: %s", exc)
    feed_task = None
    if settings.feed_url:
        feed_task = asyncio.create_task(
            feed_loop(settings.feed_url, settings.feed_interval_hours, db, torrents)
        )
    app.state.settings = settings
    app.state.db = db
    app.state.torrents = torrents
    app.state.library = Library(settings, db, torrents)
    try:
        yield
    finally:
        if feed_task:
            feed_task.cancel()
        await torrents.stop()


app = FastAPI(title="Biblioteca", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _size(num: int | None) -> str:
    num = num or 0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit in ("B", "KB") else f"{num:.1f} {unit}"
        num /= 1024
    return ""


def _hue(text: str) -> int:
    return int(hashlib.md5(text.encode()).hexdigest()[:4], 16) % 360


templates.env.filters["size"] = _size
templates.env.filters["hue"] = _hue
templates.env.filters["urlq"] = lambda s: quote(s or "")
templates.env.filters["num"] = lambda n: "" if n is None else (f"{n:g}")
LANGUAGE_NAMES = {
    "es": "Español", "spa": "Español", "en": "Inglés", "eng": "Inglés", "ca": "Catalán",
    "cat": "Catalán", "gl": "Gallego", "glg": "Gallego", "eu": "Euskera", "eus": "Euskera",
    "fr": "Francés", "fra": "Francés", "pt": "Portugués", "por": "Portugués",
    "it": "Italiano", "ita": "Italiano", "de": "Alemán", "deu": "Alemán",
}
templates.env.filters["language"] = lambda code: LANGUAGE_NAMES.get(
    (code or "").lower().split("-")[0], code
)


# ---- autenticación -----------------------------------------------------------


def _token(settings: Settings) -> str:
    key = (settings.secret_key + settings.app_password).encode()
    return hmac.new(key, b"biblioteca-session-v1", hashlib.sha256).hexdigest()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    settings: Settings = request.app.state.settings
    path = request.url.path
    if settings.app_password and not path.startswith(PUBLIC_PATHS):
        cookie = request.cookies.get(COOKIE, "")
        if not hmac.compare_digest(cookie, _token(settings)):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "No autorizado"}, status_code=401)
            target = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?{urlencode({'next': target})}", status_code=303)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/", error: str = ""):
    return templates.TemplateResponse(
        request, "login.html", {"next": next, "error": error, "page": "login"}
    )


@app.post("/login")
async def login(request: Request, password: str = Form(...), next: str = Form("/")):
    settings: Settings = request.app.state.settings
    if not settings.app_password or not hmac.compare_digest(password, settings.app_password):
        await asyncio.sleep(1.5)  # frena ataques de fuerza bruta
        return RedirectResponse(f"/login?{urlencode({'next': next, 'error': '1'})}", status_code=303)
    # Solo rutas locales: "//host" o "/\\host" llevarían a otra web.
    target = next if re.match(r"^/(?![/\\])", next) else "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        COOKIE,
        _token(settings),
        max_age=365 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@app.post("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ---- catálogo ----------------------------------------------------------------


def _kindle_context(request: Request) -> dict:
    return {
        "kindles": request.app.state.db.kindles(),
        "smtp_ok": request.app.state.settings.smtp_configured,
    }


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = "",
    autor: str = "",
    serie: str = "",
    p: int = 1,
    partial: bool = False,
):
    db: Database = request.app.state.db
    books, total = await asyncio.to_thread(db.search, q, autor, serie, p)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    params = {k: v for k, v in {"q": q, "autor": autor, "serie": serie}.items() if v}
    context = {
        "books": books,
        "total": total,
        "q": q,
        "autor": autor,
        "serie": serie,
        "page_num": p,
        "pages": pages,
        "query_string": urlencode(params),
        "searching": bool(params),
        "sources": db.sources(),
        "page": "index",
        **_kindle_context(request),
    }
    template = "_results.html" if partial else "index.html"
    return templates.TemplateResponse(request, template, context)


@app.get("/libro/{book_id}", response_class=HTMLResponse)
async def book_page(request: Request, book_id: int):
    book = request.app.state.db.book(book_id)
    if book is None:
        raise HTTPException(404, "Libro no encontrado")
    return templates.TemplateResponse(
        request,
        "book.html",
        {
            "book": book,
            "page": "book",
            **_kindle_context(request),
        },
    )


@app.get("/portada/{book_id}")
async def cover(request: Request, book_id: int):
    path = request.app.state.settings.covers_dir / f"{book_id}.jpg"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=604800"})


# ---- descargas y Kindle ------------------------------------------------------


class KindleRequest(BaseModel):
    kindle_id: int


def _job_response(job) -> JSONResponse:
    return JSONResponse({"job_id": job.id, "state": job.state, "message": job.message})


@app.post("/api/libros/{book_id}/kindle")
async def api_kindle(request: Request, book_id: int, body: KindleRequest):
    try:
        job = request.app.state.library.start_kindle(book_id, body.kindle_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return _job_response(job)


@app.post("/api/archivos/{file_id}/preparar")
async def api_prepare(request: Request, file_id: int):
    try:
        job = request.app.state.library.start_download(file_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return _job_response(job)


@app.get("/api/trabajos/{job_id}")
async def api_job(request: Request, job_id: str):
    job = request.app.state.library.job(job_id)
    if job is None:
        raise HTTPException(404, "Trabajo no encontrado")
    return job


@app.get("/descargar/{file_id}")
async def download(request: Request, file_id: int, job: str = ""):
    db: Database = request.app.state.db
    file_row = db.file(file_id)
    if file_row is None:
        raise HTTPException(404, "Fichero no encontrado")
    cached = db.cached_path(file_id)
    if cached and Path(cached).exists():
        book = db.book(file_row["book_id"])
        name = ascii_filename(book["title"], book["authors"], file_row["format"])
        return FileResponse(cached, filename=name)
    # Sin JavaScript (p. ej. navegador del Kindle): página que se recarga sola.
    library: Library = request.app.state.library
    state = library.job(job) if job else None
    if state is None:
        new_job = library.start_download(file_id)
        return RedirectResponse(f"/descargar/{file_id}?job={new_job.id}", status_code=303)
    return templates.TemplateResponse(
        request,
        "preparing.html",
        {"job": state, "file_id": file_id, "page": "preparing"},
    )


# ---- ajustes -----------------------------------------------------------------


@app.get("/ajustes", response_class=HTMLResponse)
async def settings_page(request: Request, msg: str = "", error: str = ""):
    db: Database = request.app.state.db
    torrents: TorrentManager = request.app.state.torrents
    sources = [dict(s) | {"progress": torrents.source_progress(s["id"])} for s in db.sources()]
    cache_count, cache_bytes = db.cache_usage()
    settings: Settings = request.app.state.settings
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "sources": sources,
            "busy": any(s["status"] not in ("ready", "error") for s in sources),
            "cache_count": cache_count,
            "cache_bytes": cache_bytes,
            "cache_max": settings.cache_max_mb * 1024 * 1024,
            "smtp_from": settings.smtp_from,
            "feed_url": settings.feed_url,
            "stats": torrents.stats(),
            "total_books": db.count_books(),
            "msg": msg,
            "error": error,
            "auth": bool(settings.app_password),
            "page": "settings",
            **_kindle_context(request),
        },
    )


def _back(msg: str = "", error: str = "") -> RedirectResponse:
    params = urlencode({k: v for k, v in {"msg": msg, "error": error}.items() if v})
    return RedirectResponse(f"/ajustes?{params}" if params else "/ajustes", status_code=303)


@app.post("/ajustes/kindles")
async def add_kindle(request: Request, name: str = Form(...), email: str = Form(...)):
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        return _back(error="Ese correo no parece válido")
    request.app.state.db.add_kindle(name.strip() or email.split("@")[0], email)
    return _back(msg="Kindle guardado")


@app.post("/ajustes/kindles/{kindle_id}/borrar")
async def delete_kindle(request: Request, kindle_id: int):
    request.app.state.db.delete_kindle(kindle_id)
    return _back(msg="Kindle eliminado")


@app.post("/ajustes/fuentes")
async def add_source(request: Request, magnet: str = Form(...)):
    try:
        request.app.state.torrents.add_source(magnet)
    except ValueError as exc:
        return _back(error=str(exc))
    return _back(msg="Fuente añadida: obteniendo la lista de libros…")


@app.post("/ajustes/fuentes/{source_id}/borrar")
async def delete_source(request: Request, source_id: int):
    await request.app.state.torrents.remove_source(source_id)
    return _back(msg="Fuente eliminada")


@app.post("/ajustes/fuentes/{source_id}/reindexar")
async def reindex_source(request: Request, source_id: int):
    request.app.state.torrents.prepare_source(source_id)
    return _back(msg="Reindexando…")


@app.post("/ajustes/feed")
async def run_feed(request: Request):
    settings: Settings = request.app.state.settings
    if not settings.feed_url:
        return _back(error="No hay MAGNET_FEED_URL configurada")
    try:
        added = await check_feed(settings.feed_url, request.app.state.db, request.app.state.torrents)
    except Exception as exc:
        return _back(error=f"No se pudo leer el feed: {exc}")
    return _back(msg=f"Feed comprobado: {added} fuente(s) nueva(s)")


@app.post("/ajustes/cache/vaciar")
async def clear_cache(request: Request):
    request.app.state.torrents.clear_cache()
    return _back(msg="Caché vaciada")


@app.post("/ajustes/correo/probar")
async def test_email(request: Request):
    settings: Settings = request.app.state.settings
    try:
        await asyncio.to_thread(
            send_email,
            settings,
            settings.smtp_from,
            "Prueba de la biblioteca",
            "Si te llega este correo, el envío al Kindle está bien configurado.",
        )
    except SendError as exc:
        return _back(error=str(exc))
    return _back(msg=f"Correo de prueba enviado a {settings.smtp_from}")


@app.get("/api/estado")
async def api_status(request: Request):
    db: Database = request.app.state.db
    torrents: TorrentManager = request.app.state.torrents
    return {
        "books": db.count_books(),
        "sources": [
            {k: s[k] for k in ("id", "name", "status", "error", "num_books")}
            | {"progress": torrents.source_progress(s["id"])}
            for s in db.sources()
        ],
        **torrents.stats(),
    }
