import pytest
from aiosmtpd.controller import Controller

from helpers import Inbox, free_port


@pytest.fixture()
def inbox():
    box = Inbox()
    controller = Controller(box, hostname="127.0.0.1", port=free_port())
    controller.start()
    box.port = controller.port
    yield box
    controller.stop()


@pytest.fixture()
def data_dir(tmp_path, monkeypatch, inbox):
    """Configura la app para funcionar sin Internet (solo con el seeder local)."""
    path = tmp_path / "data"
    for key, value in {
        "DATA_DIR": str(path),
        "CLOUDFLARE_ACCESS_REQUIRED": "false",
        "TORRENT_LISTEN_INTERFACES": f"127.0.0.1:{free_port()}",
        "TORRENT_DHT": "0",
        "TORRENT_LSD": "0",
        "TORRENT_UPNP": "0",
        "TORRENT_PEER_TIMEOUT": "20",
        "SMTP_HOST": "127.0.0.1",
        "SMTP_PORT": str(inbox.port),
        "SMTP_SECURITY": "none",
        "SMTP_FROM": "biblioteca@example.com",
        "DOWNLOAD_KEEP_MINUTES": "0",
    }.items():
        monkeypatch.setenv(key, value)
    return path
