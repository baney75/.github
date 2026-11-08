#!/usr/bin/env python3
"""Grade one repository checkout against Donovan's repo standard, with a ratchet.

    python3 tools/repo_standards.py [--repo DIR]                 # text report; exit 1 only on new problems
    python3 tools/repo_standards.py --json
    python3 tools/repo_standards.py --markdown "$GITHUB_STEP_SUMMARY"
    python3 tools/repo_standards.py --write-baseline             # accept today's problems as the floor
    python3 tools/repo_standards.py --mode report                # never fail

What it grades (docs/PROJECT-STANDARD.md, skills/writing-for-agents): README shape, LICENSE for
public repos, AGENTS.md (under 150 lines, names a check) and CLAUDE.md as an @AGENTS.md import,
each SKILL.md against the tools/skills.py rules plus a 500-line cap, DESIGN.md for repos with a UI,
em and en dashes in agent-facing prose, structured logging for each service, the secret allow
file, and the median CI time of the default branch.

Ratchet: `.github/repo-standards-baseline.json` lists the problems a repo already had. A problem
not in it fails the run (mode ratchet); one in it is reported and doesn't. With no baseline file
everything is reported and nothing fails, so the first run in a repo is a report. Numbers in a
problem don't make it new: "AGENTS.md is 151 lines" and "... 152 lines" are the same problem.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import statistics
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import project_audit  # noqa: E402
import repo_secrets  # noqa: E402
import skills as skill_rules  # noqa: E402

BASELINE = ".github/repo-standards-baseline.json"
AGENTS_MAX_LINES = 150  # skills/writing-for-agents: an always-loaded file stays under 150 lines
SKILL_MAX_LINES = 500
CI_SLOW_MINUTES = 10
DASHES = re.compile("[–—]")
SKILL_ROOTS = ("skills", ".claude/skills", ".agents/skills", ".cursor/skills")
SKIP_PARTS = {".git", "node_modules", "dist", "build", "vendor", "_archive", ".venv", "venv", "target", "third_party"}
CODE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".go", ".rs", ".swift", ".sh"}
SERVICE_FILES = re.compile(r"^(Dockerfile|Containerfile|compose\.ya?ml|docker-compose\.ya?ml|wrangler\.(toml|jsonc?)|Procfile|fly\.toml)$|\.service$")
SERVER_CODE = re.compile(r"\b(FastAPI\(|Flask\(|aiohttp\.web|serve_forever\(|ThreadingHTTPServer|http\.createServer|express\(\)|"
                         r"Bun\.serve|Deno\.serve|ListenAndServe|axum::|actix_web|export default \{\s*(async )?fetch)")
STRUCTURED = re.compile(
    r"structlog|pythonjsonlogger|jsonlogger|"
    r"json\.dumps\(\{[^}\n]{0,80}[\"'](ts|time|timestamp|event|evt|level|msg)[\"']|"
    r"JSON\.stringify\(\{[^}\n]{0,80}\b(ts|time|event|level|msg)\b|"
    r"\bpino\b|winston\.format\.json|log/slog|zerolog|go\.uber\.org/zap|tracing_subscriber|"
    r"\bos\.Logger\b|Logger\(subsystem:|OSLog\(")


def walk(repo: Path, max_files: int = 20000):
    count = 0
    for root, dirs, files in os.walk(repo):  # sorted, so the cap stops at the same file on every filesystem
        dirs[:] = sorted(d for d in dirs if d not in SKIP_PARTS and not (d.startswith(".") and d not in (".github", ".claude", ".agents", ".cursor")))
        for name in sorted(files):
            count += 1
            if count > max_files:
                return
            yield Path(root) / name


def read(path: Path, limit: int = 400_000) -> str | None:
    try:
        if path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def key(problem: str) -> str:
    """The ratchet key: the problem with its numbers blanked."""
    return re.sub(r"\d+", "N", problem)


# --------------------------------------------------------------------------- checks

def repo_meta(repo: Path, github: str | None) -> dict:
    """Visibility and description: the Actions event payload, else `gh api`, else a private repo."""
    event = os.environ.get("GITHUB_EVENT_PATH")
    if event and Path(event).is_file():
        try:
            data = json.loads(Path(event).read_text()).get("repository") or {}
            if data:
                return data
        except (OSError, ValueError):
            pass
    if github:
        try:
            out = subprocess.run(["gh", "api", f"repos/{github}"], capture_output=True, text=True, timeout=30)
            if out.returncode == 0:
                return json.loads(out.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return {"private": True, "description": "", "full_name": github or repo.name}


def readme_checks(repo: Path, meta: dict) -> list[str]:
    names = {p.name for p in repo.iterdir()}
    readme = next((n for n in sorted(names) if re.match(r"readme(\.|$)", n, re.I)), None)
    public = not meta.get("private", True)
    problems = []
    if public and not any(project_audit.LICENSE_FILE.match(n) for n in names):
        problems.append("no LICENSE (public repo)")
    if readme is None:
        return problems + ["no README"]
    text = read(repo / readme) or ""
    owner, _, name = (meta.get("full_name") or repo.name).rpartition("/")
    profile = name.lower() in project_audit.PROFILE_REPOS or name.lower() == owner.lower()
    kind = "product" if public and not profile else "internal"
    has_ci = (repo / ".github" / "workflows").is_dir()
    return problems + project_audit.readme_problems(text, meta, kind, names, has_ci)


def agent_file_checks(repo: Path) -> list[str]:
    agents = read(repo / "AGENTS.md")
    claude = read(repo / "CLAUDE.md")
    problems = [p for p in project_audit.agents_problems(agents, claude) if "lines (max" not in p]
    if agents is not None and len(agents.splitlines()) > AGENTS_MAX_LINES:
        problems.append(f"AGENTS.md is {len(agents.splitlines())} lines (max {AGENTS_MAX_LINES}, writing-for-agents)")
    if agents is not None:
        stale = project_audit.stale_remote_paths(agents, {p.name for p in repo.iterdir()})
        problems += [f"stale path in AGENTS.md: {path}" for path in stale]
    is_ui = project_audit.looks_like_ui_repo(repo, agents)
    problems += project_audit.ui_and_install_problems(agents, has_design_md=(repo / "DESIGN.md").is_file(), is_ui=is_ui)
    if is_ui and not agents and not (repo / "DESIGN.md").is_file():
        problems.append("UI repo missing DESIGN.md")
    return problems


def skill_files(repo: Path) -> list[Path]:
    seen, found = set(), []
    for base in SKILL_ROOTS:
        root = repo / base
        if not root.is_dir():
            continue
        for manifest in sorted(root.glob("*/SKILL.md")):
            real = manifest.resolve()
            if real in seen or manifest.parent.name.startswith(("_", ".")):
                continue
            seen.add(real)
            found.append(manifest)
    return found


def skill_checks(repo: Path) -> list[str]:
    problems = []
    for manifest in skill_files(repo):
        where = manifest.relative_to(repo).as_posix()
        text = read(manifest) or ""
        meta = skill_rules.frontmatter(text)
        if not skill_rules.NAME.fullmatch(manifest.parent.name):
            problems.append(f"{where}: folder name must be lowercase letters, digits and hyphens")
        if not meta.get("name"):
            problems.append(f"{where}: front matter needs name")
        description = meta.get("description", "")
        if not description:
            problems.append(f"{where}: front matter needs description")
        elif len(description) > 1024:
            problems.append(f"{where}: description is {len(description)} characters (max 1024)")
        lines = len(text.splitlines())
        if lines > SKILL_MAX_LINES:
            problems.append(f"{where}: {lines} lines (max {SKILL_MAX_LINES})")
        for source in re.findall(r"^Source:.*$", text, re.M):
            if not skill_rules.SOURCE_LINE.match(source):
                problems.append(f"{where}: Source line must read `Source: owner/repo path @ <40-hex sha> (License)`")
    return problems


def agent_prose(repo: Path) -> list[Path]:
    files = [repo / n for n in ("AGENTS.md", "CLAUDE.md", "DESIGN.md") if (repo / n).is_file()]
    files += skill_files(repo)
    rules = repo / ".cursor" / "rules"
    if rules.is_dir():
        files += sorted(rules.glob("*.mdc"))
    return files


def dash_checks(repo: Path) -> list[str]:
    problems = []
    for path in agent_prose(repo):
        count = len(DASHES.findall(read(path) or ""))
        if count:
            problems.append(f"{path.relative_to(repo).as_posix()}: {count} em or en dash(es) in agent-facing prose")
    return problems


def services(repo: Path) -> dict[str, list[Path]]:
    """Service name -> its code files. A services/ folder holds one per child; otherwise the repo is one
    service when it ships a unit, container, worker config or server code."""
    groups: dict[str, list[Path]] = {}
    marked: set[str] = set()
    base = repo / "services"
    for path in walk(repo):
        rel = path.relative_to(repo)
        if any(part in ("tests", "test", "__tests__", "fixtures", "examples", "docs") for part in rel.parts[:-1]):
            continue
        name = rel.parts[1] if base.is_dir() and rel.parts[0] == "services" and len(rel.parts) > 2 else "."
        if SERVICE_FILES.search(path.name):
            marked.add(name)
        if path.suffix in CODE_SUFFIXES:
            groups.setdefault(name, []).append(path)
            if name not in marked and SERVER_CODE.search(read(path) or ""):
                marked.add(name)
    return {name: groups.get(name, []) for name in sorted(marked)}


def logging_checks(repo: Path) -> list[str]:
    """One problem per service, so fixing one service never reads as a new problem to the ratchet."""
    found = services(repo)
    bare = [name for name, files in found.items() if not any(STRUCTURED.search(read(f) or "") for f in files)]
    return [f"{'the service' if name == '.' else 'services/' + name} logs no structured lines (JSON or a structured logger)"
            for name in bare]


def secret_allow_checks(repo: Path) -> list[str]:
    return repo_secrets.load_allow(repo)[1]


def ci_minutes(full_name: str, branch: str, token: str | None, runs: int = 10, opener=urllib.request.urlopen) -> float | None:
    """Median minutes of the last successful default-branch runs, per workflow; the slowest workflow wins."""
    if "/" not in full_name:
        return None
    endpoint = f"repos/{full_name}/actions/runs?branch={branch}&status=success&per_page=50"
    try:
        if token:  # in Actions: the job token
            request = urllib.request.Request("https://api.github.com/" + endpoint,
                                             headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            with opener(request, timeout=30) as response:
                data = json.load(response)
        else:  # locally: gh's own sign-in, so this process never holds a token
            done = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, timeout=60)
            if done.returncode != 0:
                return None
            data = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    per: dict[str, list[float]] = {}
    for run in data.get("workflow_runs", []):
        if run.get("event") == "schedule":
            continue
        try:
            start = datetime.datetime.fromisoformat(run["run_started_at"].replace("Z", "+00:00"))
            end = datetime.datetime.fromisoformat(run["updated_at"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        times = per.setdefault(str(run.get("workflow_id")), [])
        if len(times) < runs:
            times.append((end - start).total_seconds() / 60)
    medians = [statistics.median(v) for v in per.values() if v]
    return round(max(medians), 1) if medians else None


def ci_checks(repo: Path, meta: dict) -> tuple[list[str], float | None]:
    if not (repo / ".github" / "workflows").is_dir():
        return ["no CI workflow"], None
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    minutes = ci_minutes(meta.get("full_name", ""), meta.get("default_branch") or "main", token)
    if minutes is not None and minutes > CI_SLOW_MINUTES:
        return [f"CI median is {minutes:.0f} minutes on the default branch (max {CI_SLOW_MINUTES})"], minutes
    return [], minutes


CHECKS = ("readme", "agents", "skills", "dashes", "logging", "secrets", "ci")
# CI time includes queue wait and runner load, so a slow week would fail every PR: report it only.
REPORT_ONLY = ("ci: CI median",)


def grade(repo: Path, github: str | None = None, meta: dict | None = None) -> dict:
    meta = meta or repo_meta(repo, github)
    ci_problems, minutes = ci_checks(repo, meta)
    results = {
        "readme": readme_checks(repo, meta),
        "agents": agent_file_checks(repo),
        "skills": skill_checks(repo),
        "dashes": dash_checks(repo),
        "logging": logging_checks(repo),
        "secrets": secret_allow_checks(repo),
        "ci": ci_problems,
    }
    passed = sum(1 for name in CHECKS if not results[name])
    return {"repo": meta.get("full_name") or repo.name, "visibility": "private" if meta.get("private", True) else "public",
            "checks": results, "passed": passed, "total": len(CHECKS), "ci_minutes": minutes,
            "skills": len(skill_files(repo)), "services": len(services(repo))}


# --------------------------------------------------------------------------- ratchet and output

def problems_of(result: dict) -> list[str]:
    return [f"{name}: {p}" for name in CHECKS for p in result["checks"][name]]


def compare(result: dict, baseline: dict | None) -> tuple[list[str], list[str]]:
    """(new problems, baseline problems now fixed). With no baseline nothing is new."""
    current = problems_of(result)
    if baseline is None:
        return [], []
    old = set(baseline.get("problems", []))
    new = [p for p in current if key(p) not in old and not p.startswith(REPORT_ONLY)]
    have = {key(p) for p in current}
    return new, sorted(k for k in old if k not in have)


def render(result: dict, new: list[str], fixed: list[str], has_baseline: bool) -> str:
    out = [f"{result['repo']} ({result['visibility']}): {result['passed']}/{result['total']} checks pass"
           + (f", CI median {result['ci_minutes']} min" if result["ci_minutes"] is not None else "")]
    for name in CHECKS:
        items = result["checks"][name]
        out.append(f"  {'ok  ' if not items else 'WARN'} {name}")
        out += [f"       {'NEW ' if f'{name}: {p}' in new else ''}{p}" for p in items]
    if not has_baseline:
        out.append(f"No {BASELINE}: report only. Run with --write-baseline to start the ratchet.")
    for k in fixed:
        out.append(f"fixed since the baseline (run --write-baseline to lock it in): {k}")
    if new:
        out.append(f"{len(new)} new problem(s) since the baseline; fix them, or add the key to {BASELINE} in the same pull request and say why.")
    return "\n".join(out)


def markdown(result: dict, new: list[str], fixed: list[str]) -> str:
    rows = ["| Check | Result |", "| --- | --- |"]
    for name in CHECKS:
        items = result["checks"][name]
        cell = "pass" if not items else "<br>".join(("**new:** " if f"{name}: {p}" in new else "") + p.replace("|", "\\|") for p in items)
        rows.append(f"| {name} | {cell} |")
    head = f"### Repo standards: {result['passed']}/{result['total']}\n\n"
    tail = "".join(f"\n- fixed since the baseline: {k}" for k in fixed)
    return head + "\n".join(rows) + "\n" + tail + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--repo", type=Path, default=Path.cwd())
    ap.add_argument("--github", help="owner/name, for visibility and CI time when not running in Actions")
    ap.add_argument("--mode", choices=("ratchet", "report"), default="ratchet")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--markdown", type=Path, help="append a summary table here (GITHUB_STEP_SUMMARY)")
    ap.add_argument("--write-baseline", action="store_true")
    a = ap.parse_args(argv)
    repo = a.repo.resolve()
    if project_audit.has_trading(repo):
        print("error: trading repositories are never read", file=sys.stderr)
        return 2
    result = grade(repo, a.github)
    path = repo / BASELINE
    baseline = None
    if path.is_file():
        try:
            baseline = json.loads(path.read_text())
        except ValueError:
            print(f"error: {BASELINE} is not valid JSON", file=sys.stderr)
            return 2
    if a.write_baseline:
        path.parent.mkdir(parents=True, exist_ok=True)
        keys = sorted({key(p) for p in problems_of(result)})
        path.write_text(json.dumps({"date": datetime.date.today().isoformat(), "problems": keys}, indent=2) + "\n")
        print(f"wrote {BASELINE}: {len(keys)} problem(s) accepted as the floor")
        return 0
    new, fixed = compare(result, baseline)
    if a.json:
        print(json.dumps({**result, "new": new, "fixed": fixed}, indent=2))
    else:
        print(render(result, new, fixed, baseline is not None))
    if a.markdown:
        with open(a.markdown, "a", encoding="utf-8") as fh:
            fh.write(markdown(result, new, fixed))
    return 1 if new and a.mode == "ratchet" else 0


if __name__ == "__main__":
    sys.exit(main())
