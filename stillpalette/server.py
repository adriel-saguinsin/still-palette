"""Flask app for Still Palette.

The server holds decoded images and renders PNGs. It is deliberately stateless
about palette *edits* -- the client owns that state and posts explicit colours,
which keeps render_polaroid a pure function of its arguments.
"""

from __future__ import annotations

import hashlib
import io
import json
import secrets
from collections import OrderedDict
from datetime import date

from flask import Flask, abort, jsonify, render_template, request, send_file
from PIL import Image, ImageOps, UnidentifiedImageError

from .palette import (
    Swatch,
    distinct_from,
    extract_palette,
    extract_pool,
    from_hex,
    order_swatches,
    to_hex,
)
from .render import DRAFT_EDGE, FINAL_EDGE, RenderOptions, render_polaroid

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# MPO is what many phones emit for a plain JPEG; GIF covers exports and
# screen recordings people reasonably expect to work.
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP", "BMP", "TIFF", "MPO", "GIF"}

# Guard against decompression bombs. 80MP comfortably covers real cameras.
Image.MAX_IMAGE_PIXELS = 80_000_000

MAX_IMAGES = 8
MAX_RENDERS = 24
MAX_PALETTES = 32

MIN_COLOURS, MAX_COLOURS = 2, 12


class _LRU(OrderedDict):
    def __init__(self, cap: int):
        super().__init__()
        self.cap = cap

    def put(self, key, value):
        if key in self:
            self.move_to_end(key)
        self[key] = value
        while len(self) > self.cap:
            self.popitem(last=False)

    def get_lru(self, key):
        if key not in self:
            return None
        self.move_to_end(key)
        return self[key]


