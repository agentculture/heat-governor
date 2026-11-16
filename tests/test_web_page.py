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
    assert "pulse" not in style
    assert "ripple" not in style


def test_reduced_motion_keeps_static_critical_glow():
    style = _style()
    m = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{(.*?)\}\s*\}", style, re.S)
    assert m, "reduced-motion rule missing"
    body = m.group(1)
    assert "animation:none" in body.replace(" ", "")
    assert ".critical" in body
    assert "box-shadow" in body


def test_single_band_label_and_states():
    assert "warm_c}–${" in PAGE  # one 'W–C°' label
    assert "not supported" in PAGE
    assert "unreachable" in PAGE
    assert "stroke-dasharray" in PAGE


def test_fact_hints_present():
    for k in ("admit", "containers", "gpu", "pair", "gpus", "fan", "power", "zones"):
        assert re.search(rf"\b{k}:\s*\"", PAGE), k


def test_memory_fact_hint_and_trend_columns():
    assert re.search(r"\bmemory:\s*\"In use and free", PAGE)
    assert "DDR temperature shows only where the board exposes it" in PAGE


def test_hints_are_short_and_name_no_board():
    """Hover hints stay one short sentence and never name a specific board."""
    import re

    block = PAGE[PAGE.index("HINT = {") : PAGE.index("};", PAGE.index("HINT = {"))]
    texts = re.findall(r':\s*"([^"]*)"', block)
    assert len(texts) >= 10
    for t in texts:
        assert len(t) <= 90, t
        for board in ("VENTUNO", "Jetson", "DGX", "Spark", "Thor", "Orin"):
            assert board not in t, t
    assert '<dt>Memory<${Info} k="memory" />' in PAGE
    assert "t.memory" in PAGE
    assert "m.temp_c" in PAGE  # DDR shown only when temp_c is present
    assert "s.mem_used_pct" in PAGE  # samples feed the trend
    assert "<span>Memory</span>" in PAGE  # tooltip row
    assert "<th>Memory</th>" in PAGE  # table column
    assert "http://" not in PAGE
    assert "https://" not in PAGE


def test_vendored_files_exist_with_licences():
    for name, lic in VENDORED.items():
        f = VENDOR / name
        assert f.is_file(), name
        assert f.stat().st_size > 0, name
        assert (VENDOR / lic).is_file(), lic
        assert (VENDOR / lic).stat().st_size > 0, lic
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


# -- d2: records history, 7d range, buffer note ------------------------------------


def test_trend_ranges_include_7d():
    m = re.search(r"const RANGES = \{(.*?)\};", PAGE)
    assert m
    keys = re.findall(r'"(\w+)": \{ s:', m.group(1))
    assert keys == ["15m", "1h", "6h", "24h", "7d"]
    assert '"7d": { s: 604800' in m.group(1)


def test_buffer_note_only_when_source_is_buffer():
    note = "From this session only: the machine's records were unreadable."
    assert PAGE.count(note) == 1
    assert 'source === "buffer"' in PAGE


def test_7d_axis_shows_dates():
    assert "const dayLabel" in PAGE
    assert 'rk === "7d" ? dayLabel' in PAGE


def test_gpu_fact_shows_clock_power_and_ceiling_only_when_set():
    """d3: the GPU fact is built from gpu_mhz / gpu_power_w / gpu_util_pct, shows the clock
    ceiling only when the helper reports one, and board power feeds the Power fact."""
    assert "gpu_mhz" in PAGE
    assert "gpu_power_w" in PAGE
    assert "gpu_util_pct" in PAGE
    assert "ceiling " in PAGE
    assert "String(b.gpu_clock)" not in PAGE  # the raw helper status is no longer shown


def test_fact_values_never_split_from_their_units():
    """A value and its unit are glued with a non-breaking space in the GPU, Memory and Power
    facts, so a narrow column wraps between items ("60 W"), never inside one ("60 / W")."""
    nb = "\\u00a0"
    for unit in ("°C", "MHz", "W", "GiB"):
        assert (nb + unit) in PAGE, unit
    gpu_block = PAGE[
        PAGE.index("const ceil =") : PAGE.index('.join(" · ") || "—";', PAGE.index("const ceil ="))
    ]
    assert '" MHz"' not in gpu_block
    assert '" W"' not in gpu_block
    assert '" °C"' not in gpu_block


# -- Boundaries tab (issue #18, PR 1: read-only) -------------------------------------

_TOKENS_SHA256 = "3fe97537de28755170a2c02a37da0be660c8f9d436a4cc505fb2bc521958b5b3"


def _script() -> str:
    return "\n".join(re.findall(r"<script>(.*?)</script>", PAGE, re.S))


def test_sections_nav_is_driven_by_the_hash():
    script = _script()
    assert 'aria-label="Sections"' in PAGE
    assert re.search(
        r'const TABS = \[\["machines", "Machines"\], \["boundaries", "Boundaries"\]\];', script
    )
    assert "location.hash" in script
    assert '"hashchange"' in script
    assert 'aria-current=${tab === k ? "page" : undefined}' in script
    # an empty or unknown hash shows Machines
    assert re.search(r'function readTab\(hash\) \{[^}]*: "machines"', script)
    # the Machines view is mounted exactly as before, from the one useFleet()
    assert "<${Machines} doc=${doc} err=${err} />" in script
    assert script.count("= useFleet()") == 1
    # the Boundaries button carries the rule count in the existing .pills .n style
    assert '<span class="n">${' in script


def test_boundaries_view_lists_guard_and_rules():
    script = _script()
    for text in ("Kills", "Last kill", "limits ", "free", "Rule", "Matches", "Boundary", "State"):
        assert text in script, text
    assert "Find a machine or rule" in script
    assert "mem_guard" in script
    assert "act_over_pct" in script
    assert "min_victim_mib" in script
    assert re.search(r'\banon:\s*"Anonymous memory', script)  # usage labelled as RssAnon


def test_usage_bar_marks_come_from_act_over_pct():
    script = _script()
    # the design's fixed 58.8% / 88.2% marks are computed, not copied
    assert "58.8" not in script
    assert "88.2" not in script
    assert re.search(r"function barMarks\(", script)
    assert re.search(r"function guardLabel\(", script)


def test_boundaries_styles_are_limited_and_tokens_unchanged():
    style = _style()
    root = re.search(r":root \{.*?\}", style, re.S).group(0)
    dark = re.search(r'html\[data-mode="dark"\] \{.*?\}', style, re.S).group(0)
    assert hashlib.sha256((root + "\n" + dark).encode()).hexdigest() == _TOKENS_SHA256
    assert ".ubar" in style
    assert "table.rules" in style
    # the design's own tokens, theme class and restyled duplicates are not copied
    for banned in (
        "#2f7fa6",
        "#a8661a",
        "#c4371f",
        "#c75a1f",
        "#8a8477",
        ".app.dark",
        ".badge",
        ".code",
    ):
        assert banned not in PAGE, banned
    assert "fonts.googleapis" not in PAGE


def test_without_a_token_the_boundaries_view_offers_no_write_controls():
    """PR 2: write controls appear only in the unlocked mode (writes enabled and a token)."""
    script = _script()
    assert 'mode === "unlocked" ? { gate: machineGate(row.b), go } : null' in script
    assert "Writes enabled" not in script


