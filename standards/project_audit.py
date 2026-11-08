#!/usr/bin/env python3
"""Small lint of Donovan's repositories against docs/PROJECT-STANDARD.md.

    python3 tools/project_audit.py                       # ~/Projects, text report, exit 0
    python3 tools/project_audit.py --root DIR --json
    python3 tools/project_audit.py --strict              # exit 1 when any repo has a problem
    python3 tools/project_audit.py --remote              # every baney75/barnlabs repo on GitHub
    python3 tools/project_audit.py --remote --write-baseline evidence/repo-audit.json

Per git repository directly under the root: AGENTS.md present, under 200 lines and under
Codex's 32 KiB cap; CLAUDE.md is `@AGENTS.md` (plus optional Claude-only notes); backticked
paths in AGENTS.md exist; a check command is named; t3.json, if present, is valid JSON.

Skipped: non-git folders, `_work`, hidden folders, third-party clones (origin owner not in
OWNERS) and anything whose name or resolved path contains "trading", which is never read.
--remote audits each non-archived, non-fork repo of OWNERS from its default branch through
`gh api`, with no checkout: GitHub metadata, README shape, LICENSE, AGENTS.md, CLAUDE.md and repo
skills. Every network call goes through one `fetch(endpoint)` callable (tests pass fixtures);
responses are cached for an hour under ~/.cache/project-audit/. Repos whose name holds
"polymarket", "money-system" or "trading" are never read; sandbox repos are skipped.
Anything heavier belongs to `/doctor prompt-audit` and `skills-ref validate`.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Callable

OWNERS = frozenset({"baney75", "barnlabs"})
MAX_LINES, MAX_BYTES = 200, 32 * 1024
SPAN = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
OWNER_IN_URL = re.compile(r"[:/]([^/:]+)/[^/:]+?(?:\.git)?/?$")
# Spans that look like paths but aren't: git refs, MIME types.
NOT_A_PATH = re.compile(r"^(origin|upstream|refs|application|text|image|audio|video)/", re.I)
EXTENSIONS = frozenset("md mdc txt json toml yaml yml py ts tsx js mjs sh html css swift go rs "
                       "sql lock plist xml".split())
CHECK_LABEL = re.compile(r"^\s*(?:[-*+]\s*)?\**(checks?|tests?|verify)\**\s*:", re.I)
CHECK_VERB = re.compile(r"(?<![a-z])(test|tests|check|pytest|unittest|typecheck|lint|clippy|"
                        r"vitest|jest)(?![a-z])", re.I)
NOT_RUNNERS = frozenset("git cd rm mkdir ls cat cp mv echo open grep find".split())
SKIP_DIRS = frozenset({".git", "node_modules", "dist"})
SHELL_FENCES = frozenset({"sh", "bash", "zsh", "shell", "console", "shell-session"})
CHECK_TOOLS = frozenset({"make", "just", "tox", "nox", "ctest"})



DESIGN_READ_LINE = "Read DESIGN.md before any visible UI"
# Real frozen/ci installers. Negated flags (--no-frozen-lockfile) must not match.
FROZEN_CMD = re.compile(
    r"(?:^|[^\w-])(?:"
    r"npm\s+ci\b|"
    r"pnpm\s+(?:i|install)\s+[^\n]*--frozen-lockfile\b|"
    r"bun\s+install\s+[^\n]*--frozen-lockfile\b|"
    r"yarn\s+(?:install\s+)?--frozen-lockfile\b|"
    r"uv\s+sync\s+[^\n]*--frozen\b|"
    r"cargo\s+[^\n]*--locked\b"
    r")",
    re.I,
)
NEGATED_FREEZE = re.compile(
    r"--no-frozen-lockfile\b|--no-frozen\b|without\s+frozen|not\s+frozen",
    re.I,
)
LOOSE_CMD = re.compile(
    r"(?:^|[^\w-])(?:npm\s+install\b|pnpm\s+(?:i|install)\b|bun\s+install\b|yarn\s+install\b)",
    re.I,
)


def looks_like_ui_repo(repo: Path, agents_text: str | None) -> bool:
    """Product/site UI repos: DESIGN.md when Brand exists, DESIGN.md present, or a web app."""
    if (repo / "DESIGN.md").is_file():
        return True
    if agents_text and re.search(r"^## Brand\b", agents_text, re.M):
        return True
    web_markers = (
        "index.html",
        "astro.config.mjs",
        "astro.config.ts",
        "vite.config.ts",
        "vite.config.js",
        "next.config.js",
        "next.config.mjs",
        "next.config.ts",
    )
    if any((repo / name).is_file() for name in web_markers):
        return True
    return False


def brand_requires_design_read(agents_text: str) -> bool:
    section = re.search(r"^## Brand\s*\n([\s\S]*?)(?=\n## |\Z)", agents_text, re.M)
    if not section:
        return False
    body = section.group(1)
    return DESIGN_READ_LINE in body or "Read `DESIGN.md` before any visible UI" in body


def install_line(agents_text: str) -> str | None:
    m = re.search(r"^\s*[-*]\s*Install:\s*(.+)$", agents_text, re.M | re.I)
    return m.group(1).strip() if m else None


def install_is_locked(agents_text: str) -> bool | None:
    """True locked, False loose/TODO, None if no Install guidance (ok for dep-free repos)."""
    line = install_line(agents_text)
    if line is None:
        return None
    # Strip backticks and shell comments so `# --frozen-lockfile later` cannot fake a lock.
    raw = line.replace("`", "")
    if "#" in raw:
        raw = raw.split("#", 1)[0].rstrip()
    if raw.upper().startswith("TODO") or raw.upper() == "NONE" or raw.lower() in ("n/a", "na", "-"):
        # Explicit none / N/A means no install step (dep-free); TODO is unfinished
        if raw.upper().startswith("TODO"):
            return False
        return None
    if NEGATED_FREEZE.search(raw):
        return False
    if FROZEN_CMD.search(raw):
        return True
    if LOOSE_CMD.search(raw):
        return False
    # Mention of "frozen"/"locked" alone is not enough
    return False


def ui_and_install_problems(
    agents_text: str | None,
    *,
    has_design_md: bool,
    is_ui: bool,
) -> list[str]:
    """Shared DESIGN.md / Brand / locked-install checks for local and remote audits."""
    problems: list[str] = []
    if not agents_text:
        return problems
    if is_ui:
        if not has_design_md:
            problems.append("UI repo missing DESIGN.md")
        elif not brand_requires_design_read(agents_text):
            problems.append(
                "Brand section must say: Read DESIGN.md before any visible UI"
            )
    locked = install_is_locked(agents_text)
    if locked is False:
        problems.append(
            "Install must use a locked or frozen installer "
            "(npm ci, --frozen-lockfile, uv sync --frozen, cargo --locked)"
        )
    return problems


def has_trading(path: Path) -> bool:
    """Whether the path is, or leads through a symlink into, a trading folder.

    Checks the written parts first, with no filesystem access. Then it resolves one part at a
    time with lstat and readlink, and stops before it looks up any part whose name contains
    "trading", so nothing inside a trading folder is ever touched. Too many links count as
    trading.
    """
    path = Path(path)
    if not path.is_absolute():
        path = Path(os.getcwd()) / path
    if any("trading" in part.lower() for part in path.parts):
        return True
    pending = list(reversed(path.parts[1:]))
    current, hops = Path(path.anchor), 0
    while pending:
        part = pending.pop()
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        if "trading" in part.lower():
            return True
        candidate = current / part
        try:
            mode = os.lstat(candidate).st_mode
        except OSError:
            current = candidate  # missing: the rest is checked by name only
            continue
        if not stat.S_ISLNK(mode):
            current = candidate
            continue
        hops += 1
        try:
            target = Path(os.readlink(candidate))
        except OSError:
            return True
        if hops > 40:
            return True
        if target.is_absolute():
            current = Path(target.anchor)
            pending.extend(reversed(target.parts[1:]))
        else:
            pending.extend(reversed(target.parts))
    return False


def exists(path: Path) -> bool | None:
    """exists() that never raises and never looks inside trading; None when unknown."""
    if has_trading(path):
        return None
    try:
        return path.exists()
    except OSError:
        return None


def read(path: Path) -> str | None:
    if has_trading(path):
        return None
    try:
        return path.read_text(errors="replace") if path.is_file() else None
    except OSError:
        return None


def origin_owner(repo: Path) -> str | None:
    config = read(repo / ".git" / "config") or ""
    url = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', config)
    if not url or "://" not in url.group(1) and ":" not in url.group(1).split("/")[0]:
        return None  # no origin, or a local-path clone
    match = OWNER_IN_URL.search(url.group(1))
    return match.group(1).lower() if match else "unknown"


def skip_reason(child: Path) -> str | None:
    if has_trading(child):
        return "trading"
    if child.name == "_work" or child.name.startswith("."):
        return "work-or-hidden"
    if not child.is_dir():
        return "not-a-directory"
    if not exists(child / ".git"):
        return "not-git"
    owner = origin_owner(child)
    return f"third-party ({owner})" if owner and owner not in OWNERS else None


def repo_names(repo: Path) -> set[str]:
    """Every file and folder name in the repo. Never follows symlinks, never enters .git,
    node_modules, dist or a trading folder."""
    names: set[str] = set()
    stack = [repo]
    while stack:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            if "trading" in entry.name.lower():
                continue
            names.add(entry.name)
            if entry.name not in SKIP_DIRS and entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
    return names


def git_ignored(repo: Path, span: str) -> bool:
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        done = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--", span],
                              capture_output=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def prose_lines(text: str) -> list[str]:
    """AGENTS.md lines outside fenced code blocks."""
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            out.append(line)
    return out


def looks_like_path(span: str) -> bool:
    if re.search(r"\s|://|[*?\[\]{}<>|;=()\"':$@]", span) or span.startswith(("-", "/", "#")):
        return False  # commands, URLs, globs, placeholders, flags, absolute paths
    if NOT_A_PATH.match(span) or span.split("/")[0].lower() in OWNERS:
        return False
    if "/" in span:
        return True  # stale_paths still asks for an extension, a trailing / or a real head
    return has_extension(span)


def has_extension(span: str) -> bool:
    last = span.rstrip("/").rsplit("/", 1)[-1]
    return "." in last.lstrip(".") and last.rpartition(".")[2].lower() in EXTENSIONS


def names_other_repo(line: str) -> bool:
    """A line that points at another repository (`baney75/dbaney.com`, a GitHub URL): the
    paths on it belong to that repo, not this one."""
    return "github.com/" in line or any(
        span.split("/")[0].lower() in OWNERS and span.count("/") == 1 for span in SPAN.findall(line))


def stale_paths(text: str, repo: Path, home: Path) -> list[str]:
    """Backticked paths that resolve to nothing: ~/ against home, the rest against the repo.

    A span with a slash counts as a path only with a file extension, a trailing `/` or a first
    folder that exists, so `and/or` and `TCP/IP` don't. A bare file name also passes when
    that name exists anywhere in the repo. Gitignored paths, node_modules and dist, and paths
    on a line that names another repo are skipped.
    """
    stale: list[str] = []
    names: set[str] | None = None
    for line in prose_lines(text):
        if names_other_repo(line):
            continue
        for span in (s.strip() for s in SPAN.findall(line)):
            if span in stale or not looks_like_path(span):
                continue
            base, rel = (home, span[2:]) if span.startswith("~/") else (repo, span)
            parts = [p for p in rel.split("/") if p]
            if not parts or any(p in SKIP_DIRS for p in parts):
                continue
            if has_trading(base / rel):
                continue  # never look inside a trading folder
            if "/" in rel.rstrip("/") and not (has_extension(rel) or span.endswith("/")
                                               or exists(base / parts[0])):
                continue  # `and/or`, `TCP/IP`: not a path
            if exists(base / rel) is not False:
                continue
            if "/" not in rel.rstrip("/") and base is repo:
                if names is None:
                    names = repo_names(repo)
                if rel.rstrip("/") in names:
                    continue
            if base is repo and git_ignored(repo, rel):
                continue
            stale.append(span)
    return stale


def runs_check(command: str) -> bool:
    """`npm test`, `pytest`, `npm ci && npm test`; not `git commit --no-verify`."""
    for part in re.split(r"&&|\|\||;", command):
        words = [w for w in part.split() if not w.startswith("-")]
        if not words or words[0] in NOT_RUNNERS or not re.match(r"\./|[a-z]", words[0]):
            continue  # not a command: git/cd/rm, or log output like "3 tests passed"
        if "/" in words[0] and not words[0].startswith("./"):
            continue  # a path such as `tests/`, not a command
        if any(CHECK_VERB.search(w) for w in words[:3]):
            return True
    return False


def is_script(span: str) -> bool:
    first = span.split()[0] if span.split() else ""
    return first.startswith(("./", "bin/", "scripts/")) or first.endswith(".sh")


def states_check(text: str) -> bool:
    """Whether AGENTS.md names a check.

    Counts a backticked command that runs a check (`npm test`, `pytest`); on a `Check:` or
    `Test:` line, also a check script (`./check.sh`) or a bare check tool (`make`). A build
    (`swift build`) never counts. In a fenced block only real commands count: lines in a
    sh/bash fence, or lines after a `$ ` prompt.
    """
    fence: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            fence = None if fence is not None else stripped[3:].strip().lower()
            continue
        if fence is not None:
            if stripped.startswith("$ "):
                command = stripped[2:]
            elif fence.split()[:1] and fence.split()[0] in SHELL_FENCES:
                command = stripped
            else:
                continue
            if runs_check(command):
                return True
            continue
        spans = [s.strip() for s in SPAN.findall(line) if s.strip()]
        if any(runs_check(s) for s in spans):
            return True
        if CHECK_LABEL.match(line) and any(is_script(s) or s in CHECK_TOOLS for s in spans):
            return True
    return False




def git_out(repo: Path, *args: str) -> str | None:
    """git's stdout, or None. Drops every GIT_* variable (a hook's GIT_DIR, GIT_NAMESPACE or
    -c config can't stand in) and stops git from climbing into a parent repo."""
    real = repo.resolve()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CEILING_DIRECTORIES=str(real.parent), GIT_CONFIG_NOSYSTEM="1")
    try:
        done = subprocess.run(["git", "-C", str(real), *args], capture_output=True, text=True,
                              errors="replace", timeout=10, env=env)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def gitdir_in_trading(repo: Path) -> bool:
    """True when .git leads into a trading folder, through symlinks, a gitfile or a worktree's
    commondir, or can't be resolved. Uses no git call. Alternates and symlinks inside the
    gitdir are not followed."""
    dot_git = repo / ".git"
    try:
        gitdir = dot_git.resolve(strict=True)
        if has_trading(gitdir):
            return True  # checked before reading a gitfile that lives there
        if dot_git.is_file():
            line = dot_git.read_text(errors="replace").strip()
            if not line.startswith("gitdir:"):
                return True
            gitdir = (repo / line[7:].strip()).resolve()
        if has_trading(gitdir):
            return True
        commondir = gitdir / "commondir"
        if commondir.is_file():
            return has_trading((gitdir / commondir.read_text(errors="replace").strip()).resolve())
    except (OSError, RuntimeError):
        return True
    return False


