import numpy as np
import pytest
from PIL import Image

from stillpalette.palette import (
    distinct_from,
    extract_palette,
    extract_pool,
    from_hex,
    lab_to_rgb,
    order_swatches,
    rgb_to_lab,
    to_hex,
)
from stillpalette.render import (
    HEX_FILL,
    RenderOptions,
    _fit_font,
    _measure,
    _swatch_spans,
    render_polaroid,
)


def striped(colours, rows):
    """An image built from horizontal bands of known colour and known height."""
    height = sum(rows)
    img = Image.new("RGB", (100, height))
    px = img.load()
    y = 0
    for colour, count in zip(colours, rows):
        for _ in range(count):
            for x in range(100):
                px[x, y] = colour
            y += 1
    return img


# ----------------------------------------------------------------- colour


def test_lab_roundtrip_is_lossless():
    rng = np.random.default_rng(0)
    rgb = rng.integers(0, 256, (500, 3)).astype(float)
    assert np.abs(lab_to_rgb(rgb_to_lab(rgb)) - rgb).max() < 1e-6


def test_hex_roundtrip():
    assert from_hex("#1A2B3C") == (26, 43, 60)
    assert from_hex("1a2b3c") == (26, 43, 60)
    assert from_hex("#abc") == (170, 187, 204)
    assert to_hex((26, 43, 60)) == "#1A2B3C"


@pytest.mark.parametrize("bad", ["", "#12345", "nope", "#GGGGGG"])
def test_bad_hex_rejected(bad):
    with pytest.raises(ValueError):
        from_hex(bad)


# -------------------------------------------------------------- extraction


def test_recovers_known_colours_in_dominance_order():
    colours = [(220, 30, 40), (20, 90, 200), (240, 230, 60), (15, 15, 15)]
    img = striped(colours, [40, 30, 20, 10])

    result = extract_palette(img, 4)

    assert [s.hex for s in result] == [to_hex(c) for c in colours]
    assert [round(s.weight, 2) for s in result] == [0.4, 0.3, 0.2, 0.1]


def test_extraction_is_deterministic():
    img = striped([(200, 10, 10), (10, 200, 10), (10, 10, 200)], [50, 30, 20])
    assert [s.hex for s in extract_palette(img, 3)] == [
        s.hex for s in extract_palette(img, 3)
    ]


def test_fewer_unique_colours_than_requested_returns_fewer():
    """No padding with duplicates -- the caller surfaces the shortfall."""
    img = striped([(255, 0, 0), (0, 0, 255)], [25, 25])
    result = extract_palette(img, 6)
    assert len(result) == 2
    assert {s.hex for s in result} == {"#FF0000", "#0000FF"}


def test_single_colour_image():
    result = extract_palette(Image.new("RGB", (40, 40), (10, 120, 60)), 5)
    assert len(result) == 1
    assert result[0].hex == "#0A783C"
    assert result[0].weight == pytest.approx(1.0)


def test_transparency_does_not_become_black():
    img = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    img.paste((255, 40, 40, 255), (0, 0, 40, 20))
    hexes = {s.hex for s in extract_palette(img, 2)}
    assert "#FF2828" in hexes
    assert "#000000" not in hexes


def test_weights_sum_to_one():
    img = striped([(1, 2, 3), (250, 250, 250), (120, 60, 30)], [10, 20, 30])
    assert sum(s.weight for s in extract_palette(img, 3)) == pytest.approx(1.0)


def test_n_must_be_positive():
    with pytest.raises(ValueError):
        extract_palette(Image.new("RGB", (10, 10)), 0)


# --------------------------------------------------------------- re-rolls


def test_pool_offers_more_colours_than_the_palette():
    rng = np.random.default_rng(1)
    img = Image.fromarray(
        rng.integers(0, 256, (80, 80, 3), dtype=np.uint8), mode="RGB"
    )
    assert len(extract_pool(img, 5)) > len(extract_palette(img, 5))


