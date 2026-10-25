"""Gestor BitTorrent: catálogo a partir de magnets y descarga de libros bajo demanda.

Nunca se descarga el torrent completo. Para cada petición se añade el torrent
con todos los ficheros desactivados salvo el pedido; al terminar se copia a la
caché y se elimina el torrent junto con sus datos temporales. Así el disco solo
guarda el catálogo (SQLite) y los libros que alguien ha pedido.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import shutil
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import libtorrent as lt

from .config import Settings
from .db import Database
from .indexer import TorrentFile, find_calibre_db, index_source, read_calibre

log = logging.getLogger("biblioteca.torrent")

TRACKERS_FALLBACK = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://explodie.org:6969/announce",
]
DHT_BOOTSTRAP = (
    "dht.libtorrent.org:25401,router.bittorrent.com:6881,"
    "router.utorrent.com:6881,dht.transmissionbt.com:6881"
)


# Los catálogos grandes (100.000+ ficheros) superan los límites por defecto.
LOAD_LIMITS = {
    "max_buffer_size": 256 * 1024 * 1024,
    "max_pieces": 0x800000,
    "max_decode_tokens": 50_000_000,
    "max_decode_depth": 100,
}


class DownloadError(Exception):
    pass


class NoPeersError(DownloadError):
    pass


def _hash_key(info_hashes) -> str:
    v1 = info_hashes.v1
    return str(v1) if not v1.is_all_zeros() else str(info_hashes.get_best())


def parse_magnet(magnet: str) -> tuple[str, str | None]:
    """Devuelve (infohash, nombre) o lanza ValueError."""
    magnet = magnet.strip()
    if not magnet.startswith("magnet:?"):
        raise ValueError("No parece un enlace magnet")
    try:
        atp = lt.parse_magnet_uri(magnet)
    except RuntimeError as exc:
        raise ValueError(f"Magnet no válido: {exc}") from exc
    return _hash_key(atp.info_hashes), atp.name or None


def load_torrent_info(path: Path) -> lt.torrent_info:
    return lt.torrent_info(str(path), LOAD_LIMITS)


def _layout(ti: lt.torrent_info):
    return ti.layout() if hasattr(ti, "layout") else ti.files()


def torrent_files(ti: lt.torrent_info) -> list[TorrentFile]:
    fs = _layout(ti)
    pad = lt.file_storage.flag_pad_file
    return [
        TorrentFile(i, fs.file_path(i).replace("\\", "/"), fs.file_size(i))
        for i in range(fs.num_files())
        if not fs.file_flags(i) & pad
    ]


@dataclass
class _Active:
    handle: lt.torrent_handle
    users: int = 0


class TorrentManager:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.session: lt.session | None = None
        self._active: dict[int, _Active] = {}
        self._source_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._file_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._waiters: dict[tuple[str, str], list[asyncio.Future]] = defaultdict(list)
        self._errors: dict[str, str] = {}
        self._progress: dict[int, dict] = {}
        self._tasks: set[asyncio.Task] = set()
        self._preparing: set[int] = set()
        self._dht_nodes = 0
        self._stopping = False

    # ---- ciclo de vida ------------------------------------------------------

    def _settings_pack(self) -> dict:
        s = self.settings
        return {
            "listen_interfaces": s.listen_interfaces,
            "enable_dht": s.enable_dht,
            "enable_lsd": s.enable_lsd,
            "enable_upnp": s.enable_upnp,
            "enable_natpmp": s.enable_upnp,
            "dht_bootstrap_nodes": DHT_BOOTSTRAP,
            "upload_rate_limit": s.upload_limit_kbps * 1024,
            "alert_mask": lt.alert_category.status
            | lt.alert_category.error
            | lt.alert_category.storage,
            "connections_limit": 200,
            "max_metadata_size": 128 * 1024 * 1024,
        }

    @property
    def _state_file(self) -> Path:
        return self.settings.data_dir / "session.state"

    async def start(self) -> None:
        s = self.settings
        shutil.rmtree(s.staging_dir, ignore_errors=True)
        for d in (s.staging_dir, s.torrents_dir, s.cache_dir, s.covers_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._sweep_cache_dir()
        params = None
        if self._state_file.exists():
            try:
                # Recupera los nodos DHT conocidos: arranques mucho más rápidos.
                params = lt.read_session_params(self._state_file.read_bytes())
                params.settings = self._settings_pack()
            except Exception:
                log.warning("No se pudo leer el estado de la sesión; se empieza de cero")
                params = None
        self.session = lt.session(params if params is not None else self._settings_pack())
        self._spawn(self._alert_loop())
        self._spawn(self._save_state_loop())
        for src in self.db.sources():
            if src["status"] != "ready" or not self._torrent_file(src["infohash"]).exists():
                self.prepare_source(src["id"])

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.session is not None:
            self._save_state()
            self.session.pause()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _save_state(self) -> None:
        try:
            data = lt.write_session_params_buf(self.session.session_state())
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(self._state_file)
        except Exception:
            log.exception("No se pudo guardar el estado DHT")

    async def _save_state_loop(self) -> None:
        while True:
            await asyncio.sleep(1800)
            self._save_state()

    async def _alert_loop(self) -> None:
        last_stats = 0.0
        while True:
            if time.monotonic() - last_stats > 10:
                self.session.post_session_stats()
                last_stats = time.monotonic()
            for alert in self.session.pop_alerts():
                try:
                    self._handle_alert(alert)
                except Exception:
                    log.exception("Error procesando alerta %s", alert.what())
            await asyncio.sleep(0.2)

    def _handle_alert(self, alert) -> None:
        what = alert.what()
        if what in ("torrent_deleted", "torrent_delete_failed"):
            self._resolve(_hash_key(alert.info_hashes), "deleted")
        elif what == "metadata_received":
            self._resolve(_hash_key(alert.handle.info_hashes()), "metadata")
        elif what == "cache_flushed":
            self._resolve(_hash_key(alert.handle.info_hashes()), "flushed")
        elif what in ("torrent_error", "file_error", "metadata_failed"):
            key = _hash_key(alert.handle.info_hashes())
            self._errors[key] = alert.message()
            log.warning("%s", alert.message())
        elif what == "session_stats":
            self._dht_nodes = alert.values.get("dht.dht_nodes", 0)
        elif what in ("listen_failed", "portmap_error"):
            log.warning("%s", alert.message())
        elif what in ("listen_succeeded", "portmap"):
            log.info("%s", alert.message())

    def _expect(self, infohash: str, event: str) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        self._waiters[(infohash, event)].append(fut)
        return fut

    def _resolve(self, infohash: str, event: str) -> None:
        for fut in self._waiters.pop((infohash, event), []):
            if not fut.done():
                fut.set_result(True)

    # ---- ficheros auxiliares --------------------------------------------------

    def _torrent_file(self, infohash: str) -> Path:
        return self.settings.torrents_dir / f"{infohash}.torrent"

    def _staging(self, infohash: str) -> Path:
        return self.settings.staging_dir / infohash

    def _sweep_cache_dir(self) -> None:
        known = {Path(r["path"]).name for r in self.db.cache_entries_lru()}
        for path in self.settings.cache_dir.iterdir():
            if path.name not in known:
                path.unlink(missing_ok=True)

    def _params(self, src, priorities: dict[int, int] | None) -> lt.add_torrent_params:
        atp = lt.parse_magnet_uri(src["magnet"])
        if not atp.trackers:
            atp.trackers = TRACKERS_FALLBACK
        tfile = self._torrent_file(src["infohash"])
        if tfile.exists():
            atp.ti = load_torrent_info(tfile)
            if priorities is not None:
                prio = [0] * atp.ti.num_files()
                for index, value in priorities.items():
                    prio[index] = value
                atp.file_priorities = prio
        atp.save_path = str(self._staging(src["infohash"]))
        atp.flags = (atp.flags | lt.torrent_flags.default_dont_download) & ~(
            lt.torrent_flags.auto_managed | lt.torrent_flags.paused
        )
        return atp

    async def _remove(self, source_id: int, infohash: str) -> None:
        """Quita el torrent de la sesión y borra sus datos temporales."""
        active = self._active.pop(source_id, None)
        if active is None:
            return
        deleted = self._expect(infohash, "deleted")
        self.session.remove_torrent(active.handle, lt.session.delete_files)
        if self._stopping:
            return  # al apagar no hay bucle de alertas; staging se limpia al arrancar
        try:
            await asyncio.wait_for(deleted, 30)
        except asyncio.TimeoutError:
            log.warning("libtorrent tardó en borrar %s", infohash)
        self._errors.pop(infohash, None)
        shutil.rmtree(self._staging(infohash), ignore_errors=True)

    # ---- fuentes ------------------------------------------------------------

    def add_source(self, magnet: str, origin: str = "manual") -> int:
        infohash, name = parse_magnet(magnet)
        existing = self.db.source_by_hash(infohash)
        if existing:
            return existing["id"]
        source_id = self.db.add_source(infohash, magnet.strip(), name, origin)
        self.prepare_source(source_id)
        return source_id

    def prepare_source(self, source_id: int) -> None:
        """Obtiene la lista de ficheros (si hace falta) e indexa la fuente."""
        if source_id not in self._preparing:
            self._preparing.add(source_id)
            self._spawn(self._prepare(source_id))

    def source_progress(self, source_id: int) -> dict | None:
        return self._progress.get(source_id)

    async def _prepare(self, source_id: int) -> None:
        try:
            src = self.db.source(source_id)
            if src is None:
                return
            if not self._torrent_file(src["infohash"]).exists():
                await self._fetch_metadata(src)
            self.db.update_source(source_id, status="indexing", error=None)
            ti = await asyncio.to_thread(load_torrent_info, self._torrent_file(src["infohash"]))
            files = await asyncio.to_thread(torrent_files, ti)
            calibre = None
            calibre_file = find_calibre_db(files)
            if calibre_file is not None:
                calibre = await self._load_calibre(src, calibre_file)
            num_books = await asyncio.to_thread(
                index_source, self.db, source_id, files, ti.name(), calibre
            )
            self.db.update_source(
                source_id,
                status="ready",
                name=src["name"] or ti.name(),
                num_files=len(files),
                num_books=num_books,
                total_size=sum(f.size for f in files),
                indexed_at=time.time(),
                error=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Error preparando la fuente %s", source_id)
            self.db.update_source(source_id, status="error", error=str(exc))
        finally:
            self._preparing.discard(source_id)
            self._progress.pop(source_id, None)

    async def _fetch_metadata(self, src) -> None:
        source_id, infohash = src["id"], src["infohash"]
        self.db.update_source(source_id, status="metadata", error=None)
        async with self._source_locks[source_id]:
            await self._remove(source_id, infohash)
            got = self._expect(infohash, "metadata")
            handle = self.session.add_torrent(self._params(src, None))
            self._active[source_id] = _Active(handle, users=1)
            try:
                while not handle.status().has_metadata:
                    if infohash in self._errors:
                        raise DownloadError(self._errors[infohash])
                    st = handle.status()
                    self._progress[source_id] = {"peers": st.num_peers, "since": st.active_duration}
                    try:
                        await asyncio.wait_for(asyncio.shield(got), 2)
                    except asyncio.TimeoutError:
                        pass
                ti = handle.torrent_file()
                # Solo guardamos el diccionario "info": los trackers salen del magnet.
                data = b"d4:info" + bytes(ti.info_section()) + b"e"
                tfile = self._torrent_file(infohash)
                tfile.with_suffix(".tmp").write_bytes(data)
                tfile.with_suffix(".tmp").replace(tfile)
                if not src["name"]:
                    self.db.update_source(source_id, name=ti.name())
            finally:
                await self._remove(source_id, infohash)

    async def _load_calibre(self, src, calibre_file: TorrentFile) -> dict | None:
        log.info("Biblioteca de Calibre detectada: descargando %s", calibre_file.path)
        self.db.update_source(src["id"], status="indexing", error="Descargando metadata.db de Calibre…")
        with tempfile.TemporaryDirectory(dir=self.settings.data_dir) as tmp:
            dest = Path(tmp) / "metadata.db"
            try:
                await self._download_to(src, calibre_file.index, calibre_file.size, dest, None)
                root = posixpath.dirname(calibre_file.path)
                return await asyncio.to_thread(read_calibre, dest, root)
            except Exception as exc:
                log.warning("No se pudo usar metadata.db (%s); se indexa por nombre de fichero", exc)
                return None

    # ---- descargas ----------------------------------------------------------

    async def fetch(self, file_row, progress: Callable[[dict], None] | None = None) -> Path:
        """Garantiza que el fichero está en la caché y devuelve su ruta."""
        file_id = file_row["id"]
        async with self._file_locks[file_id]:
            cached = self.db.cached_path(file_id)
            if cached and Path(cached).exists():
                return Path(cached)
            src = self.db.source(file_row["source_id"])
            dest = self.settings.cache_dir / f"{file_id}.{file_row['format']}"
            await self._download_to(src, file_row["file_index"], file_row["size"], dest, progress)
            self.db.add_cached(file_id, str(dest), dest.stat().st_size)
            self._evict(keep=file_id)
            return dest

    async def _download_to(self, src, index: int, size: int, dest: Path, progress) -> None:
        source_id, infohash = src["id"], src["infohash"]
        handle = await self._acquire(src, index)
        try:
            await self._wait_complete(infohash, handle, index, size, progress)
            flushed = self._expect(infohash, "flushed")
            handle.flush_cache()
            try:
                await asyncio.wait_for(flushed, 30)
            except asyncio.TimeoutError:
                pass
            ti = handle.torrent_file()
            staged = self._staging(infohash) / _layout(ti).file_path(index)
            tmp = dest.with_suffix(dest.suffix + ".part")
            await asyncio.to_thread(shutil.copyfile, staged, tmp)
            if tmp.stat().st_size != size:
                tmp.unlink(missing_ok=True)
                raise DownloadError("El fichero descargado está incompleto")
            tmp.replace(dest)
        finally:
            await self._release(source_id, infohash)

    async def _acquire(self, src, index: int) -> lt.torrent_handle:
        source_id, infohash = src["id"], src["infohash"]
        while True:
            async with self._source_locks[source_id]:
                active = self._active.get(source_id)
                if active is None:
                    # Cargar un .torrent de 100.000 ficheros tarda ~0,5 s: fuera del bucle.
                    atp = await asyncio.to_thread(self._params, src, {index: 7})
                    handle = self.session.add_torrent(atp)
                    self._active[source_id] = _Active(handle, users=1)
                    return handle
                if not active.handle.status().is_finished:
                    # Hay otra descarga en marcha en este torrent: añadimos el fichero.
                    active.handle.file_priority(index, 7)
                    active.users += 1
                    return active.handle
                if active.users == 0:
                    # Un torrent "terminado" no vuelve a conectar con los seeds, así que
                    # se reinicia con el nuevo fichero seleccionado.
                    await self._remove(source_id, infohash)
                    continue
            await asyncio.sleep(0.3)

    async def _release(self, source_id: int, infohash: str) -> None:
        async with self._source_locks[source_id]:
            active = self._active.get(source_id)
            if active is None:
                return
            active.users -= 1
            if active.users <= 0:
                await self._remove(source_id, infohash)

    async def _wait_complete(self, infohash, handle, index, size, progress) -> None:
        s = self.settings
        start = last_peer = time.monotonic()
        while True:
            if infohash in self._errors:
                raise DownloadError(self._errors[infohash])
            st = handle.status()
            done = handle.file_progress(flags=lt.torrent_handle.piece_granularity)[index]
            if done >= size:
                return
            now = time.monotonic()
            if st.num_peers > 0:
                last_peer = now
            elif now - last_peer > s.peer_timeout:
                raise NoPeersError("Nadie comparte este libro ahora mismo")
            if now - start > s.download_timeout:
                raise DownloadError("La descarga ha tardado demasiado")
            if progress:
                progress(
                    {
                        "progress": done / size if size else 1.0,
                        "peers": st.num_peers,
                        "seeds": st.num_seeds,
                        "rate": st.download_rate,
                    }
                )
            await asyncio.sleep(0.5)

    # ---- caché --------------------------------------------------------------

    def _evict(self, keep: int | None = None) -> None:
        limit = self.settings.cache_max_mb * 1024 * 1024
        entries = self.db.cache_entries_lru()
        total = sum(e["size"] for e in entries)
        for entry in entries:
            if total <= limit:
                break
            if entry["file_id"] == keep:
                continue
            Path(entry["path"]).unlink(missing_ok=True)
            self.db.remove_cached(entry["file_id"])
            total -= entry["size"]

    def clear_cache(self) -> None:
        for path in self.db.clear_cache():
            Path(path).unlink(missing_ok=True)

    async def remove_source(self, source_id: int) -> None:
        src = self.db.source(source_id)
        if src is None:
            return
        async with self._source_locks[source_id]:
            await self._remove(source_id, src["infohash"])
        for path in await asyncio.to_thread(self.db.delete_source, source_id):
            Path(path).unlink(missing_ok=True)
        self._torrent_file(src["infohash"]).unlink(missing_ok=True)

    def stats(self) -> dict:
        return {"dht_nodes": self._dht_nodes, "active_torrents": len(self._active)}
