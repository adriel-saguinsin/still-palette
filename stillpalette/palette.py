"""Dominant-colour extraction.

Clustering happens in CIELAB rather than RGB: Lab is perceptually uniform, so
k-means splits colours roughly the way an eye would. RGB k-means tends to merge
colours that look distinct and split ones that look identical.
"""

from __future__ import annotations

import colorsys
from dataclasses import dataclass, asdict

import numpy as np
from PIL import Image

# Pixels are subsampled to this long edge before clustering. ~200px caps the
# pixel set around 40k, which keeps a full extraction well under 100ms.
SAMPLE_EDGE = 200

# Background used to flatten transparency. Neutral mid-grey rather than black,
# so a transparent PNG does not extract a spurious dominant black.
ALPHA_BACKDROP = (128, 128, 128)

MAX_POOL = 24
KMEANS_ITERS = 40

# Lab distances at which a pool candidate counts as a different colour, tried
# in order. A smooth gradient can put every alternate within a few units of the
# palette, so a single fixed threshold leaves re-roll with nothing to offer;
# relaxing step by step keeps the button working while still preferring
# genuinely distinct colours.
POOL_DELTAS = (12.0, 8.0, 5.0, 3.0, 1.0)

# Keep relaxing until at least this many alternates are on offer.
POOL_TARGET = 4


@dataclass(frozen=True)
class Swatch:
    rgb: tuple[int, int, int]
    hex: str
    weight: float  # share of sampled pixels, 0..1

    def to_dict(self) -> dict:
        return asdict(self)


def to_hex(rgb) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(int(c) for c in rgb))


def from_hex(value: str) -> tuple[int, int, int]:
    s = str(value).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError("bad hex colour: %r" % (value,))
    return tuple(int(s[i : i + 2], 16) for i in (0, 2, 4))


# --------------------------------------------------------------------------
# sRGB <-> CIELAB
# --------------------------------------------------------------------------

_RGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ]
)
_XYZ_TO_RGB = np.linalg.inv(_RGB_TO_XYZ)
_D65 = np.array([0.95047, 1.00000, 1.08883])

_EPS = 216 / 24389
_KAPPA = 24389 / 27


def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """rgb: (..., 3) float in 0..255 -> Lab."""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    linear = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = linear @ _RGB_TO_XYZ.T / _D65
    f = np.where(xyz > _EPS, np.cbrt(xyz), (_KAPPA * xyz + 16) / 116)
    fx, fy, fz = f[..., 0], f[..., 1], f[..., 2]
    return np.stack([116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)], axis=-1)


def lab_to_rgb(lab: np.ndarray) -> np.ndarray:
    """lab: (..., 3) -> float rgb clamped to 0..255."""
    lab = np.asarray(lab, dtype=np.float64)
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    fy = (L + 16) / 116
    fx = fy + a / 500
    fz = fy - b / 200

    def finv(t):
        t3 = t**3
        return np.where(t3 > _EPS, t3, (116 * t - 16) / _KAPPA)

    xyz = np.stack([finv(fx), finv(fy), finv(fz)], axis=-1) * _D65
    linear = xyz @ _XYZ_TO_RGB.T
    linear = np.clip(linear, 0.0, 1.0)
    srgb = np.where(
        linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055
    )
    return np.clip(srgb * 255.0, 0, 255)


# --------------------------------------------------------------------------
# k-means
# --------------------------------------------------------------------------


def _kmeans_plusplus(points: np.ndarray, k: int, rng) -> np.ndarray:
    centroids = [points[rng.integers(len(points))]]
    d2 = np.sum((points - centroids[0]) ** 2, axis=1)
    for _ in range(1, k):
        total = d2.sum()
        if total <= 0:
            # Every remaining point coincides with a chosen centroid.
            centroids.append(points[rng.integers(len(points))])
        else:
            centroids.append(points[rng.choice(len(points), p=d2 / total)])
        d2 = np.minimum(d2, np.sum((points - centroids[-1]) ** 2, axis=1))
    return np.array(centroids, dtype=np.float64)


