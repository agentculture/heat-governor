"""``memory replay`` reads a records ``.jsonl.gz`` exactly like the raw ``.jsonl``."""

import gzip
import json

from heat_governor.cli import main


def _records(path):
    lines = []
    for i in range(60):
        avail = 20000.0 if i < 20 else max(200.0, 20000.0 - (i - 20) * 900.0)
        lines.append(json.dumps({"t": 1000.0 + i, "system": {"mem:avail_mib": avail}}))
        lines.append(json.dumps({"t": 1000.5 + i, "system": {"temp": 50}}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _replay(capsys, path):
    rc = main(["memory", "replay", str(path), "--total-mib", "32000", "--json"])
    out = capsys.readouterr().out
    return rc, out


def test_replay_gz_matches_raw(tmp_path, capsys):
    raw = tmp_path / "20261114.jsonl"
    _records(raw)
    gz = tmp_path / "20261114.jsonl.gz"
    gz.write_bytes(gzip.compress(raw.read_bytes()))

    rc_raw, out_raw = _replay(capsys, raw)
    rc_gz, out_gz = _replay(capsys, gz)

    assert rc_raw == 0 and rc_gz == 0
    d_raw, d_gz = json.loads(out_raw), json.loads(out_gz)
    for d in (d_raw, d_gz):
        d.pop("file")
    assert d_raw == d_gz
    assert d_gz  # something was actually replayed
