
import pytest
from PIL import Image

from image_processor import background_removal
from image_processor.pipeline import process_image


def test_remove_background_reuses_session(monkeypatch):
    background_removal._get_session.cache_clear()

    sessions = []

    def fake_new_session(model_name):
        sessions.append(model_name)
        return object()

    def fake_remove(image, session):
        return Image.new(
            "RGBA", image.size, (255, 0, 0, 0)
        )

    monkeypatch.setattr(
        background_removal, "new_session", fake_new_session
    )
    monkeypatch.setattr(
        background_removal, "remove", fake_remove
    )

    image = Image.new("RGB", (120, 80), "blue")

    try:
        result1 = background_removal.remove_background(image)
        result2 = background_removal.remove_background(image)

        assert result1.mode == "RGBA"
        assert result2.mode == "RGBA"
        assert sessions == ["isnet-general-use"]
    finally:
        background_removal._get_session.cache_clear()


def test_invalid_model():
    image = Image.new("RGB", (100, 100), "red")

    with pytest.raises(ValueError, match="Modelo no permitido"):
        background_removal.remove_background(
            image,
            model_name="modelo_inexistente",
        )


def test_pipeline_creates_white_background(monkeypatch):
    def fake_cutout(image, model_name="isnet-general-use"):
        cutout = Image.new(
            "RGBA", (200, 200), (0, 0, 0, 0)
        )
        cutout.paste(
            (255, 0, 0, 255),
            (50, 25, 150, 175),
        )
        return cutout

    monkeypatch.setattr(
        "image_processor.pipeline.remove_background",
        fake_cutout,
    )

    image = Image.new("RGB", (200, 200), "blue")
    result = process_image(image)

    assert result.size == (400, 400)
    assert result.mode == "RGB"
    assert result.getpixel((0, 0)) == (255, 255, 255)
    assert result.getpixel((200, 200)) == (255, 0, 0)