def test_csp_and_dependencies_unchanged():
    import tomllib

    from heat_governor import mesh_server

    assert mesh_server._CSP == (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'"
    )
    root = Path(__file__).resolve().parent.parent
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    assert project["dependencies"] == []


def test_rune_for_e():
    assert 'e:"ᛖ"' in _script()


_GUARD = {
    "action": "kill",
    "armed": True,
    "avail_mib": 41574,
    "last_kill": {"t": 1763187360, "pid": 4242, "comm": "python3", "rss_mib": 32460, "repeat": 1},
    "kills_last_hour": 1,
    "limits": {"floor_mib": 3072, "soft_mib": 6144, "source": "platform"},
    "act_over_pct": 50.0,
    "max_kills_per_hour": 3,
    "min_victim_mib": 2048,
    "boundaries": [
        {
            "name": "vllm-worker",
            "max_mib": 92160,
            "scope": "process",
            "usage_mib": 101800.0,
            "state": "over",
            "comm": [],
            "cmdline": "*vllm*worker*",
            "cgroup": "*docker-*.scope",
            "source": "config.toml",
        },
        {
            "name": "tiny",
            "max_mib": 1024,
            "scope": "total",
            "usage_mib": 0.0,
            "state": "idle",
            "comm": ["cc1plus", "ld"],
            "cmdline": None,
            "cgroup": None,
            "source": "config.toml",
        },
    ],
}


def _machine(name, state="ok", guard=_GUARD, thermal=True):
    t = None
    if thermal:
        t = {
            "level": "cool",
            "temp_c": 51.0,
            "platform": "spark",
            "version": "0.17.1",
            "memory": {"used_pct": 60.0, "avail_mib": 40000, "total_mib": 122000},
            "config_error": None,
            "mem_guard": guard,
        }
    return {
        "name": name,
        "transport": "ssh",
        "host": name,
        "state": state,
        "age_s": 200.0,
        "stale_after_s": 30,
        "health": {},
        "thermal": t,
    }


def _record(guard):
    return dict(guard, action="record")


def _older(guard):
    """A daemon one version older: boundaries without matchers/source, no thresholds."""
    keep = ("name", "max_mib", "scope", "usage_mib", "state")
    g = {
        k: v
        for k, v in guard.items()
        if k not in ("act_over_pct", "max_kills_per_hour", "min_victim_mib")
    }
    g["boundaries"] = [{k: b[k] for k in keep} for b in guard["boundaries"]]
    return g


FIXTURES = {
    "unreachable": _machine("thor", state="unreachable", thermal=False),
    "stale": _machine("orin", state="stale"),
    "no_guard": _machine("spark2", guard=None),
    "record": _machine("cultureos", guard=_record(_GUARD)),
    "no_rules": _machine("spark", guard=dict(_GUARD, boundaries=[])),
    "min_victim": _machine("elitemini"),
    "older": _machine("arduino", guard=_older(_GUARD)),
}

_NODE = r"""
const fs = require("fs"), vm = require("vm");
const [page, vendor, input] = process.argv.slice(2);
const { hash, expr, data } = JSON.parse(fs.readFileSync(input, "utf8"));
const src = fs.readFileSync(page, "utf8");
const script = [...src.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");
const noop = () => {};
// recording web storage fakes, so tests can see where the page keeps what
const mkStore = () => { const m = {}; return { _m: m, getItem: k => (k in m ? m[k] : null),
  setItem: (k, v) => { m[k] = String(v); }, removeItem: k => { delete m[k]; } }; };
const ctx = { console, setTimeout, clearTimeout, AbortController, location: { hash },
  history: { replaceState: noop },
  addEventListener: noop, removeEventListener: noop, fetch: () => new Promise(noop),
  localStorage: mkStore(), sessionStorage: mkStore(),
  document: { getElementById: () => ({}), documentElement: { dataset: {} } },
  ReactDOM: { createRoot: () => ({ render: noop }) } };
ctx.self = ctx; ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(vendor + "/react.production.min.js", "utf8"), ctx);
vm.runInContext(fs.readFileSync(vendor + "/htm.js", "utf8"), ctx);
vm.runInContext(script, ctx);
// a minimal static renderer: enough for hook-free components
vm.runInContext(`
  const __esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
  function renderHTML(n) {
    if (n == null || n === false || n === true) return "";
    if (Array.isArray(n)) return n.map(renderHTML).join("");
    if (typeof n !== "object") return __esc(n);
    const p = n.props || {};
    if (n.type === React.Fragment) return renderHTML(p.children);
    if (typeof n.type === "function") return renderHTML(n.type(p));
    let a = "";
    for (const [k, v] of Object.entries(p)) {
      if (k === "children" || k.startsWith("on") || v == null || v === false) continue;
      const val = k === "style" ? Object.entries(v).map(([x, y]) => x + ":" + y).join(";") : v;
      a += " " + (k === "className" ? "class" : k) + '="' + __esc(val) + '"';
    }
    return "<" + n.type + a + ">" + renderHTML(p.children) + "</" + n.type + ">";
  }
  // collapse ordinary whitespace only, so the page's no-break spaces survive
  const textOf = s => s.replace(/<[^>]*>/g, " ").replace(/[ \\n\\t]+/g, " ").trim();
`, ctx);
ctx.__data = data;
const out = vm.runInContext("(() => { const data = __data; return (" + expr + "); })()", ctx);
// an async expression is awaited, so request flows can run against a fake fetch
Promise.resolve(out).then(v => process.stdout.write(JSON.stringify(v === undefined ? null : v)),
  e => { process.stderr.write(String((e && e.stack) || e)); process.exit(1); });
"""