STALE_TREE = "; this lint reads that tree"


def checkout_note(repo: Path) -> str | None:
    """Why this lint may not describe the default branch: the checkout is on another branch or
    behind origin's. Local refs only (no fetch), so origin may be newer still."""
    if gitdir_in_trading(repo) or git_out(repo, "remote", "get-url", "origin") is None:
        return None  # trading, unreadable, or no origin to compare with
    prefix = "refs/remotes/origin/"
    resolves = lambda ref: git_out(repo, "rev-parse", "--verify", "-q", ref) is not None
    target = git_out(repo, "symbolic-ref", "-q", "refs/remotes/origin/HEAD") or ""
    if not (target.startswith(prefix) and resolves(target)):  # missing, odd or dangling
        target = next((prefix + b for b in ("main", "master") if resolves(prefix + b)), "")
    if not target:
        return "origin's default branch is unknown locally (git remote set-head origin -a)"
    default = target[len(prefix):]
    if not resolves("HEAD"):
        return f"checkout has no commits; origin/{default} does"
    head = git_out(repo, "symbolic-ref", "-q", "HEAD")
    if head is None:
        if git_out(repo, "rev-parse", "HEAD") != git_out(repo, "rev-parse", target):
            return f"checkout is a detached HEAD, not origin/{default}" + STALE_TREE
    elif head != "refs/heads/" + default:
        return f"checkout is on {head.removeprefix('refs/heads/')}, not origin/{default}" + STALE_TREE
    behind = git_out(repo, "rev-list", "--count", f"HEAD..{target}")
    if behind not in (None, "0"):
        return f"checkout is {behind} commit(s) behind origin/{default}" + STALE_TREE
    return None