def test_distinct_from_excludes_near_duplicates():
    img = striped([(220, 30, 40), (20, 90, 200), (240, 230, 60)], [30, 30, 40])
    pool = extract_pool(img, 3)
    taken = [s.hex for s in extract_palette(img, 3)]
    for candidate in distinct_from(pool, taken):
        assert candidate.hex not in taken


def test_distinct_from_with_nothing_taken_returns_everything():
    img = striped([(10, 10, 10), (200, 200, 200)], [20, 20])
    pool = extract_pool(img, 2)
    assert len(distinct_from(pool, [])) == len(pool)


def test_smooth_gradient_still_offers_alternates():
    """A gradient packs every pool candidate close to the palette. A single
    fixed threshold rejects all of them and leaves re-roll permanently dead."""
    ramp = Image.new("RGB", (200, 200))
    px = ramp.load()
    for y in range(200):
        for x in range(200):
            px[x, y] = (40 + x // 2, 60 + y // 3, 150 + x // 8)

    base = extract_palette(ramp, 5)
    alternates = distinct_from(extract_pool(ramp, 5), [s.hex for s in base])

    assert alternates, "re-roll would have nothing to offer"
    assert all(s.hex not in {b.hex for b in base} for s in alternates)


def test_two_colour_image_honestly_offers_no_alternates():
    """Relaxing the threshold must not start inventing duplicates."""
    img = striped([(255, 0, 0), (0, 0, 255)], [30, 30])
    base = extract_palette(img, 2)
    assert distinct_from(extract_pool(img, 2), [s.hex for s in base]) == []


# --------------------------------------------------------------- ordering


def swatch(hexval, weight=None):
    return {"hex": hexval, "rgb": list(from_hex(hexval)), "weight": weight}


def test_dominance_order():
    items = [swatch("#FF0000", 0.2), swatch("#00FF00", 0.5), swatch("#0000FF", 0.3)]
    assert [s["hex"] for s in order_swatches(items, "dominance")] == [
        "#00FF00",
        "#0000FF",
        "#FF0000",
    ]


def test_picked_colours_sort_last_under_dominance():
    """An eyedropped colour has no cluster share, so it must not be treated
    as though it had the largest one."""
    items = [swatch("#123456", None), swatch("#00FF00", 0.5), swatch("#0000FF", 0.3)]
    assert [s["hex"] for s in order_swatches(items, "dominance")][-1] == "#123456"


def test_lightness_order_runs_dark_to_light():
    items = [swatch("#FFFFFF"), swatch("#000000"), swatch("#808080")]
    assert [s["hex"] for s in order_swatches(items, "lightness")] == [
        "#000000",
        "#808080",
        "#FFFFFF",
    ]


def test_hue_order_groups_greys_instead_of_scattering_them():
    """Hue angle is meaningless for near-greys; left unhandled they land at
    random points along the bar."""
    items = [
        swatch("#FF0000"),
        swatch("#2E2E2F"),  # near-grey, nominally blue hue
        swatch("#00FF00"),
        swatch("#D8D8D6"),  # near-grey, nominally yellow hue
        swatch("#0000FF"),
    ]
    ordered = [s["hex"] for s in order_swatches(items, "hue")]
    assert ordered[:2] == ["#2E2E2F", "#D8D8D6"]
    assert ordered[2:] == ["#FF0000", "#00FF00", "#0000FF"]


def test_reverse_flips_any_order():
    items = [swatch("#FF0000", 0.5), swatch("#00FF00", 0.3), swatch("#0000FF", 0.2)]
    forward = order_swatches(items, "dominance")
    backward = order_swatches(items, "dominance", reverse=True)
    assert [s["hex"] for s in backward] == [s["hex"] for s in forward][::-1]


def test_custom_order_is_left_untouched():
    items = [swatch("#0000FF", 0.1), swatch("#FF0000", 0.9)]
    assert [s["hex"] for s in order_swatches(items, "custom")] == ["#0000FF", "#FF0000"]


def test_unknown_order_mode_rejected():
    with pytest.raises(ValueError):
        order_swatches([swatch("#FFFFFF")], "sideways")


# ---------------------------------------------------------------- render


@pytest.mark.parametrize("total", [800, 997, 1234])
@pytest.mark.parametrize("count", [2, 5, 7, 12])
@pytest.mark.parametrize("gutter", [3, 17, 40])
def test_swatch_spans_fill_the_width_exactly(total, count, gutter):
    """Any shortfall shows up as a seam of frame colour at the bar's right end."""
    spans = _swatch_spans(total, count, gutter)
    assert len(spans) == count
    last_x, last_w = spans[-1]
    assert last_x + last_w == total
    for (x1, w1), (x2, _) in zip(spans, spans[1:]):
        assert x2 - (x1 + w1) == gutter


def test_swatch_spans_survive_a_gutter_wider_than_the_bar():
    spans = _swatch_spans(50, 10, 40)
    assert spans[-1][0] + spans[-1][1] == 50
    assert all(w > 0 for _, w in spans)


def test_render_geometry_matches_reported_layout():
    photo = Image.new("RGB", (600, 400), (90, 120, 160))
    img, layout = render_polaroid(
        photo, ["#FF0000", "#00FF00", "#0000FF"], RenderOptions(max_edge=900)
    )
    assert img.size == (layout.width, layout.height)
    assert layout.photo_w == 600 and layout.photo_h == 400
    assert layout.photo_x == layout.border == layout.photo_y
    # Bottom bezel must be deeper than the side border, or it is not a Polaroid.
    below = layout.height - (layout.photo_y + layout.photo_h)
    assert below > layout.border * 3


def test_bar_spans_the_photo_width_with_no_seam():
    photo = Image.new("RGB", (600, 400), (255, 255, 255))
    colours = ["#FF0000", "#00FF00", "#0000FF", "#FFFF00"]
    img, layout = render_polaroid(
        photo, colours, RenderOptions(border_hex="#FFFFFF", max_edge=900)
    )
    row = np.asarray(img)[layout.photo_y + layout.photo_h + layout.border + 4]
    assert tuple(row[layout.border]) == (255, 0, 0)
    assert tuple(row[layout.border + layout.photo_w - 1]) == (255, 255, 0)


def test_text_colour_flips_for_dark_frames():
    """Hex codes printed in the frame colour would be invisible."""
    photo = Image.new("RGB", (300, 200), (128, 128, 128))
    opts = dict(border_pct=6.0, show_hex=True, max_edge=600)
    light, _ = render_polaroid(
        photo, ["#FF0000", "#00FF00"], RenderOptions(border_hex="#FFFFFF", **opts)
    )
    dark, _ = render_polaroid(
        photo, ["#FF0000", "#00FF00"], RenderOptions(border_hex="#000000", **opts)
    )

    def extremes(img):
        arr = np.asarray(img.convert("L"))
        strip = arr[-int(arr.shape[0] * 0.22) :]
        return strip.min(), strip.max()

    assert extremes(light)[0] < 120  # dark text on a light frame
    assert extremes(dark)[1] > 140  # light text on a dark frame


def test_signature_adds_depth_to_the_bezel():
    photo = Image.new("RGB", (300, 200), (10, 10, 10))
    plain, _ = render_polaroid(photo, ["#FF0000"], RenderOptions(max_edge=600))
    signed, _ = render_polaroid(
        photo,
        ["#FF0000"],
        RenderOptions(
            max_edge=600,
            signature=True,
            signature_text="Hello",
            signature_date="1 Jan 2026",
        ),
    )
    assert signed.height > plain.height


@pytest.mark.parametrize("count", range(2, 13))
@pytest.mark.parametrize("shape", [(1200, 800), (600, 900), (400, 900), (2000, 500)])
@pytest.mark.parametrize("edge", [900, 2000])
def test_hex_labels_never_collide(count, shape, edge):
    """A ratio-derived size, or any meaningful minimum, overflows at high colour
    counts on a narrow photo and the codes run together into a smear."""
    photo = Image.new("RGB", shape, (90, 110, 140))
    colours = ["#%06X" % (i * 0x1F1F1F % 0xFFFFFF) for i in range(count)]
    _, layout = render_polaroid(
        photo, colours, RenderOptions(show_hex=True, border_pct=4.5, max_edge=edge)
    )
    narrowest = min(w for _, w in layout.swatches)
    font, _ = _fit_font("#FFFFFF", narrowest * HEX_FILL, 999)
    assert _measure(font, "#FFFFFF") <= narrowest


def test_hex_font_shrinks_rather_than_hitting_a_floor():
    """Tiny is acceptable at twelve colours; colliding is not."""
    narrow = Image.new("RGB", (400, 900), (90, 110, 140))
    _, layout = render_polaroid(
        narrow,
        ["#%06X" % (i * 0x2F1B4D % 0xFFFFFF) for i in range(12)],
        RenderOptions(show_hex=True, border_pct=4.5, max_edge=900),
    )
    narrowest = min(w for _, w in layout.swatches)
    _, size = _fit_font("#FFFFFF", narrowest * HEX_FILL, 999)
    assert size < 6  # would have been clamped to 6 and overflowed


def test_long_caption_is_scaled_to_clear_the_date():
    photo = Image.new("RGB", (900, 600), (40, 40, 40))
    caption = "A deliberately long caption that would otherwise run under the date"
    date = "26 August 2026"
    img, layout = render_polaroid(
        photo,
        ["#FF0000", "#00FF00"],
        RenderOptions(
            signature=True, signature_text=caption, signature_date=date, max_edge=900
        ),
    )
    sig_h = 0.62 * max(24, round(0.14 * img.width))
    date_font, _ = _fit_font(date, layout.photo_w * 0.45, round(sig_h * 0.37))
    date_w = _measure(date_font, date)
    text_font, _ = _fit_font(
        caption, layout.photo_w - date_w - layout.border, round(sig_h * 0.46)
    )
    assert _measure(text_font, caption) + date_w + layout.border <= layout.photo_w


def test_layout_exposes_swatch_rectangles_for_the_overlay():
    """The client lays interactive controls over the bar using these."""
    photo = Image.new("RGB", (800, 600), (60, 60, 60))
    colours = ["#FF0000", "#00FF00", "#0000FF", "#FFFF00"]
    img, layout = render_polaroid(photo, colours, RenderOptions(max_edge=800))
    rects = layout.to_dict()["swatches"]

    assert len(rects) == len(colours)
    for rect in rects:
        assert 0 <= rect["x"] and rect["x"] + rect["w"] <= img.width
        assert 0 <= rect["y"] and rect["y"] + rect["h"] <= img.height
    # Rectangles must sit on the bar and line up with what was drawn.
    px = np.asarray(img)
    for rect, hexval in zip(rects, colours):
        sample = tuple(px[rect["y"] + rect["h"] // 2, rect["x"] + rect["w"] // 2])
        assert sample == from_hex(hexval)


def test_border_width_is_resolution_independent():
    """The same photo at two sizes must produce the same proportions."""
    small, small_layout = render_polaroid(
        Image.new("RGB", (400, 300)), ["#FF0000"], RenderOptions(max_edge=400)
    )
    big, big_layout = render_polaroid(
        Image.new("RGB", (1600, 1200)), ["#FF0000"], RenderOptions(max_edge=1600)
    )
    assert small.width / small.height == pytest.approx(
        big.width / big.height, abs=0.01
    )
    assert small_layout.border / small.width == pytest.approx(
        big_layout.border / big.width, abs=0.005
    )
