"""den's own agent skills, as the broker serves them to a client on another machine.

A skill is `skills/<name>/SKILL.md` (Agent Skills format: a frontmatter name and description,
then the text) with optional `references/*.md` behind it. On the machine itself they are
linked into where agents look for skills; a client elsewhere asks the broker for them instead,
and loads one when its user wants it (ADR 0007).

A skill teaches strategy; the options live in the tool description, which is built from
config.toml and what's installed (ADR 0003). So a client that shows both is showing this
machine's own answer to "how do I use this well".
"""

import re

from den import core
from den.core import DenError

SKILLS_DIR = core.ROOT / "skills"
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_BYTES = 1 << 20


def _check(name, what="skill"):
    if not name or not NAME.fullmatch(name):
        raise DenError(f"{what} names are lower-case words joined by hyphens, got {name!r}")
    return name


def _frontmatter(text):
    """The name and description a SKILL.md declares, and the text after the frontmatter."""
    match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not match:
        return {}, text
    fields = dict(re.findall(r"^([a-z_]+):[ \t]*(.*)$", match[1], re.MULTILINE))
    return fields, match[2].lstrip("\n")


def _read(path):
    try:
        return path.read_text()[:MAX_BYTES]
    except OSError as e:
        raise DenError(f"cannot read {path.name}: {e}") from e


def names():
    return sorted(p.name for p in SKILLS_DIR.glob("*/") if (p / "SKILL.md").is_file())


def listing():
    """Every skill here: name, description and the references it carries."""
    found = []
    for name in names():
        fields, _ = _frontmatter(_read(SKILLS_DIR / name / "SKILL.md"))
        references = sorted(p.stem for p in (SKILLS_DIR / name / "references").glob("*.md"))
        found.append({"name": name, "description": fields.get("description", ""), "references": references})
    return found


def get(name, reference=None):
    """One skill's text, or one of its references. Nothing outside skills/ is served."""
    _check(name)
    folder = SKILLS_DIR / name
    if not (folder / "SKILL.md").is_file():
        raise DenError(f"no skill {name!r} here; den has: {', '.join(names()) or 'none'}")
    if reference is None:
        fields, text = _frontmatter(_read(folder / "SKILL.md"))
        references = sorted(p.stem for p in (folder / "references").glob("*.md"))
        return {"name": name, "description": fields.get("description", ""), "references": references, "text": text}
    _check(reference, "reference")
    path = folder / "references" / f"{reference}.md"
    if not path.is_file():
        raise DenError(f"skill {name!r} has no reference {reference!r}")
    return {"name": name, "reference": reference, "text": _read(path)}
