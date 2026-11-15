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


def test_boundaries_tab_is_read_only():
    script = _script()
    for banned in ("Add boundary", "Writes enabled", "token", "Delete", "Edit ", "POST", "method:"):
        assert banned not in script, banned


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
const ctx = { console, setTimeout, clearTimeout, location: { hash },
  history: { replaceState: noop },
  addEventListener: noop, removeEventListener: noop, fetch: () => new Promise(noop),
  localStorage: { getItem: () => null, setItem: noop },
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
process.stdout.write(JSON.stringify(out === undefined ? null : out));
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
