"""Validate the public AlphaForge documentation set without extra packages."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
DOC_ROOT = ROOT / "docs" / "alphaforge"
REQUIRED = {
    "README.md",
    "PRODUCT_VISION.md",
    "ARCHITECTURE.md",
    "QUICKSTART.md",
    "DOCKER.md",
    "CONFIGURATION.md",
    "USER_GUIDE.md",
    "API_REFERENCE.md",
    "SECURITY_AND_AUTHORITY.md",
    "DEVELOPMENT.md",
    "GITHUB_COLLABORATION.md",
    "FRONTEND_PRODUCT_REQUIREMENTS.md",
    "FRONTEND_AI_PROMPT.md",
    "CURRENT_STATUS.md",
    "ROADMAP.md",
    "TROUBLESHOOTING.md",
}
LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
ABSOLUTE_PRIVATE_PATH_RE = re.compile(
    r"(?:[A-Za-z]:\\Users\\[^\\\s]+|[A-Za-z]:\\trader\\(?:Vibe-Trading-main|AlphaForge)\b|/Users/[^/\s]+|/home/(?!vibe(?:/|\b))[^/\s]+)"
)


def markdown_files() -> list[Path]:
    files = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "AGENT_CONTRIBUTOR_GUIDE.md"]
    files.extend(sorted(DOC_ROOT.glob("*.md")))
    return [path for path in files if path.exists()]


def validate() -> list[str]:
    errors: list[str] = []
    present = {path.name for path in DOC_ROOT.glob("*.md")}
    for missing in sorted(REQUIRED - present):
        errors.append(f"missing required document: docs/alphaforge/{missing}")

    for path in markdown_files():
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT).as_posix()
        if "\x00" in text:
            errors.append(f"{relative}: contains NUL")
        for match in ABSOLUTE_PRIVATE_PATH_RE.finditer(text):
            errors.append(f"{relative}: private user path at offset {match.start()}")
        for raw_target in LINK_RE.findall(text):
            target = raw_target.strip().strip("<>")
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = unquote(target.split("#", 1)[0])
            if not target:
                continue
            resolved = (path.parent / target).resolve()
            try:
                resolved.relative_to(ROOT.resolve())
            except ValueError:
                errors.append(f"{relative}: link escapes repository: {raw_target}")
                continue
            if not resolved.exists():
                errors.append(f"{relative}: broken link: {raw_target}")
    return errors


def main() -> int:
    errors = validate()
    if errors:
        print("AlphaForge documentation validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"AlphaForge documentation validation passed: {len(markdown_files())} Markdown files checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
