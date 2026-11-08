#!/usr/bin/env python3
"""Check and maintain the Magnus Skill Commons.

skills/<name>/SKILL.md is the single source. .claude/skills, .agents/skills and
.cursor/skills are symlinks to it, so Claude Code, Grok Bots and Cursor load the
same bytes. .cursor/rules/agents.mdc is generated from AGENTS.md.

    python3 tools/skills.py list            # name, eval status, description
    python3 tools/skills.py check           # exit 1 on any problem
    python3 tools/skills.py gen-rules       # rewrite .cursor/rules/agents.mdc from AGENTS.md

Standard library only. The eval-file reader here is shared with tools/skill_eval.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = "skills"
VIEWS = (".claude/skills", ".agents/skills", ".cursor/skills")
RULES = ".cursor/rules/agents.mdc"
BOUNDARY_FILE = "policy/modding-boundary.md"
BOUNDARY_START = "<!-- modding-boundary:start -->"
BOUNDARY_END = "<!-- modding-boundary:end -->"
# Agent Skills names: lowercase letters, digits and hyphens; a digit may lead (3d-printing).
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
SOURCE_LINE = re.compile(r"^Source: (?P<repo>[\w.-]+/[\w.-]+) (?P<path>\S+) @ (?P<sha>[0-9a-f]{40}) \((?P<license>[\w.-]+)\)", re.M)
MODDING_WORDS = re.compile(r"\b(mod|mods|modding|modder|cheat|cheats|trainer|reverse[- ]engineer\w*|game|games)\b", re.I)
KINDS = ("direct", "indirect", "incomplete", "non-trigger")
SPLITS = ("held-in", "held-out")
MIN_CASES = 4


class EvalFileError(ValueError):
    pass


# --------------------------------------------------------------------------- parsing

def frontmatter(text: str) -> dict[str, str]:
    """Top-level scalar keys from a SKILL.md front matter block. Folded (>-, |) values are joined."""
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    result: dict[str, str] = {}
    key = None
    for line in text[4:end].splitlines():
        match = re.match(r"([A-Za-z_][\w-]*):\s*(.*)$", line)
        if match and not line.startswith((" ", "\t")):
            key, value = match.group(1), match.group(2).strip()
            result[key] = "" if value in (">-", ">", "|", "|-") else _scalar_text(value)
        elif key and line.strip():
            result[key] = (result[key] + " " + line.strip()).strip()
    return result


# skills/<name>/evals/tested_on.json: which model scored this skill, when, and where the record is.
# Optional. It lives beside the eval cases, not in SKILL.md front matter, because the Agent Skills
# spec makes `metadata` a string-to-string map and skill fingerprints exclude evals/.
TESTED_ON = "tested_on.json"
TESTED_KEYS = ("model", "date", "score", "eval")
# An effective model id ("claude-sonnet-5-5", "gpt-6.1-sol"), never an alias like "sonnet".
MODEL_ID = re.compile(r"[a-z][a-z0-9.]*(-[a-z0-9.]+)*-\d[a-z0-9.-]*\Z")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def tested_on(path: Path) -> tuple[list[dict], list[str]]:
    """Read evals/tested_on.json: {"tested_on": [{"model", "date", "score", "eval"}, ...]}.
    Returns (entries, shape problems)."""
    if not path.is_file():
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [], [f"not JSON: {exc}"]
    entries = data.get("tested_on") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries or set(data) != {"tested_on"}:
        return [], ['must be {"tested_on": [ ...one or more entries... ]} and nothing else']
    problems = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != set(TESTED_KEYS):
            problems.append(f"each entry needs exactly {', '.join(TESTED_KEYS)}: {entry!r}")
        elif not all(isinstance(entry[k], str) for k in ("model", "date", "eval")) or isinstance(
                entry["score"], bool) or not isinstance(entry["score"], (int, float)):
            problems.append(f"model, date and eval are strings and score is a number: {entry!r}")
    return ([] if problems else entries), problems


def check_tested_on(root: Path, name: str, entries: list[dict]) -> list[str]:
    """Each entry must match its record: the skill, the effective model, the date and the held-out score."""
    problems = []
    for entry in entries:
        if tuple(sorted(entry)) != tuple(sorted(TESTED_KEYS)):
            problems.append(f"tested_on entry needs exactly {', '.join(TESTED_KEYS)}: {sorted(entry)}")
            continue
        model, date, score, record = entry["model"], entry["date"], entry["score"], entry["eval"]
        if not MODEL_ID.match(model):
            problems.append(f"tested_on model {model!r} is not an effective model id (use the record's effective_models)")
        if not DATE.match(date):
            problems.append(f"tested_on date {date!r} is not YYYY-MM-DD")
        try:
            score = float(score)
        except ValueError:
            problems.append(f"tested_on score {score!r} is not a number")
            continue
        path = root / record
        if not record.startswith("evidence/evals/") or not path.is_file():
            problems.append(f"tested_on eval {record!r} is not a file under evidence/evals/")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            problems.append(f"tested_on eval {record} is not JSON")
            continue
        if not isinstance(data, dict) or not isinstance(data.get("results") or [], list):
            problems.append(f"tested_on eval {record} is not a skill_eval record")
            continue
        result = next((r for r in data.get("results") or () if isinstance(r, dict) and r.get("skill") == name), None)
        if result is None or data.get("dry_run"):
            problems.append(f"tested_on eval {record} holds no scored run of {name}")
            continue
        if model not in (data.get("effective_models") or ()):
            problems.append(f"tested_on model {model} did not run {record} (effective_models: {data.get('effective_models')})")
        if str(data.get("date", ""))[:10] != date:
            problems.append(f"tested_on date {date} differs from {record} ({str(data.get('date'))[:10]})")
        split = (result.get("splits") or {}).get("held-out") if isinstance(result.get("splits"), dict) else None
        held_out = split.get("score") if isinstance(split, dict) else None
        try:
            matches = held_out is not None and round(float(held_out), 4) == round(score, 4)
        except (TypeError, ValueError):
            matches = False
        if not matches:
            problems.append(f"tested_on score {score} is not {record}'s held-out score ({held_out})")
    return problems


def _scalar_text(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _value(raw: str, where: str):
    raw = raw.strip()
    if raw.startswith(("\"", "[", "{")):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EvalFileError(f"{where}: bad JSON-style value: {exc}") from exc
    if " #" in raw:
        raw = raw.split(" #", 1)[0].rstrip()
    if raw in ("true", "false"):
        return raw == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


def load_eval_file(path: Path) -> dict:
    """Read the eval YAML subset: top-level `key: value` lines and a `cases:` list of flat mappings.

    Values are plain scalars, true/false, integers, or JSON (double-quoted strings, [lists]).
    This is deliberately small so the evaluator needs no third-party YAML library.
    """
    data: dict = {}
    cases: list[dict] = []
    current: dict | None = None
    in_cases = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        where = f"{path}:{number}"
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            match = re.match(r"([\w-]+):\s*(.*)$", line)
            if not match:
                raise EvalFileError(f"{where}: expected `key: value`")
            key, raw = match.groups()
            if key == "cases":
                if raw.strip():
                    raise EvalFileError(f"{where}: `cases:` takes an indented list")
                in_cases = True
                continue
            in_cases = False
            data[key] = _value(raw, where)
            continue
        if not in_cases:
            raise EvalFileError(f"{where}: indented line outside `cases:`")
        if stripped.startswith("- "):
            current = {}
            cases.append(current)
            stripped = stripped[2:]
        if current is None:
            raise EvalFileError(f"{where}: case field before the first `- `")
        match = re.match(r"([\w-]+):\s*(.*)$", stripped)
        if not match:
            raise EvalFileError(f"{where}: expected `key: value` inside a case")
        key, raw = match.groups()
        if key in current:
            raise EvalFileError(f"{where}: duplicate key {key}")
        current[key] = _value(raw, where)
    data["cases"] = cases
    return data


def validate_eval(data: dict, skill: str, split: str, where: str) -> list[str]:
    problems = []
    if data.get("skill") != skill:
        problems.append(f"{where}: skill must be {skill!r}")
    if data.get("split") != split:
        problems.append(f"{where}: split must be {split!r}")
    cases = data.get("cases", [])
    if len(cases) < MIN_CASES:
        problems.append(f"{where}: needs at least {MIN_CASES} cases")
    kinds = set()
    for index, case in enumerate(cases):
        label = f"{where} case {case.get('id', index)}"
        for field in ("id", "kind", "prompt", "trigger", "expect"):
            if field not in case:
                problems.append(f"{label}: missing {field}")
        kind = case.get("kind")
        kinds.add(kind)
        if kind not in KINDS:
            problems.append(f"{label}: kind must be one of {', '.join(KINDS)}")
        if not isinstance(case.get("trigger"), bool):
            problems.append(f"{label}: trigger must be true or false")
        elif (kind == "non-trigger") == case["trigger"]:
            problems.append(f"{label}: non-trigger cases have trigger false; every other kind has trigger true")
        for check in case.get("checks", []) or []:
            if not isinstance(check, str) or not re.match(r"(re|not):", check):
                problems.append(f"{label}: checks are strings starting with re: or not:")
                continue
            try:
                re.compile(check.split(":", 1)[1])
            except re.error as exc:
                problems.append(f"{label}: bad regex {check!r}: {exc}")
    if "non-trigger" not in kinds:
        problems.append(f"{where}: needs at least one non-trigger case")
    if not kinds & {"indirect", "incomplete"}:
        problems.append(f"{where}: needs an indirect or incomplete case")
    return problems


def load_evals(skill_dir: Path) -> dict[str, dict]:
    return {split: load_eval_file(skill_dir / "evals" / f"{split}.yaml") for split in SPLITS}


# --------------------------------------------------------------------------- fingerprints

def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def tree_fingerprint(entries: list[tuple[str, str]]) -> str:
    """sha256 over sorted (path, git blob sha) pairs. Computable from files or from `git ls-tree`."""
    digest = hashlib.sha256()
    for path, blob in sorted(entries):
        digest.update(f"{path}\0{blob}\n".encode())
    return digest.hexdigest()


def skill_entries(root: Path, name: str) -> list[tuple[str, str]]:
    """Files that make up a skill's behavior: everything under skills/<name>/ except evals/."""
    base = root / SKILLS / name
    entries = []
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not rel.startswith(f"{SKILLS}/{name}/evals/"):
            entries.append((rel, git_blob_sha(path.read_bytes())))
    return entries