def claude_shape(repo: Path, agents_text: str | None) -> str:
    return claude_text_shape(read(repo / "CLAUDE.md"), agents_text)


def claude_text_shape(text: str | None, agents_text: str | None) -> str:
    if text is None:
        return "none"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if agents_text:
        long_lines = {l.strip() for l in agents_text.splitlines() if len(l.strip()) > 20}
        shared = sum(1 for l in lines if l in long_lines)
        if long_lines and shared >= max(3, len(long_lines) // 3):
            return "copy"
    if lines[:1] == ["@AGENTS.md"] and lines[1:2] in ([], ["## Claude-only notes"]):
        return "import"
    return "other"


def t3_state(repo: Path) -> str:
    text = read(repo / "t3.json")
    if text is None:
        return "missing"
    try:
        return "valid" if isinstance(json.loads(text), dict) else "invalid"
    except ValueError:
        return "invalid"


def audit_repo(repo: Path, home: Path) -> dict:
    text = read(repo / "AGENTS.md")
    report = {
        "repo": repo.name,
        "agents_md": text is not None,
        "agents_lines": len(text.splitlines()) if text else 0,
        "agents_bytes": len(text.encode()) if text else 0,
        "claude_md": claude_shape(repo, text),
        "stale_paths": stale_paths(text, repo, home) if text else [],
        "check_stated": states_check(text) if text else False,
        "t3_json": t3_state(repo),
        "checkout": checkout_note(repo),
    }
    problems = []
    if text is None:
        problems.append("no AGENTS.md")
    else:
        if report["agents_lines"] > MAX_LINES:
            problems.append(f"AGENTS.md is {report['agents_lines']} lines (max {MAX_LINES})")
        if report["agents_bytes"] > MAX_BYTES:
            problems.append(f"AGENTS.md is {report['agents_bytes']} bytes (Codex cap {MAX_BYTES})")
        if report["stale_paths"]:
            problems.append(f"{len(report['stale_paths'])} stale path(s) in AGENTS.md")
        if not report["check_stated"]:
            problems.append("no check command named")

        problems.extend(
            ui_and_install_problems(
                text,
                has_design_md=(repo / "DESIGN.md").is_file(),
                is_ui=looks_like_ui_repo(repo, text),
            )
        )
    if report["claude_md"] != "import":
        problems.append(f"CLAUDE.md is {report['claude_md']}, not an @AGENTS.md import")
    if report["t3_json"] == "invalid":
        problems.append("t3.json is not valid JSON")
    report["problems"] = problems
    return report


def audit(root: Path, home: Path | None = None) -> dict:
    home = home or Path.home()
    repos, skipped = [], []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        reason = skip_reason(child)
        if reason is None:
            repos.append(audit_repo(child, home))
        elif reason != "not-a-directory":
            skipped.append({"name": child.name, "reason": reason})
    return {"root": str(root), "repos": repos, "skipped": skipped}


# --------------------------------------------------------------------------- remote mode

CACHE_DIR = Path("~/.cache/project-audit")
CACHE_TTL = 3600
NEVER_READ = ("polymarket", "money-system", "trading")
# Calibrated on the exemplars (shotate, baneyspaces, tabkyra, 2026-10-28): long feature READMEs with
# screenshots run past 400 lines and still read well; GitHub allows 20 topics.
README_MAX_LINES = 500
PLACEHOLDER_USERS = (r"(?i:you|me|user|username|your[-_]?(?:user)?name|name|example|<[^>/]+>|\$USER|\$\{?HOME\}?"
                     r"|\.\.\.|\{[^}/]+\})")
# A home path starts a path: not inside a URL or a longer path (localhost:3000/home/dashboard).
REAL_HOME = re.compile(r"(?<![\w.:/-])/(?:Users|home)/(?!" + PLACEHOLDER_USERS + r"(?:/|\b))[A-Za-z0-9._-]+(?:/|\b)")
# Any MagicDNS (tailnet) host name is a leak unless its tailnet label is a teaching placeholder. Tailnets come as
# tail<hex> or as word names, so the rule lists placeholders rather than guessing the real shape.
# Labels are words, <placeholders> or an ellipsis, so markup around a name (<code>host...) isn't part of it.
TS_NET = re.compile(r"((?:[A-Za-z0-9_-]+|<[^<>\s]+>|\.\.\.)(?:\.(?:[A-Za-z0-9_-]+|<[^<>\s]+>))*)\.ts\.net\b")
PLACEHOLDER_TAILNETS = {"tail1234", "tail0000", "tailxxxx", "tailabcd", "example", "tailnet", "tailnet-name",
                        "your-tailnet", "your-tailnet-name", "my-tailnet"}


def real_tailnet(text: str) -> bool:
    for m in TS_NET.finditer(text):
        label = m.group(1).rsplit(".", 1)[-1].lower()
        if label.startswith("<") or label in ("", "...") or label in PLACEHOLDER_TAILNETS:
            continue
        return True
    return False
STOPWORDS = set("a an and the for of to in on with your you it its is are by or from that this as at be".split())


def description_echoed(description: str, opening: str) -> bool:
    """The README's first 30 lines (alt text included) carry most of the description's words: the one-line
    summary and the GitHub description say the same thing, not necessarily verbatim."""
    words = [w for w in re.findall(r"[a-z0-9]+", description.lower()) if w not in STOPWORDS][:10]
    if not words:
        return True
    have = set(re.findall(r"[a-z0-9]+", opening.lower()))
    return sum(w in have for w in words) >= 0.6 * len(words)


THROWAWAY = re.compile(r"\b(sandbox|throwaway|throw-away|scratch)\b", re.I)
PROFILE_REPOS = frozenset({".github"})
INSTALL_HEADING = re.compile(r"^#{2,3}\s*(install|get started|getting started|quick ?start)\b", re.I | re.M)
LICENSE_HEADING = re.compile(r"^#{2,}\s*licen[sc]e\b", re.I | re.M)
LICENSE_LINE = re.compile(r"\blicen[sc]ed\s+under\b|^\s*licen[sc]e\s*:", re.I | re.M)
IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)|<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)", re.I)
BADGE_URL = re.compile(r"shields\.io|badge|badgen|/actions/workflows/", re.I)
HTML_TAG = re.compile(r"<[^>]+>")
FENCE_OPEN = re.compile(r"^\s*```\s*(\w+)?")
LICENSE_FILE = re.compile(r"^(licen[sc]e|copying|unlicense)(\.|$)", re.I)


