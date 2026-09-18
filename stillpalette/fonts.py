"""Cross-platform font resolution.

Pillow's built-in bitmap font is unusable at print scale, so probe for a real
system face and cache what we find. Nothing is bundled, so the app stays
dependency-light and still renders legible text wherever it runs.
"""

from __future__ import annotations

import functools
import os

from PIL import ImageFont

# Ordered by preference, most-likely-present first per platform.
_REGULAR = [
    "segoeui.ttf",          # Windows
    "SegoeUI.ttf",
    "arial.ttf",
    "Helvetica.ttc",        # macOS
    "HelveticaNeue.ttc",
    "DejaVuSans.ttf",       # Linux, and ships with many Pillow builds
    "LiberationSans-Regular.ttf",
    "NotoSans-Regular.ttf",
]

_MEDIUM = [
    "segoeuib.ttf",
    "seguisb.ttf",
    "arialbd.ttf",
    "Helvetica.ttc",
    "DejaVuSans-Bold.ttf",
    "LiberationSans-Bold.ttf",
    "NotoSans-Bold.ttf",
]

_SEARCH_DIRS = [
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
    "/System/Library/Fonts",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    os.path.expanduser("~/.fonts"),
]


@functools.lru_cache(maxsize=4)
def _find(bold: bool) -> str | None:
    names = _MEDIUM if bold else _REGULAR
    for name in names:
        # Pillow searches its own font path plus the system path by name.
        try:
            ImageFont.truetype(name, 12)
            return name
        except OSError:
            pass
    for directory in _SEARCH_DIRS:
        if not directory or not os.path.isdir(directory):
            continue
        for name in names:
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                return path
    return None


def load(size: int, bold: bool = False):
    """A truetype font at `size` px, falling back to Pillow's scalable default."""
    size = max(1, int(size))
    target = _find(bold)
    if target:
        try:
            return ImageFont.truetype(target, size)
        except OSError:
            pass
    try:
        # Scalable since Pillow 10.1; far better than the fixed bitmap default.
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()
