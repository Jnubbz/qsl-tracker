"""
Generate print-ready QSL label PDFs -- two kinds, two different physical
sheets:

  * A **mailing label** (`generate_mailing_label_pdf`) from a QrzRecord
    -- the recipient's name/address, for the envelope a card goes out
    in. Printed on **Avery 8160** (and geometric twins -- 5160, 5260,
    5520, 8460, 8660, and others sharing the same layout): the standard
    1"x2-5/8" address-label sheet, 30 per US Letter page (3 columns x
    10 rows) -- what Josh actually has on hand.
  * A **QSO label** (`generate_qso_label_pdf`) from one of Josh's own
    logged QSOs (see photomap_store.py's `my_qsos` -- imported from his
    ADIF log, never QRZ, since QRZ only knows a station's own profile,
    not the specifics of a contact with them) -- callsign as a header,
    date/time in UTC underneath, then a two-column grid of whichever of
    Band/Mode/Freq/Grid square/RST sent/RST received that QSO actually
    has. Printed on **Avery 5163** (and twins -- 8163, 18163, 5263,
    5513, 5523, 5795, 5923, 5963): the 2"x4" shipping-label sheet, 10
    per page (2 columns x 5 rows) -- deliberately kept at this size
    since it's meant to be cut out and stuck onto a blank 2"x4" spot on
    the physical QSL card itself, not printed on whatever address-label
    stock happens to be on hand. The grid layout (`_render_qso_grid_pdf_multi`)
    is purpose-built to use the full cell rather than reusing the
    generic stacked-line renderer the mailing label uses.

Both sheet geometries are Avery's own published specs, each
hand-verified to add up to an exact 8.5in x 11in US Letter sheet:
    LEFT_MARGIN + columns*LABEL_W + (columns-1)*H_GAP + RIGHT_MARGIN == 8.5in
    TOP_MARGIN + rows*LABEL_H + BOTTOM_MARGIN == 11.0in

A single-label PDF (`generate_mailing_label_pdf`/`generate_qso_label_pdf`)
is always a full US Letter page with only ONE label position filled in
and the rest left blank, so a partially-used physical sheet (common
once you're printing labels one lookup at a time) can be fed back into
the printer at whichever position is still unused. A **batch** PDF
(`generate_mailing_batch_pdf`/`generate_qso_batch_pdf`) is the same
idea generalized: several different labels, each at its own explicitly
chosen position, on one page -- everything else still left blank. Both
kinds share one page-rendering core (`_render_pdf_multi`) so there's
only one place that knows how to lay text out inside a label cell.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from qrz import QrzRecord

PAGE_W, PAGE_H = letter  # 8.5in x 11in, in points


@dataclass(frozen=True)
class SheetLayout:
    """One physical label-sheet geometry, plus the type sizing tuned to
    fit comfortably inside a label that size. `columns`/`rows` number
    positions left-to-right, top-to-bottom starting at 1."""

    label_w: float
    label_h: float
    top_margin: float
    left_margin: float
    h_pitch: float  # column-to-column distance (label width + horizontal gap)
    v_pitch: float  # row-to-row distance (label height + vertical gap, if any)
    columns: int
    rows: int
    padding: float  # inset from the label's own edges, so text never sits at a die-cut edge
    header_font: str
    header_size: float
    body_font: str
    body_size: float
    line_height: float

    @property
    def count(self) -> int:
        return self.columns * self.rows

    def clamp(self, position: int) -> int:
        return max(1, min(self.count, position))

    def origin(self, position: int) -> tuple[float, float]:
        """Bottom-left corner (PDF points, origin bottom-left of the
        page) of the label at `position`."""
        index = self.clamp(position) - 1
        row, col = divmod(index, self.columns)
        x = self.left_margin + col * self.h_pitch
        y = PAGE_H - self.top_margin - (row + 1) * self.v_pitch
        return x, y


# Avery 8160 / 5160 / 5260 / 5520 / 8460 / 8660 and other geometric twins:
# 1" x 2-5/8" address labels, 30 per US Letter page (3 columns x 10 rows).
# Confirmed against a source showing the exact arithmetic check:
#   0.1875in + 3*2.625in + 2*0.125in + 0.1875in == 8.5in
#   0.5in + 10*1.0in + 0.5in == 11.0in
# Type sized down from the 5163 layout below to comfortably fit a label
# a fifth the area -- 4 lines at ~8-9pt is what real address labels
# print at this size.
AVERY_8160 = SheetLayout(
    label_w=2.625 * inch,
    label_h=1.0 * inch,
    top_margin=0.5 * inch,
    left_margin=0.1875 * inch,
    h_pitch=2.75 * inch,
    v_pitch=1.0 * inch,
    columns=3,
    rows=10,
    padding=0.09 * inch,
    header_font="Helvetica-Bold",
    header_size=9,
    body_font="Helvetica",
    body_size=8,
    line_height=0.155 * inch,
)

# Avery 5163 / 8163 / 18163 and other geometric twins: 2"x4" shipping
# labels, 10 per US Letter page (2 columns x 5 rows). Confirmed against
# Avery's own published spec, hand-verified the same way:
#   0.162in + 2*4.0in + 0.188in + 0.15in == 8.5in
#   0.5in + 5*2.0in + 0.5in == 11.0in
AVERY_5163 = SheetLayout(
    label_w=4.0 * inch,
    label_h=2.0 * inch,
    top_margin=0.5 * inch,
    left_margin=0.162 * inch,
    h_pitch=4.188 * inch,
    v_pitch=2.0 * inch,
    columns=2,
    rows=5,
    padding=0.2 * inch,
    header_font="Helvetica-Bold",
    header_size=12,
    body_font="Helvetica",
    body_size=10.5,
    line_height=0.19 * inch,
)

MAILING_LAYOUT = AVERY_8160
QSO_LAYOUT = AVERY_5163

# Kept for backward compatibility with anything importing the old flat
# constants directly.
MAILING_LABEL_COUNT = MAILING_LAYOUT.count
QSO_LABEL_COUNT = QSO_LAYOUT.count


def clamp_mailing_position(position: int) -> int:
    return MAILING_LAYOUT.clamp(position)


def clamp_qso_position(position: int) -> int:
    return QSO_LAYOUT.clamp(position)


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


def _wrap_lines(raw_lines: list[str], layout: SheetLayout) -> list[tuple[str, str, float]]:
    """Used by label_lines() (the mailing label's generic stacked-line
    renderer): the first line renders bold/larger as a header, the rest
    as body text, each wrapped to fit the given layout's label width."""
    max_width = layout.label_w - 2 * layout.padding
    out: list[tuple[str, str, float]] = []
    for i, text in enumerate(raw_lines):
        font, size = (
            (layout.header_font, layout.header_size)
            if i == 0
            else (layout.body_font, layout.body_size)
        )
        for wrapped in _wrap(text, font, size, max_width):
            out.append((wrapped, font, size))
    return out