class FetchError(RuntimeError):
    """A network or gh failure that is not a plain 404."""


Fetch = Callable[[str], object]  # endpoint -> parsed JSON; None when GitHub says 404


def gh_fetch(endpoint: str) -> object:
    """One `gh api` call. Paginated list endpoints (per_page in the query) return one flat list."""
    cmd = ["gh", "api", endpoint]
    if "per_page=" in endpoint:
        cmd[2:2] = ["--paginate", "--slurp"]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise FetchError(f"gh api {endpoint}: {exc}") from exc
    if done.returncode != 0:
        if "HTTP 404" in done.stderr or '"status":"404"' in done.stdout.replace(" ", ""):
            return None
        raise FetchError(f"gh api {endpoint}: {done.stderr.strip() or done.stdout.strip()[:200]}")
    try:
        data = json.loads(done.stdout)
    except ValueError as exc:
        raise FetchError(f"gh api {endpoint}: not JSON") from exc
    if "--slurp" in cmd and isinstance(data, list):
        data = [item for page in data for item in (page if isinstance(page, list) else [page])]
    return data


class CachedFetch:
    """Wraps a fetch with a one-hour file cache keyed by endpoint. 404s are cached too."""

    def __init__(self, inner: Fetch, directory: Path = CACHE_DIR, ttl: int = CACHE_TTL,
                 clock: Callable[[], float] = time.time):
        self.inner, self.ttl, self.clock = inner, ttl, clock
        self.directory = Path(directory).expanduser()

    def __call__(self, endpoint: str) -> object:
        path = self.directory / (hashlib.sha256(endpoint.encode()).hexdigest() + ".json")
        try:
            entry = json.loads(path.read_text())
            if self.clock() - entry["time"] < self.ttl:
                return entry["data"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        data = self.inner(endpoint)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"time": self.clock(), "data": data}))
            path.chmod(0o600)  # private repos' file contents are cached here
        except OSError:
            pass
        return data


