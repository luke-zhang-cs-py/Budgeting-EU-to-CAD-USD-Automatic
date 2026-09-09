"""
ocr.py
------
Reading text out of a screenshot, and nothing else.

This module is deliberately thin, and the split matters. All it does is turn
an image into a list of `Box` -- some text, where it sat, how tall it was
rendered, and how sure the engine was. Deciding which of those boxes is the
amount, which is the date and which is the merchant belongs to receipts.py,
which is pure and therefore testable without a 60 MB model on disk.

The engine is optional. It is a local ONNX model, so no screenshot of
somebody's spending ever leaves the machine, but it is a large install and CI
has no use for it. Nothing here imports it at module scope: `available()`
answers honestly, the app says so in the interface, and the rest of the
wallet works exactly as before without it.
"""
import collections
import functools
import io
import os

# text      -- what the engine read, whitespace-collapsed
# left/top  -- pixel position of the box's top-left corner
# width/height -- how large it was *rendered*, which is the single most useful
#             signal on a purchase screenshot: the amount is the biggest thing
#             on the screen, because whoever designed that screen knew it was
#             the number you opened it to see.
# confidence -- 0..1 from the recogniser, carried through so the interface can
#             show a low-confidence read differently rather than pretending
#             every number is equally trustworthy.
Box = collections.namedtuple("Box", "text left top width height confidence")

# Below this the engine is guessing at the glyphs. Such boxes are still
# returned -- dropping them silently would hide the reason a screenshot came
# back with nothing -- but receipts.py will not choose one as the amount
# without saying so.
WEAK = 0.55

# Screenshots are small. A 25 MB upload is not a screenshot, and reading one
# would tie up the request for as long as it takes.
MAX_BYTES = 12 * 1024 * 1024


class OcrError(RuntimeError):
    """The image could not be read at all.

    Distinct from "read it and found no purchase in it", which is
    receipts.parse's answer and not an error.
    """


@functools.lru_cache(maxsize=1)
def _engine():
    """The recogniser, built once.

    Constructing it loads several ONNX models and takes a couple of seconds,
    which is per-process rather than per-request.
    """
    from rapidocr_onnxruntime import RapidOCR      # noqa: PLC0415
    return RapidOCR()


def available():
    """Whether a screenshot can be read on this machine.

    Checked rather than assumed, because the answer changes what the
    interface should offer: without an engine the upload still works and
    stores the image, and you type the amount yourself.
    """
    try:
        import rapidocr_onnxruntime            # noqa: F401, PLC0415
    except Exception:
        # Deliberately broad. A missing package raises ImportError, but a
        # half-installed onnxruntime raises OSError from the loader, and both
        # mean the same thing to a caller: not available.
        return False
    return True


def read(image):
    """Every piece of text in `image`, as Boxes.

    `image` is bytes or a path. Returns [] for an image with no text in it,
    which is a real answer -- a photo of a wall is not an error.
    """
    if not available():
        raise OcrError(
            "no OCR engine installed; pip install -r requirements-ocr.txt")

    payload = _as_bytes(image)
    if not payload:
        raise OcrError("empty image")
    if len(payload) > MAX_BYTES:
        raise OcrError(
            f"image is {len(payload) // 1024}KB; the limit is "
            f"{MAX_BYTES // 1024}KB")

    try:
        result, _ = _engine()(payload)
    except Exception as bad:                  # pragma: no cover - engine guts
        raise OcrError(f"could not read the image: {bad}") from bad

    return boxes_from(result)


def boxes_from(result):
    """The engine's raw output as Boxes, top-to-bottom.

    Its own shape is [[corners], text, confidence-as-a-string]. Kept separate
    from `read` so the parser's tests can build this list by hand and never
    need the engine at all.
    """
    boxes = []
    for item in result or []:
        try:
            corners, text, confidence = item[0], item[1], item[2]
            xs = [float(point[0]) for point in corners]
            ys = [float(point[1]) for point in corners]
        except (TypeError, ValueError, IndexError):   # pragma: no cover
            continue
        text = " ".join(str(text).split())
        if not text:
            continue
        boxes.append(Box(
            text=text,
            left=min(xs), top=min(ys),
            width=max(xs) - min(xs), height=max(ys) - min(ys),
            # The confidence arrives as a string, which is why this is a float
            # call and not an assumption.
            confidence=_as_float(confidence)))
    boxes.sort(key=lambda box: (box.top, box.left))
    return boxes


def _as_float(value):
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):            # pragma: no cover
        return 0.0


def _as_bytes(image):
    """Bytes from bytes, a path, or a file object."""
    if isinstance(image, (bytes, bytearray)):
        return bytes(image)
    if isinstance(image, io.IOBase) or hasattr(image, "read"):
        return image.read()
    if isinstance(image, str) and os.path.isfile(image):
        with open(image, "rb") as handle:
            return handle.read()
    raise OcrError("expected image bytes or an existing path")
