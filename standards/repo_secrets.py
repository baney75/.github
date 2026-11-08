#!/usr/bin/env python3
"""Secret scanning for any of Donovan's repositories, run locally and in CI. Replaces GitGuardian.

    python3 tools/repo_secrets.py scan [--repo DIR] [--range A..B]   # gitleaks + fleet rules; whole history by default
    python3 tools/repo_secrets.py verify [--repo DIR]                # trufflehog: only keys the issuer says are live
    python3 tools/repo_secrets.py check-allow [--repo DIR]           # the allow file is well formed
    python3 tools/repo_secrets.py fetch gitleaks|trufflehog          # download the pinned binary, print its path

Nothing leaves the machine except in `verify`, which sends each candidate key to its own issuer
(GitHub, AWS, ...) to ask whether it is live; no code is uploaded anywhere. Binaries are pinned by
version and sha256 and cached under ~/.cache/fleet-secrets (FLEET_SECRETS_CACHE overrides it);
GITLEAKS and TRUFFLEHOG point at a local binary instead.

Exceptions live in the repo's allow file, `.github/secret-allow.toml`. Each entry names a `path`
glob (optionally one `rule`) or an exact gitleaks `fingerprint`, and must give a `reason`. A
verified live key is never allowed: rotate it. Output names the file, line, commit and rule, never
the matched value. Exit 1 on any finding not allowed, 2 on a usage or download error.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "secrets" / "gitleaks.toml"
ALLOW_FILE = ".github/secret-allow.toml"
MIN_REASON = 12

# Pinned releases. Bump only with the new checksums from the release's checksums.txt, then rerun
# tools/secret_bench.py and compare.
PINS = {
    "gitleaks": {
        "version": "8.30.1",
        "url": "https://github.com/gitleaks/gitleaks/releases/download/v{v}/gitleaks_{v}_{plat}.tar.gz",
        "plat": {("linux", "x86_64"): "linux_x64", ("linux", "aarch64"): "linux_arm64",
                 ("darwin", "arm64"): "darwin_arm64", ("darwin", "x86_64"): "darwin_x64"},
        "sha256": {
            "linux_x64": "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
            "linux_arm64": "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080",
            "darwin_arm64": "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
            "darwin_x64": "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709",
        },
    },
    "trufflehog": {
        "version": "3.99.4",
        "url": "https://github.com/trufflesecurity/trufflehog/releases/download/v{v}/trufflehog_{v}_{plat}.tar.gz",
        "plat": {("linux", "x86_64"): "linux_amd64", ("linux", "aarch64"): "linux_arm64",
                 ("darwin", "arm64"): "darwin_arm64", ("darwin", "x86_64"): "darwin_amd64"},
        "sha256": {
            "linux_amd64": "5ae9b313342d1bfa96404064c0f59e77a2cbd1824d797ca7b11e6655aa8fd9d1",
            "linux_arm64": "e6285aff62e692131255dca91ad300a62a479330e0b72e4eb1d60ae58908f8d5",
            "darwin_arm64": "dc942daecb4a7795fd0a7421c66281bd4cc2ba29a265f3f6c63ecb684a6db291",
            "darwin_amd64": "c10e3ec17550e24081b9e045396cf78e494a86616736f0472024519b7416aa37",
        },
    },
}


class SetupError(RuntimeError):
    pass


def platform_key(tool: str, system: str | None = None, machine: str | None = None) -> str:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    machine = {"amd64": "x86_64", "arm64": "arm64" if system == "darwin" else "aarch64"}.get(machine, machine)
    plat = PINS[tool]["plat"].get((system, machine))
    if not plat:
        raise SetupError(f"no pinned {tool} build for {system}/{machine}")
    return plat


def fetch(tool: str, cache: Path | None = None, opener=urllib.request.urlopen) -> Path:
    """The pinned binary: from $GITLEAKS/$TRUFFLEHOG, the cache, or a checked download."""
    override = os.environ.get(tool.upper())
    if override:
        return Path(override)
    pin = PINS[tool]
    plat = platform_key(tool)
    cache = cache or Path(os.environ.get("FLEET_SECRETS_CACHE", Path.home() / ".cache" / "fleet-secrets"))
    target = cache / f"{tool}-{pin['version']}-{plat}" / tool
    if target.is_file():
        return target
    url = pin["url"].format(v=pin["version"], plat=plat)
    with opener(url, timeout=120) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != pin["sha256"][plat]:
        raise SetupError(f"{tool} {pin['version']} {plat}: sha256 {digest} does not match the pin; not running it")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        member = tar.getmember(tool)
        if not member.isfile():
            raise SetupError(f"{tool} archive has no regular file named {tool}")
        source = tar.extractfile(member)
        if source is None:
            raise SetupError(f"{tool} archive member {tool} can't be read")
        tmp = target.with_suffix(".part")
        with open(tmp, "wb") as out:
            shutil.copyfileobj(source, out)
    tmp.chmod(0o755)
    tmp.replace(target)
    return target


# --------------------------------------------------------------------------- allow file

def load_allow(repo: Path) -> tuple[list[dict], list[str]]:
    """Entries and problems. A problem makes the scan fail: an exception without a reason is a hole."""
    path = repo / ALLOW_FILE
    if not path.is_file():
        return [], []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return [], [f"{ALLOW_FILE}: not valid TOML ({exc})"]
    entries, problems = [], []
    for i, entry in enumerate(data.get("allow", []), 1):
        where = f"{ALLOW_FILE} entry {i}"
        if not isinstance(entry, dict):
            problems.append(f"{where}: must be a table")
            continue
        unknown = set(entry) - {"path", "rule", "fingerprint", "reason"}
        if unknown:
            problems.append(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        reason = str(entry.get("reason", "")).strip()
        if len(reason) < MIN_REASON:
            problems.append(f"{where}: needs a reason of at least {MIN_REASON} characters")
        if not entry.get("path") and not entry.get("fingerprint"):
            problems.append(f"{where}: needs a path or a fingerprint")
        glob = str(entry.get("path", ""))
        if glob and not entry.get("fingerprint") and not entry.get("rule"):
            problems.append(f"{where}: a path entry needs a rule, or allow by fingerprint")
        if glob and not re.sub(r"[*?/.]", "", glob):
            problems.append(f"{where}: path {glob!r} would allow the whole repository")
        entries.append(entry)
    return entries, problems


def allowed_by(finding: dict, entries: list[dict]) -> dict | None:
    for entry in entries:
        if entry.get("fingerprint"):
            if entry["fingerprint"] == finding.get("Fingerprint"):
                return entry
            continue
        if entry.get("rule") and entry["rule"] != finding.get("RuleID"):
            continue
        if glob_match(entry["path"], finding.get("File", "")):
            return entry
    return None


def glob_match(glob: str, path: str) -> bool:
    """`*` and `?` stay inside one path segment; only `**/` spans directories."""
    rx = re.escape(glob).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return re.fullmatch(rx, path) is not None


# --------------------------------------------------------------------------- scanners

def gitleaks_findings(binary: Path, repo: Path, rev_range: str | None, config: Path = CONFIG,
                      timeout_s: int = 0, max_file_mb: int = 0) -> list[dict]:
    """Findings, values redacted. timeout_s and max_file_mb bound a scan of a huge history (0: no bound)."""
    if (repo / ".gitleaksignore").exists():  # gitleaks reads it from the source folder whatever -i says
        raise SetupError(f"{repo / '.gitleaksignore'} hides findings without a reason: move its entries to {ALLOW_FILE}")
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        cmd = [str(binary), "git", str(repo), "--config", str(config), "--redact", "--no-banner",
               "--report-format", "json", "--report-path", str(report), "--exit-code", "0", "--log-level", "error",
               # No .gitleaksignore: exceptions live in the allow file, with a reason.
               "--gitleaks-ignore-path", str(Path(tmp) / "none"), "--ignore-gitleaks-allow"]
        # Without --log-opts gitleaks scans every ref (--all), so one stray branch would fail main.
        cmd += ["--log-opts", rev_range or "HEAD"]
        if timeout_s:
            cmd += ["--timeout", str(timeout_s)]
        if max_file_mb:
            cmd += ["--max-target-megabytes", str(max_file_mb)]
        done = subprocess.run(cmd, capture_output=True, text=True)
        if done.returncode != 0:
            raise SetupError(f"gitleaks failed ({done.returncode}): {done.stderr.strip()[-400:]}")
        return json.loads(report.read_text() or "[]")


def scan(repo: Path, rev_range: str | None, binary: Path) -> int:
    entries, problems = load_allow(repo)
    findings = gitleaks_findings(binary, repo, rev_range)
    used: set[int] = set()
    blocked, allowed = [], 0
    for f in findings:
        entry = allowed_by(f, entries)
        if entry is None:
            blocked.append(f)
        else:
            allowed += 1
            used.add(id(entry))
    seen = set()
    for f in blocked:
        key = (f["File"], f["StartLine"], f["RuleID"])
        if key in seen:
            continue  # the same line in several commits is one thing to fix
        seen.add(key)
        print(f"{f['File']}:{f['StartLine']}: {f['RuleID']} (commit {f['Commit'][:12]}, fingerprint {f['Fingerprint']})")
    for problem in problems:
        print(problem)
    scope = rev_range or "history of HEAD"
    print(f"secret scan ({scope}): {len(seen)} finding(s) to fix, {allowed} allowed by {ALLOW_FILE}, "
          f"{len(problems)} allow-file problem(s)")
    if not rev_range:
        for entry in entries:
            if id(entry) not in used:
                print(f"note: allow entry no longer matches anything, delete it: {entry.get('path') or entry.get('fingerprint')}")
    if seen:
        print(f"Rotate any real key first. A fake or harmless match goes in {ALLOW_FILE} with a reason.")
    return 1 if seen or problems else 0


def verify(repo: Path, binary: Path) -> int:
    done = subprocess.run([str(binary), "git", f"file://{repo.resolve()}", "--json", "--results=verified",
                           "--no-update", "--log-level=-1"], capture_output=True, text=True)
    live = []
    for line in done.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        git = row.get("SourceMetadata", {}).get("Data", {}).get("Git", {})
        live.append((git.get("file", "?"), git.get("line", "?"), (git.get("commit") or "?")[:12], row.get("DetectorName", "?")))
    for path, line, commit, detector in sorted(set(live)):
        print(f"LIVE {detector} key at {path}:{line} (commit {commit}): rotate it now")
    if done.returncode not in (0, 183) and not live:
        print(f"trufflehog failed ({done.returncode}): {done.stderr.strip()[-400:]}", file=sys.stderr)
        return 2
    print(f"verified scan: {len(set(live))} live key(s)")
    return 1 if live else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--repo", type=Path, default=Path.cwd())
    s.add_argument("--range", help="git log range, e.g. origin/main..HEAD; default is the whole history")
    v = sub.add_parser("verify")
    v.add_argument("--repo", type=Path, default=Path.cwd())
    c = sub.add_parser("check-allow")
    c.add_argument("--repo", type=Path, default=Path.cwd())
    f = sub.add_parser("fetch")
    f.add_argument("tool", choices=sorted(PINS))
    a = ap.parse_args(argv)
    try:
        if a.cmd == "fetch":
            print(fetch(a.tool))
            return 0
        if a.cmd == "check-allow":
            entries, problems = load_allow(a.repo)
            for problem in problems:
                print(problem)
            print(f"{ALLOW_FILE}: {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}, {len(problems)} problem(s)")
            return 1 if problems else 0
        if a.cmd == "scan":
            return scan(a.repo, a.range, fetch("gitleaks"))
        return verify(a.repo, fetch("trufflehog"))
    except (SetupError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
