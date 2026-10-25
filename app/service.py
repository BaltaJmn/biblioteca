"""Orquesta descargas y envíos al Kindle como trabajos con progreso."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Settings
from .db import Database
from .epub import fix_for_kindle, make_thumbnail, normalize_language, read_metadata
from .kindle import SendError, ascii_filename, send_email
from .torrents import DownloadError, NoPeersError, TorrentManager

log = logging.getLogger("biblioteca.service")


@dataclass
class Job:
    id: str
    kind: str  # "download" | "kindle"
    book_id: int
    title: str
    state: str = "queued"  # queued | downloading | sending | done | error
    progress: float = 0.0
    peers: int = 0
    rate: int = 0
    message: str = "En cola…"
    file_id: int | None = None
    created: float = field(default_factory=time.time)

    def update(self, info: dict) -> None:
        self.state = "downloading"
        self.progress = info.get("progress", 0.0)
        self.peers = info.get("peers", 0)
        self.rate = info.get("rate", 0)
        if self.peers == 0:
            self.message = "Buscando a alguien que lo comparta…"
        else:
            self.message = f"Descargando {self.progress:.0%} · {self.peers} conexiones"


class Library:
    def __init__(self, settings: Settings, db: Database, torrents: TorrentManager):
        self.settings = settings
        self.db = db
        self.torrents = torrents
        self.jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _new_job(self, kind: str, book: dict) -> Job:
        cutoff = time.time() - 3600
        for job_id in [j.id for j in self.jobs.values() if j.created < cutoff]:
            del self.jobs[job_id]
        job = Job(id=uuid.uuid4().hex, kind=kind, book_id=book["id"], title=book["title"])
        self.jobs[job.id] = job
        return job

    def job(self, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        return asdict(job) if job else None

    # ---- descarga ------------------------------------------------------------

    def start_download(self, file_id: int) -> Job:
        file_row = self.db.file(file_id)
        if file_row is None:
            raise LookupError("Fichero no encontrado")
        book = self.db.book(file_row["book_id"])
        job = self._new_job("download", book)
        job.file_id = file_id
        self._spawn(self._run_download(job, file_row))
        return job

    async def _run_download(self, job: Job, file_row) -> None:
        try:
            await self._fetch_any(job, file_row["book_id"], file_row["format"], file_row["id"])
            job.state, job.progress, job.message = "done", 1.0, "Listo"
        except Exception as exc:
            self._fail(job, exc)

    async def _fetch_any(self, job: Job, book_id: int, fmt: str, prefer: int | None = None) -> Path:
        """Prueba el fichero preferido y, si nadie lo comparte, el de otras fuentes."""
        candidates = self.db.files_for_book(book_id, fmt)
        candidates.sort(key=lambda r: r["id"] != prefer)
        last_error: Exception = DownloadError("Libro no disponible")
        for row in candidates:
            try:
                path = await self.torrents.fetch(row, job.update)
            except NoPeersError as exc:
                last_error = exc
                continue
            job.file_id = row["id"]
            await asyncio.to_thread(self._enrich, book_id, path, fmt)
            return path
        raise last_error

    def _enrich(self, book_id: int, path: Path, fmt: str) -> None:
        """Completa la ficha con lo que trae el EPUB (sinopsis, portada…)."""
        if fmt != "epub":
            return
        meta = read_metadata(path)
        cover = meta.pop("cover", None)
        has_cover = None
        if cover:
            dest = self.settings.covers_dir / f"{book_id}.jpg"
            if not dest.exists() and make_thumbnail(cover, dest):
                has_cover = 1
        self.db.enrich_book(book_id, has_cover=has_cover, **meta)

    # ---- Kindle ------------------------------------------------------------

    def start_kindle(self, book_id: int, kindle_id: int) -> Job:
        book = self.db.book(book_id)
        if book is None:
            raise LookupError("Libro no encontrado")
        kindle = self.db.kindle(kindle_id)
        if kindle is None:
            raise LookupError("Kindle no encontrado")
        fmt = book["kindle_format"]
        if fmt is None:
            raise ValueError("Este libro no tiene un formato que acepte Send to Kindle (EPUB o PDF)")
        if not self.settings.smtp_configured:
            raise ValueError("Falta configurar el correo (SMTP) en el fichero .env")
        job = self._new_job("kindle", book)
        self._spawn(self._run_kindle(job, book, dict(kindle), fmt))
        return job

    async def _run_kindle(self, job: Job, book: dict, kindle: dict, fmt: str) -> None:
        try:
            path = await self._fetch_any(job, book["id"], fmt)
            job.state, job.progress, job.message = "sending", 1.0, "Enviando al Kindle…"
            await asyncio.to_thread(self._send, book, kindle, path, fmt)
            job.state = "done"
            job.message = f"Enviado a {kindle['name']}. Aparecerá en unos minutos."
        except Exception as exc:
            self._fail(job, exc)

    def _send(self, book: dict, kindle: dict, path: Path, fmt: str) -> None:
        filename = ascii_filename(book["title"], book["authors"], fmt)
        with tempfile.TemporaryDirectory(dir=self.settings.data_dir) as tmp:
            attachment = path
            if fmt == "epub":
                attachment = Path(tmp) / filename
                language = normalize_language(book.get("language"), self.settings.default_language)
                try:
                    fix_for_kindle(path, attachment, language)
                except Exception:
                    log.exception("No se pudo adaptar el EPUB; se envía tal cual")
                    attachment = path
            send_email(
                self.settings,
                to=kindle["email"],
                subject=book["title"],
                body=f"{book['title']} — {book['authors'] or 'Autor desconocido'}",
                attachment=attachment,
                filename=filename,
            )

    def _fail(self, job: Job, exc: Exception) -> None:
        job.state = "error"
        if isinstance(exc, (DownloadError, SendError, ValueError, LookupError)):
            job.message = str(exc)
        else:
            log.exception("Error en el trabajo %s", job.id, exc_info=exc)
            job.message = "Error inesperado; revisa los logs"
