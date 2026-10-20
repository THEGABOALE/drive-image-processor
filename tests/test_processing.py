import pytest
from PIL import Image

from image_processor.processing import normalize_image


def test_output_dimensions():
    image = Image.new("RGB", (1200, 800))

    result= normalize_image(image)

    assert result.size == (400, 400)
    assert result.mode == "RGB"

def test_white_background():
    image = Image.new("RGB", (200, 100), "blue")

    result = normalize_image(image)

    assert result.getpixel((0, 0)) == (255, 255, 255)
    assert result.getpixel((200, 200)) == (0, 0, 255)


def test_aspect_ratio_and_centering():
    image = Image.new("RGB", (200, 100), "blue")

    result = normalize_image(image)

    #200x100 escala proporcionalmente a 360x180
    assert result.getpixel((20, 110)) == (0, 0, 255)
    assert result.getpixel((200, 109)) == (255, 255, 255)


def test_transparent_edges_are_cropped():
    image = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    image.paste((255, 0, 0, 255), (0, 0, 40, 80))

    result = normalize_image(image)

    assert result.getpixel((200, 200)) == (255, 0, 0)
    assert result.getpixel((0, 0)) == (255, 255, 255)


def test_original_not_modified():
    image = Image.new("RGB", (200, 100), "green")
    original_bytes = image.tobytes()

    normalize_image(image)

    assert image.size == (200, 100)
    assert image.tobytes() == original_bytes


def test_invalid_margin():
    image = Image.new("RGB", (100, 100), "red")

    with pytest.raises(ValueError):
        normalize_image(image, margin=200)


def test_fully_transparent_image():
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))

    result = normalize_image(image)

    assert result.size == (400, 400)
    assert result.getpixel((200, 200)) == (255, 255, 255)

