from fastapi.testclient import TestClient


def test_requests_without_cloudflare_access_are_rejected(data_dir, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_ACCESS_REQUIRED", "true")
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 403 and "Cloudflare Access" in r.text
        assert client.post("/ajustes/fuentes", data={"magnet": "x"}).status_code == 403
        assert client.get("/healthz").status_code == 200  # healthcheck de Docker
        ok = client.get("/", headers={"Cf-Access-Authenticated-User-Email": "ana@example.com"})
        assert ok.status_code == 200
        assert client.get("/", headers={"Cf-Access-Jwt-Assertion": "token"}).status_code == 200
