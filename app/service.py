"""Orquesta descargas y envíos al Kindle como trabajos con progreso.

Los libros no se guardan: el envío al Kindle trabaja en una carpeta temporal
que se borra al terminar, y una descarga se borra unos minutos después de
entregarla al navegador (o al cabo de una hora si nadie la recoge)."""

from __future__ import annotations

import asyncio
import logging
import shutil
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
    path: str | None = None  # fichero listo para descargar
    expires: float | None = None

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

    @property
    def temp_dir(self) -> Path:
        return self.settings.temp_dir

    def start(self) -> None:
        self._spawn(self._sweep_loop())

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _new_job(self, kind: str, book: dict) -> Job:
        job = Job(id=uuid.uuid4().hex, kind=kind, book_id=book["id"], title=book["title"])
        self.jobs[job.id] = job
        return job

    def job(self, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        data = asdict(job)
        del data["path"], data["expires"]
        return data

    # ---- limpieza -------------------------------------------------------------

    def sweep(self) -> None:
        """Borra las descargas caducadas, los ficheros huérfanos y los trabajos viejos."""
        now = time.time()
        for job in list(self.jobs.values()):
            if job.path and job.expires is not None and job.expires <= now:
                Path(job.path).unlink(missing_ok=True)
                job.path = None
            if job.state in ("done", "error") and not job.path and now - job.created > 3600:
                del self.jobs[job.id]
        for entry in self.temp_dir.iterdir():
            if entry.name.split(".")[0] not in self.jobs:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                self.sweep()
            except Exception:
                log.exception("Error limpiando ficheros temporales")

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
            dest = self.temp_dir / f"{job.id}.{file_row['format']}"
            await self._fetch_any(job, file_row["book_id"], file_row["format"], dest, file_row["id"])
            job.path = str(dest)
            job.expires = time.time() + 3600  # si nadie lo recoge
            job.state, job.progress, job.message = "done", 1.0, "Listo"
        except Exception as exc:
            self._fail(job, exc)

    def take_download(self, job_id: str) -> tuple[Path, str] | None:
        """Fichero listo de un trabajo de descarga y su nombre para el navegador."""
        job = self.jobs.get(job_id)
        if job is None or job.state != "done" or not job.path or not Path(job.path).exists():
            return None
        # Margen para reintentos del navegador; después se borra.
        keep_until = time.time() + self.settings.download_keep_minutes * 60
        job.expires = min(job.expires or keep_until, keep_until)
        file_row = self.db.file(job.file_id)
        book = self.db.book(job.book_id)
        return Path(job.path), ascii_filename(book["title"], book["authors"], file_row["format"])

    async def _fetch_any(
        self, job: Job, book_id: int, fmt: str, dest: Path, prefer: int | None = None
    ) -> None:
        """Prueba el fichero preferido y, si nadie lo comparte, el de otras fuentes."""
        candidates = self.db.files_for_book(book_id, fmt)
        candidates.sort(key=lambda r: r["id"] != prefer)
        last_error: Exception = DownloadError("Libro no disponible")
        for row in candidates:
            try:
                await self.torrents.download(row, dest, job.update)
            except NoPeersError as exc:
                last_error = exc
                continue
            job.file_id = row["id"]
            await asyncio.to_thread(self._enrich, book_id, dest, fmt)
            return
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
            # La carpeta (libro + copia corregida) desaparece al salir del bloque.
            with tempfile.TemporaryDirectory(dir=self.temp_dir, prefix=f"{job.id}.") as tmp:
                path = Path(tmp) / f"libro.{fmt}"
                await self._fetch_any(job, book["id"], fmt, path)
                job.state, job.progress, job.message = "sending", 1.0, "Enviando al Kindle…"
                await asyncio.to_thread(self._send, book, kindle, path, fmt)
            job.state = "done"
            job.message = f"Enviado a {kindle['name']}. Aparecerá en unos minutos."
        except Exception as exc:
            self._fail(job, exc)

    def _send(self, book: dict, kindle: dict, path: Path, fmt: str) -> None:
        filename = ascii_filename(book["title"], book["authors"], fmt)
        attachment = path
        if fmt == "epub":
            attachment = path.with_name(f"kindle.{fmt}")
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
