"""
Everything that touches actual photo files: listing, fast decoding for
previews/thumbnails, and pulling EXIF details.
"""
import os
from datetime import datetime

from PIL import Image, ImageOps

from . import config

# these are the user's own local event photos (e.g. big panoramas/high-res
# DSLR shots), not untrusted uploads -- PIL's decompression-bomb guard is
# meant for the latter and was rejecting legitimate large photos here
Image.MAX_IMAGE_PIXELS = None


def list_files():
    return sorted(
        f for f in os.listdir(config.SRC)
        if f.lower().endswith((".jpg", ".jpeg", ".png"))
    )


def fast_open(path, target_size):
    """Open an image and, for JPEGs, tell the decoder to downsample while
    decoding instead of decoding full resolution and shrinking after --
    much faster for large photos when we only need a small preview."""
    img = Image.open(path)
    try:
        img.draft("RGB", target_size)
    except Exception:
        pass
    return ImageOps.exif_transpose(img)


def _format_exposure(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return f"1/{round(1 / f)}s" if f < 1 else f"{f:g}s"


def _format_fnumber(v):
    try:
        return f"f/{float(v):.1f}"
    except (TypeError, ValueError):
        return None


def _format_focal(v):
    try:
        return f"{float(v):.0f}mm"
    except (TypeError, ValueError):
        return None


def get_photo_info(path):
    info = {
        "width": None, "height": None, "size_bytes": None,
        "date": None, "camera": None, "iso": None,
        "exposure": None, "fnumber": None, "focal_length": None,
    }
    try:
        info["size_bytes"] = os.path.getsize(path)
    except OSError:
        pass
    try:
        img = Image.open(path)
        info["width"], info["height"] = img.size
        exif = img.getexif()
        make = exif.get(0x010F)
        model = exif.get(0x0110)
        if model:
            model = str(model).strip()
            info["camera"] = f"{str(make).strip()} {model}".strip() if make else model

        try:
            exif_ifd = exif.get_ifd(0x8769)
        except Exception:
            exif_ifd = {}

        date = exif_ifd.get(0x9003) or exif.get(0x0132)
        if date:
            info["date"] = str(date).replace(":", "-", 2)

        iso = exif_ifd.get(0x8827)
        if iso is not None:
            info["iso"] = iso[0] if isinstance(iso, (tuple, list)) else iso

        exposure = exif_ifd.get(0x829A)
        if exposure is not None:
            info["exposure"] = _format_exposure(exposure)

        fnumber = exif_ifd.get(0x829D)
        if fnumber is not None:
            info["fnumber"] = _format_fnumber(fnumber)

        focal = exif_ifd.get(0x920A)
        if focal is not None:
            info["focal_length"] = _format_focal(focal)
    except Exception:
        pass
    return info


def photo_datetime(path):
    """EXIF capture time as a datetime, or None if missing/unparseable --
    used to spot burst/duplicate shots (see routes.api_state)."""
    date_str = get_photo_info(path).get("date")
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
