"""Utilidades compartidas por los tests: seeder BitTorrent local, EPUB falsos y SMTP."""

import email
import io
import socket
import time
import zipfile
from email import policy
from pathlib import Path

import libtorrent as lt
from PIL import Image


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_epub(path: Path, title: str, author: str, language: str | None) -> None:
    cover = io.BytesIO()
    Image.new("RGB", (600, 900), (120, 40, 20)).save(cover, "PNG")
    lang = f"<dc:language>{language}</dc:language>" if language else ""
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>{lang}
    <dc:description>&lt;p&gt;Una novela sobre {title}.&lt;/p&gt;</dc:description>
    <dc:publisher>Editorial de Prueba</dc:publisher><dc:date>2001-04-01</dc:date>
    <meta name="cover" content="cover-img"/>
  </metadata>
  <manifest>
    <item id="cover-img" href="images/cover.png" media-type="image/png"/>
    <item id="c1" href="text/c1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="text/c2.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>"""
    # Sin declaración XML, con enlace a "#id-del-body" e <img> sin src: lo que rompe Send to Kindle.
    c1 = '<html xmlns="http://www.w3.org/1999/xhtml"><body id="inicio"><p>Hola</p><img alt="x"/><a href="c2.xhtml#fin">sigue</a></body></html>'
    c2 = '<html xmlns="http://www.w3.org/1999/xhtml"><body id="fin"><p>Adiós</p></body></html>'
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/text/c1.xhtml", c1)
        z.writestr("OEBPS/text/c2.xhtml", c2)
        z.writestr("OEBPS/images/cover.png", cover.getvalue())


class Seeder:
    def __init__(self, root: Path, folder: str = "Biblioteca"):
        self.port = free_port()
        self.session = lt.session(
            {
                "listen_interfaces": f"127.0.0.1:{self.port}",
                "enable_dht": False, "enable_lsd": False, "enable_upnp": False, "enable_natpmp": False,
                "allow_multiple_connections_per_ip": True,
            }
        )
        ct = lt.create_torrent(lt.list_files(str(root / folder)), 16384, lt.create_torrent.v1_only)
        lt.set_piece_hashes(ct, str(root))
        self.ti = lt.torrent_info(lt.bdecode(lt.bencode(ct.generate())))
        atp = lt.add_torrent_params()
        atp.ti = self.ti
        atp.save_path = str(root)
        atp.flags = (atp.flags | lt.torrent_flags.seed_mode) & ~(
            lt.torrent_flags.paused | lt.torrent_flags.auto_managed
        )
        self.handle = self.session.add_torrent(atp)
        deadline = time.time() + 10
        while time.time() < deadline:
            st = self.handle.status()
            if st.state == lt.torrent_status.seeding and not st.paused:
                break
            time.sleep(0.05)
        self.magnet = lt.make_magnet_uri(self.ti) + f"&x.pe=127.0.0.1:{self.port}"


class Inbox:
    def __init__(self):
        self.messages = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(email.message_from_bytes(envelope.content, policy=policy.default))
        return "250 OK"


def wait_for(fn, timeout=30, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(interval)
    raise AssertionError("Tiempo de espera agotado")


def login(client):
    r = client.post("/login", data={"password": "secreto", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and "biblioteca_session" in r.cookies


def wait_job(client, job_id):
    def done():
        job = client.get(f"/api/trabajos/{job_id}").json()
        return job if job["state"] in ("done", "error") else None

    return wait_for(done, timeout=60)


def wait_source_ready(client):
    def ready():
        status = client.get("/api/estado").json()
        sources = status["sources"]
        return status if sources and all(s["status"] in ("ready", "error") for s in sources) else None

    status = wait_for(ready)
    assert all(s["status"] == "ready" for s in status["sources"]), status
    return status
