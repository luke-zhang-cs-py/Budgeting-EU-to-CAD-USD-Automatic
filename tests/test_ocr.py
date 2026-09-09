"""The OCR wrapper: availability, input handling, and the box conversion.

The engine itself is not exercised here beyond one integration test that
skips when it is not installed. That is the point of the split -- these tests
run in milliseconds on a machine, and in CI, that has never downloaded a
60 MB model.
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ocr        # noqa: E402
import receipts   # noqa: E402

# The engine's own output shape: [[corners], text, confidence-as-a-string].
RAW = [
    [[[24.0, 44.0], [102.0, 46.0], [102.0, 60.0], [23.0, 59.0]],
     "Transaction", "0.8688374509414037"],
    [[[24.0, 80.0], [220.0, 80.0], [220.0, 100.0], [24.0, 100.0]],
     "REWE SAGTDANKE", "0.81"],
]


def test_boxes_carry_position_size_and_confidence():
    boxes = ocr.boxes_from(RAW)
    assert [b.text for b in boxes] == ["Transaction", "REWE SAGTDANKE"]
    assert boxes[0].top == 44.0
    assert boxes[0].height == 16.0
    assert boxes[1].height == 20.0
    assert 0.86 < boxes[0].confidence < 0.87


def test_the_confidence_arrives_as_a_string_and_is_read_as_a_number():
    """It really is a string from this engine. Compared as one, 0.9 sorts
    below 0.86, so a ranking on it would be quietly wrong."""
    assert isinstance(RAW[0][2], str)
    assert isinstance(ocr.boxes_from(RAW)[0].confidence, float)


def test_confidence_is_clamped_and_a_bad_one_does_not_raise():
    weird = [[[[0, 0], [10, 0], [10, 10], [0, 10]], "x", "17"],
             [[[0, 20], [10, 20], [10, 30], [0, 30]], "y", "not a number"]]
    assert [b.confidence for b in ocr.boxes_from(weird)] == [1.0, 0.0]


def test_boxes_come_back_top_to_bottom():
    """Reading order, so a caller comparing positions does not have to sort
    first. The engine does not guarantee an order."""
    shuffled = [
        [[[0, 90], [10, 90], [10, 100], [0, 100]], "last", "0.9"],
        [[[0, 10], [10, 10], [10, 20], [0, 20]], "first", "0.9"],
    ]
    assert [b.text for b in ocr.boxes_from(shuffled)] == ["first", "last"]


def test_whitespace_is_collapsed_and_empty_text_dropped():
    messy = [
        [[[0, 0], [10, 0], [10, 10], [0, 10]], "  REWE   SAGT  ", "0.9"],
        [[[0, 20], [10, 20], [10, 30], [0, 30]], "   ", "0.9"],
    ]
    boxes = ocr.boxes_from(messy)
    assert [b.text for b in boxes] == ["REWE SAGT"]


def test_nothing_and_none_convert_to_an_empty_list():
    assert ocr.boxes_from(None) == []
    assert ocr.boxes_from([]) == []


# ------------------------------------------------------------ availability

def test_available_answers_without_raising():
    assert ocr.available() in (True, False)


def test_a_missing_engine_is_reported_not_raised(monkeypatch):
    """The app has to keep working without the optional dependency: the
    upload still stores the screenshot and you type the amount."""
    monkeypatch.setattr(ocr, "available", lambda: False)
    with pytest.raises(ocr.OcrError) as bad:
        ocr.read(b"\x89PNG\r\n\x1a\n")
    assert "requirements-ocr.txt" in str(bad.value)


def test_a_broken_install_counts_as_unavailable(monkeypatch):
    """A half-installed onnxruntime raises OSError from the loader rather
    than ImportError, and both mean the same thing to a caller. This is why
    the check catches broadly."""
    import builtins
    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("rapidocr"):
            raise OSError("DLL load failed")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    assert ocr.available() is False


# ------------------------------------------------------------------- input

def test_bytes_a_path_and_a_file_object_are_all_accepted(tmp_path,
                                                         monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)
    seen = []

    def fake_engine():
        # The engine returns (result, elapsed), which is why read() unpacks
        # two values -- a stub returning just a list fails inside the
        # try/except and surfaces as "could not read the image".
        def run(payload):
            seen.append(payload)
            return [], 0.0
        return run

    monkeypatch.setattr(ocr, "_engine", fake_engine)

    png = b"\x89PNG\r\n\x1a\n" + b"0" * 40
    where = tmp_path / "shot.png"
    where.write_bytes(png)

    ocr.read(png)
    ocr.read(str(where))
    ocr.read(io.BytesIO(png))
    assert seen == [png, png, png]


def test_something_that_is_not_an_image_at_all_is_refused(monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)
    with pytest.raises(ocr.OcrError):
        ocr.read(12345)
    with pytest.raises(ocr.OcrError):
        ocr.read("C:/no/such/file.png")


def test_an_empty_image_is_refused(monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: True)
    with pytest.raises(ocr.OcrError) as bad:
        ocr.read(b"")
    assert "empty" in str(bad.value)


def test_an_absurdly_large_upload_is_refused_before_the_engine(monkeypatch):
    """Reading a 25 MB image ties up the request for as long as it takes, and
    a screenshot is never that big."""
    monkeypatch.setattr(ocr, "available", lambda: True)
    called = []
    monkeypatch.setattr(ocr, "_engine", lambda: called.append(1))
    with pytest.raises(ocr.OcrError) as bad:
        ocr.read(b"\x89PNG\r\n\x1a\n" + b"0" * ocr.MAX_BYTES)
    assert "limit" in str(bad.value)
    assert not called, "the engine was constructed for an oversized upload"


# ------------------------------------------------------------- integration

@pytest.mark.skipif(not ocr.available(),
                    reason="no OCR engine installed (optional dependency)")
def test_a_real_screenshot_reads_end_to_end(tmp_path):
    """The one test that needs the engine. Draws a purchase screen with
    Pillow, reads it, and checks the parser gets the amount and the date --
    so the two halves are known to fit together and not merely to work
    against each other's assumptions.
    """
    from PIL import Image, ImageDraw, ImageFont

    def font(size):
        for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
            path = os.path.join("C:\\Windows\\Fonts", name)
            if os.path.isfile(path):
                return ImageFont.truetype(path, size)
        return ImageFont.load_default()

    image = Image.new("RGB", (390, 300), "white")
    draw = ImageDraw.Draw(image)
    draw.text((24, 30), "REWE SAGT DANKE", font=font(22), fill="black")
    draw.text((24, 90), "-52,30 EUR", font=font(38), fill="black")
    draw.text((24, 170), "8 September 2026", font=font(15), fill="black")
    where = tmp_path / "shot.png"
    image.save(where)

    boxes = ocr.read(str(where))
    assert boxes, "the engine read nothing at all"

    import datetime as dt
    found = receipts.parse(boxes, today=dt.date(2026, 9, 9))
    assert found["amount"] is not None, [b.text for b in boxes]
    assert found["amount"]["minor"] == 5230
    assert found["date"]["iso"] == "2026-09-08"