def _kmeans(points: np.ndarray, k: int, seed: int):
    """Returns (centroids, counts) with empty clusters dropped."""
    rng = np.random.default_rng(seed)
    k = max(1, min(k, len(points)))
    centroids = _kmeans_plusplus(points, k, rng)
    labels = np.full(len(points), -1, dtype=np.int64)

    for _ in range(KMEANS_ITERS):
        # (n, k) squared distances via the expansion of |p - c|^2.
        d = (
            np.sum(points**2, axis=1)[:, None]
            - 2 * points @ centroids.T
            + np.sum(centroids**2, axis=1)[None, :]
        )
        new_labels = np.argmin(d, axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for i in range(k):
            member = points[labels == i]
            if len(member):
                centroids[i] = member.mean(axis=0)

    counts = np.bincount(labels, minlength=k)
    keep = counts > 0
    return centroids[keep], counts[keep]


def _sample_pixels(img: Image.Image) -> np.ndarray:
    """Downsampled RGB pixels as an (n, 3) float array."""
    im = img
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        backdrop = Image.new("RGBA", im.size, ALPHA_BACKDROP + (255,))
        im = Image.alpha_composite(backdrop, im)
    im = im.convert("RGB")
    im = im.copy()
    # NEAREST keeps original pixel values intact; interpolating would invent
    # in-between colours that are not actually in the photo.
    im.thumbnail((SAMPLE_EDGE, SAMPLE_EDGE), Image.Resampling.NEAREST)
    return np.asarray(im, dtype=np.float64).reshape(-1, 3)


def _swatches_from(centroids_lab: np.ndarray, counts: np.ndarray) -> list[Swatch]:
    rgb = np.rint(lab_to_rgb(centroids_lab)).astype(int)
    total = float(counts.sum())
    out = []
    for colour, count in zip(rgb, counts):
        t = tuple(int(c) for c in colour)
        out.append(Swatch(rgb=t, hex=to_hex(t), weight=float(count) / total))
    out.sort(key=lambda s: -s.weight)
    return out


def extract_palette(img: Image.Image, n: int, seed: int = 0) -> list[Swatch]:
    """The n dominant colours, most-dominant first.

    May return fewer than n when the image holds fewer distinct colours; callers
    should surface that rather than padding with duplicates.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    lab = rgb_to_lab(_sample_pixels(img))
    centroids, counts = _kmeans(lab, n, seed)
    return _swatches_from(centroids, counts)


def extract_pool(img: Image.Image, n: int, seed: int = 0) -> list[Swatch]:
    """Alternate colours for re-rolling, from a finer partition of the image.

    Deliberately a separate clustering run rather than "cluster at 24 and take
    the top n" -- k-means at a higher k partitions the image differently, so its
    top n are not the true n dominant colours. The headline palette has to stay
    faithful to the count the user actually asked for.
    """
    lab = rgb_to_lab(_sample_pixels(img))
    centroids, counts = _kmeans(lab, min(max(n * 3, 8), MAX_POOL), seed + 1)
    return _swatches_from(centroids, counts)


def distinct_from(
    candidates: list[Swatch], taken: list[str], target: int = POOL_TARGET
) -> list[Swatch]:
    """Pool candidates far enough from every colour already on the bar.

    Prefers clearly different colours -- re-rolling through near-identical
    shades reads as a broken button -- but relaxes the threshold rather than
    returning nothing, so a low-contrast photo still offers alternates.
    Exact duplicates of a displayed colour are always excluded.
    """
    if not candidates:
        return []
    if not taken:
        return list(candidates)

    taken_set = {h.upper() for h in taken}
    usable = [c for c in candidates if c.hex.upper() not in taken_set]
    if not usable:
        return []

    taken_lab = rgb_to_lab(np.array([from_hex(h) for h in taken], dtype=np.float64))
    gaps = [
        float(
            np.min(
                np.linalg.norm(
                    taken_lab - rgb_to_lab(np.array(c.rgb, dtype=np.float64)), axis=1
                )
            )
        )
        for c in usable
    ]

    for threshold in POOL_DELTAS:
        out = [c for c, gap in zip(usable, gaps) if gap >= threshold]
        if len(out) >= target:
            return out
    # Nothing cleared even the loosest bar: offer the most distant ones anyway.
    return [c for _, c in sorted(zip(gaps, usable), key=lambda p: -p[0])]


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------

# Below this HSV saturation a colour is treated as achromatic. Hue angle is
# meaningless for greys -- a 1/255 channel wobble swings it wildly -- so they
# are grouped instead of scattered across the bar.
ACHROMATIC_S = 0.12


def _hue_key(rgb) -> tuple:
    r, g, b = (c / 255.0 for c in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < ACHROMATIC_S:
        return (0, v, 0.0)  # greys first, dark -> light
    return (1, h, v)


def order_swatches(items: list[dict], mode: str, reverse: bool = False) -> list[dict]:
    """Sort dicts carrying at least 'rgb' (and 'weight' for dominance)."""
    if mode == "dominance":
        # A picked colour has no cluster share, so it sorts last rather than
        # pretending to a weight it does not have.
        ordered = sorted(
            items,
            key=lambda s: (
                0 if s.get("weight") is not None else 1,
                -(s.get("weight") or 0.0),
            ),
        )
    elif mode == "lightness":
        ordered = sorted(
            items, key=lambda s: float(rgb_to_lab(np.array(s["rgb"], float))[0])
        )
    elif mode == "hue":
        ordered = sorted(items, key=lambda s: _hue_key(s["rgb"]))
    elif mode == "custom":
        ordered = list(items)
    else:
        raise ValueError("unknown order mode: %r" % (mode,))
    return ordered[::-1] if reverse else ordered
