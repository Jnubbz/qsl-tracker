"""
Generate print-ready QSL mailing-label PDFs from a QrzRecord.

Supports one sheet layout for now: Avery 5163 / 8163 / 18163 (and their
many geometric twins -- 5263, 5513, 5523, 5795, 5923, 5963, and others
Avery lists as compatible), the standard 2"x4" shipping-label sheet, 10
labels per US Letter page (2 columns x 5 rows).

Geometry below is Avery's own published spec for that layout and was
sanity-checked by hand: it has to add up to an exact 8.5in x 11in sheet,
and does:
    LEFT_MARGIN + 2*LABEL_W + H_GAP + RIGHT_MARGIN == 8.5in
    TOP_MARGIN + 5*LABEL_H + BOTTOM_MARGIN == 11.0in

The PDF is always a full US Letter page with only ONE label position
filled in and the rest left blank, so a partially-used physical sheet
(common once you're printing labels one lookup at a time) can be fed
back into the printer at whichever position is still unused.
"""
from __future__ import annotations

import io

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from qrz import QrzRecord

PAGE_W, PAGE_H = letter  # 8.5in x 11in, in points

LABEL_W = 4.0 * inch
LABEL_H = 2.0 * inch
TOP_MARGIN = 0.5 * inch
LEFT_MARGIN = 0.162 * inch
H_PITCH = 4.188 * inch  # column-to-column distance (label width + gap)
V_PITCH = 2.0 * inch    # row-to-row distance (no vertical gap on this layout)
COLUMNS = 2
ROWS = 5
LABEL_COUNT = COLUMNS * ROWS

# Inset from the label's own edges so text never sits near a die-cut edge.
PADDING = 0.2 * inch

HEADER_FONT, HEADER_SIZE = "Helvetica-Bold", 12
BODY_FONT, BODY_SIZE = "Helvetica", 10.5
LINE_HEIGHT = 0.19 * inch


def clamp_position(position: int) -> int:
    return max(1, min(LABEL_COUNT, position))


def _label_origin(position: int) -> tuple[float, float]:
    """Bottom-left corner (PDF points, origin bottom-left of the page) of
    the label at `position` (1-10, numbered left-to-right, top-to-bottom)."""
    index = clamp_position(position) - 1
    row, col = divmod(index, COLUMNS)
    x = LEFT_MARGIN + col * H_PITCH
    y = PAGE_H - TOP_MARGIN - (row + 1) * V_PITCH
    return x, y


def _wrap(text: str, font: str, size: float, max_width: float) -> list[str]:
    """Greedy word-wrap so a long street address/city line never runs
    past the label's right edge instead of just overflowing silently."""
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if stringWidth(candidate, font, size) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _city_line(record: QrzRecord) -> str:
    city_line = ", ".join(p for p in (record.city, record.state) if p)
    if record.zip_code:
        city_line = f"{city_line} {record.zip_code}".strip()
    return city_line


def label_lines(record: QrzRecord) -> list[tuple[str, str, float]]:
    """The (text, font, size) lines a label for `record` renders, already
    wrapped to fit -- shared by the PDF generator and anything that wants
    to preview the same content (e.g. the admin page)."""
    header = record.name.strip()
    header = f"{header}  ({record.callsign})" if header else record.callsign

    raw_lines = [header] + [
        line for line in (record.address, _city_line(record), record.country) if line
    ]

    max_width = LABEL_W - 2 * PADDING
    out: list[tuple[str, str, float]] = []
    for i, text in enumerate(raw_lines):
        font, size = (HEADER_FONT, HEADER_SIZE) if i == 0 else (BODY_FONT, BODY_SIZE)
        for wrapped in _wrap(text, font, size, max_width):
            out.append((wrapped, font, size))
    return out


def generate_avery_5163_pdf(record: QrzRecord, position: int = 1) -> bytes:
    """Render a US Letter PDF with one QSL mailing label filled in at
    `position` (1-10), everything else left blank."""
    x0, y0 = _label_origin(position)
    lines = label_lines(record)

    block_height = LINE_HEIGHT * len(lines)
    top_of_block = y0 + (LABEL_H + block_height) / 2
    inner_x = x0 + PADDING

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.setTitle(f"QSL mailing label -- {record.callsign}")

    y = top_of_block - LINE_HEIGHT
    for text, font, size in lines:
        c.setFont(font, size)
        c.drawString(inner_x, y, text)
        y -= LINE_HEIGHT

    c.showPage()
    c.save()
    return buffer.getvalue()