def _city_line(record: QrzRecord) -> str:
    city_line = ", ".join(p for p in (record.city, record.state) if p)
    if record.zip_code:
        city_line = f"{city_line} {record.zip_code}".strip()
    return city_line


def label_lines(record: QrzRecord) -> list[tuple[str, str, float]]:
    """The (text, font, size) lines a mailing label for `record` renders,
    already wrapped to fit the Avery 8160 layout -- shared by the PDF
    generator and the on-page preview so they never drift apart."""
    header = record.name.strip()
    header = f"{header}  ({record.callsign})" if header else record.callsign

    raw_lines = [header] + [
        line for line in (record.address, _city_line(record), record.country) if line
    ]
    return _wrap_lines(raw_lines, MAILING_LAYOUT)


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


def qso_label_fields(qso: dict) -> dict:
    """Structured content for the QSO label's two-column details grid:
    `header` (callsign), `subheader` (date/time, UTC), and `pairs` (a
    list of (label, value) tuples -- Band/Mode/Freq/Grid/RST Sent/RST
    Rcvd, each included only if that QSO actually has a value for it,
    so an older record missing e.g. a grid square just renders fewer
    pairs rather than a blank one). Shared by the PDF grid renderer and
    the on-page preview so they never drift apart."""
    header = qso.get("callsign", "")
    subheader = " * ".join(
        p for p in (_format_qso_date(qso.get("qso_date", "")), _format_qso_time(qso.get("time_on", ""))) if p
    )

    freq = qso.get("freq", "")
    pairs: list[tuple[str, str]] = []
    if qso.get("band"):
        pairs.append(("Band", qso["band"]))
    if qso.get("mode"):
        pairs.append(("Mode", qso["mode"]))
    if freq:
        pairs.append(("Freq", f"{freq} MHz"))
    if qso.get("gridsquare"):
        pairs.append(("Grid", qso["gridsquare"]))
    if qso.get("rst_sent"):
        pairs.append(("RST Sent", qso["rst_sent"]))
    if qso.get("rst_rcvd"):
        pairs.append(("RST Rcvd", qso["rst_rcvd"]))

    return {"header": header, "subheader": subheader, "pairs": pairs}


