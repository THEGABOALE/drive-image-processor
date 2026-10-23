import pytest
from PIL import Image, ImageChops

from image_processor.processing import OutputOptions, normalize_image


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


def content_box(result):
    """Caja de los píxeles que no son fondo."""

    if result.mode == "RGBA":
        return result.getchannel("A").getbbox()

    background = Image.new("RGB", result.size, "white")

    return ImageChops.difference(result, background).getbbox()


def translucent_product():
    image = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    image.paste((255, 0, 0, 200), (40, 30, 260, 170))
    image.paste((255, 0, 0, 255), (60, 50, 240, 150))
    return image


def test_default_options_match_previous_defaults():
    image = translucent_product()

    default = normalize_image(image)
    explicit = normalize_image(
        image,
        options=OutputOptions(
            width=400, height=400, background="white"
        ),
    )

    assert OutputOptions() == OutputOptions(400, 400, "white")
    assert default.mode == "RGB"
    assert default.size == (400, 400)
    assert default.tobytes() == explicit.tobytes()


def test_white_background_option_is_rgb():
    result = normalize_image(
        translucent_product(),
        options=OutputOptions(background="white"),
    )

    assert result.mode == "RGB"
    assert result.getpixel((0, 0)) == (255, 255, 255)
    assert result.getpixel((399, 399)) == (255, 255, 255)
    assert result.getpixel((200, 200)) == (255, 0, 0)


def test_transparent_background_option_is_rgba():
    result = normalize_image(
        translucent_product(),
        options=OutputOptions(background="transparent"),
    )

    assert result.mode == "RGBA"
    assert result.getpixel((0, 0))[3] == 0
    assert result.getpixel((399, 399))[3] == 0
    assert result.getpixel((200, 200)) == (255, 0, 0, 255)


def test_transparent_background_preserves_edge_alpha():
    # Exactamente 360 x 360: escala 1, sin remuestreo.
    image = Image.new("RGBA", (360, 360), (0, 0, 255, 128))
    image.paste((0, 0, 255, 255), (10, 10, 350, 350))

    transparent = normalize_image(
        image,
        options=OutputOptions(background="transparent"),
    )

    product = transparent.crop((20, 20, 380, 380))

    assert product.tobytes() == image.tobytes()
    assert transparent.getpixel((20, 200)) == (0, 0, 255, 128)
    assert transparent.getpixel((19, 200))[3] == 0

    # Con fondo blanco, el mismo borde se mezcla con el blanco.
    white = normalize_image(image)

    assert white.getpixel((20, 200)) == (127, 127, 255)


@pytest.mark.parametrize(
    "side, band",
    [
        # Reducción: 1120 px a 560 px (escala 0,5).
        (1120, 80),
        # Ampliación: 280 px a 560 px (escala 2).
        (280, 20),
    ],
)
def test_resized_product_preserves_semitransparent_edges(side, band):
    # Producto azul opaco con un borde de alfa 128.
    image = Image.new("RGBA", (side, side), (0, 0, 255, 128))
    image.paste(
        (0, 0, 255, 255), (band, band, side - band, side - band)
    )

    options = OutputOptions(800, 600, "transparent")
    result = normalize_image(image, options=options)

    assert result.mode == "RGBA"
    assert result.size == (800, 600)

    # Escala 560 / side: el producto ocupa x 120..680, y 20..580,
    # y el borde, 40 px. Se miden los centros de los bordes,
    # lejos de las transiciones del filtro.
    assert result.getchannel("A").getbbox() == (120, 20, 680, 580)

    for point in [(140, 300), (660, 300), (400, 40), (400, 560)]:
        red, green, blue, alpha = result.getpixel(point)

        assert abs(alpha - 128) <= 1
        # Sin halo oscuro: el borde conserva el color del producto.
        assert (red, green) == (0, 0)
        assert blue >= 254

    assert result.getpixel((400, 300)) == (0, 0, 255, 255)
    assert result.getpixel((119, 300))[3] == 0
    assert result.getpixel((680, 300))[3] == 0

    # El mismo borde sobre blanco se mezcla al 50 %.
    white = normalize_image(
        image, options=OutputOptions(800, 600, "white")
    )
    red, green, blue = white.getpixel((140, 300))

    assert abs(red - 127) <= 1
    assert abs(green - 127) <= 1
    assert blue == 255


@pytest.mark.parametrize("background", ["white", "transparent"])
@pytest.mark.parametrize(
    "source_size, canvas, expected_box",
    [
        # 200 x 100 escala 3,8 -> 760 x 380.
        ((200, 100), (800, 600), (20, 110, 780, 490)),
        # 100 x 300 escala 1,2 -> 120 x 360.
        ((100, 300), (800, 400), (340, 20, 460, 380)),
        # 300 x 300 escala 560/300 -> 560 x 560.
        ((300, 300), (600, 900), (20, 170, 580, 730)),
    ],
)
def test_rectangular_canvas_preserves_aspect_and_centers(
    background, source_size, canvas, expected_box
):
    image = Image.new("RGB", source_size, "blue")

    result = normalize_image(
        image,
        options=OutputOptions(
            width=canvas[0],
            height=canvas[1],
            background=background,
        ),
    )

    assert result.size == canvas

    box = content_box(result)
    assert box == expected_box

    left, top, right, bottom = box
    assert (right - left) / (bottom - top) == pytest.approx(
        source_size[0] / source_size[1]
    )

    # Centrado: márgenes opuestos iguales (±1 px por redondeo).
    assert abs(left - (canvas[0] - right)) <= 1
    assert abs(top - (canvas[1] - bottom)) <= 1


def test_fully_transparent_image_with_transparent_background():
    image = Image.new("RGBA", (100, 100), (0, 0, 0, 0))

    result = normalize_image(
        image,
        options=OutputOptions(
            width=500, height=300, background="transparent"
        ),
    )

    assert result.mode == "RGBA"
    assert result.size == (500, 300)
    assert result.getchannel("A").getbbox() is None


@pytest.mark.parametrize(
    "arguments",
    [
        {"width": 0},
        {"height": -1},
        {"width": 63},
        {"height": 6001},
        {"width": 5000, "height": 5000},
        {"width": 400.0},
        {"height": "400"},
        {"width": True},
        {"background": "azul"},
        {"background": "WHITE"},
    ],
)
def test_invalid_output_options(arguments):
    with pytest.raises(ValueError):
        OutputOptions(**arguments)


def test_largest_allowed_canvas_is_accepted():
    options = OutputOptions(width=6000, height=2666)

    assert options.width * options.height <= 16_000_000


def test_margin_is_validated_against_smaller_side():
    image = Image.new("RGB", (100, 100), "red")

    with pytest.raises(ValueError, match="margen"):
        normalize_image(
            image,
            options=OutputOptions(width=800, height=64),
            margin=32,
        )


def test_options_must_be_output_options():
    image = Image.new("RGB", (100, 100), "red")

    with pytest.raises(TypeError):
        normalize_image(image, 400)
