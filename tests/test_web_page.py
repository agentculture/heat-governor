"""Static and end-to-end checks for the mesh dashboard page (heat_governor/web)."""

from __future__ import annotations

import hashlib
import re
import urllib.request
from pathlib import Path

import pytest

from heat_governor.mesh_server import MeshServer

WEB = Path(__file__).resolve().parent.parent / "heat_governor" / "web"
VENDOR = WEB / "vendor"
PAGE = (WEB / "index.html").read_text(encoding="utf-8")

# vendored file -> the licence file that sits beside it
VENDORED = {
    "react.production.min.js": "react-LICENSE.txt",
    "react-dom.production.min.js": "react-LICENSE.txt",
    "htm.js": "htm-LICENSE.txt",
    "lato-latin-300.woff2": "lato-OFL.txt",
    "lato-latin-400.woff2": "lato-OFL.txt",
    "lato-latin-700.woff2": "lato-OFL.txt",
    "lato-latin-900.woff2": "lato-OFL.txt",
}


def _style() -> str:
    return "\n".join(re.findall(r"<style>(.*?)</style>", PAGE, re.S))


def test_title_is_heat_governor():
    assert "<title>Heat Governor</title>" in PAGE
    assert "<h1>Heat Governor</h1>" in PAGE


def test_page_never_names_another_host():
    assert "http://" not in PAGE
    assert "https://" not in PAGE
    assert "fonts.googleapis" not in PAGE
    assert "//" + "cdn" not in PAGE


@pytest.mark.parametrize(
    "banned",
    [
        "Example history",
        "governs",
        "Geothermal",
        "Kiln",
        "Aurora",
        'data-skin="geo"',
        "SKINS",
        "Survey",
        "Decisions",
        "const DATA",
        "EXAMPLE",
        "MIXED",
        "invented",
        "rng(",
    ],
)
def test_design_leftovers_are_gone(banned):
    assert banned not in PAGE


def test_references_vendored_scripts_and_fonts():
    for name in VENDORED:
        assert f"/vendor/{name}" in PAGE, name
    for src in re.findall(r'<script[^>]*\ssrc="([^"]+)"', PAGE):
        assert src.startswith("/vendor/"), src


def test_lato_for_every_role():
    style = _style()
    assert style.count("@font-face") == 4
    assert re.search(r"--font-body:\s*\"Lato\"", style)


def test_reads_the_live_api():
    assert "/api/fleet" in PAGE
    assert "/api/history" in PAGE


def test_polar_night_tokens_both_modes():
    style = _style()
    assert "--bg:#f5f0e6" in style
    assert "--ink:#38362f" in style
    assert '[data-mode="dark"]' in style
    assert "--surface:#151922" in style


def test_appearance_override_auto_light_dark():
    for label in ('"Auto"', '"Light"', '"Dark"'):
        assert label in PAGE
    assert "prefers-color-scheme: dark" in PAGE
    assert "localStorage" in PAGE


def test_only_critical_rows_and_tiles_animate():
    style = _style()
    keyframes = re.findall(r"@keyframes\s+([\w-]+)", style)
    infinite = re.findall(r"([^{}]+)\{[^{}]*animation:\s*([\w-]+)[^;{}]*infinite", style)
    # the only looping animation is the aura, on critical lanes and tiles
    assert [name for _, name in infinite] == ["aura"]
    selectors = [s.strip() for s in infinite[0][0].split(",")]
    assert sorted(selectors) == [".lane.critical", ".tile.critical"]
    assert "aura" in keyframes
    # one-shot entrance animations (drawer, scrim) are the only other keyframes
    assert set(keyframes) <= {"aura", "slide", "fade"}
    assert "pulse" not in style and "ripple" not in style


def test_reduced_motion_keeps_static_critical_glow():
    style = _style()
    m = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\}\s*\}", style, re.S)
    assert m, "reduced-motion rule missing"
    body = m.group(1)
    assert "animation:none" in body.replace(" ", "")
    assert ".critical" in body and "box-shadow" in body


def test_single_band_label_and_states():
    assert "warm_c}–${" in PAGE  # one 'W–C°' label
    assert "not supported" in PAGE
    assert "unreachable" in PAGE
    assert "stroke-dasharray" in PAGE


def test_fact_hints_present():
    for k in ("admit", "containers", "gpu", "pair", "gpus", "fan", "power", "zones"):
        assert re.search(rf"\b{k}:\s*\"", PAGE), k


def test_vendored_files_exist_with_licences():
    for name, lic in VENDORED.items():
        f = VENDOR / name
        assert f.is_file() and f.stat().st_size > 0, name
        assert (VENDOR / lic).is_file() and (VENDOR / lic).stat().st_size > 0, lic
    assert "Apache License" in (VENDOR / "htm-LICENSE.txt").read_text()
    assert "MIT License" in (VENDOR / "react-LICENSE.txt").read_text()
    assert "SIL OPEN FONT LICENSE" in (VENDOR / "lato-OFL.txt").read_text().upper()


def test_vendor_readme_sha256_match():
    readme = (VENDOR / "README.txt").read_text()
    entries = re.findall(r"file: (\S+)\n(?:.*\n)*?sha256: ([0-9a-f]{64})", readme)
    listed = dict(entries)
    files = {p.name for p in VENDOR.iterdir() if p.name != "README.txt"}
    assert set(listed) == files
    for name, digest in listed.items():
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name


class _Reader:
    def snapshot(self) -> dict:
        return {"schema": 1, "reader": {}, "machines": []}

    def history(self, name: str, since_s: int = 3600, max_points: int = 300) -> list:
        raise KeyError(name)


def test_server_serves_page_and_every_referenced_vendor_file():
    srv = MeshServer(_Reader(), port=0)
    srv.start()
    try:
        host, port = srv.address[:2]
        base = f"http://{host}:{port}"
        with urllib.request.urlopen(base + "/", timeout=5) as r:  # nosec B310 - loopback test
            assert r.status == 200
            assert r.headers["Content-Type"].startswith("text/html")
            page = r.read().decode()
        refs = sorted(set(re.findall(r"/vendor/([A-Za-z0-9._-]+)", page)))
        assert set(VENDORED) <= set(refs)
        types = {".js": "application/javascript", ".woff2": "font/woff2", ".txt": "text/plain"}
        for name in refs + sorted(set(VENDORED.values())):
            with urllib.request.urlopen(f"{base}/vendor/{name}", timeout=5) as r:  # nosec B310
                assert r.status == 200, name
                ext = "." + name.rsplit(".", 1)[-1]
                assert r.headers["Content-Type"].startswith(types[ext]), name
                assert r.read() == (VENDOR / name).read_bytes()
    finally:
        srv.stop()
