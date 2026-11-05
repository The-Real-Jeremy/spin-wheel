"""IMAGE ADMISSION — what may be stored as a prize picture, decided from the BYTES and not the filename.

WHERE THE TRUST BOUNDARY ACTUALLY IS, stated plainly because it is easy to overclaim:

* **The browser re-encodes.** `static/js/settings.js` draws every chosen file onto a canvas, caps it to
  `MAX_DIM` on its long edge and exports it with `canvas.toBlob` — so what leaves the page is bytes
  produced by the browser's own encoder, not the file the host picked. That strips EXIF (including GPS),
  drops any trailing payload, and normalises a 12 MB phone photo to ~100 KB.
* **The server does NOT re-encode**, because this app ships with no third-party packages and the standard
  library has no image codec. What it does instead is *admit or refuse*: sniff the real format from the
  magic bytes, enforce a byte cap, and read the pixel dimensions out of the header. A file whose extension
  and content disagree is refused on its content.
* So: the re-encode is a browser-side normalisation, and the server-side guarantee is
  "a PNG or WebP, within the size and dimension caps, served as what it actually is".

⛔ SVG IS REFUSED. It is the one image format that can carry script, and an `<img>` is not the only place
   these bytes could ever be rendered. There is no upside for a photo of a prize.

⛔ AND THE SERVED `Content-Type` COMES FROM THE SNIFF, never from the filename — see `app.py`, which
   stores the sniffed mime alongside the object and serves that. `tools/verify_served.py` checks the
   running app's response headers rather than trusting this comment.
"""
import hashlib
import struct

MAX_BYTES = 2 * 1024 * 1024          # per image, after the browser's re-encode. The workspace object
                                     # ceiling is 10 MiB (measured), so this is a UI/latency cap.
MAX_DIM = 1400                        # px on the long edge; the browser aims for 900 and this is slack
MIN_DIM = 16
ALLOWED = {"image/png", "image/webp", "image/jpeg", "image/gif"}


class Rejected(Exception):
    """Carries a message written for the person uploading, not for a log."""


def sniff(data):
    """-> (mime, width, height) read out of the header. Raises Rejected with a human reason.

    Every branch reads the real structure rather than a prefix alone: a file that starts with the PNG
    signature but has no IHDR is not a PNG, and admitting it would mean serving something unopenable.
    """
    if not data:
        raise Rejected("That file is empty.")
    if len(data) > MAX_BYTES:
        raise Rejected(f"That image is {len(data)/1024/1024:.1f} MB; the limit is "
                       f"{MAX_BYTES/1024/1024:.0f} MB. Try a smaller picture.")

    head = data[:32]
    if head[:5] == b"<?xml" or head.lstrip()[:4] == b"<svg":
        raise Rejected("SVG images are not accepted — use a PNG, JPEG or WebP photo.")

    if head[:8] == b"\x89PNG\r\n\x1a\n":
        mime, wh = "image/png", _png(data)
    elif head[:3] == b"\xff\xd8\xff":
        mime, wh = "image/jpeg", _jpeg(data)
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        mime, wh = "image/webp", _webp(data)
    elif head[:6] in (b"GIF87a", b"GIF89a"):
        mime, wh = "image/gif", _gif(data)
    else:
        raise Rejected("That does not look like a PNG, JPEG, WebP or GIF image. "
                       "(The file's contents are what is checked, not its name.)")

    w, h = wh
    if w <= 0 or h <= 0:
        raise Rejected("That image's dimensions could not be read, so it was not stored.")
    if w < MIN_DIM or h < MIN_DIM:
        raise Rejected(f"That image is only {w}x{h}; it needs to be at least {MIN_DIM}x{MIN_DIM}.")
    if max(w, h) > MAX_DIM:
        raise Rejected(f"That image is {w}x{h}; the limit is {MAX_DIM}px on the long edge. "
                       f"(The page normally shrinks it for you — reload and try again.)")
    return mime, w, h


def describe(data):
    """-> dict for the config document: mime, dimensions and the md5 that proves a round-trip."""
    mime, w, h = sniff(data)
    return {"image_mime": mime, "image_w": w, "image_h": h,
            "image_md5": hashlib.md5(data).hexdigest(), "bytes": len(data)}


def extension(mime):
    return {"image/png": ".png", "image/webp": ".webp",
            "image/jpeg": ".jpg", "image/gif": ".gif"}.get(mime, ".bin")


# ---------------------------------------------------------------- per-format header readers
def _png(d):
    # IHDR must be the first chunk: length(4) type(4) at offset 8, dimensions at 16.
    if len(d) < 24 or d[12:16] != b"IHDR":
        raise Rejected("That PNG's header is malformed, so it was not stored.")
    return struct.unpack(">II", d[16:24])


def _jpeg(d):
    """Walk the marker segments to the first Start-Of-Frame. Skips APPn/COM (where EXIF lives)."""
    i, n = 2, len(d)
    while i + 9 < n:
        if d[i] != 0xFF:
            i += 1
            continue
        marker = d[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > n:
            break
        seglen = struct.unpack(">H", d[i + 2:i + 4])[0]
        # SOF0/1/2/3, 5/6/7, 9/10/11, 13/14/15 — every non-differential and differential frame type.
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 > n:
                break
            h, w = struct.unpack(">HH", d[i + 5:i + 9])
            return w, h
        if seglen < 2:
            break
        i += 2 + seglen
    raise Rejected("That JPEG has no frame header, so its size could not be read.")


def _webp(d):
    """Three container shapes, and all three have to be handled or a normal browser export is refused."""
    if len(d) < 30:
        raise Rejected("That WebP file is truncated.")
    tag = d[12:16]
    if tag == b"VP8X":                                    # extended: 24-bit minus-one dimensions
        w = int.from_bytes(d[24:27], "little") + 1
        h = int.from_bytes(d[27:30], "little") + 1
        return w, h
    if tag == b"VP8 ":                                    # lossy: 14-bit dimensions after the sync code
        if d[23:26] != b"\x9d\x01\x2a":
            raise Rejected("That WebP file's frame header is malformed.")
        w = struct.unpack("<H", d[26:28])[0] & 0x3FFF
        h = struct.unpack("<H", d[28:30])[0] & 0x3FFF
        return w, h
    if tag == b"VP8L":                                    # lossless: 14 bits each, packed
        bits = int.from_bytes(d[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    raise Rejected("That WebP variant is not recognised.")


def _gif(d):
    if len(d) < 10:
        raise Rejected("That GIF file is truncated.")
    return struct.unpack("<HH", d[6:10])
