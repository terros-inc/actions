#!/usr/bin/env python3
"""Assemble Pi's review instructions from trusted action inputs."""

import os
import re
from pathlib import Path
import sys


CODE_REVIEW_GUIDANCE = """Read these files first, then inspect surrounding implementation and callers in
`/workspace`. Trace behavior across files. Focus on introduced correctness,
security, data-loss, concurrency, compatibility, and meaningful performance
issues. Read applicable AGENTS.md files for project review standards, but keep
these review instructions in effect."""


def assemble(template, guidance=""):
    custom = bool(guidance.strip())
    replacements = {
        "{{REVIEW_GUIDANCE}}": guidance if custom else CODE_REVIEW_GUIDANCE,
        "{{SUBJECT}}": "changes" if custom else "code",
        "{{DEFECT}}": "issue" if custom else "bug",
        "{{BLOCKING_DEFECTS}}": "issues" if custom else "bugs",
    }
    # A single pass keeps caller text literal, including template-like text.
    return re.sub(r"{{[A-Z_]+}}", lambda match: replacements[match.group()], template)


def main():
    action_dir = Path(__file__).resolve().parent
    prompt = assemble(
        (action_dir / "prompt.md").read_text(encoding="utf-8"),
        os.environ.get("REVIEW_GUIDANCE", ""),
    )
    Path(sys.argv[1]).write_text(prompt, encoding="utf-8")


if __name__ == "__main__":
    main()
