"""The preview size of a screenshot: read from the file's own header, fitted into media.PREVIEW_BOX."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from arceus_runner import media
from arceus_runner.delivery import mr_media_markdown, ticket_media_text

from .test_delivery import WEBM, png_header


def jpeg_header(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof2 = b"\xff\xc2" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x00" * 9
    return b"\xff\xd8" + app0 + b"\xff\xfe\x00\x04hi" + sof2 + b"\xff\xda"


def gif_header(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 3


def webp(chunk: bytes, body: bytes) -> bytes:
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(body)) + b"WEBP" + chunk + struct.pack("<I", len(body)) + body


def webp_vp8(width: int, height: int) -> bytes:
    return webp(b"VP8 ", b"\x00" * 3 + b"\x9d\x01\x2a" + struct.pack("<HH", width, height) + b"\x00" * 4)


def webp_vp8l(width: int, height: int) -> bytes:
    bits = (width - 1) | (height - 1) << 14
    return webp(b"VP8L", b"\x2f" + bits.to_bytes(4, "little") + b"\x00" * 4)


def webp_vp8x(width: int, height: int) -> bytes:
    return webp(b"VP8X", b"\x00" * 4 + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little"))


@pytest.mark.parametrize("data, size", [
    (png_header(2928, 1762), (2928, 1762)),
    (jpeg_header(1920, 1080), (1920, 1080)),
    (gif_header(640, 400), (640, 400)),
    (webp_vp8(1000, 500), (1000, 500)),
    (webp_vp8l(1234, 567), (1234, 567)),
    (webp_vp8x(3000, 2000), (3000, 2000)),
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, None),      # a signature and nothing behind it
    (b"\x89PNG\r\n\x1a\n", None),                    # cut short
    (png_header(0, 100), None),                      # a zero side says nothing
    (b"\xff\xd8\xff\xe0\x00", None),                 # a JPEG cut inside its first segment
    (b"\xff\xd8\xff\xe0\x00\x01xx", None),           # a segment length that cannot be
    (webp_vp8(1000, 500)[:28], None),               # each WebP form cut inside its size field
    (webp_vp8l(1234, 567)[:23], None),
    (webp_vp8x(3000, 2000)[:26], None),
    (gif_header(640, 400)[:8], None),
    (jpeg_header(1920, 1080)[:30], None),            # cut inside the frame header
    (WEBM, None),
])
def test_the_size_comes_from_the_header(data, size):
    assert media.image_size(data) == size


def item(data: bytes, name: str = "shot.png", kind: str = "image") -> media.MediaItem:
    return media.MediaItem(Path(name), "Caption", None, "ab" * 32, "image/png", kind, data)


@pytest.mark.parametrize("size, width", [
    ((2928, 1762), 480),     # landscape: the width is the bound (480 × 288)
    ((2880, 1800), 480),     # a retina 16:10 window: 480 × 300
    ((1440, 9000), 51),      # a full page, very tall: the height is the bound (51 × 318)
    ((750, 1334), 179),      # a phone: 179 × 318
    ((200, 100), 200),       # small: shown as it is, never enlarged
    ((600, 400), 480),       # exactly the box's proportions: 480 × 320
])
def test_a_screenshot_fits_the_preview_box_without_being_enlarged(size, width):
    w, h = size
    assert item(png_header(w, h)).preview_width == width
    box_w, box_h = media.PREVIEW_BOX
    assert width <= min(w, box_w) and width * h / w <= box_h


UNKNOWN_SIZE = [b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,                  # signature, no IHDR
                png_header(0, 100),                                     # a zero side
                b"\xff\xd8\xff\xe0\x00\x01xx",                           # a broken JPEG segment
                b"RIFF\x00\x00\x00\x00WEBPVP8Z" + b"\x00" * 20]          # a WebP chunk it does not know


@pytest.mark.parametrize("data", UNKNOWN_SIZE)
def test_an_image_of_unknown_size_is_linked_not_shown_in_place(data):
    shot = item(data)
    assert shot.preview_width is None and shot.shown_as == "link"
    md = mr_media_markdown(shot, "/uploads/x/f.png")
    assert md == "**Caption**\n\n[Open the screenshot: Caption](/uploads/x/f.png)" and "![" not in md
    text = ticket_media_text(Tracker(), [shot])
    assert text.endswith(f"[Open the screenshot: Caption]({shot.upload_name})") and "![" not in text
    assert shot.data() == data  # the original still goes out, whole


def test_a_video_keeps_its_player():
    video = item(WEBM, "retry.webm", "video")
    assert video.preview_width is None and video.shown_as == "player"
    assert item(png_header(2880, 1800)).shown_as == "preview"


class Tracker:
    """The ticket side of the formatting: YouTrackTracker's own Markdown."""
    def attachment_markdown(self, name, caption, width=None):
        from arceus_runner.youtrack import YouTrackTracker
        return YouTrackTracker.attachment_markdown(self, name, caption, width)

    def attachment_link(self, name, caption):
        from arceus_runner.youtrack import YouTrackTracker
        return YouTrackTracker.attachment_link(self, name, caption)


def test_the_markdown_names_the_original_file_and_only_sizes_screenshots():
    shot, video = item(png_header(2880, 1800)), item(WEBM, "retry.webm", "video")
    assert mr_media_markdown(shot, "/uploads/x/f.png") == "**Caption**\n\n![Caption](/uploads/x/f.png){width=480}"
    assert mr_media_markdown(video, "/uploads/y/r.webm") == "**Caption**\n\n![Caption](/uploads/y/r.webm)"

    text = ticket_media_text(Tracker(), [shot, video])
    assert f"[![Caption]({shot.upload_name}){{width=480}}]({shot.upload_name})" in text
    assert text.endswith(f"![Caption]({video.upload_name})")
    assert shot.data() == png_header(2880, 1800)  # the file that goes out is the original


def test_a_linked_preview_names_its_file_once_for_the_ticket_reader():
    from urllib.parse import unquote
    from arceus_runner.materials import _REF
    from arceus_runner.youtrack import YouTrackTracker
    text = YouTrackTracker.attachment_markdown(None, "1a2b3c4d-shot(2).png", "Shot", 480)
    names = {unquote(m.group(1) or m.group(2)) for m in _REF.finditer(text)}
    assert names == {"1a2b3c4d-shot(2).png"}
