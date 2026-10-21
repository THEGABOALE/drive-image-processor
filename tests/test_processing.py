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


def test_low_alpha_noise_does_not_shift_center():
    image = Image.new(
        "RGBA", (300, 200), (0, 0, 0, 0)
    )

    # Producto completamente opaco.
    image.paste(
        (255, 0, 0, 255),
        (20, 70, 100, 130),
    )

    # Residuo casi invisible lejos del producto.
    image.putpixel(
        (290, 15), (0, 0, 0, 1)
    )

    result = normalize_image(
        image,
        alpha_threshold=16,
    )

    red_pixels = [
        (x, y)
        for y in range(400)
        for x in range(400)
        if (
            result.getpixel((x, y))[0] > 230
            and result.getpixel((x, y))[1] < 60
            and result.getpixel((x, y))[2] < 60
        )
    ]

    assert red_pixels

    xs = [point[0] for point in red_pixels]
    ys = [point[1] for point in red_pixels]

    center_x = (min(xs) + max(xs)) / 2
    center_y = (min(ys) + max(ys)) / 2

    assert abs(center_x - 199.5) <= 3
    assert abs(center_y - 199.5) <= 3


def test_faint_translucent_object_is_preserved():
    image = Image.new(
        "RGBA", (80, 80), (0, 0, 0, 0)
    )

    image.paste(
        (0, 0, 255, 10),
        (20, 20, 60, 60),
    )

    result = normalize_image(
        image,
        alpha_threshold=16,
    )

    # El objeto casi transparente no debe desaparecer.
    center = result.getpixel((200, 200))

    assert center[2] == 255
    assert center[0] < 255
    assert center[1] < 255


@pytest.mark.parametrize(
    "threshold",
    [-1, 255],
)
def test_invalid_alpha_threshold(threshold):
    image = Image.new(
        "RGBA", (100, 100), "red"
    )

    with pytest.raises(
        ValueError,
        match="umbral alfa",
    ):
        normalize_image(
            image,
            alpha_threshold=threshold,
        )
