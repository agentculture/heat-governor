# Boundaries: the write contract

Since 0.19.0 the memory guard's boundary rules (`[[memory.allow]]`, described in
[`memory.md`](memory.md)) can be created, changed and deleted without editing `config.toml` by
hand: from the dashboard's Boundaries tab, or with `heat-governor boundaries`. This document is
the contract for that write path. The read side (`mem_guard` in `/state`, `/api/fleet`,
`mesh status`, MCP) is in [`state.md`](state.md).

A boundary rule can make a box kill processes. Saving a rule on a live machine, and enabling a
token file on a live `mesh serve`, both need the machine owner's go-ahead. Tests never write
under a real config directory.

## The file

Page-managed and CLI-managed rules live in `boundaries.toml`, beside the `config.toml` the
daemon actually loaded (never a path worked out again from the environment). The file holds
only `[[memory.allow]]` tables with the keys `name`, `comm` (a list of strings), `cmdline`,
`cgroup`, `max_mib` and `scope`. Any other key or table makes the file invalid.

```toml
# Managed by heat-governor (boundaries tab / `heat-governor boundaries`).

[[memory.allow]]
name = "vllm"
cmdline = "*vllm*serve*"
max_mib = 102400
scope = "process"
```

Rules written by hand in `config.toml` keep working. The page and the CLI show them as
read-only (`source: "config.toml"`, `editable: false`) and never rewrite `config.toml`.

The write path edits boundary rules only. It cannot change `[memory] action`, `act_over_pct`,
the limits, or any other setting.

## Merge, collisions and reload

