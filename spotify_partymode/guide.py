"""Printable guest guide (PDF).

Builds a one-page "how to join the party" sheet with a QR code and the join
URL derived from the configured Spotify redirect (callback) URI. The admin
prints this and puts it up so guests can scan and join.

Kept dependency-light: fpdf2 (pure-Python PDF) + segno (pure-Python QR).
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

from . import db, settings_store

# Spotify-ish palette to match the app's dark UI accent.
_GREEN = (29, 185, 84)
_DARK = (18, 18, 18)
_GREY = (110, 110, 110)
_LIGHT = (240, 240, 240)


class GuideError(Exception):
    """Raised when the guide cannot be built (e.g. no usable join URL)."""


def _l1(text: str) -> str:
    """Make text safe for the latin-1 core PDF font (umlauts pass through).

    Any character the core font cannot render (e.g. emoji in a party name) is
    replaced instead of crashing the whole guide.
    """
    return str(text).encode("latin-1", "replace").decode("latin-1")


def join_url() -> str:
    """Derive the guest join URL (scheme://host[:port]) from the redirect URI."""
    _, _, redirect = settings_store.get_spotify_credentials()
    parts = urlsplit(redirect.strip())
    if not parts.scheme or not parts.netloc:
        raise GuideError(
            "No usable address could be read from the redirect URI. "
            "Set a valid Redirect URI in settings first."
        )
    return f"{parts.scheme}://{parts.netloc}/"


def _draw_qr(pdf, url: str, x: float, y: float, size: float) -> None:
    """Draw the QR code as crisp vector rectangles (no image decoding needed).

    Rendering the modules directly avoids relying on fpdf2's built-in PNG
    decoder (which mangles segno's paletted output) and needs no Pillow.
    """
    import segno

    matrix = segno.make(url, error="m").matrix
    modules = len(matrix)
    border = 4  # standard quiet zone, in modules
    total = modules + 2 * border
    unit = size / total

    # White background incl. the quiet zone (required for scanners).
    pdf.set_fill_color(255, 255, 255)
    pdf.rect(x, y, size, size, style="F")

    pdf.set_fill_color(0, 0, 0)
    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            if dark:
                pdf.rect(
                    x + (c + border) * unit,
                    y + (r + border) * unit,
                    unit,
                    unit,
                    style="F",
                )


def build_guide_pdf() -> bytes:
    """Return the guest guide as PDF bytes (German, A4 portrait)."""
    from fpdf import FPDF

    url = join_url()

    # Contextual values shown on the sheet.
    session = db.get_session(db.current_session_id()) if db.current_session_id() else None
    party_name = session["name"] if session else None
    tokens = settings_store.get_skip_tokens_per_hour()
    max_artists = settings_store.get_guest_block_artists_max()
    max_tracks = settings_store.get_guest_block_tracks_max()

    pdf = FPDF(format="A4")
    # Fixed one-pager: disable auto page-break so nothing ever spills over.
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()
    page_w = pdf.w
    inner_w = page_w - 2 * pdf.l_margin

    # --- header band ---
    pdf.set_fill_color(*_GREEN)
    pdf.rect(0, 0, page_w, 42, style="F")
    pdf.set_xy(0, 10)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("helvetica", "B", 30)
    pdf.cell(page_w, 12, "Party Mode", align="C")
    pdf.set_xy(0, 24)
    pdf.set_font("helvetica", "", 15)
    pdf.cell(page_w, 8, "So machst du mit", align="C")

    # --- intro ---
    pdf.set_xy(pdf.l_margin, 52)
    pdf.set_text_color(*_DARK)
    pdf.set_font("helvetica", "", 13)
    pdf.multi_cell(
        inner_w,
        7,
        _l1(
            "Scanne den QR-Code mit deiner Handykamera oder gib die Adresse im Browser ein. "
            "Danach nur noch deinen Namen eingeben - fertig!"
        ),
        align="C",
    )

    # --- QR code ---
    qr_size = 70
    qr_x = (page_w - qr_size) / 2
    qr_y = pdf.get_y() + 4
    _draw_qr(pdf, url, qr_x, qr_y, qr_size)

    # --- URL box ---
    box_y = qr_y + qr_size + 6
    box_h = 16
    pdf.set_fill_color(*_LIGHT)
    pdf.rect(pdf.l_margin, box_y, inner_w, box_h, style="F")
    pdf.set_xy(pdf.l_margin, box_y + 3)
    pdf.set_text_color(*_GREY)
    pdf.set_font("helvetica", "", 9)
    pdf.cell(inner_w, 4, "Adresse", align="C")
    pdf.set_xy(pdf.l_margin, box_y + 7)
    pdf.set_text_color(*_DARK)
    pdf.set_font("courier", "B", 15)
    pdf.cell(inner_w, 7, url, align="C")

    if party_name:
        pdf.set_xy(pdf.l_margin, box_y + box_h + 4)
        pdf.set_text_color(*_GREEN)
        pdf.set_font("helvetica", "B", 13)
        pdf.cell(inner_w, 7, _l1(f"Party: {party_name}"), align="C")

    # --- steps ---
    pdf.ln(10)
    pdf.set_x(pdf.l_margin)
    pdf.set_text_color(*_DARK)
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(inner_w, 8, _l1("Und so geht's:"))
    pdf.ln(9)

    steps = [
        "QR-Code scannen oder die Adresse oben im Browser öffnen.",
        "Deinen Namen eingeben und der Party beitreten.",
        "Songs suchen und zur Wunschliste hinzufügen.",
    ]
    if tokens > 0:
        steps.append(
            f"Song nervt? Du hast {tokens} Skip-Token pro Stunde, um Songs zu überspringen "
            "(füllt sich jede volle Stunde wieder auf)."
        )
    if max_artists > 0 or max_tracks > 0:
        steps.append(
            f"Gar keine Lust auf bestimmte Musik? Blocke bis zu {max_artists} Künstler und "
            f"{max_tracks} Songs für die Party - sie werden dann übersprungen."
        )

    pdf.set_font("helvetica", "", 12)
    for i, step in enumerate(steps, start=1):
        y0 = pdf.get_y()
        pdf.set_text_color(*_GREEN)
        pdf.set_font("helvetica", "B", 12)
        pdf.set_x(pdf.l_margin)
        pdf.cell(8, 7, f"{i}.")
        pdf.set_text_color(*_DARK)
        pdf.set_font("helvetica", "", 12)
        pdf.set_xy(pdf.l_margin + 8, y0)
        pdf.multi_cell(inner_w - 8, 7, _l1(step))
        pdf.ln(1)

    # --- footer ---
    pdf.set_y(-18)
    pdf.set_text_color(*_GREY)
    pdf.set_font("helvetica", "", 8)
    stamp = time.strftime("%d.%m.%Y %H:%M")
    pdf.cell(inner_w, 6, f"Spotify Party Mode · erstellt am {stamp}", align="C")

    out = pdf.output()
    return bytes(out)
