#!/usr/bin/env python3
"""Generate the VS Code / Copilot plugin tree from the Claude plugin tree.

One source, two packages. `claude/` is the source of truth and stays exactly as
it is; everything under `copilot/` is derived from it by this script, and the
derived tree is gitignored so it cannot drift into a second hand-maintained
copy. Re-run this after touching anything in claude/.

Why a mirror rather than a second manifest in place:

  * Agent Plugins 1.0 (the 2026 VS Code / Copilot format) resolves skills as
    `skills/<name>/SKILL.md` -- one flat level. This repo nests them by category
    and by stack (`skills/development/frontend/react/frontend-developer/`).
  * Copilot-specific components live in `com.github.copilot/`, and a Claude
    plugin must not grow that directory.
  * Claude omits `version` on purpose (the commit SHA is the version); VS Code
    needs a concrete version to detect updates. A second package is where that
    difference belongs.

Run: python3 scripts/build-copilot-plugin.py
Out: copilot/  (untracked; install locally with chat.pluginLocations)
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "claude"
DEST = ROOT / "copilot"

# Agent Plugins 1.0 manifest. VS Code detects the format from `$schema`; an
# unknown top-level key is reported and ignored rather than fatal, but the
# schema is closed, so anything outside this list is a mistake worth catching.
MANIFEST: dict = {
    "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
    "name": "ai-dev-team",
    "version": "5.3.0",
    "description": (
        "Specialist agent team + enforced proportional workflow, with cross-cutting "
        "process skills for lifecycle, verification, review tiering, model selection "
        "and research method."
    ),
    "author": {"name": "Oleh Svyrydov"},
    "homepage": "https://github.com/olehsvyrydov/AI-development-team",
    "repository": "https://github.com/olehsvyrydov/AI-development-team",
    "license": "MIT",
    "keywords": [
        "ai-team",
        "multi-agent",
        "agents",
        "skills",
        "workflow",
        "code-review",
        "tdd",
        "orchestration",
    ],
}

# Marketplace catalogue. Same shape for both clients: Copilot reads
# `.github/plugin/marketplace.json` first and falls back to
# `.claude-plugin/marketplace.json`, which already ships in this repo -- so this
# file is for publishing the Copilot package, not a prerequisite for installing
# it from this repository.
MARKETPLACE: dict = {
    "name": "aidevteam",
    "owner": {"name": "Oleh Svyrydov"},
    "metadata": {"description": "The AI Dev Team agent framework.", "version": "5.3.0"},
    "plugins": [
        {
            "name": "ai-dev-team",
            "source": "./copilot",
            "description": MANIFEST["description"],
            "version": MANIFEST["version"],
            "author": {"name": "Oleh Svyrydov"},
            "homepage": MANIFEST["homepage"],
            "repository": MANIFEST["repository"],
            "license": "MIT",
            "keywords": ["agents", "skills", "workflow", "code-review", "tdd"],
        }
    ],
}

NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)

failures: list[str] = []


def fail(detail: str) -> None:
    failures.append(detail)


def frontmatter(path: Path) -> dict:
    match = FRONTMATTER.match(path.read_text(encoding="utf-8"))
    if not match:
        fail(f"{path.relative_to(ROOT)}: no frontmatter block")
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        fail(f"{path.relative_to(ROOT)}: frontmatter does not parse -- {str(exc).splitlines()[0]}")
        return {}
    return data if isinstance(data, dict) else {}


def build_skills() -> int:
    """Flatten skills/<anything...>/<name>/SKILL.md to skills/<name>/.

    Copilot requires the directory name to equal the frontmatter `name` and
    silently skips the skill otherwise, so the frontmatter wins and a directory
    mismatch is reported rather than papered over. Everything else in a skill
    directory (references/, nested reference packs) is copied verbatim.
    """
    out = DEST / "skills"
    count = 0
    for path in sorted(SRC.rglob("SKILL.md")):
        data = frontmatter(path)
        name = str(data.get("name", "")).strip()
        if not name:
            fail(f"{path.relative_to(ROOT)}: no 'name' in frontmatter")
            continue
        if not NAME.match(name):
            fail(f"{path.relative_to(ROOT)}: name {name!r} is not kebab-case")
            continue
        if path.parent.name != name:
            fail(
                f"{path.relative_to(ROOT)}: directory {path.parent.name!r} does not match "
                f"name {name!r}"
            )
            continue
        target = out / name
        if target.exists():
            fail(f"duplicate skill name {name!r}")
            continue
        shutil.copytree(path.parent, target)
        count += 1
    return count


def rewrite_commands() -> int:
    """Map claude/commands/*.md onto Copilot slash commands.

    Each command file is a persona prompt whose body points at the matching
    skill ("Invoke product-owner skill..."), and the skill names are identical
    on both sides because the flatten above preserves them. So the files copy
    across unchanged.
    """
    out = DEST / "com.github.copilot" / "commands"
    out.mkdir(parents=True, exist_ok=True)
    sources = sorted((SRC / "commands").glob("*.md"))
    for path in sources:
        shutil.copy2(path, out / path.name)
    return len(sources)


def build_agents() -> int:
    """Publish one custom agent per persona alias.

    `claude/commands/max.md` is the `/max` alias for the Product Owner. As a
    custom agent it has to be named `max.agent.md` -- Copilot derives the agent
    ID from the filename, so the alias is what makes the agent selectable.
    Files with no `description` in frontmatter are skipped: Copilot shows the
    description in the agent picker and an empty one is not worth publishing.
    """
    out = DEST / "com.github.copilot" / "agents"
    out.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted((SRC / "commands").glob("*.md")):
        data = frontmatter(path)
        description = str(data.get("description", "")).strip()
        if not description:
            continue
        (out / f"{path.stem}.agent.md").write_text(
            f"---\nname: {path.stem}\ndescription: {description}\n---\n\n"
            + FRONTMATTER.sub("", path.read_text(encoding="utf-8")).lstrip(),
            encoding="utf-8",
            newline="\n",
        )
        count += 1
    return count


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    if not SRC.is_dir():
        print(f"error: {SRC} is missing", file=sys.stderr)
        return 1

    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)

    skills = build_skills()
    commands = rewrite_commands()
    agents = build_agents()

    mcp = SRC / "mcp.json"
    if mcp.exists():
        shutil.copy2(mcp, DEST / "mcp.json")

    (DEST / "AGENTS.md").write_text(
        (SRC / "AGENTS.md").read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )

    write_json(DEST / "plugin.json", MANIFEST)
    write_json(ROOT / ".github" / "plugin" / "marketplace.json", MARKETPLACE)

    if failures:
        print(f"{len(failures)} source problem(s):\n", file=sys.stderr)
        for failure in failures:
            print(f"  FAIL {failure}", file=sys.stderr)
        return 1

    print(f"  built copilot/: {skills} skills, {commands} commands, {agents} agents")
    return 0


if __name__ == "__main__":
    sys.exit(main())