- The loader merges the rules of both files (config.toml's first) before validation, and
  tags each with its source file.
- A name present in both files is an error that names both files.
- A rule with no `comm` whose globs match anything (`*`, `?*`, a cgroup of `/*`) is rejected
  in either file: it would match every process. `comm` names are compared exactly, so a rule
  with a `comm` list is never match-all.
- The daemon watches both files and reloads within `reload_every_s` (2 s by default) when
  either changes. Deleting `boundaries.toml` means no page-managed rules.
- An invalid `boundaries.toml` on reload keeps the last good boundaries (minus any whose
  name `config.toml` now defines) and publishes the message in `config_error`; a valid `config.toml` edit made at the same time still applies.
  At daemon start an invalid `boundaries.toml` is a start-up error, like an invalid config.
- A change that touches only the rules swaps them inside the running guard: the fill-rate
  window, the kill budget and the repeat history are kept. Any other `[memory]` change
  rebuilds the guard as before.

## The on-box command

`heat-governor boundaries` has the verbs `overview`, `list`, `add`, `set`, `rm`, `apply` and
`preview`. All take `--json` and `--config PATH` (default: the usual config path); the
boundaries file is always the sibling of that path. Without `--machine` they act on this box.

Every `--json` result is one object on stdout with an `outcome` field. Exit code 0 for `ok`
and `saved`, 1 for `conflict`, `would_kill` and `invalid`, 2 for an environment error (with
the structured error on stderr).

`apply` reads one JSON request on stdin:

```json
{"base_version": "VERSION", "operation": "add", "rule": {"name": "vllm", "cmdline": "*vllm*", "max_mib": 102400}, "allow_kill": false}
```

- `operation` is `add`, `set` (replace the whole rule of that name) or `rm` (`rule` is then
  `{"name": ...}` only).
- `rule` accepts only the six rule keys. Anything else is refused with the validator's
  message and nothing is written.
- `add`, `set` and `rm` are flag-driven wrappers that read the current version and build the
  same request.

| Outcome | Meaning |
|---|---|
| `saved` | Written. Carries the new `version` and `rules`; `forced: true` with `processes` when `allow_kill` let through a change that puts processes far over. |
| `conflict` | `base_version` is not the file's version. Nothing written; carries the current `version` and `rules`. |
| `would_kill` | The change would put a killable own-user process far over its boundary. Nothing written; carries those `processes`. Repeat with `"allow_kill": true` to save anyway. |
| `invalid` | The request or the rule is invalid (`problems` holds the validator's messages), the name already exists in `config.toml`, or the file on disk is rejected. Nothing written. |

`list` returns the rules of both files (each with `source` and `editable`), the file
`version`, `exists`, and `error` when the file on disk is rejected. `preview` reads
`{"rule": {...}}` and returns what the draft rule matches right now.

## Versions and conflicts

The version of `boundaries.toml` is the SHA-256 of its bytes, or the fixed word `missing`
when there is no file, so creating the file also conflicts correctly. A write takes an
exclusive lock on a sibling lock file, compares the base version with the file's current
version, and writes only when they are equal. Two writes from one base yield exactly one
success. A hand edit changes the version, so a page that read the older file gets a conflict.

## Atomic writes, backups and restoring one

A write validates first, then writes a temporary file, syncs it and renames it over
`boundaries.toml`. A failure midway leaves the previous file byte-identical.

Before each replacement the previous file is copied to
`boundaries.toml.<UTC timestamp>.bak` (mode 0600) in the same directory. The five newest
backups are kept.

To restore one: copy the chosen `.bak` file over `boundaries.toml` on that box. The daemon
reloads it within about 2 s and records the change as an event. Deleting `boundaries.toml`
removes every page-managed rule.

## The preview and the kill check

The preview runs on the box against its real process table, using the guard's own matching
(`memboundary.assess`) and the killer's own eligibility check, so it agrees with what the
guard would do. For each matching process it gives the pid, uid, user name, process name,
redacted command line, container id, anonymous memory, state (`within`, `over`, `far_over`),
`killable` and the reason when not.

The guard kills a process only when all of these hold: it belongs to the daemon's own user,
it is not on the protect list, it holds at least `min_victim_mib` of anonymous memory (per
process), `[memory] action` is `kill`, and it is far over its boundary (more than
`act_over_pct` percent over). It acts within about 12 s of a save (2 s reload plus the 10 s
routine scan).

The kill check is enforced on the box, not only shown in the page. `apply` re-runs the
preview for the whole would-be rule set at write time and refuses with `would_kill` when the
change newly puts a killable own-user process far over, whatever `action` is configured
(the action can change later by reload). A process that is already far over under the rules
on disk does not block an unrelated edit.

## From the reader: `--machine` and the page

`heat-governor boundaries list|add|set|rm|apply|preview --machine M` and the page both go
through the reader's boundaries service:

1. **Version gate.** A machine whose daemon reports a version below 0.19.0 (or none) is
   refused before any call to it, with the needed version in the message.
2. **Remote call.** The reader runs the fixed command
   `BIN boundaries VERB --config PATH --json` over its existing SSH connection (or directly
   for a local machine), with the request on stdin. The command line never depends on the
   request. `PATH` is the config path the daemon reported in `/state`. `BIN` is
   `heat-governor` in the per-user local bin directory unless the machine has a `bin` key in
   `fleet.toml`. The call has its own 30 s deadline.
3. **Rebuilt request.** The request sent to the box is rebuilt from validated fields, never
   forwarded as received.
4. **Lost response.** If the call fails after the request may have been delivered (the
   deadline, or ssh itself failing with exit 255), the service reads the file again: a
   changed version whose rules reflect the request is `saved` (`recovered: true`), an
   unchanged file is `not_saved`, an unreadable one is `unknown`. After a timeout an
   unchanged file is also `unknown`: the command on the box was not stopped and may still
   write, so read the list again before retrying. Repeating a request whose result already holds is `saved`
   (`idempotent: true`) and writes nothing.

The CLI path needs no token: its authority is the SSH access the reader already has.

## HTTP routes of `mesh serve`

`MACHINE` is a fleet machine name. All responses are JSON with `Cache-Control: no-store`.

| Route | Method | Token | Request | Success |
|---|---|---|---|---|
| `/api/boundaries` | GET | no | none | `{"writes_enabled": bool}` |
| `/api/boundaries/MACHINE` | GET | yes | none | The `list` result, with `ETag: "VERSION"` |
| `/api/boundaries/MACHINE/preview` | POST | yes | `{"rule": {...}}` | The `preview` result |
| `/api/boundaries/MACHINE` | POST | yes | `If-Match: "VERSION"` and `{"operation", "rule", "allow_kill"}` | `saved` |

Status codes:

| Status | When |
|---|---|
| 200 | `ok`, `saved` |
| 400 | `invalid`; a malformed body, length or `If-Match` |
| 401 | Missing or wrong token |
| 403 | No token file configured (`writes_disabled`); wrong `Host`; foreign `Origin` |
| 404 | Unknown machine or path |
| 405 | Wrong method |
| 409 | `conflict` (with the current rules and `ETag`) and `would_kill` |
| 411, 413, 415 | No `Content-Length`; body over 16 KiB; `Content-Type` not `application/json` |
| 422 | Daemon too old (`version_too_old`, with `needed_version`), or it reports no config path |
| 428 | A write without `If-Match` |
| 429 | Ten failed authentications within a minute from one client address |
| 500 | The audit log could not be written (nothing was sent to the box) |
| 502 | Machine unreachable, bad reply, box error, or `not_saved` after a lost response |
| 503 | More than four boundaries calls in flight |
| 504 | `unknown` after a lost response: read the list again |

Every route that existed before 0.19.0 answers exactly as before, and daemons still listen
only on loopback.

## Tokens

- `mesh serve --token-file PATH` is the only way to enable the token routes. There is no
  default path; without the flag every token route answers 403.
- The file holds `label = token` lines (`#` comments and blank lines allowed). Labels are
  unique and plain; tokens are 32 to 512 printable characters with no whitespace.
- The file must be a regular file owned by the user running `mesh serve`, with no group or
  other permission bits. Otherwise `mesh serve` exits 2 with a message that names the problem
  and never prints a token.
- It is read once at start: restart `mesh serve` to rotate tokens.
- A token is accepted only in `Authorization: Bearer TOKEN`, never in the query string, a
  cookie or the body. It is compared in constant time against every configured token and
  never logged.
- The list and preview routes need a token too, because they show command lines and rule
  data beyond what `/api/fleet` shows.
- A token file cannot be combined with a wildcard listen address. `--server-name NAME`
  (repeatable) adds a host name the page is opened by, accepted in `Host` and `Origin`.
- The page keeps the token for the browser tab's session only and sends it only in the
  `Authorization` header.
- Transport encryption is the tailnet's (an assumption of this design): the server speaks
  plain HTTP, so do not expose it beyond a network you trust.

## Audit log

Every write through the reader's service is recorded in `boundaries-audit.jsonl` in the
reader's state directory (mode 0600, append-only). Each line is a JSON object with the time,
the label (the token's label, or `user@host` for the CLI), the machine, the operation, the
old rule, the new rule and the outcome. An apply that reaches the box writes two lines: an
`attempt` line before the call and the final outcome after it. If the attempt line cannot be
written, the write is refused and nothing is sent to the box. A refusal by the version gate
writes one line. The log never holds tokens or process data.

## The event

The daemon, and only the daemon, records `memory_boundary_changed` when the merged rules
change, whichever file changed and however it was edited, and ships it like the other memory
events. It also fires once at start when the rules differ from the ones it last applied. See
[`memory.md`](memory.md).

## Downgrade

A daemon older than 0.19.0 does not read `boundaries.toml`: after a downgrade the
page-managed rules silently stop applying. Move them into `config.toml` first if they must
survive a downgrade.

## Issue #18 acceptance criteria

| Criterion | Covered by | Tests |
|---|---|---|
| `/api/fleet` and MCP `machine_state` include `mem_guard`; the reader passes it through and tolerates its absence | Spec c2, c3 (0.18.0) | `tests/test_fleet_reader.py`, `tests/test_mcp.py` |
| The page shows every machine's rules and live state, and marks `config.toml` rules read-only | c11, c12, c16 | `tests/test_web_page.py` |
| Create, change and delete work for a local and an SSH machine; reload within about 2 s; the page reflects it | c4, c6, c7, c28 | `tests/test_config_memory_allow.py`, `tests/test_cli_boundaries.py`, `tests/test_transport.py`, `tests/test_boundaries_end_to_end.py`; on live machines: the staged live check |
| An invalid rule is rejected with the validator's message before anything is written | c5, c6, c35 | `tests/test_boundaries_file.py`, `tests/test_cli_boundaries.py` |
| 401 without a valid token, 403 when none is configured, 409 for a stale ETag, GET unchanged | c8, c17, c36 | `tests/test_mesh_server.py` |
| Writes are atomic and backed up, appear in the audit log, and produce a recorded and shipped `memory_boundary_changed` | c5, c9, c39, c40 | `tests/test_boundaries_file.py`, `tests/test_boundaries_service.py`, `tests/test_recorder_memory.py` |
| `heat-governor boundaries list\|add\|set\|rm` work with `--json` and pass `teken cli doctor --strict` | c6 | `tests/test_cli_boundaries.py`, the CI rubric gate |
| Tests cover the loader merge, emitter round-trip, auth, ETag conflicts, remote apply via a fake transport, and preview | c4, c5, c8, c7, c10 | The files above, `tests/test_mempreview.py` |
| Lint, Sonar gate, scan-secrets and the version bump pass; no third-party runtime dependency | c13 | CI |
| Changing `[memory] action`, `act_over_pct` or the limits from the page | Out of scope (c16, c18) | `tests/test_cli_boundaries.py` (unknown keys refused) |