def _node(tmp_path, expr, data=None, hash_=""):
    import json
    import shutil
    import subprocess  # nosec B404 - runs the local node binary on the repo's own page

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = tmp_path / "harness.js"
    harness.write_text(_NODE)
    inp = tmp_path / "input.json"
    inp.write_text(json.dumps({"hash": hash_, "expr": expr, "data": data}))
    res = subprocess.run(  # nosec B603 - fixed argv, local files
        [node, str(harness), str(WEB / "index.html"), str(VENDOR), str(inp)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


@pytest.mark.parametrize("hash_", ["", "#", "#machines", "#bogus", "#boundaries"])
def test_read_tab_defaults_to_machines(tmp_path, hash_):
    want = "boundaries" if hash_ == "#boundaries" else "machines"
    assert _node(tmp_path, "readTab(location.hash)", hash_=hash_) == want


def test_bar_marks_follow_act_over_pct(tmp_path):
    got = _node(
        tmp_path,
        "[barMarks(50), barMarks(100), barMarks(undefined), barMarks(null)]"
        ".map(m => [Math.round(m.max * 10) / 10, Math.round(m.far * 10) / 10])",
    )
    assert got[0] == [58.8, 88.2]  # the design's marks, at act_over_pct 50
    assert got[1] == [45.5, 90.9]
    assert got[2] == got[0]  # absent -> 50, for the bar only
    assert got[3] == got[0]


def test_bar_fill_and_rule_classes(tmp_path):
    got = _node(
        tmp_path,
        "({ fill: [barFill({ usage_mib: 0, max_mib: 1024 }, barMarks(50)),"
        " Math.round(barFill({ usage_mib: 1024, max_mib: 1024 }, barMarks(50)) * 10) / 10,"
        " barFill({ usage_mib: 99999, max_mib: 1024 }, barMarks(50)),"
        " barFill({ usage_mib: 5, max_mib: 0 }, barMarks(50)), barFill({}, barMarks(50))],"
        " cls: ['within', 'over', 'far_over', 'idle', 'nonsense']"
        ".map(s => ruleClass(ruleState({ state: s }))),"
        " word: ['within', 'over', 'far_over', 'idle'].map(ruleWord) })",
    )
    assert got["fill"] == [0, 58.8, 100, 0, 0]
    assert got["cls"] == ["cool", "warm", "hot", "idle", "idle"]
    assert got["word"] == ["within", "over", "far over", "idle"]


def test_guard_label_from_action_and_armed(tmp_path):
    got = _node(
        tmp_path,
        "[guardLabel({ action: 'kill', armed: true }),"
        " guardLabel({ action: 'kill', armed: false }),"
        " guardLabel({ action: 'record', armed: true }), guardLabel(null)]",
    )
    assert got == ["armed · kill", "not armed · kill", "armed · record", "no memory guard"]


def test_counts_sizes_and_texts(tmp_path):
    got = _node(
        tmp_path,
        "({ counts: ruleCounts(data.map(toGuardRow)), total: ruleTotal(data.map(toGuardRow)),"
        " gib: [mibText(1024), mibText(92160), mibText(null)],"
        " kills: killsText(data[0].thermal.mem_guard), last: lastKillText(null),"
        " limits: limitsText({ floor_mib: null, soft_mib: null, source: 'percent' }) })",
        data=[FIXTURES["min_victim"], FIXTURES["record"], FIXTURES["no_guard"]],
    )
    assert got["counts"] == {"within": 0, "over": 2, "far_over": 0, "idle": 2}
    assert got["total"] == 4
    assert got["gib"] == ["1.0 GiB", "90.0 GiB", "—"]
    assert got["kills"] == "1 in the last hour, of 3 allowed"
    assert got["last"] == "none"
    assert "percent" in got["limits"]


def test_filter_by_state_and_search(tmp_path):
    got = _node(
        tmp_path,
        "(() => { const rows = data.map(toGuardRow);"
        " const n = r => r.map(x => x.b.name + ':' + x.rules.map(y => y.name).join(','));"
        " return { all: n(filterRows(rows, [], '')), over: n(filterRows(rows, ['over'], '')),"
        " byRule: n(filterRows(rows, [], 'TINY')),"
        " byMachine: n(filterRows(rows, [], 'spark2')) }; })()",
        data=[FIXTURES["min_victim"], FIXTURES["no_guard"]],
    )
    assert got["all"] == ["elitemini:vllm-worker,tiny", "spark2:"]
    assert got["over"] == ["elitemini:vllm-worker"]
    assert got["byRule"] == ["elitemini:tiny"]
    assert got["byMachine"] == ["spark2:"]


def _card_text(tmp_path, key):
    return _node(
        tmp_path,
        "textOf(renderHTML(html`<${BoundaryCard} row=${toGuardRow(data)} />`))",
        data=FIXTURES[key],
    )


def test_fixture_states_render_distinctly(tmp_path):
    texts = {k: _card_text(tmp_path, k) for k in FIXTURES}
    assert len(set(texts.values())) == len(texts)
    assert "unreachable" in texts["unreachable"]
    assert "no reading" in texts["unreachable"].lower()
    assert "stale" in texts["stale"]
    assert "last reading shown" in texts["stale"]
    assert "No memory guard" in texts["no_guard"]
    assert "older than 0.17" in texts["no_guard"]
    assert "armed · record" in texts["record"]
    assert "never kills" in texts["record"]
    assert "No boundaries" in texts["no_rules"]
    assert "judged by growth alone" in texts["no_rules"]
    # tiny (max 1 GiB) is under min_victim_mib (2 GiB): blamed, never killed
    assert "blamed, never killed" in texts["min_victim"]
    assert "blamed, never killed" not in texts["record"]  # record never kills anyway
    # the guard facts and each rule's parts
    full = texts["min_victim"]
    for part in (
        "armed · kill",
        "40.6 GiB free",
        "3.0 GiB",
        "6.0 GiB",
        "platform",
        "1 in the last hour",
        "python3",
        "vllm-worker",
        "cmdline *vllm*worker*",
        "cgroup *docker-*.scope",
        "comm cc1plus",
        "90.0 GiB",
        "per process",
        "total of all matches",
        "anonymous",
        "over",
        "idle",
        "config.toml",
    ):
        assert part in full, part


def test_older_daemon_renders_without_matchers_or_thresholds(tmp_path):
    text = _card_text(tmp_path, "older")
    assert "vllm-worker" in text
    assert "not reported" in text
    assert "blamed, never killed" not in text  # no min_victim_mib: nothing invented
    assert "config.toml" not in text  # no source: no tag


def test_bar_marks_rendered_from_the_machines_act_over_pct(tmp_path):
    g100 = dict(_GUARD, act_over_pct=100.0)
    out = _node(
        tmp_path,
        "renderHTML(html`<${BoundaryCard} row=${toGuardRow(data)} />`)",
        data=_machine("spark", guard=g100),
    )
    assert "left:45.45" in out
    assert "left:90.91" in out
    assert "58.8" not in out


class _GuardReader(_Reader):
    def snapshot(self) -> dict:
        return {"schema": 1, "reader": {}, "machines": [FIXTURES["min_victim"]]}


def test_server_serves_the_boundaries_page_and_mem_guard():
    import json

    srv = MeshServer(_GuardReader(), port=0)
    srv.start()
    try:
        host, port = srv.address[:2]
        base = f"http://{host}:{port}"
        with urllib.request.urlopen(base + "/", timeout=5) as r:  # nosec B310 - loopback test
            page = r.read().decode()
        assert 'aria-label="Sections"' in page
        assert "function BoundaryCard(" in page
        with urllib.request.urlopen(base + "/api/fleet", timeout=5) as r:  # nosec B310
            doc = json.loads(r.read())
        guard = doc["machines"][0]["thermal"]["mem_guard"]
        assert guard["boundaries"][0]["cmdline"] == "*vllm*worker*"
    finally:
        srv.stop()


def test_guard_badge_of_a_machine_that_is_not_live_is_last_seen_and_neutral(tmp_path):
    got = _node(
        tmp_path,
        "[guardLabel({ action: 'kill', armed: true }, false),"
        " guardClass({ action: 'kill', armed: true }, false),"
        " guardClass({ action: 'kill', armed: true }, true)]",
    )
    assert got == ["last seen: armed · kill", "idle", "cool"]
    assert "the guard may not be running" in PAGE


def test_kill_note_covers_total_scope_rules(tmp_path):
    g = "{ action: 'kill', min_victim_mib: 2048 }"
    got = _node(
        tmp_path,
        f"[killNote({g}, {{ max_mib: 8192, scope: 'total' }}) !== '',"
        f" killNote({g}, {{ max_mib: 8192, scope: 'process' }}),"
        f" killNote({g}, {{ max_mib: 1024, scope: 'process' }}) !== '',"
        " killNote({ action: 'record', min_victim_mib: 2048 }, { max_mib: 8192, scope: 'total' })]",
    )
    assert got == [True, "", True, ""]


def test_bar_marks_say_when_act_over_pct_is_assumed(tmp_path):
    got = _node(tmp_path, "[barMarks(100).known, barMarks(null).known, barMarks(null).pct]")
    assert got == [True, False, 50]
    assert "does not report act_over_pct" in PAGE


def test_nav_rule_count_and_tabs_keep_their_state(tmp_path):
    data = [
        {"thermal": {"mem_guard": {"boundaries": [{"name": "a"}, None, {"name": "b"}]}}},
        {"thermal": None},
        {"thermal": {"mem_guard": None}},
    ]
    assert _node(tmp_path, "fleetRuleTotal({ machines: data })", data) == 2
    # both views stay mounted (hidden, not unmounted), so filters and picks survive a tab switch
    assert 'hidden=${tab !== "machines"}' in PAGE
    assert 'hidden=${tab !== "boundaries"}' in PAGE
    # the tab change goes through the hash, so Back returns to the previous tab
    assert "location.hash = t" in PAGE
    assert "replaceState" not in PAGE


# -- Boundaries tab (issue #18, PR 2: the editor sub-view, token, modals) ---------------------

_LISTING = {
    "outcome": "ok",
    "rules": [
        {
            "name": "vllm-worker",
            "cmdline": "*vllm*worker*",
            "max_mib": 92160,
            "scope": "process",
            "source": "config.toml",
            "editable": False,
        },
        {
            "name": "node-tests",
            "comm": ["node"],
            "max_mib": 8192,
            "scope": "total",
            "source": "boundaries.toml",
            "editable": True,
        },
    ],
    "version": "3f9c2e1aa0",
    "exists": True,
    "error": None,
    "config_path": "/home/u/.config/heat-governor/config.toml",
    "boundaries_path": "/home/u/.config/heat-governor/boundaries.toml",
}


def _editor_block() -> str:
    script = _script()
    return script[script.index("// ---- Boundaries editor") : script.index("function App() {")]


def test_edit_routes_parse_and_build(tmp_path):
    got = _node(
        tmp_path,
        "({ r: ['#boundaries/elitemini/+new', '#boundaries/elitemini/vllm%20worker',"
        " '#boundaries/elitemini/%2Bnew', '#boundaries/elitemini/a%2Fb', '#boundaries/bad name/x',"
        " '#boundaries/elitemini/%E0%A4%A', '#boundaries/elitemini', '#boundaries/m/x/y',"
        " '#machines/elitemini/x', '#boundaries/elitemini/'].map(readRoute),"
        " paths: [editPath('thor', null), editPath('thor', '+new'), editPath('thor', 'a/b c')],"
        " trip: ['+new', 'a/b c', 'ü', '%x', 'x#y'].map(n => readRoute('#' + "
        "editPath('thor', n)).rule),"
        " tabs: ['#boundaries/elitemini/+new', '#machines/x'].map(readTab) })",
    )
    r = got["r"]
    assert r[0] == {"tab": "boundaries", "machine": "elitemini", "rule": None, "isNew": True}
    assert r[1] == {
        "tab": "boundaries",
        "machine": "elitemini",
        "rule": "vllm worker",
        "isNew": False,
    }
    assert r[2]["rule"] == "+new"  # a rule really named "+new" is not the new-rule route
    assert r[2]["isNew"] is False
    assert r[3]["rule"] == "a/b"
    for bad in r[4:]:
        assert bad["machine"] is None
        assert bad["isNew"] is False
    assert r[8]["tab"] == "machines"
    assert got["paths"] == [
        "boundaries/thor/+new",
        "boundaries/thor/%2Bnew",
        "boundaries/thor/a%2Fb%20c",
    ]
    assert got["trip"] == ["+new", "a/b c", "ü", "%x", "x#y"]
    assert got["tabs"] == ["boundaries", "machines"]


def test_editor_is_a_sub_view_of_the_boundaries_tab_with_a_back_link():
    script = _script()
    assert 'hidden=${tab !== "boundaries"}' in script
    assert "<div hidden=${!!route.machine}><${Boundaries}" in script
    assert "<${Editor} key=${editKey}" in script
    assert '<a class="back" href="#boundaries">' in script
    assert 'go("boundaries")' in script  # after a save and on Cancel


def test_form_to_rule_and_back(tmp_path):
    got = _node(
        tmp_path,
        "(() => { const f = formFromRule(data);"
        " const typed = { ...f, commDraft: ' vllm ', cmdline: '  ', cgroup: '*docker-*', "
        "max: '4096' };"
        " return { f, rule: ruleFromForm(typed), blank: ruleFromForm(blankForm()),"
        " added: addComm({ ...f, commDraft: 'node' }).comm, removed: removeComm(f, 'node').comm,"
        " rm: applyBody('rm', ruleFromForm(f), true), set: applyBody('set', { name: 'a' }, "
        "false) }; })()",
        data=_LISTING["rules"][1],
    )
    assert got["f"] == {
        "name": "node-tests",
        "comm": ["node"],
        "commDraft": "",
        "cmdline": "",
        "cgroup": "",
        "max": "8192",
        "scope": "total",
    }
    # comm is a list; a typed name not yet added still counts; empty matchers are left out
    assert got["rule"] == {
        "name": "node-tests",
        "scope": "total",
        "comm": ["node", "vllm"],
        "cgroup": "*docker-*",
        "max_mib": 4096,
    }
    assert got["blank"] == {"name": "", "scope": "process"}
    assert got["added"] == ["node"]  # no duplicate
    assert got["removed"] == []
    assert got["rm"] == {"operation": "rm", "rule": {"name": "node-tests"}, "allow_kill": True}
    assert got["set"] == {"operation": "set", "rule": {"name": "a"}, "allow_kill": False}


def test_requests_carry_the_token_only_in_the_authorization_header(tmp_path):
    got = _node(
        tmp_path,
        "(async () => { const seen = [];"
        " fetch = (url, init) => { seen.push({ url, init: { ...init, signal: !!init.signal } });"
        " return Promise.resolve({ status: 200, json: () => Promise.resolve({ outcome: 'ok' "
        "}) }); };"
        " const api = boundaryApi('thor', 's3cret-token');"
        " await api.list(); await api.preview({ name: 'a' });"
        " await api.apply(applyBody('add', { name: 'a' }, false), 'v1');"
        " return seen; })()",
    )
    assert [s["url"] for s in got] == [
        "/api/boundaries/thor",
        "/api/boundaries/thor/preview",
        "/api/boundaries/thor",
    ]
    for s in got:
        assert "s3cret" not in s["url"]
        assert "s3cret" not in s["init"].get("body", "")
        assert s["init"]["headers"]["Authorization"] == "Bearer s3cret-token"
        assert s["init"]["signal"] is True  # every request has a timeout
    assert got[0]["init"]["method"] == "GET"
    assert "If-Match" not in got[0]["init"]["headers"]
    assert got[1]["init"]["method"] == "POST"
    assert got[1]["init"]["headers"]["Content-Type"] == "application/json"
    assert got[2]["init"]["headers"]["If-Match"] == '"v1"'
    assert '"operation":"add"' in got[2]["init"]["body"]


def test_token_lives_in_session_storage_only(tmp_path):
    got = _node(
        tmp_path,
        "(() => { const before = tokenStore.get(); tokenStore.set('tok'); const mid = "
        "tokenStore.get();"
        " const kept = { ...sessionStorage._m }; tokenStore.clear();"
        " return { before, mid, kept, after: tokenStore.get(), local: { ...localStorage._m "
        "} }; })()",
    )
    assert got["before"] == ""
    assert got["mid"] == "tok"
    assert got["kept"] == {"hg-boundaries-token": "tok"}
    assert got["after"] == ""
    assert got["local"] == {}
    script = _script()
    # sessionStorage is touched only through tokenStore; localStorage only by the old store
    assert script.count("sessionStorage.") == 3
    assert len(re.findall(r"sessionStorage\.\w+Item\(TOKEN_KEY", script)) == 3
    assert script.count("localStorage.") == 2
    assert "TOKEN_KEY" not in script[: script.index("const TOKEN_KEY")]
    # the token travels in one place: the Authorization header; never in a URL
    assert script.count("Authorization") == 1
    assert '{ Authorization: "Bearer " + token }' in script
    for banned in ("token=", "?token", "#token", "encodeURIComponent(token", 'store.set("hg-token'):
        assert banned not in script, banned
    # a refused token is cleared and asked for again
    assert "const refuse = msg => { tokenStore.clear();" in script
    assert 'res.status === 401) return { kind: "unauthorized" }' in script


def test_token_field_is_a_password_input_and_disabled_writes_show_none(tmp_path):
    got = _node(
        tmp_path,
        "(() => { const w = { note: 'The token was refused. Enter it again.', probeErr: "
        "'boom', save: () => {}, forget: () => {} };"
        " const r = mode => renderHTML(html`<${WritesPanel} writes=${w} mode=${mode}"
        ' id="tok-x" />`);'
        " return { locked: r('locked'), disabled: r('disabled'), unknown: r('unknown'),"
        " checking: r('checking'), unlocked: r('unlocked') }; })()",
    )
    locked = got["locked"]
    assert 'type="password"' in locked
    assert 'autoComplete="off"' in locked
    assert 'htmlFor="tok-x"' in locked
    assert 'id="tok-x"' in locked
    assert "Write token" in locked
    assert "Enter it again" in locked
    assert "for this browser tab only" in locked
    for mode in ("disabled", "unknown", "checking"):
        assert "<input" not in got[mode], mode
    assert "not enabled on this server" in got["disabled"]
    assert "boom" in got["unknown"]
    assert "Forget the token" in got["unlocked"]
    assert "<input" not in got["unlocked"]


def test_writes_mode(tmp_path):
    got = _node(
        tmp_path,
        "[writesMode(null, '', ''), writesMode(null, 't', 'down'), writesMode(false, 't', ''),"
        " writesMode(true, '', ''), writesMode(true, 't', '')]",
    )
    assert got == ["checking", "unknown", "disabled", "locked", "unlocked"]


def test_validator_messages_map_to_their_fields(tmp_path):
    problems = [
        "memory.allow[0].name is required",
        "rule.name must be a non-empty string",
        "a rule named 'x' already exists; use set",
        "memory.allow[0].comm must be a non-empty list of process names",
        "memory.allow[0].cmdline must be a non-empty glob",
        "memory.allow[0].cgroup must be a non-empty glob",
        "memory.allow[0].max_mib must be at least 64",
        "memory.allow[0].scope must be one of process, total",
        "memory.allow[0] needs at least one of comm, cmdline, cgroup",
        "memory.allow[0] matches every process: give a matcher that is not a bare wildcard",
        "no rule named 'x' in boundaries.toml (it is read-only in config.toml)",
        "unknown rule key 'colour'",
    ]
    got = _node(tmp_path, "problemsByField(data)", data=problems)
    assert got["name"] == problems[0:3]
    assert got["comm"] == [problems[3]]
    assert got["cmdline"] == [problems[4]]
    assert got["cgroup"] == [problems[5]]
    assert got["max_mib"] == [problems[6]]
    assert got["scope"] == [problems[7]]
    assert got["matchers"] == problems[8:10]
    assert got["general"] == problems[10:]


def test_rule_form_labels_every_field_and_shows_messages_for_any_field(tmp_path):
    errs = {
        k: [f"{k} problem"]
        for k in ("name", "comm", "cmdline", "cgroup", "max_mib", "scope", "matchers", "general")
    }
    got = _node(
        tmp_path,
        "(() => { const f = { ...formFromRule(data.rule), commDraft: '' };"
        " const out = renderHTML(html`<${RuleForm} form=${f} upd=${() => {}} isNew=${false} "
        "errs=${data.errs}"
        " g=${data.g} machine=\"thor\" busy=${false} status=${{ state: 'not_saved', text: "
        "'Not saved: x.' }}"
        " onSave=${() => {}} onDelete=${() => {}} onCancel=${() => {}} />`);"
        " const fresh = renderHTML(html`<${RuleForm} form=${blankForm()} upd=${() => {}} "
        "isNew=${true} errs=${{}}"
        ' g=${null} machine="thor" busy=${true} status=${null} onSave=${() => {}}'
        " onDelete=${() => {}}"
        " onCancel=${() => {}} />`);"
        " return { out, text: textOf(out), fresh, freshText: textOf(fresh) }; })()",
        data={"rule": _LISTING["rules"][1], "errs": errs, "g": _GUARD},
    )
    out = got["out"]
    ids = re.findall(r'<(?:input|select)[^>]*\sid="([^"]+)"', out)
    assert sorted(ids) == ["f-cgroup", "f-cmdline", "f-comm", "f-max", "f-name", "f-scope"]
    for i in ids:
        assert f'htmlFor="{i}"' in out, i
    for k in errs:
        assert f"{k} problem" in got["text"], k
    assert out.count('aria-invalid="true"') == 6
    assert 'aria-describedby="f-max-h f-max-e"' in out
    assert 'min="64"' in out
    assert 'readOnly="true"' in out  # an existing rule keeps its name
    assert "Delete boundary" in got["text"]
    assert "Save boundary" in got["text"]
    assert 'role="status"' in out
    assert "Not saved: x." in got["text"]
    assert "Remove process name node" in out
    # far over at 8 GiB x 1.5 from this machine's act_over_pct
    assert "Far over above 12.0\u00a0GiB (act_over_pct 50 on thor)" in got["text"]
    assert 'value="total"' in out
    # a new rule: editable name, no delete, saving in progress
    assert 'readOnly="true"' not in got["fresh"]
    assert "Delete boundary" not in got["freshText"]
    assert "Saving…" in got["freshText"]
    assert 'disabled="true"' in got["fresh"]


_BUTTONS_NAMED = r"""
(s => [...s.matchAll(/<button([^>]*)>([\s\S]*?)<\/button>/g)]
  .filter(m => !/aria-label="[^"]+"/.test(m[1]) && !textOf(m[2]))
  .map(m => m[0]))
"""


def test_every_editor_button_has_an_accessible_name(tmp_path):
    got = _node(
        tmp_path,
        "(() => { const f = formFromRule(data.rule); const no = () => {};"
        " const pv = { kind: 'ok', data: { matching: 1, processes: [] } };"
        " const parts = [html`<${RuleForm} form=${f} upd=${no} isNew=${false} errs=${{}} g=${null}"
        ' machine="thor" busy=${false} status=${null} onSave=${no} onDelete=${no}'
        " onCancel=${no} />`,"
        ' html`<${DeleteBody} name="a" machine="thor" pv=${pv} onCancel=${no} onConfirm=${no} />`,'
        ' html`<${KillBody} procs=${[]} g=${null} machine="thor"'
        " onCancel=${no} onConfirm=${no} />`,"
        ' html`<${ConflictBody} machine="thor" base="a" now="b" onCancel=${no} onReload=${no} />`,'
        " html`<${BoundaryCard} row=${toGuardRow(data.m)} edit=${{ gate: '', go: no }} />`,"
        ' html`<${WritesPanel} writes=${{ forget: no }} mode="unlocked" id="t" />`];'
        " const out = parts.map(renderHTML).join('');"
        " return { unnamed: ("
        + _BUTTONS_NAMED
        + ")(out), n: (out.match(/<button/g) || []).length }; })()",
        data={"rule": _LISTING["rules"][1], "m": _machine("elitemini", guard=_FILE_GUARD)},
    )
    assert got["unnamed"] == []
    assert got["n"] >= 12


def test_editor_actions_are_real_controls_without_div_click_handlers():
    block = _editor_block()
    assert not re.search(r"<(?:div|span|section|p|li|tr|td|ul)\b[^>]*\bonClick=", block)
    assert not re.search(r"<(?:div|span|section|p|li|tr|td|ul)\b[^>]*\bonKeyDown=", block)
    assert 'role="button"' not in block
    for tag in ("<button", "<input", "<select", "<label"):
        assert tag in block, tag
    for asset in (
        "data:image",
        "data:font",
        "url(",
        "/vendor/",
    ):  # no new asset; stroke SVG icons only
        assert asset not in block, asset
    assert 'stroke="currentColor"' in block


def test_editor_code_has_no_nested_template_literals():
    """SonarCloud rejects a template literal inside another one's ${} in this file."""
    block = _editor_block()
    depth, i, found = [], 0, []
    while i < len(block):
        c = block[i]
        if depth and depth[-1] == "t":
            if c == "\\":
                i += 2
                continue
            if c == "`":
                depth.pop()
            elif block.startswith("${", i):
                depth.append(0)
                i += 1
        elif c in "\"'":
            j = i + 1
            while block[j] != c:
                j += 2 if block[j] == "\\" else 1
            i = j
        elif block.startswith("//", i):
            i = block.index("\n", i)
        elif c == "`":
            if depth:
                found.append(block[max(0, i - 60) : i + 20])
            depth.append("t")
        elif c == "{" and depth:
            depth[-1] += 1
        elif c == "}" and depth:
            if depth[-1] == 0:
                depth.pop()
            else:
                depth[-1] -= 1
        i += 1
    assert found == []


_FILE_GUARD = dict(
    _GUARD,
    boundaries=[
        dict(_GUARD["boundaries"][0]),
        {
            "name": "node-tests",
            "max_mib": 8192,
            "scope": "total",
            "usage_mib": 13400.0,
            "state": "over",
            "comm": ["node"],
            "cmdline": None,
            "cgroup": None,
            "source": "boundaries.toml",
        },
    ],
)


def _edit_machine(version="0.19.0", **kw):
    m = _machine("elitemini", guard=_FILE_GUARD, **kw)
    if m["thermal"]:
        m["thermal"]["version"] = version
    return m


def test_with_a_token_cards_offer_add_and_edit_and_config_rules_stay_read_only(tmp_path):
    got = _node(
        tmp_path,
        "(() => { const row = toGuardRow(data); const go = () => {};"
        " const r = edit => renderHTML(html`<${BoundaryCard} row=${row} edit=${edit} />`);"
        " return { on: r({ gate: '', go }), off: r(null),"
        " gate: machineGate(row.b) }; })()",
        data=_edit_machine(),
    )
    on = got["on"]
    assert got["gate"] == ""
    assert 'aria-label="Add boundary on elitemini"' in on
    assert 'aria-label="Edit node-tests on elitemini"' in on
    assert "Edit vllm-worker" not in on  # its source is config.toml
    assert "lives in config.toml, read-only here" in on
    assert "Actions" in on
    assert "Add boundary" not in got["off"]
    assert "Edit " not in got["off"]
    assert "Actions" not in got["off"]


@pytest.mark.parametrize(
    ("version", "state", "thermal", "want"),
    [
        ("0.19.0", "ok", True, ""),
        ("0.19.0.dev3", "ok", True, ""),
        ("1.0.0", "ok", True, ""),
        (
            "0.18.2",
            "ok",
            True,
            "it runs heat-governor 0.18.2; changing boundaries needs 0.19.0 or newer",
        ),
        ("", "ok", True, "does not report its version"),
        ("0.19.0", "unreachable", False, "the machine is unreachable"),
        ("0.19.0", "stale", True, "the machine's reading is stale"),
        ("0.19.0", "unsupported", True, "heat-governor can't read this machine"),
    ],
)
def test_machine_gate_names_why_a_machine_cannot_be_changed(
    tmp_path, version, state, thermal, want
):
    m = _edit_machine(version, state=state, thermal=thermal)
    got = _node(tmp_path, "machineGate(toBox(data))", data=m)
    if want:
        assert want in got
    else:
        assert got == ""
    gated = _node(
        tmp_path,
        "renderHTML(html`<${BoundaryCard} row=${toGuardRow(data)} edit=${{ gate: "
        "machineGate(toBox(data)), go: () => {} }} />`)",
        data=m,
    )
    assert ("Add boundary" in gated) == (not want)
    assert ("Read-only: " in gated) == bool(want)


def test_machine_not_in_the_fleet_is_gated(tmp_path):
    assert _node(tmp_path, "machineGate(null)") == "the machine is not in the fleet file"


def test_edit_state_comes_from_the_file_read_on_the_box(tmp_path):
    got = _node(
        tmp_path,
        "[editState(data, 'node-tests', false).mode, editState(data, 'vllm-worker', false).mode,"
        " editState(data, 'gone', false).mode, editState(data, null, true).mode,"
        " editState({ ...data, error: 'line 3: bad' }, 'node-tests', false).mode,"
        " editState(data, 'node-tests', false).rule.comm]",
        data=_LISTING,
    )
    assert got == ["edit", "readonly", "missing", "new", "rejected", ["node"]]
    script = _script()
    # the editor's rules and base version come from GET /api/boundaries/MACHINE, not /api/fleet
    assert "editState(listing.data, ruleName, isNew)" in script
    assert "version: data.version" in script


def test_rejected_file_shows_its_message_and_the_last_good_rules_as_live(tmp_path):
    text = _node(
        tmp_path,
        'textOf(renderHTML(html`<${RejectedView} machine="elitemini" error="line 3: expected \']\'"'
        " row=${toGuardRow(data)} />`))",
        data=_edit_machine(),
    )
    assert "boundaries.toml on elitemini is rejected: line 3: expected ']'" in text
    assert "Editing is disabled" in text
    assert "Last good rules, still live" in text
    assert "node-tests" in text
    assert "vllm-worker" in text
    assert "Edit " not in text


def test_config_rule_opened_by_hash_is_read_only(tmp_path):
    text = _node(
        tmp_path,
        'textOf(renderHTML(html`<${ReadOnlyRule} machine="elitemini" rule=${data} />`))',
        data=_LISTING["rules"][0],
    )
    assert "lives in config.toml on elitemini" in text
    assert "*vllm*worker*" in text
    assert "90.0\u00a0GiB" in text


def test_save_outcomes_from_each_answer(tmp_path):
    got = _node(
        tmp_path,
        "data.map(([status, d, lost]) => saveOutcome({ status, data: d, lost }).kind)",
        data=[
            [200, {"outcome": "saved", "version": "v2"}, False],
            [409, {"outcome": "conflict", "version": "v3"}, False],
            [409, {"outcome": "would_kill", "processes": [{"pid": 1}]}, False],
            [400, {"outcome": "invalid", "problems": ["x"]}, False],
            [400, {"outcome": "invalid", "error": "If-Match must be one version"}, False],
            [401, {"outcome": "unauthorized", "error": "a valid bearer token is required"}, False],
            [502, {"outcome": "not_saved", "message": "lost; the file is unchanged"}, False],
            [422, {"outcome": "version_too_old", "message": "needs 0.19.0"}, False],
            [403, {"outcome": "writes_disabled", "error": "not enabled"}, False],
            [504, {"outcome": "unknown", "message": "may or may not"}, False],
            [0, {"error": "no answer in 70 s"}, True],
        ],
    )
    assert got == [
        "saved",
        "conflict",
        "would_kill",
        "invalid",
        "invalid",
        "unauthorized",
        "not_saved",
        "not_saved",
        "not_saved",
        "lost",
        "lost",
    ]


_RUN_SAVE = r"""
(async () => {
  const rule = { name: "node-tests", comm: ["node"], max_mib: 4096, scope: "total" };
  const req = { op: "set", rule, version: "v1", allowKill: false };
  const listing = (version, rules) => ({ status: 200, data: { outcome: "ok", version, error: null,
    rules: rules.map(r => ({ ...r, source: "boundaries.toml", editable: true })) } });
  const lost = { status: 0, lost: true, data: { error: "no answer in 70 s" } };
  const run = async (applyRes, listRes, r) => {
    let lists = 0;
    const api = { apply: async () => applyRes, list: async () => { lists++; return listRes; } };
    const out = await runSave(api, r || req);
    return { kind: out.kind, state: finalStatus(out).state, text: finalStatus(out).text, lists,
      recovered: !!out.recovered };
  };
  return {
    landed: await run(lost, listing("v2", [rule])),
    unchanged: await run(lost, listing("v1", [{ ...rule, max_mib: 8192 }])),
    other: await run(lost, listing("v2", [{ ...rule, max_mib: 9999 }])),
    gateway: await run({ status: 504, data: { outcome: "unknown" } }, listing("v2", [rule])),
    rmLanded: await run(lost, listing("v2", []), { ...req, op: "rm" }),
    unreadable: await run(lost, { status: 0, lost: true, data: { error: "down" } }),
    saved: await run({ status: 200, data: { outcome: "saved", version: "v2" } }, null),
    conflict: await run({ status: 409, data: { outcome: "conflict", version: "v9" } }, null),
    refused: await run({ status: 401, data: { outcome: "unauthorized" } }, null),
    invalid: await run({ status: 400, data: { outcome: "invalid", problems: ["x"] } }, null),
    failed: await run({ status: 502,
      data: { outcome: "machine_unreachable", message: "ssh: timed out" } }, null),
  };
})()
"""


def test_a_lost_or_timed_out_save_is_decided_from_a_re_read(tmp_path):
    got = _node(tmp_path, _RUN_SAVE)
    # timeout / network error / 504: the list is read again and decides
    assert got["landed"]["kind"] == "saved"
    assert got["landed"]["recovered"] is True
    assert got["landed"]["lists"] == 1
    assert got["unchanged"]["state"] == "not_saved"
    assert "unchanged" in got["unchanged"]["text"]
    assert got["other"]["state"] == "conflict"
    assert got["gateway"]["state"] == "saved"
    assert got["rmLanded"]["state"] == "saved"
    assert got["unreadable"]["state"] == "not_saved"
    assert "may still have landed" in got["unreadable"]["text"]
    # an answered request is not re-read
    for k in ("saved", "conflict", "refused", "invalid", "failed"):
        assert got[k]["lists"] == 0, k
    assert got["saved"]["state"] == "saved"
    assert got["conflict"]["state"] == "conflict"
    assert "token was refused" in got["refused"]["text"]
    assert "ssh: timed out" in got["failed"]["text"]
    # every attempt ends in exactly one of the three states
    for k, v in got.items():
        assert v["state"] in ("saved", "not_saved", "conflict"), k


def test_a_request_that_gets_no_answer_times_out_as_lost(tmp_path):
    got = _node(
        tmp_path,
        "(async () => { fetch = (url, init) => new Promise((_, no) =>"
        " init.signal.addEventListener('abort', () => no(new Error('aborted'))));"
        " const timed = await call('/api/boundaries/thor', requestInit('t'), 20);"
        " fetch = () => Promise.reject(new TypeError('Failed to fetch'));"
        " const down = await call('/api/boundaries/thor', requestInit('t'), 1000);"
        " return { timed, down, kinds: [saveOutcome(timed).kind, saveOutcome(down).kind] }; })()",
    )
    assert got["timed"] == {"status": 0, "lost": True, "data": {"error": "no answer in 0 s"}}
    assert got["down"]["data"]["error"] == "Failed to fetch"
    assert got["kinds"] == ["lost", "lost"]
    script = _script()
    assert "SAVE_TIMEOUT_MS = 70000" in script


def test_kill_warning_states_the_real_conditions(tmp_path):
    got = _node(
        tmp_path,
        "({ g: killConditions(data), none: killConditions(null), when: KILL_WHEN,"
        " budget: killBudget(data, 'elitemini') })",
        data=_GUARD,
    )
    g = " | ".join(got["g"])
    for part in (
        "daemon's own user",
        "protect list",
        "at least 2.0\u00a0GiB of anonymous memory itself (min_victim_mib)",
        "action is kill (here it is kill)",
        "more than 50% over its boundary (act_over_pct)",
    ):
        assert part in g, part
    none = " | ".join(got["none"])
    assert "at least min_victim_mib" in none
    assert "act_over_pct percent" in none
    assert "12 s" in got["when"]
    assert got["budget"] == "A kill counts against the budget of 3 per hour on elitemini (1 used)."


def test_modal_frame_is_a_labelled_modal_dialog(tmp_path):
    out = _node(
        tmp_path,
        'renderHTML(html`<${ModalFrame} id="dlg-x" title="Delete a on thor?" icon="bin" '
        'tone="danger">'
        "<p>body</p></${ModalFrame}>`)",
    )
    assert 'role="dialog"' in out
    assert 'aria-modal="true"' in out
    assert 'aria-labelledby="dlg-x-t"' in out
    assert '<h2 id="dlg-x-t">Delete a on thor?</h2>' in out
    assert 'tabIndex="-1"' in out


def test_modals_move_focus_trap_tab_and_close_on_escape(tmp_path):
    got = _node(
        tmp_path,
        "[trapIndex(3, 2, false), trapIndex(3, 0, true), trapIndex(3, 1, false), "
        "trapIndex(3, 1, true),"
        " trapIndex(3, -1, false), trapIndex(3, -1, true), trapIndex(0, 0, false)]",
    )
    assert got == [0, 2, -1, -1, 0, 2, -1]
    block = _editor_block()
    focus = block[block.index("function useModalFocus") : block.index("function ModalFrame")]
    assert "const prev = document.activeElement;" in focus
    assert "first.focus()" in focus
    assert "prev.focus()" in focus  # restored on close
    assert 'document.addEventListener("keydown", on, true)' in focus
    key = block[block.index("function modalKey") : block.index("function useModalFocus")]
    assert 'e.key === "Escape"' in key
    assert 'e.key !== "Tab"' in key
    # delete, a would-kill change and a conflict each open a modal
    for kind in ('m.kind === "delete"', 'm.kind === "kill"', 'id="dlg-conflict"'):
        assert kind in block, kind
    assert "s.attempt(m.req.op, m.req.rule, true)" in block  # re-sent with allow_kill on confirm
    assert "ctx.onReload()" in block


def test_modal_bodies_say_what_happens(tmp_path):
    procs = [
        {
            "pid": 2231,
            "uid": 1000,
            "user": "ori",
            "comm": "node",
            "cmdline": "node --test js/test/search-panel.test.js",
            "usage_mib": 13414.4,
            "far_over_killable": True,
        }
    ]
    got = _node(
        tmp_path,
        "(() => { const no = () => {};"
        " const t = n => textOf(renderHTML(n));"
        ' return { kill: t(html`<${KillBody} procs=${data.procs} g=${data.g} machine="elitemini"'
        " onCancel=${no} onConfirm=${no} />`),"
        ' del: t(html`<${DeleteBody} name="node-tests" machine="elitemini"'
        ' path="/b/boundaries.toml"'
        " pv=${{ kind: 'ok', data: { matching: 3 } }} onCancel=${no} onConfirm=${no} />`),"
        ' conflict: t(html`<${ConflictBody} machine="thor" base="3f9c2e1aa"'
        ' now="8b04d7a00" isNew=${false} onCancel=${no} onReload=${no} />`),'
        ' fresh: t(html`<${ConflictBody} machine="thor" base="missing"'
        ' now="8b04d7a00" isNew=${true} onCancel=${no} onReload=${no} />`) }; })()',
        data={"procs": procs, "g": _GUARD},
    )
    kill = got["kill"]
    assert "One running process would be far over" in kill
    assert "pid 2231" in kill
    assert "node --test js/test/search-panel.test.js" in kill
    assert "13.1\u00a0GiB" in kill
    assert "protect list" in kill
    assert "12 s" in kill
    assert "Save and allow the kill" in kill
    assert "Back to the rule" in kill
    assert "budget of 3 per hour" in kill
    assert "It matches 3 running processes now." in got["del"]
    assert "/b/boundaries.toml" in got["del"]
    assert "kept as a backup" in got["del"]
    assert "Keep it" in got["del"]
    assert "You started from 3f9c2e1 · thor now has 8b04d7a." in got["conflict"]
    assert "your edits are discarded" in got["conflict"]
    assert "Reload the current rules" in got["conflict"]
    assert "no file yet" in got["fresh"]
    assert "keeps your form" in got["fresh"]


def test_preview_panel_lists_matches_and_whether_the_guard_could_kill(tmp_path):
    procs = [
        {
            "pid": 41822,
            "uid": 0,
            "user": "root",
            "comm": "python3",
            "cmdline": "python3 -m vllm worker",
            "container": "a3f9c1e07b22",
            "rss_anon_mib": 101800.0,
            "usage_mib": 101800.0,
            "state": "far_over",
            "killable": False,
            "reason": "uid 0",
            "far_over_killable": False,
        },
        {
            "pid": 5150,
            "uid": 1000,
            "user": "ori",
            "comm": "node",
            "cmdline": "node --test",
            "container": None,
            "rss_anon_mib": 13414.4,
            "usage_mib": 13414.4,
            "state": "far_over",
            "killable": True,
            "reason": None,
            "far_over_killable": True,
        },
        {
            "pid": 7,
            "uid": 1000,
            "user": "ori",
            "comm": "node",
            "cmdline": "node",
            "container": None,
            "rss_anon_mib": 100.0,
            "usage_mib": 100.0,
            "state": "within",
            "killable": False,
            "reason": "below min_victim_mib 2048",
            "far_over_killable": False,
        },
    ]
    ok = {
        "kind": "ok",
        "data": {
            "outcome": "ok",
            "action": "kill",
            "matching": 3,
            "far_over_killable": 1,
            "guard_configured": True,
            "processes": procs,
        },
    }
    got = _node(
        tmp_path,
        "(() => { const r = (pv, scanning) => textOf(renderHTML(html`<${PreviewPanel}"
        ' machine="thor" pv=${pv}'
        " scanning=${scanning} rule=${{ max_mib: 4096, scope: 'process' }} g=${data.g} />`));"
        " return { ok: r(data.ok, false), busy: r(data.ok, true), bad: r({ kind: 'invalid', "
        "problems: ['x'] }, false),"
        " none: r(null, false), err: r({ kind: 'error', text: 'busy, retry shortly' }, "
        "false) }; })()",
        data={"ok": ok, "g": _GUARD},
    )
    ok_text = got["ok"]
    assert (
        "3 processes match; each is judged on its own against 4.0\u00a0GiB. 1 would be killed."
        in ok_text
    )
    assert "Live scan on thor" in ok_text
    assert "never killed: uid 0" in ok_text
    assert "the guard would kill it" in ok_text
    assert "never killed: below min_victim_mib 2048" in ok_text
    assert "far over" in ok_text
    assert "a3f9c1e07b22" in ok_text
    assert "Guard action: kill" in ok_text
    assert "Scanning thor" in got["busy"]
    assert "Fix the messages beside the form" in got["bad"]
    assert "Change the rule to see" in got["none"]
    assert "busy, retry shortly" in got["err"]
    for k in got:
        assert "only when all of these hold" in got[k], k  # the kill conditions beside the preview


def test_list_and_preview_answers(tmp_path):
    got = _node(
        tmp_path,
        "({ list: [listState({ status: 200, data: data }).kind, listState({ status: 401, "
        "data: {} }).kind,"
        " listState({ status: 422, data: { outcome: 'version_too_old', message: 'needs "
        "0.19.0' } }),"
        " listState({ status: 502, data: { outcome: 'machine_unreachable', message: 'ssh "
        "down' } }).text],"
        " pv: [previewState({ status: 400, data: { outcome: 'invalid', problems: ['p'] } }),"
        " previewState({ status: 401, data: {} }).kind, previewState({ status: 503, data: { "
        "error: 'busy' } })] })",
        data=_LISTING,
    )
    assert got["list"][:2] == ["ok", "unauthorized"]
    assert got["list"][2] == {"kind": "error", "text": "needs 0.19.0"}
    assert got["list"][3] == "ssh down"
    assert got["pv"][0] == {"kind": "invalid", "problems": ["p"]}
    assert got["pv"][1] == "unauthorized"
    assert got["pv"][2] == {"kind": "error", "text": "busy"}


def test_saved_text_and_editor_new_styles(tmp_path):
    got = _node(
        tmp_path,
        "[savedText('set', 'a', 'thor', { kind: 'saved', data: { forced: false } }),"
        " savedText('rm', 'a', 'thor', { kind: 'saved', recovered: true, data: {} })]",
    )
    assert got[0].startswith("Saved a on thor.")
    assert got[1].startswith("Deleted a on thor (confirmed by re-reading")
    style = _style()
    for sel in (
        ".btn",
        ".btn.primary",
        ".btn.danger",
        ".ibtn",
        ".field",
        ".input",
        ".modal",
        ".modal-scrim",
    ):
        assert sel + " " in style or sel + "{" in style, sel
    block = style[style.index("/* boundaries editor") :]
    # new rules use the page's variables only: no new colour beyond white on the danger button
    assert re.findall(r"#[0-9a-fA-F]{3,6}\b", block) == ["#fff"]
