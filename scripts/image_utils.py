"""Small, dependency-free image signature helpers.

This replaces the standard-library ``imghdr`` module, which was removed in
Python 3.13. It deliberately checks file signatures rather than extensions.
"""
import os


_JPEG = b"\xff\xd8\xff"
_PNG = b"\x89PNG\r\n\x1a\n"
_GIF = (b"GIF87a", b"GIF89a")
_BMP = b"BM"
_TIFF = (b"II*\x00", b"MM\x00*")

_MIME_TYPES = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "webp": "image/webp",
}


def image_type(path):
    """Return a supported image type, or ``None`` for invalid/unsupported files."""
    try:
        with open(path, "rb") as stream:
            header = stream.read(32)
            if header.startswith(_JPEG):
                return "jpeg"
            if header.startswith(_PNG):
                return "png"
            if header.startswith(_GIF):
                return "gif"
            if header.startswith(_BMP):
                return "bmp"
            if header.startswith(_TIFF):
                return "tiff"
            # WebP is a RIFF container whose form type is WEBP.
            if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
                return "webp"
    except (OSError, TypeError):
        return None
    return None


def image_mime_type(path):
    """Return the MIME type detected from image bytes, never its filename."""
    detected = image_type(path)
    return _MIME_TYPES.get(detected)
