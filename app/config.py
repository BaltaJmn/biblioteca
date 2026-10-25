"""Configuración leída de variables de entorno (ver .env.example)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


def _list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [part.strip() for part in raw.replace(",", "\n").splitlines() if part.strip()]


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    app_password: str
    secret_key: str
    cache_max_mb: int
    # BitTorrent
    listen_interfaces: str
    enable_dht: bool
    enable_lsd: bool
    enable_upnp: bool
    upload_limit_kbps: int
    peer_timeout: int
    download_timeout: int
    initial_magnets: list[str]
    feed_url: str
    feed_interval_hours: float
    # Correo (Send to Kindle)
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_security: str
    default_language: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "biblioteca.db"

    @property
    def torrents_dir(self) -> Path:
        return self.data_dir / "torrents"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def covers_dir(self) -> Path:
        return self.data_dir / "covers"

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("DATA_DIR", "./data")).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        secret = os.environ.get("SECRET_KEY", "").strip()
        if not secret:
            # Se genera una vez y se guarda para que las sesiones sobrevivan a reinicios.
            key_file = data_dir / "secret.key"
            if key_file.exists():
                secret = key_file.read_text().strip()
            else:
                secret = secrets.token_hex(32)
                key_file.write_text(secret)
        security = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()
        if security not in {"starttls", "ssl", "none"}:
            raise ValueError("SMTP_SECURITY debe ser starttls, ssl o none")
        port = _int("TORRENT_PORT", 6881)
        return cls(
            data_dir=data_dir,
            app_password=os.environ.get("APP_PASSWORD", ""),
            secret_key=secret,
            cache_max_mb=_int("CACHE_MAX_MB", 1024),
            listen_interfaces=os.environ.get(
                "TORRENT_LISTEN_INTERFACES", f"0.0.0.0:{port},[::]:{port}"
            ),
            enable_dht=_bool("TORRENT_DHT", True),
            enable_lsd=_bool("TORRENT_LSD", True),
            enable_upnp=_bool("TORRENT_UPNP", True),
            upload_limit_kbps=_int("TORRENT_UPLOAD_LIMIT_KBPS", 0),
            peer_timeout=_int("TORRENT_PEER_TIMEOUT", 120),
            download_timeout=_int("TORRENT_DOWNLOAD_TIMEOUT", 900),
            initial_magnets=_list("MAGNETS"),
            feed_url=os.environ.get("MAGNET_FEED_URL", "").strip(),
            feed_interval_hours=_float("MAGNET_FEED_INTERVAL_HOURS", 24),
            smtp_host=os.environ.get("SMTP_HOST", "").strip(),
            smtp_port=_int("SMTP_PORT", 465 if security == "ssl" else 587),
            smtp_user=os.environ.get("SMTP_USER", "").strip(),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            smtp_from=os.environ.get("SMTP_FROM", "").strip()
            or os.environ.get("SMTP_USER", "").strip(),
            smtp_security=security,
            default_language=os.environ.get("DEFAULT_LANGUAGE", "es").strip() or "es",
        )
