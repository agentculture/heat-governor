# Boundaries tab, read side: live check (EliteMini reader, 2026-11-16)

Live check of heat-governor 0.18.0 (PR #19, plan task t6 of
`docs/plans/2026-11-15-boundaries-tab.md`): the fleet reader passes each daemon's `mem_guard`
through and the dashboard's Boundaries tab shows it. Only the EliteMini was changed, with the
owner's go-ahead. The five remote boxes were read, not touched.

## What was changed

- **EliteMini:** heat-governor 0.17.0 to 0.18.0, installed from PyPI with `uv tool install --force`.
  `uv tool upgrade` failed first, because the 0.17.0 install pointed at a local wheel that no
  longer exists. The daemon unit and the mesh dashboard unit were restarted, each twice: once
  before the failed upgrade was noticed (still 0.17.0) and once after the install.
- **Remote boxes (cultureos, spark, spark2, thor, orin):** unchanged, still daemon 0.17.0.

## Results

| Check | Target | Result |
|---|---|---|
| Daemon after restart | 0.18.0, guard on | `version 0.18.0`, `action kill`, `armed true`, `act_over_pct 50.0`, `max_kills_per_hour 3`, `min_victim_mib 2048`, `boundaries []`, `config_error null` |
| `/api/fleet` carries `mem_guard` | all 6 machines | 6 of 6: every machine `ok` with `action kill`, `armed true`, `kills_last_hour 0` |
| Older daemons tolerated | state `ok` on 0.17.0 daemons | 5 of 5 remote boxes `ok`; their `mem_guard` has no `act_over_pct`, kill cap, `min_victim_mib` or matchers, as expected for 0.17.0 |
| `mesh status` guard column | one column per machine | `guard kill armed 0k 0b` on all 6 rows, version column aligned |
| Boundaries tab | each machine's guard state | 6 cards, each `armed · kill` with free memory, limits and source (`platform` on five, `percent` on the EliteMini), kills and last kill; "No boundaries" on all six; no console errors |

Limits shown on the page: cultureos 1.0 / 2.0 GiB, orin 2.0 / 4.0 GiB, spark 3.0 / 6.0 GiB
(platform); EliteMini 2.2 / 9.0 GiB (percent).

## What this does not show

- **Timing was not measured to the poll.** The first read of `/api/fleet` was 14 s after the
  dashboard unit restarted, and the page was looked at 3 s after it loaded; both already showed all
  six guards. That the tab fills "within one 3 s poll" follows from the reader's design (the guard
  arrives with every poll) and was not timed.
- **No rule is rendered live.** No box has a `[[memory.allow]]` rule, so the rule table, the usage
  bar and the rule states were seen only against a canned document before the merge.
- **The remote daemons are not upgraded,** so matchers, source and thresholds from a remote box
  have not been seen live. Only the EliteMini's daemon reports them.
- **Each restart left the EliteMini without its guard for a few seconds.** Nothing was running
  that needed it; the unit came back `armed`.