def list_repos(fetch: Fetch, owners=sorted(OWNERS)) -> list[dict]:
    """Repo records of every owner. `user/repos` covers the signed-in user's private repos."""
    seen: dict[str, dict] = {}
    for owner in owners:
        data = fetch(f"orgs/{owner}/repos?type=all&per_page=100")
        if data is None:
            data = fetch(f"user/repos?affiliation=owner&per_page=100")
            data = [r for r in data or [] if r.get("owner", {}).get("login", "").lower() == owner]
        for repo in data or []:
            seen[repo["full_name"].lower()] = repo
    return sorted(seen.values(), key=lambda r: r["full_name"].lower())


def remote_skip_reason(repo: dict, fetch: Fetch | None = None) -> str | None:
    name = repo["name"].lower()
    if any(word in name for word in NEVER_READ):
        return "never-read"
    if repo.get("archived"):
        return "archived"
    if repo.get("fork") and not own_fork(repo, fetch):
        return "fork"
    if THROWAWAY.search(repo.get("description") or ""):
        return "sandbox"
    return None


def own_fork(repo: dict, fetch: Fetch | None) -> bool:
    """A fork that has become Donovan's own project (tabkyra): its description differs from its upstream's.
    A plain mirror keeps the upstream description and is skipped."""
    if fetch is None:
        return False
    try:
        full = fetch("repos/" + repo["full_name"]) or {}
    except FetchError:
        return False
    parent = (full.get("parent") or {}) if isinstance(full, dict) else {}
    mine, theirs = (repo.get("description") or "").strip(), (parent.get("description") or "").strip()
    return bool(mine) and mine != theirs


