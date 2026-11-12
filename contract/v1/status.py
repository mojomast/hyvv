"""Render contract/v1/STATUS.json as the markdown table embedded in the READMEs.

    python -m contract.v1.status          # print the table
    python -m contract.v1.status --write  # rewrite the marked blocks in README.md and contract/v1/README.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
STATUS = json.loads((HERE / "STATUS.json").read_text(encoding="utf-8"))
BEGIN, END = "<!-- status:begin (generated from contract/v1/STATUS.json; python -m contract.v1.status --write) -->", "<!-- status:end -->"
DOCS = [ROOT / "README.md", HERE / "README.md"]
TITLES = {"launcher-0.1.15": "Implemented in launcher 0.1.15", "reference": "Implemented in the /v1 reference only",
          "specified": "Specified, not implemented", "proposed": "Proposed"}


def render() -> str:
    lines = ["| Status | Endpoints | Features |", "| --- | --- | --- |"]
    for label in STATUS["labels"]:
        eps = [f"`{k}`" for k, v in STATUS["endpoints"].items() if v == label]
        fts = [k for k, v in STATUS["features"].items() if v == label]
        lines.append(f"| **{TITLES[label]}** (`{label}`) | {', '.join(eps) or '—'} | {'; '.join(fts) or '—'} |")
    return "\n".join(lines)


def block() -> str:
    return f"{BEGIN}\n{render()}\n{END}"


def current_block(text: str) -> str | None:
    if BEGIN not in text or END not in text:
        return None
    i = text.index(BEGIN)
    return text[i:text.index(END, i) + len(END)]


def write() -> None:
    for p in DOCS:
        t = p.read_text(encoding="utf-8")
        cur = current_block(t)
        if cur is None:
            raise SystemExit(f"no status markers in {p.relative_to(ROOT)}")
        p.write_text(t.replace(cur, block()), encoding="utf-8")


if __name__ == "__main__":
    if "--write" in sys.argv:
        write()
    else:
        print(render())