USER_EVALS = "_user"


def eval_home(root: Path, name: str) -> str:
    """Repo-relative folder whose evals/ holds a skill's cases: skills/<name>, or skills/_user/<name> for a
    skill that lives outside this repo (scored with `skill_eval.py --catalog user`)."""
    if (root / SKILLS / name / "evals").is_dir() or (root / SKILLS / name / "SKILL.md").is_file():
        return f"{SKILLS}/{name}"
    return f"{SKILLS}/{USER_EVALS}/{name}"


def eval_entries(root: Path, name: str) -> list[tuple[str, str]]:
    home = eval_home(root, name)
    entries = [(f"{home}/evals/{split}.yaml",
                git_blob_sha((root / home / "evals" / f"{split}.yaml").read_bytes())) for split in SPLITS]
    evaluator = root / "tools" / "skill_eval.py"
    entries.append(("tools/skill_eval.py", git_blob_sha(evaluator.read_bytes())))
    return entries


# --------------------------------------------------------------------------- checks

def skill_dirs(root: Path) -> list[Path]:
    return sorted(p for p in (root / SKILLS).iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))


def boundary_block(root: Path) -> str:
    text = (root / BOUNDARY_FILE).read_text(encoding="utf-8")
    start, end = text.index(BOUNDARY_START), text.index(BOUNDARY_END) + len(BOUNDARY_END)
    return text[start:end]


