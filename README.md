# heat-governor

Keeps machines running heavy AI work without thermal resets, while losing as
little throughput as possible. One daemon per host senses the thermal zones
about once a second, tells clients when to hold new work, duty-cycles and
CPU-caps the containers that opt in, and records each run so the next one is
tuned better.

It was built after a DGX Spark hard-reset twice in one day under long-context
LLM decode: the hottest ACPI zone reached ~95 C while the GPU sat at ~80 C,
and the zone moved up to 5 C in two seconds. See
[`docs/HANDOFF-2026-10-23.md`](docs/HANDOFF-2026-10-23.md).

`heat-governor` is also an [AgentCulture](https://github.com/agentculture)
mesh agent with an agent-first CLI. The runtime package has no third-party
dependencies.

## Quickstart

```bash
uv tool install heat-governor     # or, from a checkout: uv sync && uv run heat-governor ...
heat-governor capabilities        # what this host lets us read and change
heat-governor sense               # one reading of every zone
heat-governor run --once          # one dry-run tick: what it would do right now
heat-governor run                 # the daemon, signalling and recording only
heat-governor run --apply         # ...and governing the containers that opted in
```

Install it as a user service:

```bash
heat-governor unit -- --apply > $XDG_CONFIG_HOME/systemd/user/heat-governor.service
systemctl --user daemon-reload && systemctl --user enable --now heat-governor
loginctl enable-linger "$USER"
```

## How it governs

| Level | When (effective temperature) | New work | Batch containers |
|---|---|---|---|
| cool | below warm | admitted, full concurrency | running |
| warm | warm to hot | admitted, half concurrency | paused for a growing share of each 2 s period |
| hot | hot to critical | held | paused for most of each period |
| critical | at or above critical | held | paused |

Two inputs feed it: the hottest board zone, and the GPU's own temperature with
its own bands (DGX Spark: 82 / 87 / 91 C, under the ~94 C limit the GB10
reports as headroom). Whichever is closer to its own limit governs. On the
Jetsons the GPU zone is already one of the watched board zones.

The effective temperature adds a short lookahead on a rising temperature.
Levels rise at once and fall only after the temperature has stayed below the
threshold (minus a hysteresis) for a dwell time. CPU caps drop at once and
rise one band at a time. If every sensor is lost the host is treated as hot.

| Platform | warm / hot / critical | Source |
|---|---|---|
| DGX Spark (GB10) | 88 / 91.5 / 93 C on the ACPI zones | measured: resets at ~94.5-95 C |
| Jetson AGX Thor | 95 / 100 / 104 C | declared from the 109 C passive trip; unmeasured |
| Jetson AGX Orin | 85 / 90 / 94 C | declared from the 99 C passive trip; unmeasured |
| anything else | from the lowest critical trip minus 15 C | derived |

Containers opt in by label: `heat-governor.role=batch` (duty-cycled and
CPU-capped) or `heat-governor.role=serving` (never paused; CPU-capped only
with `heat-governor.cpu-cap=true`). Unlabelled containers are left alone.
The daemon only ever undoes a pause it made itself.

With the root helper installed and `gpu_clock = true`, it also lowers the GPU
clock ceiling per band (`nvidia-smi -lgc` on the Spark, devfreq `max_freq` on
the Jetsons). It does not touch power modes or fans.

## Configure and tune

Everything above is a setting in one TOML file that a running daemon re-reads
within about 2 s. A bad edit is rejected and the last good settings stay.

```bash
heat-governor config init --write     # $XDG_CONFIG_HOME/heat-governor/config.toml from the built-in profile
$EDITOR $XDG_CONFIG_HOME/heat-governor/config.toml
heat-governor config check            # every problem at once; unknown keys are errors
heat-governor config show             # what is in force
heat-governor gpu-clock helper        # the root helper + sudoers line for GPU clocks
```

## For other tools

- Label your containers, or read the signal before starting new work:
  `heat-governor wait --timeout 1800 && next-job`, or in Python
  `heat_governor.client.wait()` / `client.Gate(4).slot()`.
- The JSON contract: [`docs/contracts/state.md`](docs/contracts/state.md).
- The guide for lobes, sparkrun, mjolnir and scripts:
  [`docs/integration.md`](docs/integration.md).

## CLI

| Verb | What it does |
|------|--------------|
| `sense` | Read every zone, fan and the GPU once. |
| `capabilities` | What this host lets heat-governor read and change, and the active profile. |
| `run` | The daemon (dry-run without `--apply`; `--once` for one tick). |
| `state` | Read the published admission state (local file or `--source URL`). |
| `wait` | Block until new work is admitted; exit 3 on `--timeout` while held. |
| `report` | Reboots seen in the records, and what preceded each. |
| `replay <trace>` | Run the controller over a recorded trace to tune a profile. |
| `unit` | Print a systemd user unit. |
| `config overview\|path\|show\|init\|check` | The config file a running daemon re-reads. |
| `gpu-clock overview\|status\|helper\|lock\|reset` | The GPU clock ceiling (root helper). |
| `whoami`, `learn`, `explain <path>`, `overview`, `doctor`, `cli overview` | Agent verbs. |

Every command supports `--json`. Results go to stdout and errors/diagnostics
to stderr. Exit codes: `0` success, `1` user error, `2` environment error,
`3` `wait` timed out while held.

## Development

```bash
uv sync
uv run pytest -n auto
uv run teken cli doctor . --strict    # the agent-first rubric gate CI runs
```

## Agent harnesses

Four harnesses can work in this repo. Each reads exactly one root prompt file,
and there is deliberately **no `AGENTS.md`** acting as a shared base:

| Harness | File(s) |
|---------|---------|
| Claude Code | [`CLAUDE.md`](CLAUDE.md) (the fullest write-up; read it first) |
| Pi / associate | [`AGENTS.override.md`](AGENTS.override.md) + [`.pi/SYSTEM.md`](.pi/SYSTEM.md) |
| colleague | [`AGENTS.colleague.md`](AGENTS.colleague.md) |
| Qwen Code | [`QWEN.md`](QWEN.md) |

`culture.yaml` declares `backend: claude`, so `CLAUDE.md` is the mesh
resident's prompt. That declaration does not stop you running any of the four
harnesses interactively. See
[`docs/harness-selection.md`](docs/harness-selection.md) and
[`docs/automation-contract.md`](docs/automation-contract.md).
`scripts/harness-smoke.py` checks that all four configs are correct.

The guildmaster skill kit is vendored under `.claude/skills/`, cited
verbatim. Its provenance is recorded in
[`docs/skill-sources.md`](docs/skill-sources.md).

## Contributing

- Every PR bumps the version (`version-bump` skill). CI's `version-check` job
  blocks the merge otherwise.
- CI runs pytest with coverage and SonarCloud, black, isort, flake8, bandit,
  markdownlint, the secret scan, the agent-first rubric gate, and the
  per-harness smoke check.
- A merge to `main` that touches `pyproject.toml` or `heat_governor/`
  publishes to PyPI via Trusted Publishing. Such PRs publish a dev build to
  TestPyPI.

## The `gate` section

`culture.yaml` carries a top-level `gate` section that declares how to set up
and test this repo, so tooling such as a PR-fixer needs no per-repo knowledge:

```yaml
gate:
  setup:
  - [uv, sync]
  test:
  - [uv, run, pytest, -n, auto]
```

`setup` and `test` are each a list of commands. Each command is an argv list
(never a shell string), run in order from the repo root. Culture's loader
ignores unknown top-level keys, so the section is safe alongside `agents`.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
