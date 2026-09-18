"""Polaroid composition.

Layout is driven by one number: the border width, expressed as a percentage of
the photo's short edge. Everything else is derived from it, which is what keeps
an 800px photo and a 6000px photo producing visually identical prints.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw

from . import fonts
from .palette import from_hex

# Working sizes. The draft is what the user sees while dragging a slider; the
# final is what a right-click save actually hands over.
DRAFT_EDGE = 900
FINAL_EDGE = 2000

# Colour bar height as a fraction of the frame width -- proportional to the
# print rather than the photo, so a wide panorama does not get a sliver of a bar.
BAR_RATIO = 0.14
BAR_MIN_PX = 24

# Extra depth below the bar with no signature, so the frame reads as a Polaroid
# chin rather than a symmetric border.
CHIN_RATIO = 2.2

# Fraction of a swatch a hex code may occupy. The rest is breathing room, so
# adjacent codes never touch even at twelve colours.
HEX_FILL = 0.86


@dataclass
class RenderOptions:
    border_hex: str = "#FFFFFF"
    border_pct: float = 5.0
    show_hex: bool = False
    signature: bool = False
    signature_text: str = ""
    signature_date: str = ""
    max_edge: int = FINAL_EDGE


@dataclass
class Layout:
    """Geometry of a rendered print, in output pixels.

    Retained alongside the image so the client can map clicks back onto the
    photo (the eyedropper) and lay interactive controls precisely over each
    swatch in the bar.
    """

    border: int
    photo_x: int
    photo_y: int
    photo_w: int
    photo_h: int
    bar_y: int
    bar_h: int
    swatches: list[tuple[int, int]]  # (x, width) in canvas coordinates
    width: int
    height: int

    def to_dict(self) -> dict:
        return {
            "border": self.border,
            "photoX": self.photo_x,
            "photoY": self.photo_y,
            "photoW": self.photo_w,
            "photoH": self.photo_h,
            "barY": self.bar_y,
            "barH": self.bar_h,
            "swatches": [
                {"x": x, "y": self.bar_y, "w": w, "h": self.bar_h}
                for x, w in self.swatches
            ],
            "width": self.width,
            "height": self.height,
        }


def _luminance(rgb) -> float:
    """WCAG relative luminance, used to pick a readable text colour."""

    def channel(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _text_colours(border_rgb):
    """Primary and secondary text colours that stay legible on the frame.

    Without this a black frame would render black hex codes into invisibility,
    and black is an obvious setting for someone to try first.
    """
    if _luminance(border_rgb) > 0.45:
        return (34, 34, 34), (122, 122, 122)
    return (238, 238, 238), (150, 150, 150)


def _measure(font, text: str) -> float:
    try:
        return float(font.getlength(text))
    except AttributeError:  # very old bitmap fallback
        return float(font.getsize(text)[0])


def _fit_font(sample: str, avail: float, max_size: int, min_size: int = 1):
    """Largest font at which `sample` fits inside `avail` pixels.

    Ratio-based sizing is not enough: glyph widths vary by face, so a size that
    fits one font overflows another. There is deliberately no meaningful floor
    -- at twelve colours on a narrow photo the codes go genuinely tiny, which is
    preferable to letting them collide into an unreadable smear.
    """
    size = max(min_size, int(max_size))
    font = fonts.load(size)
    width = _measure(font, sample)
    if width <= avail:
        return font, size

    # Text width is near-linear in size, so one scaled guess lands close; the
    # loop then walks down the last pixel or two of hinting error.
    if width > 0:
        size = max(min_size, int(size * avail / width))
        font = fonts.load(size)
    while size > min_size and _measure(font, sample) > avail:
        size -= 1
        font = fonts.load(size)
    return font, size


def _fit(img: Image.Image, max_edge: int) -> Image.Image:
    if max(img.size) <= max_edge:
        return img.convert("RGB") if img.mode != "RGB" else img
    im = img.copy()
    im.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    return im.convert("RGB") if im.mode != "RGB" else im


def _flatten(img: Image.Image) -> Image.Image:
    """Composite transparency onto white before framing."""
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        backdrop = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(backdrop, rgba).convert("RGB")
    return img.convert("RGB")


def _swatch_spans(total: int, count: int, gutter: int) -> list[tuple[int, int]]:
    """(x, width) per swatch, filling `total` exactly.

    The integer remainder is spread one pixel at a time across the leading
    swatches so the bar lands flush with the photo's right edge -- otherwise a
    thin seam of frame colour appears there at most widths.
    """
    if count <= 0:
        return []
    usable = total - gutter * (count - 1)
    if usable < count:
        # Degenerate: gutters would consume the bar. Drop them.
        gutter = 0
        usable = total
    base, extra = divmod(usable, count)
    spans = []
    x = 0
    for i in range(count):
        w = base + (1 if i < extra else 0)
        spans.append((x, w))
        x += w + gutter
    return spans


def render_polaroid(img: Image.Image, colours: list[str], opts: RenderOptions):
    """Compose the print. Returns (image, layout)."""
    photo = _fit(_flatten(img), opts.max_edge)
    w, h = photo.size

    border_rgb = from_hex(opts.border_hex)
    b = max(1, round(opts.border_pct / 100.0 * min(w, h)))

    n = len(colours)
    bar_h = max(BAR_MIN_PX, round(BAR_RATIO * (w + 2 * b))) if n else 0
    spans = _swatch_spans(w, n, b) if n else []

    fg, muted = _text_colours(border_rgb)

    # Size the hex codes before laying anything out, so the band can shrink to
    # the text rather than leaving a wide empty strip when the codes go small.
    hex_font = None
    hex_size = 0
    hex_h = 0
    if opts.show_hex and n:
        band = round(bar_h * 0.42)
        narrowest = min(sw for _, sw in spans)
        hex_font, hex_size = _fit_font(
            "#FFFFFF", narrowest * HEX_FILL, max(1, round(band * 0.52))
        )
        hex_h = min(band, max(round(hex_size * 2.1), 4))

    sig_h = 0
    if opts.signature and (opts.signature_text or opts.signature_date):
        sig_h = round(bar_h * 0.62)

    chin = round(CHIN_RATIO * b)
    total_h = b + h + (b + bar_h if n else 0) + hex_h + sig_h + chin
    total_w = w + 2 * b

    canvas = Image.new("RGB", (total_w, total_h), border_rgb)
    canvas.paste(photo, (b, b))
    draw = ImageDraw.Draw(canvas)

    y = b + h
    bar_y = y + b
    if n:
        y = bar_y
        for (sx, sw), hexval in zip(spans, colours):
            draw.rectangle(
                [b + sx, y, b + sx + sw - 1, y + bar_h - 1], fill=from_hex(hexval)
            )
        y += bar_h

    if hex_h:
        ty = y + (hex_h - hex_size) / 2
        for (sx, sw), hexval in zip(spans, colours):
            draw.text(
                (b + sx + sw / 2, ty),
                hexval.upper(),
                font=hex_font,
                fill=muted,
                anchor="ma",
            )
        y += hex_h

    if sig_h:
        cy = y + sig_h / 2
        date_w = 0.0
        date_font = None
        if opts.signature_date:
            date_font, _ = _fit_font(
                opts.signature_date, w * 0.45, round(sig_h * 0.37)
            )
            date_w = _measure(date_font, opts.signature_date)
        if opts.signature_text:
            # The caption gets whatever the date leaves, so a long one scales
            # down instead of running underneath it.
            avail = w - date_w - (b if date_w else 0)
            text_font, _ = _fit_font(
                opts.signature_text, max(1.0, avail), round(sig_h * 0.46)
            )
            draw.text((b, cy), opts.signature_text, font=text_font, fill=fg, anchor="lm")
        if date_font is not None:
            draw.text(
                (b + w, cy), opts.signature_date, font=date_font, fill=muted, anchor="rm"
            )

    layout = Layout(
        border=b,
        photo_x=b,
        photo_y=b,
        photo_w=w,
        photo_h=h,
        bar_y=bar_y,
        bar_h=bar_h,
        swatches=[(b + sx, sw) for sx, sw in spans],
        width=total_w,
        height=total_h,
    )
    return canvas, layout
