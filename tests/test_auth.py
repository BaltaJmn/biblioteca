import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    "target, expected",
    [("/libro/3?x=1", "/libro/3?x=1"), ("//evil.example", "/"), ("/\\evil.example", "/"), ("https://evil.example", "/")],
)
def test_login_only_redirects_to_local_paths(data_dir, target, expected):
    from app.main import app

    with TestClient(app) as client:
        r = client.post("/login", data={"password": "secreto", "next": target}, follow_redirects=False)
        assert r.headers["location"] == expected


def test_protected_pages_redirect_to_login(data_dir):
    from app.main import app

    with TestClient(app) as client:
        r = client.get("/ajustes?msg=hola", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/login?next=%2Fajustes%3Fmsg%3Dhola"
        assert client.get("/healthz").status_code == 200
        assert client.get("/static/style.css").status_code == 200
