import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_ui_is_served_with_strict_csp(client):
    r = client.get("/")
    assert r.status_code == 200 and "Lookout" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/app.js").status_code == 200 and client.get("/style.css").status_code == 200


def test_unknown_api_paths_still_return_the_json_error(client):
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"


def test_no_inline_script_or_style_that_the_csp_would_block():
    html = (STATIC / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)", html) and "style=" not in html and " onclick=" not in html


def test_js_never_writes_html(client):
    js = (STATIC / "app.js").read_text()
    assert "innerHTML" not in js and "outerHTML" not in js and "insertAdjacentHTML" not in js