def _render_pdf_multi(
    entries: list[tuple[list[tuple[str, str, float]], int]], title: str, layout: SheetLayout
) -> bytes:
    """Render a single US Letter PDF page with each (lines, position) in
    `entries` filled in at its own label position on the given `layout`,
    everything else left blank. Shared by every PDF generator below --
    the single-label ones just pass a one-item list, so there's exactly
    one place that knows how to lay text out inside a label cell."""
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.setTitle(title)

    for lines, position in entries:
        x0, y0 = layout.origin(position)
        block_height = layout.line_height * len(lines)
        top_of_block = y0 + (layout.label_h + block_height) / 2
        inner_x = x0 + layout.padding

        y = top_of_block - layout.line_height
        for text, font, size in lines:
            c.setFont(font, size)
            c.drawString(inner_x, y, text)
            y -= layout.line_height

    c.showPage()
    c.save()
    return buffer.getvalue()


def _render_pdf(
    lines: list[tuple[str, str, float]], position: int, title: str, layout: SheetLayout
) -> bytes:
    """Render a US Letter PDF with `lines` filled in at label `position`
    on the given `layout`, everything else left blank -- the
    one-label case of _render_pdf_multi() above."""
    return _render_pdf_multi([(lines, position)], title, layout)


# Type scale for the QSO label's two-column details grid -- distinct
# from SheetLayout.header_font/body_font (which stay in place for the
# generic stacked-line renderer above, still used by the mailing
# label). Sized to actually fill the Avery 5163's 4"x2" cell now that
# the label isn't just a handful of short centered lines.
_QSO_HEADER_FONT, _QSO_HEADER_SIZE = "Helvetica-Bold", 18
_QSO_SUB_FONT, _QSO_SUB_SIZE = "Helvetica", 11
_QSO_PAIR_LABEL_FONT, _QSO_PAIR_LABEL_SIZE = "Helvetica-Bold", 10.5
_QSO_PAIR_VALUE_FONT, _QSO_PAIR_VALUE_SIZE = "Helvetica", 10.5
_QSO_ROW_HEIGHT = 0.32 * inch