def quoted(path: str) -> str:
    return urllib.parse.quote(path)


def listing(fetch: Fetch, full: str, path: str = "") -> list[dict] | None:
    data = fetch(f"repos/{full}/contents/{quoted(path)}")
    return data if isinstance(data, list) else None


def file_text(fetch: Fetch, full: str, path: str) -> str | None:
    data = fetch(f"repos/{full}/contents/{quoted(path)}")
    if not isinstance(data, dict) or data.get("type", "file") != "file":
        return None
    content = data.get("content")
    if isinstance(content, str) and data.get("encoding", "base64") == "base64":
        return base64.b64decode(content).decode("utf-8", errors="replace")
    return content if isinstance(content, str) else ""


def frontmatter_of(text: str) -> dict[str, str]:
    """tools/skills.py's parser when it loads (by path, so a repo-root skills/ can't shadow it)."""
    try:
        spec = importlib.util.spec_from_file_location("_skills_parser", Path(__file__).with_name("skills.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("_skills_parser", module)
        spec.loader.exec_module(module)
        return module.frontmatter(text)
    except Exception:  # noqa: BLE001 - any failure falls back to the minimal parse
        pass
    if not text.startswith("---\n") or text.find("\n---", 4) < 0:
        return {}
    block = text[4:text.find("\n---", 4)]
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$", block, re.M)}


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def fence_blocks(text: str) -> list[str]:
    """Language tags of the fenced blocks in a README ('' for a bare fence)."""
    tags, fenced = [], False
    for line in text.splitlines():
        match = FENCE_OPEN.match(line)
        if match:
            if not fenced:
                tags.append((match.group(1) or "").lower())
            fenced = not fenced
    return tags


def purpose_line(lines: list[str]) -> bool:
    """Prose under the title (from the top when there is none): a line of 4+ words that is not a
    heading, image, badge or HTML wrapper, found before the next heading."""
    title = next((i for i, l in enumerate(lines) if re.match(r"#\s|\s*<h1\b", l)), -1)
    for line in lines[title + 1:title + 40]:
        if line.startswith("#"):
            return False
        plain = HTML_TAG.sub("", IMAGE.sub("", line))
        plain = re.sub(r"\[\s*\]\([^)]*\)|\[!?\]|^[>\s*_-]+", "", plain).strip()
        if len(plain.split()) >= 4 and not plain.startswith(("|", "```")):
            return True
    return False


def readme_problems(text: str, repo: dict, kind: str, root_names: set[str], has_ci: bool) -> list[str]:
    problems: list[str] = []
    lines = text.splitlines()
    public = not repo.get("private")
    head = "\n".join(lines[:30])
    if not any(re.match(r"#\s|\s*<h1\b", l) for l in lines):
        problems.append("README has no title")
    if kind == "product":
        if not IMAGE.search(head):
            problems.append("README has no image in the first 30 lines")
        if not purpose_line(lines):
            problems.append("README has no one-line summary under the title")
        if has_ci and not any(BADGE_URL.search(m.group(1) or m.group(2) or "") for m in IMAGE.finditer(head)):
            problems.append("README has no badges row (public repo with CI)")
        if not INSTALL_HEADING.search(text):
            problems.append("README has no ## Install (or Get started/Quick start) heading")
        if not (LICENSE_HEADING.search(text) or LICENSE_LINE.search(text)):
            problems.append("README has no ## License heading or license line")
        if len(lines) > README_MAX_LINES:
            problems.append(f"README is {len(lines)} lines (max {README_MAX_LINES})")
        if not description_echoed(repo.get("description") or "", "\n".join(lines[:30])):
            problems.append("README's opening doesn't say what the GitHub description says")
    else:
        if not purpose_line(lines):
            problems.append("README has no purpose line under the title")
        if not ({"sh", "bash", "zsh", "shell", "console"} & set(fence_blocks(text))
                or re.search(r"\b(check|run|install)\b", text, re.I)):
            problems.append("README doesn't say how to run or check")
    if re.search(r"\bTODO\b", text):
        problems.append("README still has TODO markers")
    # A leak is a real home or tailnet name. Teaching placeholders (/Users/you, a tail1234 tailnet label,
    # an ellipsis host) are how a README explains setup, so they pass.
    if REAL_HOME.search(text):
        problems.append("README has a real home path")
    if public and real_tailnet(text):
        problems.append("public README names a real tailnet host")
    broken = []
    for match in IMAGE.finditer(text):
        src = (match.group(1) or match.group(2) or "").split("#")[0].split("?")[0]
        if not src or re.match(r"[a-z][a-z0-9+.-]*:|//|data:", src, re.I) or src.startswith("$"):
            continue
        first = [p for p in src.lstrip("/").split("/") if p not in ("", ".")][:1]
        if first and first[0] not in root_names and first[0] != "..":
            broken.append(src)
    if broken:
        problems.append(f"README image path(s) don't resolve: {', '.join(sorted(set(broken))[:3])}")
    return problems


def agents_problems(agents: str | None, claude: str | None) -> list[str]:
    problems: list[str] = []
    if agents is None:
        problems.append("no AGENTS.md")
    else:
        lines, size = len(agents.splitlines()), len(agents.encode())
        if lines > MAX_LINES:
            problems.append(f"AGENTS.md is {lines} lines (max {MAX_LINES})")
        if size > MAX_BYTES:
            problems.append(f"AGENTS.md is {size} bytes (Codex cap {MAX_BYTES})")
        if not states_check(agents):
            problems.append("no check command named")
    shape = claude_text_shape(claude, agents)
    if shape != "import":
        problems.append(f"CLAUDE.md is {shape}, not an @AGENTS.md import")
    return problems


def stale_remote_paths(agents: str, root_names: set[str]) -> list[str]:
    """Backticked repo-relative paths whose first folder is missing from the root listing."""
    stale: list[str] = []
    for line in prose_lines(agents):
        if names_other_repo(line):
            continue
        for span in (s.strip() for s in SPAN.findall(line)):
            if "/" not in span or span.startswith("~") or not looks_like_path(span):
                continue
            span = span.removeprefix("./")
            if "/" not in span or (span.count("/") == 1 and span.endswith("/")):
                continue  # `inference/` may name a nested folder; only deeper spans are checked
            parts = [p for p in span.split("/") if p]
            if parts and parts[0] not in root_names and parts[0] not in SKIP_DIRS and span not in stale \
                    and (has_extension(span) or span.endswith("/")) and not git_ignorable(parts[0]):
                stale.append(span)
    return stale


def git_ignorable(first: str) -> bool:
    return first in {"build", "out", "coverage", "target", "venv", ".venv", "__pycache__", ".next"}


def skill_problems(fetch: Fetch, full: str) -> list[str]:
    problems: list[str] = []
    for base in (".claude/skills", "skills"):
        for entry in listing(fetch, full, base) or []:
            if entry.get("type") != "dir" or entry["name"].startswith(("_", ".")):
                continue
            if any(word in entry["name"].lower() for word in NEVER_READ):
                continue  # trading material is never read, wherever it sits
            where = f"{base}/{entry['name']}/SKILL.md"
            text = file_text(fetch, full, where)
            if text is None:
                problems.append(f"{where} is missing")
                continue
            meta = frontmatter_of(text)
            for key in ("name", "description"):
                if not meta.get(key):
                    problems.append(f"{where} front matter needs {key}")
    return problems


def audit_remote_repo(repo: dict, fetch: Fetch) -> dict:
    full = repo["full_name"]
    public = not repo.get("private")
    profile = repo["name"].lower() in PROFILE_REPOS or repo["name"].lower() == full.split("/")[0].lower()
    kind = "product" if public and not profile else "internal"
    problems: list[str] = []
    if not (repo.get("description") or "").strip():
        problems.append("no GitHub description")
    topics = repo.get("topics") or []
    if not 3 <= len(topics) <= 20:
        problems.append(f"{len(topics)} topics (want 3 to 20)")
    entries = listing(fetch, full)
    root = {e["name"]: e for e in entries or []}
    if public and not (repo.get("license") or {}).get("spdx_id") \
            and not any(LICENSE_FILE.match(n) for n in root):
        problems.append("no LICENSE")
    elif public and not (repo.get("license") or {}).get("spdx_id"):
        problems.append("LICENSE not recognized by GitHub")
    if repo.get("default_branch") != "main":
        problems.append(f"default branch is {repo.get('default_branch')}, not main")
    if entries is None:
        problems.append("repository has no files")
        return {"repo": full, "kind": kind, "visibility": "public" if public else "private",
                "problems": problems}
    names = set(root)
    readme = next((n for n in sorted(root) if re.match(r"readme(\.|$)", n, re.I)), None)
    text = file_text(fetch, full, readme) if readme else None
    if text is None:
        problems.append("no README")
    else:
        has_ci = bool(listing(fetch, full, ".github/workflows"))
        problems += readme_problems(text, repo, kind, names, has_ci)
    agents = file_text(fetch, full, "AGENTS.md") if "AGENTS.md" in names else None
    claude = file_text(fetch, full, "CLAUDE.md") if "CLAUDE.md" in names else None
    problems += agents_problems(agents, claude)
    if agents:
        stale = stale_remote_paths(agents, names)
        if stale:
            problems.append(f"{len(stale)} stale path(s) in AGENTS.md: {', '.join(stale[:3])}")
    remote_ui = (
        "DESIGN.md" in names
        or bool(agents and re.search(r"^## Brand\b", agents, re.M))
        or bool(names & {
            "index.html", "astro.config.mjs", "astro.config.ts",
            "vite.config.ts", "vite.config.js",
            "next.config.js", "next.config.mjs", "next.config.ts",
        })
    )
    problems += ui_and_install_problems(
        agents,
        has_design_md="DESIGN.md" in names,
        is_ui=remote_ui,
    )
    if "t3.json" in names:
        try:
            if not isinstance(json.loads(file_text(fetch, full, "t3.json") or ""), dict):
                raise ValueError
        except ValueError:
            problems.append("t3.json is not valid JSON")
    problems += skill_problems(fetch, full)
    return {"repo": full, "kind": kind, "visibility": "public" if public else "private",
            "problems": problems}


def audit_remote(fetch: Fetch) -> dict:
    repos, skipped = [], []
    for repo in list_repos(fetch):
        reason = remote_skip_reason(repo, fetch)
        if reason is None:
            repos.append(audit_remote_repo(repo, fetch))
        else:
            skipped.append({"name": repo["full_name"], "reason": reason})
    return {"repos": repos, "skipped": skipped}


def render_remote(result: dict) -> str:
    out = []
    for r in result["repos"]:
        head = f"{'WARN' if r['problems'] else 'OK  '} {r['repo']} ({r['kind']}, {r['visibility']})"
        out.append(head + "".join(f"\n       {p}" for p in r["problems"]))
    clean = sum(1 for r in result["repos"] if not r["problems"])
    out.append(f"\n{len(result['repos'])} repos audited, {clean} clean, {len(result['skipped'])} skipped.")
    return "\n".join(out)


def baseline(result: dict, today: datetime.date | None = None) -> dict:
    repos = result["repos"]
    return {"date": (today or datetime.date.today()).isoformat(),
            "clean": sum(1 for r in repos if not r["problems"]), "audited": len(repos),
            "repos": {r["repo"]: r["problems"] for r in repos}}


def run_remote(args, fetch: Fetch | None = None) -> int:
    fetch = fetch or (gh_fetch if args.no_cache else CachedFetch(gh_fetch))
    try:
        result = audit_remote(fetch)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.write_baseline:
        Path(args.write_baseline).write_text(json.dumps(baseline(result), indent=2) + "\n")
    print(json.dumps(result["repos"], indent=2) if args.json else render_remote(result))
    return 1 if args.strict and any(r["problems"] for r in result["repos"]) else 0


def render(result: dict) -> str:
    repos = result["repos"]
    out = [f"{'WARN' if r['problems'] else 'OK  '} {r['repo']}: "
           + ("; ".join(r["problems"]) or "meets the standard")
           + (f"\n       note: {r['checkout']}" if r.get("checkout") else "")
           + "".join(f"\n       stale: {s}" for s in r["stale_paths"]) for r in repos]
    clean = sum(1 for r in repos if not r["problems"])
    out.append(f"\n{len(repos)} repos linted, {clean} clean, {len(result['skipped'])} skipped.")
    return "\n".join(out)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=os.path.expanduser("~/Projects"))
    parser.add_argument("--home", default=None, help="resolve ~/ paths here (tests)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true", help="exit 1 when any repo has a problem")
    parser.add_argument("--remote", action="store_true",
                        help="audit baney75 and barnlabs repos on GitHub, no checkout")
    parser.add_argument("--write-baseline", metavar="FILE", help="with --remote: write the ratchet file")
    parser.add_argument("--no-cache", action="store_true", help="with --remote: skip the 1-hour cache")
    args = parser.parse_args(argv)
    if args.write_baseline and not args.remote:
        parser.error("--write-baseline needs --remote")
    if args.remote:
        return run_remote(args)
    root = Path(args.root).expanduser()
    if has_trading(root) or not root.is_dir():
        print(f"error: --root {root} is not a directory, or is inside a trading folder",
              file=sys.stderr)
        return 2
    result = audit(root, Path(args.home) if args.home else None)
    print(json.dumps(result, indent=2) if args.json else render(result))
    if not result["repos"]:
        print(f"warning: no repositories directly under {root}", file=sys.stderr)
        return 1 if args.strict else 0
    return 1 if args.strict and any(r["problems"] for r in result["repos"]) else 0


if __name__ == "__main__":
    sys.exit(run())
