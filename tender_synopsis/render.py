"""Render a Synopsis to a Word document in the house style."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_COLOR_INDEX, WD_PARAGRAPH_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .schema import Block, Synopsis

NAVY = RGBColor(0x1F, 0x38, 0x64)
GREY = RGBColor(0x59, 0x59, 0x59)
HEADER_FILL = "D9E2F3"
SUMMARY_FILL = "EEF3E8"
FONT = "Calibri"
CONTENT_WIDTH_CM = 17.0

TOKEN = re.compile(r"(\*\*|==|\+\+)")


def add_rich_text(par, text: str, bold: bool = False, size: float | None = None):
    """Write text with **bold**, ==yellow==, ++cyan++ markup as formatted runs."""
    state = {"**": bold, "==": False, "++": False}
    for part in TOKEN.split(text or ""):
        if part in state:
            state[part] = not state[part] if part != "**" or not bold else True
            continue
        if not part:
            continue
        run = par.add_run(part)
        run.bold = state["**"]
        if size:
            run.font.size = Pt(size)
        if state["++"]:
            run.font.highlight_color = WD_COLOR_INDEX.TURQUOISE
        elif state["=="]:
            run.font.highlight_color = WD_COLOR_INDEX.YELLOW
    return par


def _shade(element, fill: str):
    """Background shading for a table cell (tcPr) or paragraph (pPr)."""
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    element.append(shd)


def _set_cell_width(cell, cm: float):
    cell.width = Cm(cm)
    tcw = cell._tc.get_or_add_tcPr().first_child_found_in("w:tcW")
    if tcw is not None:
        tcw.set(qn("w:type"), "dxa")


def _table(doc, headers: list[str], rows: list[list[str]], widths: list[float],
           bold_first_col: bool = False):
    ncols = max([len(headers)] + [len(r) for r in rows]) if (headers or rows) else 2
    if len(widths) != ncols:
        widths = _auto_widths(headers, rows, ncols)
    t = doc.add_table(rows=0, cols=ncols)
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    if headers:
        cells = t.add_row().cells
        for i, h in enumerate(headers[:ncols]):
            p = cells[i].paragraphs[0]
            add_rich_text(p, h, bold=True)
            _shade(cells[i]._tc.get_or_add_tcPr(), HEADER_FILL)
    for r in rows:
        cells = t.add_row().cells
        for i in range(ncols):
            val = r[i] if i < len(r) else ""
            add_rich_text(cells[i].paragraphs[0], val, bold=bold_first_col and i == 0)
    for i, col in enumerate(t.columns):
        col.width = Cm(widths[i])
    for row in t.rows:
        for i, c in enumerate(row.cells):
            _set_cell_width(c, widths[i])
            for p in c.paragraphs:
                p.paragraph_format.space_after = Pt(2)
    doc.add_paragraph().paragraph_format.space_after = Pt(0)
    return t


def _auto_widths(headers, rows, ncols) -> list[float]:
    """Column widths proportional to content length (min 2.5 cm), summing to page width."""
    lens = [2.0] * ncols
    for r in [headers] + rows:
        for i, v in enumerate(r[:ncols]):
            lens[i] = max(lens[i], min(len(TOKEN.sub("", v or "")), 60))
    total = sum(lens)
    w = [max(2.5, CONTENT_WIDTH_CM * n / total) for n in lens]
    scale = CONTENT_WIDTH_CM / sum(w)
    return [x * scale for x in w]


def _numbered(doc, items, start=1):
    for n, it in enumerate(items, start):
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.left_indent, pf.first_line_indent, pf.space_after = Cm(0.9), Cm(-0.6), Pt(3)
        p.add_run(f"{n}.\t")
        pf.tab_stops.add_tab_stop(Cm(0.9))
        add_rich_text(p, it.text)
        _subs(doc, it.sub_items)


def _bullets(doc, items):
    for it in items:
        p = doc.add_paragraph(style="List Bullet")
        p.paragraph_format.space_after = Pt(3)
        add_rich_text(p, it.text)
        _subs(doc, it.sub_items)


def _subs(doc, subs):
    for s in subs:
        p = doc.add_paragraph(style="List Bullet 2")
        p.paragraph_format.space_after = Pt(2)
        add_rich_text(p, s)


def _block(doc, b: Block):
    if b.type == "subheading":
        p = doc.add_paragraph()
        p.paragraph_format.space_before, p.paragraph_format.space_after = Pt(6), Pt(3)
        add_rich_text(p, b.text, bold=True)
    elif b.type == "paragraph":
        add_rich_text(doc.add_paragraph(), b.text).paragraph_format.space_after = Pt(4)
    elif b.type == "bullets":
        _bullets(doc, b.items)
    elif b.type == "numbered":
        _numbered(doc, b.items)
    elif b.type == "kv_table":
        _table(doc, b.headers or ["Particular", "Details"], b.rows, [5.0, 12.0], bold_first_col=True)
    elif b.type == "table":
        _table(doc, b.headers, b.rows, [])


def _base_styles(doc):
    st = doc.styles["Normal"]
    st.font.name, st.font.size = FONT, Pt(11)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    st.paragraph_format.space_after = Pt(4)
    for sec in doc.sections:
        sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
        sec.left_margin = sec.right_margin = Cm(2.0)
        sec.top_margin, sec.bottom_margin = Cm(1.8), Cm(1.8)


def _page_number(par):
    for tag, text in (("begin", None), (None, "PAGE"), ("end", None)):
        run = par.add_run()
        if tag:
            el = OxmlElement("w:fldChar")
            el.set(qn("w:fldCharType"), tag)
        else:
            el = OxmlElement("w:instrText")
            el.set(qn("xml:space"), "preserve")
            el.text = text
        run._r.append(el)
        run.font.size, run.font.color.rgb = Pt(8), GREY


def render_docx(syn: Synopsis, out_path: str | Path, source_files: list[str] | None = None) -> Path:
    doc = Document()
    _base_styles(doc)
    sec = doc.sections[0]

    hp = sec.header.paragraphs[0]
    hp.alignment = WD_PARAGRAPH_ALIGNMENT.RIGHT
    from .config import org
    r = hp.add_run(org()["brand_line"])
    r.font.size, r.font.color.rgb = Pt(8), GREY

    fp = sec.footer.paragraphs[0]
    src = ", ".join(source_files or [])
    r = fp.add_run(f"Prepared {date.today():%d.%m.%Y}" + (f" from {src}" if src else "")
                   + ". Verify against the tender document before bidding.   Page ")
    r.font.size, r.font.color.rgb = Pt(8), GREY
    _page_number(fp)

    p = doc.add_paragraph()
    run = p.add_run(syn.profile.synopsis_title)
    run.bold, run.font.size, run.font.color.rgb = True, Pt(16), NAVY
    p.paragraph_format.space_after = Pt(2)

    pr = syn.profile
    meta = doc.add_paragraph()
    r = meta.add_run(f"{pr.document_type}  |  {pr.sector}  |  Ref: {pr.reference_no}  |  "
                     f"Issued: {pr.date_of_issue}  |  Deadline: {pr.submission_deadline}")
    r.font.size, r.font.color.rgb = Pt(8.5), GREY
    meta.paragraph_format.space_after = Pt(8)

    if syn.key_facts_table:
        _table(doc, ["Particular", "Details"], syn.key_facts_table, [5.0, 12.0], bold_first_col=True)

    for i, s in enumerate(syn.sections, 1):
        h = doc.add_paragraph()
        h.paragraph_format.space_before, h.paragraph_format.space_after = Pt(10), Pt(4)
        h.paragraph_format.keep_with_next = True
        heading = re.sub(r"^\s*\d+[.)]\s*", "", s.heading)
        run = h.add_run(f"{i}. ")
        run.bold, run.font.size, run.font.color.rgb = True, Pt(12), NAVY
        for rr in add_rich_text(h, heading, bold=True, size=12).runs[1:]:
            if rr.font.highlight_color is None:
                rr.font.color.rgb = NAVY
        for b in s.blocks:
            _block(doc, b)

    if syn.summary:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(10)
        _shade(p._p.get_or_add_pPr(), SUMMARY_FILL)
        summary = re.sub(r"^\W*(?:\**\s*in summary\s*:?\s*\**)?\s*", "", syn.summary, flags=re.I)
        add_rich_text(p, "✅ **In Summary:** " + summary)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path