def generated_rules(root: Path) -> str:
    body = (root / "AGENTS.md").read_text(encoding="utf-8")
    return ("---\ndescription: Magnus Singularity repository rules, generated from AGENTS.md\nalwaysApply: true\n---\n"
            "<!-- Generated by `python3 tools/skills.py gen-rules`. Edit AGENTS.md, not this file. -->\n\n" + body)


def check(root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    skills_root = (root / SKILLS).resolve()
    for view in VIEWS:
        path = root / view
        if not path.is_symlink():
            problems.append(f"{view}: must be a symlink to ../{SKILLS}")
        elif path.resolve() != skills_root:
            problems.append(f"{view}: points to {path.resolve()}, not {skills_root}")
    block = boundary_block(root)
    for skill in skill_dirs(root):
        name = skill.name
        where = f"{SKILLS}/{name}"
        if not NAME.fullmatch(name):
            problems.append(f"{where}: directory name must be lowercase letters, digits and hyphens")
        manifest = skill / "SKILL.md"
        if not manifest.is_file():
            problems.append(f"{where}: missing SKILL.md")
            continue
        text = manifest.read_text(encoding="utf-8")
        meta = frontmatter(text)
        if not meta.get("name"):
            problems.append(f"{where}/SKILL.md: front matter needs name")
        description = meta.get("description", "")
        if not description:
            problems.append(f"{where}/SKILL.md: front matter needs description")
        if len(description) > 1024:
            problems.append(f"{where}/SKILL.md: description is {len(description)} characters; the limit is 1024")
        for source in re.findall(r"^Source:.*$", text, re.M):
            if not SOURCE_LINE.match(source):
                problems.append(f"{where}/SKILL.md: source line must read "
                                "`Source: owner/repo path @ <40-hex sha> (License)`: {source!r}")
        if MODDING_WORDS.search(description) and block not in text:
            problems.append(f"{where}/SKILL.md: a modding or game skill must carry the block from {BOUNDARY_FILE} verbatim")
        elif BOUNDARY_START in text and block not in text:
            problems.append(f"{where}/SKILL.md: modding boundary block differs from {BOUNDARY_FILE}")
        if re.search(r"^\s*tested_on\s*:", text.split("\n---", 1)[0] if text.startswith("---\n") else "", re.M):
            problems.append(f"{where}/SKILL.md: move tested_on out of front matter into evals/{TESTED_ON}")
        entries, shape = tested_on(skill / "evals" / TESTED_ON)
        problems.extend(f"{where}/evals/{TESTED_ON}: {p}" for p in shape + check_tested_on(root, name, entries))
        if (skill / "evals").exists():
            problems.extend(check_evals(skill / "evals", name, where))
    user_evals = root / SKILLS / USER_EVALS
    if user_evals.is_dir():
        for entry in sorted(p for p in user_evals.iterdir() if p.is_dir()):
            where = f"{SKILLS}/{USER_EVALS}/{entry.name}"
            if (root / SKILLS / entry.name).exists():
                problems.append(f"{where}: {entry.name} lives in this repo; keep its evals in {SKILLS}/{entry.name}/evals")
            if list(entry.rglob("SKILL.md")):
                problems.append(f"{where}: holds evals only; the skill text stays where it is installed")
            problems.extend(check_evals(entry / "evals", entry.name, where))
    clusters = root / SKILLS / "_clusters"
    if clusters.is_dir():
        from skill_eval import validate_cluster  # skill_eval imports this module; import late
        for path in sorted(clusters.glob("*.yaml")):
            try:
                data = load_eval_file(path)
            except EvalFileError as exc:
                problems.append(str(exc))
                continue
            problems.extend(f"{SKILLS}/_clusters/{path.name}: {p}" for p in validate_cluster(data, path.stem))
    archive = root / SKILLS / "_archive"
    if archive.is_dir():
        readme = (archive / "README.md").read_text(encoding="utf-8") if (archive / "README.md").is_file() else ""
        for entry in sorted(p for p in archive.iterdir() if p.is_dir()):
            if list(entry.rglob("SKILL.md")):
                problems.append(f"{SKILLS}/_archive/{entry.name}: rename SKILL.md to SKILL.archived.md so no runtime loads it")
            if f"`{entry.name}`" not in readme:
                problems.append(f"{SKILLS}/_archive/README.md: add a reason line for `{entry.name}`")
    rules = root / RULES
    if not rules.is_file() or rules.read_text(encoding="utf-8") != generated_rules(root):
        problems.append(f"{RULES}: out of date; run `python3 tools/skills.py gen-rules`")
    return problems


def check_evals(evals: Path, name: str, where: str) -> list[str]:
    problems: list[str] = []
    seen_prompts: dict[str, str] = {}
    seen_ids: set[str] = set()
    for split in SPLITS:
        path = evals / f"{split}.yaml"
        if not path.is_file():
            problems.append(f"{where}/evals: missing {split}.yaml")
            continue
        try:
            data = load_eval_file(path)
        except EvalFileError as exc:
            problems.append(str(exc))
            continue
        problems.extend(validate_eval(data, name, split, f"{where}/evals/{split}.yaml"))
        for case in data["cases"]:
            if case.get("id") in seen_ids:
                problems.append(f"{where}/evals: case id {case.get('id')!r} repeats")
            seen_ids.add(case.get("id"))
            prompt = str(case.get("prompt", "")).strip().lower()
            if prompt in seen_prompts and seen_prompts[prompt] != split:
                problems.append(f"{where}/evals: a prompt appears in both splits")
            seen_prompts[prompt] = split
    return problems


def listing(root: Path = ROOT) -> list[dict]:
    rows = []
    for skill in skill_dirs(root):
        meta = frontmatter((skill / "SKILL.md").read_text(encoding="utf-8")) if (skill / "SKILL.md").is_file() else {}
        rows.append({"skill": skill.name, "evals": (skill / "evals").is_dir(),
                     "description": meta.get("description", "")})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("list", "check", "gen-rules"))
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.command == "list":
        for row in listing(root):
            print(f"{row['skill']:<34} {'evals' if row['evals'] else '     '}  {row['description'][:90]}")
        return 0
    if args.command == "gen-rules":
        target = root / RULES
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(generated_rules(root), encoding="utf-8")
        print(f"wrote {RULES}")
        return 0
    problems = check(root)
    for problem in problems:
        print(problem)
    print(f"{len(problems)} problem(s) in {len(skill_dirs(root))} skills")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
