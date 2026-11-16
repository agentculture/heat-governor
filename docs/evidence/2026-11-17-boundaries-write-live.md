# Boundaries, write side: live check (EliteMini and orin, 2026-11-17)

Live check of heat-governor 0.19.0 (PR #21, plan task t18 of
`docs/plans/2026-11-15-boundaries-tab.md`): a boundary rule is saved, changed and deleted on
the local machine and on one machine reached over SSH, from the CLI and through the token
routes of `mesh serve`. Every step had the owner's go-ahead. Times are UTC, 2026-11-16 23:10
to 23:20. Every rule used `comm = ["hg-live-check"]`, a name no process had on either box
(`pgrep -x` found none), so no rule ever matched a process. Both boxes end with no rules.

## What was changed

- **EliteMini:** heat-governor 0.18.0 to 0.19.0 from PyPI (`uv tool install --force`); the
  daemon unit and the dashboard unit restarted. A token file (mode 0600, one token) was
  created and `--token-file` added to the dashboard unit, which was restarted again. The
  earlier unit file is kept beside it with a `.bak-2026-11-17` suffix.
- **orin:** heat-governor 0.17.0 to 0.19.0 the same way; its daemon unit (which runs with
  `apply = true`) was restarted while the box was at level cool, duty 0.
- **cultureos, spark, spark2, thor:** read only, still daemon 0.17.0.
- **A second `mesh serve` on a loopback port** of the EliteMini, with a throwaway token, ran
  for the HTTP checks and was stopped; its token file is deleted.

## Results

| Check | EliteMini (local) | orin (SSH) |
|---|---|---|
| Daemon after the upgrade | 0.19.0, guard `kill`, armed, no config error | 0.19.0, guard `kill`, armed, no config error |
| `boundaries preview` of the test rule | 0 matching, 0 far over | 0 matching, 0 far over |
| CLI `add`, `set` (4096 to 8192 MiB), `rm` | `saved` three times; each state in `mem_guard.boundaries` at the next look, 4 s later | the same, through `--machine orin` |
| Guard state across the three reloads | armed, 0 kills; the log says "boundaries updated, window and kill budget kept" each time | the same |
| `memory_boundary_changed` events | 3 recorded, each with old and new rules and hashes | 3 recorded |
| Backups | 2 backup files (mode 0600) after 3 writes, the first write having no earlier file | the same |
| HTTP `POST` with no token, and with a wrong token | 401, 401 | 401, 401 |
| HTTP `POST` with no `If-Match` | 428 | 428 |
| HTTP `add` with the token | 200; the rule was in `mem_guard.boundaries` 2.4 s after the request was sent | 200; 1.9 s |
| The saved rule in `boundaries list --machine M` | listed, source `boundaries.toml`, editable | listed |
| HTTP `add` with a stale version | 409 `conflict`; the file's SHA-256 is the same before and after | 409; unchanged |
| HTTP `rm` with the token | 200; no rules 4 s later | 200; no rules |

Other checks:

- **Dashboard unit with the token file:** `/api/boundaries` answers `writes_enabled: true`,
  the list and write routes answer 401 without a token, and `/api/fleet` answers 200 as
  before.
- **Stale version and match-all from the CLI (EliteMini):** a request with an old version
  is `conflict` (exit 1); a rule with `cmdline = "*"` is `invalid`.
- **Old daemon refused:** `boundaries list --machine thor` exits 1 with "thor reports version
  0.17.0; boundaries need 0.19.0 or newer"; the HTTP route answers 422.
- **Audit log:** mode 0600; an attempt line and an outcome line per write, labelled
  `user@host` for the CLI and with the token's label for HTTP; no token in it.

## What this does not show

- **The page itself was not used.** The plan asked for a rule "created from the page". The
  browser available to the check runs on another machine, and the check may not enter the
  real token there, so the saves went through the same HTTP routes the page calls, with
  `curl`, on a loopback copy of the dashboard. The editor, its dialogs, its phone-width
  layout and its dark theme have still not been seen with a token in a real browser.
- **The real token on the tailnet dashboard was never used.** That unit was checked only for
  what it refuses.
- **No rule matched a process,** by design: a boundary's state other than `idle`, the usage
  bar and the would-kill refusal were not seen live.
- **Shipping of the new event was not checked** in the bucket.
- **The cost of a preview on a box short of memory** (thor and the Sparks, a risk the plan
  names) was not measured: those boxes are not upgraded.
- **Each restart left a box without its guard for a few seconds**, and orin without its
  thermal governor; both came back armed.
- **A wart seen in passing:** for a refused machine the CLI's `--json` error puts
  `outcome: version_too_old` in the `remediation` field.
