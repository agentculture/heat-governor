# Record retention live: first pass on all six boxes (2026-11-24)

heat-governor 0.20.0 (PR #23) was deployed to the whole fleet on 2026-11-24 between 23:20 and
23:30 UTC, the live check t10 of `docs/plans/2026-11-24-record-retention.md`. Each daemon ran its
first retention pass at start. The seven-day size check (success signal c19) is still to come
**(planned, 2026-12-01)**.

## What was changed

- **Every box:** heat-governor from PyPI with `uv tool install --force heat-governor==0.20.0`
  (spark2's `uv` had a stale index cache and needed `--refresh`; the other boxes then used it
  too), then `heat-governor config check` (valid on all six), then `heat-governor records prune
  --dry-run`, then a restart of the daemon unit (`heat-governor.service`), only when the box was
  at level cool. All six were cool. The EliteMini's dashboard unit (`heat-governor-mesh.service`)
  was restarted too.
- **No config change:** every box runs the defaults, `keep_days = 7` and `log_max_mib = 10`.
- **Versions before:** cultureos, spark, spark2 and thor ran 0.17.0; orin and the EliteMini ran
  0.19.0.

## Results

UTC date of the passes: 2026-11-24, so today and yesterday are 20261124 and 20261123.

| Box | Before | Dry run | First pass (`records_pruned`) | After |
|---|---|---|---|---|
| EliteMini | 16 days, 1.4 GB | 8 delete, 6 compress | deleted 8, compressed 6, freed 1.20 GB, errors 0 | 8 files (2 raw, 6 gz), 220 MB |
| cultureos | 2 days, 422 MB (hand-trimmed) | 0 / 0 | nothing to do, so no row (as designed) | 2 raw, 423 MB |
| spark | 33 days, 3.4 GB | 25 delete, 6 compress | deleted 25, compressed 6, freed 3.34 GB, errors 0 | 8 files (2 raw, 6 gz), 279 MB |
| spark2 | 33 days, 3.4 GB | 25 delete, 6 compress | deleted 25, compressed 6, freed 3.32 GB, errors 0 | 8 files (2 raw, 6 gz), 266 MB |
| thor | 33 days, 3.7 GB | 25 delete, 6 compress | deleted 25, compressed 6, freed 3.70 GB, errors 0 | 8 files (2 raw, 6 gz), 241 MB |
| orin | 33 days, 3.4 GB | 25 delete, 6 compress | deleted 25, compressed 6, freed 3.31 GB, errors 0 | 8 files (2 raw, 6 gz), 275 MB |

- **Every dry run listed exactly what the real pass then did.**
- **Compression:** on the EliteMini, each ~105 MB day became 3.4-3.5 MB (about 30x).
- **No gap check fired:** every box had a continuous run of day files.
- **Readers on gzipped days:** `heat-governor report` runs on the EliteMini. The dashboard's
  `/api/history?range=7d` answers from the records for all six machines (`source: records`). Five
  return 300 samples starting 2026-11-17/18, so the window reaches into the gzipped days.
  cultureos returns 85 samples from 2026-11-23, since only two days are left.
- **After the deploy:** `mesh status` shows all six on 0.20.0 at level cool, none failing, no
  last error, and every memory guard armed with `action = kill`.

## What this does not show

- **The seven-day bound (c19):** 8 day files, at most 2 raw, under 300 MB per box (under 600 MB
  on cultureos). It is measured once a week of rollovers has run **(planned)**.
- **A pass at a UTC date change** (not at start): the first is due at 2026-11-25 00:00 UTC.
- **Compression time and memory on cultureos with the shipped code** (h16): cultureos had
  nothing to compress yet. Its first compress is the 20261123 day, two rollovers from now.
- **`governor.log` rotation:** no box has a log near 10 MiB yet.
- **The gap check and `--allow-gap` on a live box.**
- **The 2026-10-23 days on spark, spark2, thor and orin are gone,** as accepted in the spec (c22).
