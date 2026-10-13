"""Tray/window icon generation.

The icon encodes the active thermal profile by colour, which is the one piece of
state a G-Helper-style app should communicate without being opened.
"""

from __future__ import annotations

from ..model import ThermalProfile

#: Chosen to read at 16x16 px on a dark taskbar.
PROFILE_COLORS: dict[str, str] = {
    ThermalProfile.OPTIMIZED.value: "#4a90d9",        # blue  - balanced
    ThermalProfile.QUIET.value: "#5cb85c",            # green - silent
    ThermalProfile.COOL.value: "#3fc1c9",             # cyan  - cool
    ThermalProfile.ULTRA_PERFORMANCE.value: "#e05252",  # red   - performance
}
FALLBACK_COLOR = "#9e9e9e"


def color_for(profile: ThermalProfile | None) -> str:
    if profile is None:
        return FALLBACK_COLOR
    return PROFILE_COLORS.get(profile.value, FALLBACK_COLOR)


def make_icon_image(profile: ThermalProfile | None, size: int = 64):
    """Return a Pillow image for the tray, or None if Pillow is unavailable."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    color = color_for(profile)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = max(2, size // 16)
    draw.ellipse((margin, margin, size - margin, size - margin), fill=color)
    # A simple inner ring, so the icon is distinguishable from other tray apps.
    inner = size * 0.34
    draw.ellipse((inner, inner, size - inner, size - inner), fill=(255, 255, 255, 235))
    return image


def make_photo_icon(root, profile: ThermalProfile | None, size: int = 64):
    """Return a tkinter PhotoImage, or None if Pillow/ImageTk is unavailable.

    Used for the window title-bar icon. Callers must keep a reference to the
    returned object or tkinter will garbage-collect the image and show nothing.
    """
    image = make_icon_image(profile, size=size)
    if image is None:
        return None
    try:
        from PIL import ImageTk
    except ImportError:
        return None
    try:
        return ImageTk.PhotoImage(image)
    except Exception:  # noqa: BLE001 - needs a display; never fatal
        return None
