#!/usr/bin/env python3
"""Fail CI while SonarCloud has unaccepted issues or unreviewed security hotspots.

The quality gate (``sonar.qualitygate.wait=true``) passes code smells as long as
the maintainability rating holds, so a PR could merge with open findings. This
step runs after the scan and asks SonarCloud directly:

* issues that are still unresolved (``resolved=false``): Accepted and False
  Positive are resolutions, so an issue someone deliberately accepted on
  SonarCloud does not count;
* security hotspots still ``TO_REVIEW``.

Scope:
    --pull-request N   the findings SonarCloud reports on that PR (its new code);
    --branch NAME      the findings in the branch's new-code period, so the
                       backlog that predates this gate does not turn main red.

Sends ``SONAR_TOKEN`` as a Bearer token when set (anonymous works for a public
project) and reads the project key from
``sonar-project.properties``. Stdlib only. Exit 0 clean, 1 findings, 2 error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HOST = "https://sonarcloud.io"
PAGE = 100


def project_key(props: Path) -> str:
    for line in props.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "sonar.projectKey":
            return value.strip()
    raise SystemExit(f"sonar-gate: no sonar.projectKey in {props}")


def _get(path: str, params: dict[str, str], token: str) -> dict:
    url = f"{HOST}{path}?{urllib.parse.urlencode(params)}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 - fixed https host
        return json.load(resp)


def _paged(path: str, params: dict[str, str], token: str, items: str) -> list[dict]:
    out: list[dict] = []
    page = 1
    while True:
        doc = _get(path, {**params, "ps": str(PAGE), "p": str(page)}, token)
        batch = doc.get(items, [])
        out.extend(batch)
        total = doc.get("total", doc.get("paging", {}).get("total", len(out)))
        if not batch or len(out) >= total:
            return out
        page += 1


def scope_params(pull_request: str | None, branch: str | None) -> tuple[dict, dict]:
    """Query parameters for issues and hotspots in the requested scope."""
    if pull_request:
        return {"pullRequest": pull_request}, {"pullRequest": pull_request}
    if branch:
        return (
            {"branch": branch, "inNewCodePeriod": "true"},
            {"branch": branch, "inNewCodePeriod": "true"},
        )
    raise SystemExit("sonar-gate: give --pull-request or --branch")


def findings(key: str, pull_request: str | None, branch: str | None, token: str) -> list[str]:
    iscope, hscope = scope_params(pull_request, branch)
    issues = _paged(
        "/api/issues/search",
        {"componentKeys": key, "resolved": "false", **iscope},
        token,
        "issues",
    )
    hotspots = _paged(
        "/api/hotspots/search",
        {"projectKey": key, "status": "TO_REVIEW", **hscope},
        token,
        "hotspots",
    )
    return [describe(i, "issue") for i in issues] + [describe(h, "hotspot") for h in hotspots]


def describe(item: dict, kind: str) -> str:
    where = item.get("component", "").split(":", 1)[-1]
    line = item.get("line")
    rule = item.get("rule") or item.get("ruleKey") or item.get("securityCategory", "")
    sev = item.get("severity") or item.get("vulnerabilityProbability", "")
    loc = f"{where}:{line}" if line else where
    return f"{kind} {rule} {sev} {loc}: {item.get('message', '')}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pull-request")
    ap.add_argument("--branch")
    ap.add_argument("--properties", default="sonar-project.properties")
    args = ap.parse_args(argv)
    token = os.environ.get("SONAR_TOKEN", "")  # empty: anonymous, public projects only
    key = project_key(Path(args.properties))
    try:
        found = findings(key, args.pull_request, args.branch, token)
    except OSError as exc:  # URLError and HTTPError are OSErrors
        print(f"sonar-gate: SonarCloud query failed: {exc}", file=sys.stderr)
        return 2
    scope = f"PR #{args.pull_request}" if args.pull_request else f"{args.branch} (new code)"
    if not found:
        print(f"sonar-gate: {scope}: no unaccepted issues, no hotspots to review")
        return 0
    print(f"sonar-gate: {scope}: {len(found)} finding(s) to fix, or accept on SonarCloud:")
    for f in found:
        print(f"  {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
