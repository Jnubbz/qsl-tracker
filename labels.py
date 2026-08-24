"""
Generate print-ready QSL label PDFs -- two kinds:

  * A **mailing label** (`generate_avery_5163_pdf`) from a QrzRecord --
    the recipient's name/address, for the envelope a card goes out in.
  * A **QSO label** (`generate_qso_avery_5163_pdf`) from one of Josh's
    own logged QSOs (see photomap_store.py's `my_qsos` -- imported from
    his ADIF log, never QRZ, since QRZ only knows a station's own
    profile, not the specifics of a contact with them) -- callsign,
    date/time in UTC, band/mode/freq, and RST sent/received, meant to be
    printed and stuck onto a blank 2"x4" spot on the physical QSL card
    itself instead of hand-written.

Both share the same sheet layout for now: Avery 5163 / 8163 / 18163
(and their many geometric twins -- 5263, 5513, 5523, 5795, 5923, 5963,
and others Avery lists as compatible), the standard 2"x4" shipping-label
sheet, 10 labels per US Letter page (2 columns x 5 rows).

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
from datetime import datetime

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
    return _wrap_lines(raw_lines)


def _format_qso_date(raw: str) -> str:
    """ADIF QSO_DATE is YYYYMMDD -- render as "24 Aug 2026". Falls back
    to the raw value untouched if it isn't 8 digits (a QSO logged before
    a field was required, or a hand-edited log)."""
    raw = (raw or "").strip()
    if len(raw) == 8 and raw.isdigit():
        try:
            return datetime.strptime(raw, "%Y%m%d").strftime("%-d %b %Y")
        except ValueError:
            pass
    return raw


def _format_qso_time(raw: str) -> str:
    """ADIF TIME_ON is HHMM or HHMMSS, always UTC -- render as the
    "1432Z" ham-log convention (seconds dropped for a label; hundredths
    of a QSO rarely matter once it's printed)."""
    raw = (raw or "").strip()
    if len(raw) in (4, 6) and raw.isdigit():
        return f"{raw[:4]}Z"
    return f"{raw}Z" if raw else ""


def qso_label_lines(qso: dict) -> list[tuple[str, str, float]]:
    """The (text, font, size) lines a QSO label renders -- callsign,
    date/time (UTC), band/mode/freq, RST sent/received -- wrapped to
    fit, same as label_lines() below for the mailing-label case."""
    # " * " (not just extra spaces) as the separator between sub-fields on
    # the same line -- _wrap()'s word-wrap normalizes whitespace via
    # str.split()/" ".join(), which would otherwise collapse a double
    # space down to one and lose the visual separation entirely.
    date_time = " * ".join(
        p for p in (_format_qso_date(qso.get("qso_date", "")), _format_qso_time(qso.get("time_on", ""))) if p
    )

    freq = qso.get("freq", "")
    band_mode_freq = " * ".join(
        p for p in (qso.get("band", ""), qso.get("mode", ""), f"{freq} MHz" if freq else "") if p
    )

    rst_bits = []
    if qso.get("rst_sent"):
        rst_bits.append(f"RST Sent {qso['rst_sent']}")
    if qso.get("rst_rcvd"):
        rst_bits.append(f"RST Rcvd {qso['rst_rcvd']}")

    raw_lines = [qso.get("callsign", ""), date_time, band_mode_freq, " * ".join(rst_bits)]
    raw_lines = [line for line in raw_lines if line]

    return _wrap_lines(raw_lines)


def _wrap_lines(raw_lines: list[str]) -> list[tuple[str, str, float]]:
    """Shared by label_lines() and qso_label_lines(): the first line
    renders bold/larger as a header, the rest as body text, each wrapped
    to the label's inner width."""
    max_width = LABEL_W - 2 * PADDING
    out: list[tuple[str, str, float]] = []
    for i, text in enumerate(raw_lines):
        font, size = (HEADER_FONT, HEADER_SIZE) if i == 0 else (BODY_FONT, BODY_SIZE)
        for wrapped in _wrap(text, font, size, max_width):
            out.append((wrapped, font, size))
    return out


def _render_pdf(lines: list[tuple[str, str, float]], position: int, title: str) -> bytes:
    """Render a US Letter PDF with `lines` filled in at label `position`
    (1-10), everything else left blank -- shared by both label kinds."""
    x0, y0 = _label_origin(position)

    block_height = LINE_HEIGHT * len(lines)
    top_of_block = y0 + (LABEL_H + block_height) / 2
    inner_x = x0 + PADDING

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.setTitle(title)

    y = top_of_block - LINE_HEIGHT
    for text, font, size in lines:
        c.setFont(font, size)
        c.drawString(inner_x, y, text)
        y -= LINE_HEIGHT

    c.showPage()
    c.save()
    return buffer.getvalue()


def generate_avery_5163_pdf(record: QrzRecord, position: int = 1) -> bytes:
    """Render a US Letter PDF with one QSL mailing label filled in at
    `position` (1-10), everything else left blank."""
    return _render_pdf(label_lines(record), position, f"QSL mailing label -- {record.callsign}")


def generate_qso_avery_5163_pdf(qso: dict, position: int = 1) -> bytes:
    """Render a US Letter PDF with one QSO label (callsign, date/time
    UTC, band/mode/freq, RST) filled in at `position` (1-10), everything
    else left blank -- meant to be stuck onto the physical QSL card."""
    return _render_pdf(qso_label_lines(qso), position, f"QSO label -- {qso.get('callsign', '')}")
