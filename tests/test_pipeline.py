
import pytest
from PIL import Image

from image_processor import pipeline
from image_processor.background_removal import ModelInitializationError
from image_processor.errors import EmptySegmentationError
from image_processor.processing import OutputOptions


VARIANTS = [
    OutputOptions(),
    OutputOptions(background="transparent"),
    OutputOptions(800, 600, "white"),
    OutputOptions(640, 960, "transparent"),
]


def fake_segmentation(cutout):
    """Simula remove_background devolviendo un recorte fijo."""

    def fake_remove_background(image, model_name="isnet-general-use"):
        return cutout.copy()

    return fake_remove_background


def empty_cutout(size=(300, 200)):
    return Image.new("RGBA", size, (0, 0, 0, 0))


def test_empty_segmentation_is_a_specific_error():
    assert not issubclass(EmptySegmentationError, ModelInitializationError)
    assert not issubclass(ModelInitializationError, EmptySegmentationError)


@pytest.mark.parametrize("options", VARIANTS)
def test_empty_segmentation_raises(monkeypatch, options):
    monkeypatch.setattr(
        pipeline, "remove_background", fake_segmentation(empty_cutout())
    )

    with pytest.raises(
        EmptySegmentationError, match="birefnet-general-lite"
    ):
        pipeline.process_image(
            Image.new("RGB", (300, 200), "blue"),
            model_name="birefnet-general-lite",
            options=options,
        )


def test_empty_segmentation_is_rejected_before_normalizing(monkeypatch):
    calls = []

    monkeypatch.setattr(
        pipeline, "remove_background", fake_segmentation(empty_cutout())
    )
    monkeypatch.setattr(
        pipeline,
        "normalize_image",
        lambda *args, **kwargs: calls.append(args),
    )

    with pytest.raises(EmptySegmentationError):
        pipeline.process_image(Image.new("RGB", (300, 200), "blue"))

    assert calls == []


@pytest.mark.parametrize("options", VARIANTS)
def test_small_product_is_accepted(monkeypatch, options):
    # 3 x 3 píxeles en 2000 x 2000: cobertura del 0,0002 %.
    cutout = empty_cutout((2000, 2000))
    cutout.paste((255, 0, 0, 255), (1500, 300, 1503, 303))

    monkeypatch.setattr(
        pipeline, "remove_background", fake_segmentation(cutout)
    )

    result = pipeline.process_image(
        Image.new("RGB", (2000, 2000), "blue"), options=options
    )

    assert result.size == (options.width, options.height)

    center = result.getpixel((options.width // 2, options.height // 2))
    assert center[:3] == (255, 0, 0)


@pytest.mark.parametrize("options", VARIANTS)
def test_faint_product_is_accepted(monkeypatch, options):
    # Alfa 1: por debajo del umbral de recorte, pero no nulo.
    cutout = empty_cutout((200, 200))
    cutout.paste((0, 0, 255, 1), (50, 50, 150, 150))

    monkeypatch.setattr(
        pipeline, "remove_background", fake_segmentation(cutout)
    )

    result = pipeline.process_image(
        Image.new("RGB", (200, 200), "blue"), options=options
    )

    center = result.getpixel((options.width // 2, options.height // 2))

    if options.background == "transparent":
        assert result.mode == "RGBA"
        assert center == (0, 0, 255, 1)
    else:
        # Comportamiento actual: casi invisible sobre blanco.
        assert result.mode == "RGB"
        assert center == (254, 254, 255)


@pytest.mark.parametrize("options", VARIANTS)
def test_white_product_is_accepted(monkeypatch, options):
    cutout = empty_cutout((300, 300))
    cutout.paste((255, 255, 255, 255), (100, 100, 200, 200))

    monkeypatch.setattr(
        pipeline, "remove_background", fake_segmentation(cutout)
    )

    result = pipeline.process_image(
        Image.new("RGB", (300, 300), "white"), options=options
    )

    if options.background == "transparent":
        assert result.getchannel("A").getbbox() is not None
    else:
        # El PNG final parece vacío, pero la máscara no lo está.
        assert result.getcolors() == [
            (options.width * options.height, (255, 255, 255))
        ]
