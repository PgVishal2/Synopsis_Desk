"""Turn the team's past synopses (.docx) into markup examples for the prompt.

Drop any approved synopsis into the `style_examples/` folder and it is used
as a style reference automatically - no code change needed.
Highlights are preserved: yellow -> ==text==, cyan/turquoise -> ++text++, bold -> **text**.
"""
from __future__ import annotations

from pathlib import Path

import docx
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml.ns import qn

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "style_examples"
CYANS = {WD_COLOR_INDEX.TURQUOISE, WD_COLOR_INDEX.TEAL}


def _runs_to_markup(par) -> str:
    out = []
    for r in par.runs:
        t = r.text
        if not t:
            continue
        core = t.strip()
        if not core:
            out.append(t)
            continue
        lead, trail = t[: len(t) - len(t.lstrip())], t[len(t.rstrip()):]
        if r.bold:
            core = f"**{core}**"
        hl = r.font.highlight_color
        if hl in CYANS:
            core = f"++{core}++"
        elif hl is not None and hl != WD_COLOR_INDEX.AUTO:
            core = f"=={core}=="
        out.append(lead + core + trail)
    s = "".join(out)
    for m in ("**", "==", "++"):          # merge adjacent identical markers
        s = s.replace(m + m, "").replace(f"{m} {m}", " ")
    return s.strip()


def docx_to_markup(path: Path) -> str:
    d = docx.Document(path)
    body = d.element.body
    lines = []
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            par = docx.text.paragraph.Paragraph(child, d)
            txt = _runs_to_markup(par)
            if not txt:
                continue
            style = (par.style.name or "").lower()
            numbered = child.find(".//" + qn("w:numPr")) is not None
            if "list" in style or numbered:
                txt = "- " + txt
            lines.append(txt)
        elif child.tag == qn("w:tbl"):
            tbl = docx.table.Table(child, d)
            for row in tbl.rows:
                cells = []
                for c in row.cells:
                    ct = " ".join(_runs_to_markup(p) for p in c.paragraphs if p.text.strip())
                    if not cells or ct != cells[-1]:
                        cells.append(ct)
                lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def load_examples(folder: Path = DEFAULT_DIR, max_examples: int = 4,
                  exclude: str | None = None) -> str:
    """exclude: skip examples whose file name contains this text (used when testing)."""
    files = sorted(folder.glob("*.docx")) if folder.exists() else []
    if exclude:
        files = [f for f in files if exclude.lower() not in f.stem.lower()]
    files = files[:max_examples]
    parts = [f"### Example {i} ({f.stem})\n{docx_to_markup(f)}" for i, f in enumerate(files, 1)]
    return "\n\n".join(parts)
