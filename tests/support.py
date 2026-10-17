"""Where the test suite stores every generated artefact.

Nothing in the suite writes to the console or to the repository root: raw output
goes to ``tests/artifacts/`` and the summary lines are read back from there. That
keeps the terminal clean and makes a failing run easy to inspect afterwards.
"""

from __future__ import annotations

from pathlib import Path

ARTIFACTS = Path(__file__).resolve().parent / "artifacts"
ARTIFACTS.mkdir(parents=True, exist_ok=True)

# Reusable binary payloads, generated rather than committed.
SMALL_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000d49484452000010000000108060000001f15c489"
    "0000a49444154789c6300010000050001'0d0a2db40000049454e44ae426082"
    .replace("'", "")
)

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /TypeCatalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


def png_bytes(width: int = 8, height: int = 8) -> bytes:
    """A real PNG produced by Pillow, so image validation is genuinely exercised."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 160, 140)).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_with_exif() -> bytes:
    """A JPEG carrying EXIF metadata, used to prove EXIF is stripped on upload."""
    import io

    from PIL import Image

    image = Image.new("RGB", (16, 16), (200, 180, 120))
    exif = Image.Exif()
    exif[0x010F] = "TestCamera"          # Make
    exif[0x0110] = "TestModel"           # Model
    exif[0x010E] = "secret-caption"      # ImageDescription
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()