images = _LRU(MAX_IMAGES)      # token -> {"image", "name"}
palettes = _LRU(MAX_PALETTES)  # (token, n) -> {"base", "pool"}
renders = _LRU(MAX_RENDERS)    # render_id -> {"png", "layout", "token"}


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

    @app.get("/")
    def index():
        return render_template("index.html", today=date.today().isoformat())

    @app.post("/api/upload")
    def upload():
        file = request.files.get("image")
        if file is None or not file.filename:
            return jsonify(error="No image was uploaded."), 400

        raw = file.read()
        if not raw:
            return jsonify(error="That file was empty."), 400

        # verify() then reopen: verify consumes the file object, so the decode
        # has to start from a fresh stream.
        try:
            probe = Image.open(io.BytesIO(raw))
            fmt = probe.format
            probe.verify()
        except (UnidentifiedImageError, OSError, ValueError):
            return jsonify(error="That does not look like an image file."), 400

        if fmt not in ALLOWED_FORMATS:
            return jsonify(error=f"{fmt or 'That format'} is not supported."), 400

        try:
            img = Image.open(io.BytesIO(raw))
            # Honour EXIF orientation, or phone photos frame up sideways.
            img = ImageOps.exif_transpose(img)
            img.load()
        except (OSError, ValueError):
            return jsonify(error="That image could not be read."), 400

        token = secrets.token_urlsafe(12)
        images.put(token, {"image": img, "name": file.filename})
        return jsonify(
            token=token, width=img.width, height=img.height, name=file.filename
        )

    @app.post("/api/extract")
    def extract():
        data = request.get_json(silent=True) or {}
        entry = images.get_lru(data.get("token"))
        if entry is None:
            return jsonify(error="expired"), 404

        try:
            n = int(data.get("n", 5))
        except (TypeError, ValueError):
            return jsonify(error="Invalid colour count."), 400
        n = max(MIN_COLOURS, min(MAX_COLOURS, n))

        key = (data["token"], n)
        cached = palettes.get_lru(key)
        if cached is None:
            base = extract_palette(entry["image"], n)
            pool = extract_pool(entry["image"], n)
            cached = {"base": base, "pool": pool}
            palettes.put(key, cached)

        base: list[Swatch] = cached["base"]
        alternates = distinct_from(cached["pool"], [s.hex for s in base])
        return jsonify(
            base=[s.to_dict() for s in base],
            pool=[s.to_dict() for s in alternates],
            requested=n,
            # Fewer than requested means the image genuinely lacks that many
            # distinct colours; the UI says so rather than padding duplicates.
            short=len(base) < n,
        )

    @app.post("/api/render")
    def render():
        data = request.get_json(silent=True) or {}
        token = data.get("token")
        entry = images.get_lru(token)
        if entry is None:
            return jsonify(error="expired"), 404

        # Ordering happens here rather than in the client so hue and lightness
        # use the same Lab maths as extraction, and so a render costs one round
        # trip instead of two.
        try:
            items = [_normalise(c) for c in (data.get("colors") or [])]
        except (KeyError, ValueError, TypeError):
            return jsonify(error="Invalid colour."), 400
        if not items:
            return jsonify(error="No colours to render."), 400

        mode = data.get("order", "dominance")
        if mode not in ("dominance", "hue", "lightness", "custom"):
            return jsonify(error="Invalid order mode."), 400
        ordered = order_swatches(items, mode, bool(data.get("reverse")))
        colours = [i["hex"] for i in ordered]

        raw = data.get("options") or {}
        try:
            border_hex = to_hex(from_hex(raw.get("borderHex", "#FFFFFF")))
            border_pct = float(raw.get("borderPct", 5.0))
        except (ValueError, TypeError):
            return jsonify(error="Invalid frame settings."), 400
        border_pct = max(0.5, min(12.0, border_pct))

        draft = bool(raw.get("draft"))
        opts = RenderOptions(
            border_hex=border_hex,
            border_pct=border_pct,
            show_hex=bool(raw.get("showHex")),
            signature=bool(raw.get("signature")),
            signature_text=str(raw.get("signatureText") or "")[:120],
            signature_date=str(raw.get("signatureDate") or "")[:40],
            max_edge=DRAFT_EDGE if draft else FINAL_EDGE,
        )

        # The cache key must cover the colours themselves, not just their count:
        # two renders with the same n differ once the palette has been edited.
        fingerprint = json.dumps(
            [token, colours, opts.__dict__], sort_keys=True, default=str
        )
        render_id = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]

        cached = renders.get_lru(render_id)
        if cached is None:
            image, layout = render_polaroid(entry["image"], colours, opts)
            buf = io.BytesIO()
            image.save(buf, format="PNG", optimize=not draft)
            cached = {"png": buf.getvalue(), "layout": layout, "token": token}
            renders.put(render_id, cached)

        layout = cached["layout"]
        return jsonify(
            renderId=render_id,
            url=f"/render/{render_id}/{_filename()}",
            draft=draft,
            layout=layout.to_dict(),
            ordered=ordered,
        )

    @app.get("/render/<render_id>/<path:filename>")
    def serve_render(render_id, filename):
        entry = renders.get_lru(render_id)
        if entry is None:
            abort(404)
        return send_file(
            io.BytesIO(entry["png"]),
            mimetype="image/png",
            max_age=0,
            download_name=filename,
        )

    @app.post("/api/sample")
    def sample():
        """Eyedropper: a click on the rendered print -> a colour from the source.

        Done server-side because the server holds the full-resolution original;
        sampling the scaled-down preview would return a resampled colour rather
        than one that is genuinely in the photo.
        """
        data = request.get_json(silent=True) or {}
        entry = images.get_lru(data.get("token"))
        rendered = renders.get_lru(data.get("renderId"))
        if entry is None or rendered is None:
            return jsonify(error="expired"), 404

        layout = rendered["layout"]
        try:
            x = float(data["x"])
            y = float(data["y"])
        except (KeyError, TypeError, ValueError):
            return jsonify(error="Invalid point."), 400

        px = x - layout.photo_x
        py = y - layout.photo_y
        if not (0 <= px < layout.photo_w and 0 <= py < layout.photo_h):
            return jsonify(error="Click inside the photo to pick a colour."), 400

        src = entry["image"].convert("RGB")
        sx = int(px / layout.photo_w * src.width)
        sy = int(py / layout.photo_h * src.height)
        sx = max(0, min(src.width - 1, sx))
        sy = max(0, min(src.height - 1, sy))

        # Average a small neighbourhood so JPEG artefacts and sensor noise do
        # not hand back a colour that is not really what the user pointed at.
        r = max(1, round(min(src.size) / 400))
        box = (
            max(0, sx - r),
            max(0, sy - r),
            min(src.width, sx + r + 1),
            min(src.height, sy + r + 1),
        )
        patch = src.crop(box)
        stat = patch.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
        return jsonify(hex=to_hex(stat), rgb=list(stat))

    @app.errorhandler(413)
    def too_large(_):
        mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        return jsonify(error=f"That image is larger than {mb}MB."), 413

    return app


def _normalise(colour) -> dict:
    """Accept either a bare hex string or a full swatch dict."""
    if isinstance(colour, str):
        rgb = from_hex(colour)
        return {
            "hex": to_hex(rgb),
            "rgb": list(rgb),
            "weight": None,
            "source": "extracted",
        }
    rgb = from_hex(colour["hex"])
    weight = colour.get("weight")
    return {
        "hex": to_hex(rgb),
        "rgb": list(rgb),
        "weight": None if weight is None else float(weight),
        "source": colour.get("source", "extracted"),
    }


def _filename() -> str:
    return f"still-palette-{date.today().isoformat()}.png"