def _render_qso_grid_pdf_multi(entries: list[tuple[dict, int]], title: str) -> bytes:
    """Render a single US Letter PDF with each `(qso, position)` in
    `entries` drawn as a two-column details grid at its own Avery 5163
    position, everything else left blank: the callsign as a centered
    header banner, the date/time underneath, a divider rule, then
    Band/Mode/Freq/Grid/RST Sent/RST Rcvd (whichever the QSO actually
    has) as labeled pairs two to a row -- filling the full 4"x2" cell
    instead of a few short centered lines huddled in the middle of it."""
    layout = QSO_LAYOUT
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    c.setTitle(title)

    inner_w = layout.label_w - 2 * layout.padding
    col_w = inner_w / 2

    for qso, position in entries:
        fields = qso_label_fields(qso)
        x0, y0 = layout.origin(position)
        center_x = x0 + layout.label_w / 2
        top = y0 + layout.label_h - layout.padding

        c.setFont(_QSO_HEADER_FONT, _QSO_HEADER_SIZE)
        y = top - _QSO_HEADER_SIZE * 0.85
        c.drawCentredString(center_x, y, fields["header"])

        if fields["subheader"]:
            c.setFont(_QSO_SUB_FONT, _QSO_SUB_SIZE)
            y -= _QSO_SUB_SIZE + 4
            c.drawCentredString(center_x, y, fields["subheader"])

        pairs = fields["pairs"]
        if pairs:
            y -= 10
            c.setLineWidth(0.75)
            c.line(x0 + layout.padding, y, x0 + layout.label_w - layout.padding, y)
            y -= _QSO_ROW_HEIGHT * 0.75

            for i, (label, value) in enumerate(pairs):
                row, col = divmod(i, 2)
                cell_x = x0 + layout.padding + col * col_w
                cell_y = y - row * _QSO_ROW_HEIGHT

                c.setFont(_QSO_PAIR_LABEL_FONT, _QSO_PAIR_LABEL_SIZE)
                label_text = f"{label}: "
                c.drawString(cell_x, cell_y, label_text)

                c.setFont(_QSO_PAIR_VALUE_FONT, _QSO_PAIR_VALUE_SIZE)
                value_x = cell_x + stringWidth(label_text, _QSO_PAIR_LABEL_FONT, _QSO_PAIR_LABEL_SIZE)
                c.drawString(value_x, cell_y, value)

    c.showPage()
    c.save()
    return buffer.getvalue()


def generate_mailing_label_pdf(record: QrzRecord, position: int = 1) -> bytes:
    """Render a US Letter PDF with one QSL mailing label filled in at
    `position` (1-30, Avery 8160 layout), everything else left blank."""
    return _render_pdf(
        label_lines(record), position, f"QSL mailing label -- {record.callsign}", MAILING_LAYOUT
    )


def generate_qso_label_pdf(qso: dict, position: int = 1) -> bytes:
    """Render a US Letter PDF with one QSO label -- callsign, date/time
    UTC, then Band/Mode/Freq/Grid/RST as a two-column details grid --
    filled in at `position` (1-10, Avery 5163 layout), everything else
    left blank -- meant to be stuck onto the physical QSL card."""
    return _render_qso_grid_pdf_multi([(qso, position)], f"QSO label -- {qso.get('callsign', '')}")


def generate_mailing_batch_pdf(items: list[tuple[QrzRecord, int]]) -> bytes:
    """Render a single US Letter PDF with several different QSL mailing
    labels, each `(record, position)` pair filled in at its own
    position (1-30, Avery 8160 layout) -- so one sheet can carry more
    than one address instead of using a whole page for just one."""
    entries = [(label_lines(record), position) for record, position in items]
    return _render_pdf_multi(entries, "QSL mailing label batch", MAILING_LAYOUT)


def generate_qso_batch_pdf(items: list[tuple[dict, int]]) -> bytes:
    """Render a single US Letter PDF with several different QSO labels,
    each `(qso, position)` pair drawn as its own two-column details
    grid at its own position (1-10, Avery 5163 layout) -- so one sheet
    can carry more than one QSO instead of using a whole page for just
    one."""
    return _render_qso_grid_pdf_multi(items, "QSO label batch")
