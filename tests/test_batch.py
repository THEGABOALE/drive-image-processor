
import errno
import os
import time
from pathlib import Path

import pytest
from PIL import Image

from image_processor import background_removal, batch, batch_cli, pipeline
from image_processor.background_removal import ModelInitializationError
from image_processor.manifest import MANIFEST_NAME
from image_processor.processing import OutputOptions


@pytest.fixture(autouse=True)
def mock_ai(monkeypatch):
    """Evita ejecutar o descargar modelos durante las pruebas."""

    def fake_process(image, model_name, options=None):
        return Image.new("RGB", (400, 400), "white")

    monkeypatch.setattr(
        batch, "process_image", fake_process
    )

    monkeypatch.setattr(
        batch, "prepare_model", lambda model_name: None
    )


@pytest.fixture
def real_model_init(monkeypatch):
    """Usa la inicialización real con new_session simulado."""

    background_removal._get_session.cache_clear()

    monkeypatch.setattr(
        batch, "prepare_model", background_removal.prepare_model
    )

    yield

    background_removal._get_session.cache_clear()


def test_batch_preserves_original(tmp_path):
    source = tmp_path / "producto.jpg"
    Image.new("RGB", (800, 600), "red").save(source)

    original_bytes = source.read_bytes()

    result = batch.process_folder(tmp_path)

    assert result.processed == 1
    assert result.failed == 0
    assert source.read_bytes() == original_bytes

    output = tmp_path / "TRANSFORMADA_producto.png"
    assert output.exists()

    with Image.open(output) as image:
        assert image.size == (400, 400)


def test_batch_skips_existing_results(tmp_path):
    source = tmp_path / "producto.jpg"
    Image.new("RGB", (800, 600), "blue").save(source)

    batch.process_folder(tmp_path)
    result = batch.process_folder(tmp_path)

    assert result.processed == 0
    assert result.skipped == 1
    assert result.failed == 0


def test_batch_continues_after_error(tmp_path):
    (tmp_path / "defectuosa.jpg").write_text(
        "Esto no es una imagen",
        encoding="utf-8",
    )

    Image.new("RGB", (200, 200), "green").save(
        tmp_path / "valida.jpg"
    )

    result = batch.process_folder(tmp_path)

    assert result.processed == 1
    assert result.failed == 1
    assert len(result.errors) == 1
    assert (
        tmp_path / "TRANSFORMADA_valida.png"
    ).exists()


class FailingResult:
    """Resultado cuyo guardado escribe datos parciales y falla."""

    def __init__(self, error):
        self.error = error

    def save(self, fp, format=None):
        fp.write(b"\x89PNG\r\n\x1a\n parcial")
        raise self.error


def assert_valid_png(path, size=(400, 400)):
    with Image.open(path) as image:
        image.load()
        assert image.format == "PNG"
        assert image.size == size


def test_batch_interrupted_save_leaves_no_output(tmp_path, monkeypatch):
    Image.new("RGB", (50, 50), "red").save(tmp_path / "producto.jpg")

    monkeypatch.setattr(
        batch,
        "process_image",
        lambda image, model_name, options=None: FailingResult(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        batch.process_folder(tmp_path)

    output = tmp_path / "TRANSFORMADA_producto.png"
    assert not output.exists()
    assert list(tmp_path.glob("*.tmp")) == []

    # La siguiente ejecución procesa la imagen pendiente.
    monkeypatch.setattr(
        batch,
        "process_image",
        lambda image, model_name, options=None: Image.new("RGB", (400, 400), "white"),
    )

    result = batch.process_folder(tmp_path)

    assert result.processed == 1
    assert result.skipped == 0
    assert_valid_png(output)


def test_batch_removes_stale_temp_and_processes(tmp_path):
    Image.new("RGB", (50, 50), "red").save(tmp_path / "producto.jpg")

    stale = tmp_path / "TRANSFORMADA_producto.png.abc12_yz.tmp"
    stale.write_bytes(b"parcial")
    old = time.time() - 25 * 60 * 60
    os.utime(stale, (old, old))

    result = batch.process_folder(tmp_path)

    assert result.temp_removed == 1
    assert result.processed == 1
    assert not stale.exists()
    assert_valid_png(tmp_path / "TRANSFORMADA_producto.png")


def test_batch_same_folder_ignores_temp_files(tmp_path):
    original = tmp_path / "producto.jpg"
    Image.new("RGB", (50, 50), "red").save(original)
    original_bytes = original.read_bytes()

    # Temporal reciente: podría pertenecer a otro proceso.
    recent = tmp_path / "TRANSFORMADA_otro.png.abc12_yz.tmp"
    recent.write_bytes(b"en curso")

    result = batch.process_folder(tmp_path, output_dir=tmp_path)

    assert result.processed == 1
    assert result.failed == 0
    assert result.temp_removed == 0
    assert original.read_bytes() == original_bytes
    assert recent.read_bytes() == b"en curso"

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        MANIFEST_NAME,
        "TRANSFORMADA_otro.png.abc12_yz.tmp",
        "TRANSFORMADA_producto.png",
        "producto.jpg",
    ]

    assert_valid_png(tmp_path / "TRANSFORMADA_producto.png")


def test_batch_continues_after_disk_full(tmp_path, monkeypatch):
    for name in ("a.jpg", "b.jpg"):
        Image.new("RGB", (50, 50), "red").save(tmp_path / name)

    calls = []

    def fake_process(image, model_name, options=None):
        calls.append(image)
        if len(calls) == 1:
            return FailingResult(
                OSError(errno.ENOSPC, "No queda espacio en el disco")
            )
        return Image.new("RGB", (400, 400), "white")

    monkeypatch.setattr(batch, "process_image", fake_process)

    result = batch.process_folder(tmp_path)

    assert result.processed == 1
    assert result.failed == 1
    assert "a.jpg" in result.errors[0]
    assert not (tmp_path / "TRANSFORMADA_a.png").exists()
    assert list(tmp_path.glob("*.tmp")) == []
    assert_valid_png(tmp_path / "TRANSFORMADA_b.png")


def make_images(directory, names):
    originals = {}

    for name in names:
        path = directory / name
        Image.new("RGB", (50, 50), "red").save(path)
        originals[path] = path.read_bytes()

    return originals


def failing_new_session(attempts):
    def fake_new_session(model_name):
        attempts.append(model_name)
        raise OSError("SSL: CERTIFICATE_VERIFY_FAILED (simulado)")

    return fake_new_session


def test_model_failure_is_attempted_once_per_batch(
    tmp_path, monkeypatch, real_model_init
):
    make_images(tmp_path, [f"p{i}.jpg" for i in range(5)])

    attempts = []
    processed = []

    monkeypatch.setattr(
        background_removal,
        "new_session",
        failing_new_session(attempts),
    )
    monkeypatch.setattr(
        batch,
        "process_image",
        lambda image, model_name, options=None: processed.append(image),
    )

    with pytest.raises(
        ModelInitializationError, match="isnet-general-use"
    ):
        batch.process_folder(tmp_path)

    assert attempts == ["isnet-general-use"]
    assert processed == []


def test_model_failure_creates_no_outputs_and_preserves_originals(
    tmp_path, monkeypatch, real_model_init
):
    originals = make_images(tmp_path, ["a.jpg", "b.png", "c.webp"])

    monkeypatch.setattr(
        background_removal,
        "new_session",
        failing_new_session([]),
    )

    # Misma carpeta como origen y destino.
    with pytest.raises(ModelInitializationError) as error:
        batch.process_folder(tmp_path, output_dir=tmp_path)

    assert isinstance(error.value.__cause__, OSError)

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "a.jpg", "b.png", "c.webp",
    ]

    for path, content in originals.items():
        assert path.read_bytes() == content


def test_batch_without_pending_images_skips_model_init(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(batch, "prepare_model", calls.append)

    (tmp_path / "notas.txt").write_text("No es una imagen")

    result = batch.process_folder(tmp_path)

    assert calls == []
    assert (result.processed, result.skipped, result.failed) == (0, 0, 0)


def test_batch_with_existing_results_skips_model_init(
    tmp_path, monkeypatch
):
    make_images(tmp_path, ["a.jpg", "b.jpg"])
    batch.process_folder(tmp_path)

    calls = []
    monkeypatch.setattr(batch, "prepare_model", calls.append)

    result = batch.process_folder(tmp_path)

    assert calls == []
    assert result.processed == 0
    assert result.skipped == 2


def test_batch_prepares_selected_model_once(tmp_path, monkeypatch):
    make_images(tmp_path, ["a.jpg", "b.jpg", "c.jpg"])

    calls = []
    monkeypatch.setattr(batch, "prepare_model", calls.append)

    result = batch.process_folder(
        tmp_path, model_name="birefnet-general-lite"
    )

    assert calls == ["birefnet-general-lite"]
    assert result.processed == 3


def test_initialized_session_is_reused_during_batch(
    tmp_path, monkeypatch, real_model_init
):
    make_images(tmp_path, ["a.jpg", "b.jpg", "c.jpg"])

    sessions = []
    used = []

    def fake_new_session(model_name):
        session = object()
        sessions.append(session)
        return session

    def fake_remove(image, session):
        used.append(session)
        cutout = Image.new("RGBA", image.size, (0, 0, 0, 0))
        cutout.paste((255, 0, 0, 255), (10, 10, 40, 40))
        return cutout

    monkeypatch.setattr(
        background_removal, "new_session", fake_new_session
    )
    monkeypatch.setattr(background_removal, "remove", fake_remove)
    monkeypatch.setattr(
        batch, "process_image", pipeline.process_image
    )

    result = batch.process_folder(tmp_path)

    assert result.processed == 3
    assert len(sessions) == 1
    assert used == sessions * 3

    for name in ("a", "b", "c"):
        assert_valid_png(tmp_path / f"TRANSFORMADA_{name}.png")


def options_aware_process(image, model_name, options=None):
    """Simula el pipeline respetando dimensiones y fondo."""

    options = options or OutputOptions()

    if options.background == "transparent":
        return Image.new(
            "RGBA", (options.width, options.height), (255, 0, 0, 128)
        )

    return Image.new("RGB", (options.width, options.height), "white")


def test_output_name_policy():
    assert (
        batch.output_name("producto", OutputOptions())
        == "TRANSFORMADA_producto.png"
    )
    assert (
        batch.output_name("producto", OutputOptions(400, 400, "white"))
        == "TRANSFORMADA_producto.png"
    )
    assert (
        batch.output_name(
            "producto", OutputOptions(800, 600, "transparent")
        )
        == "TRANSFORMADA_producto__800x600_transparent.png"
    )
    assert (
        batch.output_name(
            "producto", OutputOptions(400, 400, "transparent")
        )
        == "TRANSFORMADA_producto__400x400_transparent.png"
    )
    assert (
        batch.output_name("producto", OutputOptions(600, 800, "white"))
        == "TRANSFORMADA_producto__600x800_white.png"
    )


def test_batch_passes_default_options(tmp_path, monkeypatch):
    make_images(tmp_path, ["producto.jpg"])

    received = []

    def spy_process(image, model_name, options=None):
        received.append(options)
        return Image.new("RGB", (400, 400), "white")

    monkeypatch.setattr(batch, "process_image", spy_process)

    batch.process_folder(tmp_path)

    assert received == [OutputOptions()]
    assert (tmp_path / "TRANSFORMADA_producto.png").exists()


def test_batch_variants_do_not_collide_or_overwrite(
    tmp_path, monkeypatch
):
    originals = make_images(tmp_path, ["producto.jpg"])

    monkeypatch.setattr(batch, "process_image", options_aware_process)

    variants = {
        OutputOptions(): ("RGB", (400, 400)),
        OutputOptions(800, 600, "transparent"): ("RGBA", (800, 600)),
        OutputOptions(800, 600, "white"): ("RGB", (800, 600)),
        OutputOptions(400, 400, "transparent"): ("RGBA", (400, 400)),
    }

    # Misma carpeta como origen y destino.
    for options in variants:
        result = batch.process_folder(tmp_path, options=options)

        assert (result.processed, result.skipped, result.failed) == (
            1, 0, 0,
        )

    outputs = {}

    for options, (mode, size) in variants.items():
        path = tmp_path / batch.output_name("producto", options)

        with Image.open(path) as image:
            image.load()
            assert image.mode == mode
            assert image.size == size

        outputs[path] = path.read_bytes()

    assert len(outputs) == 4

    # Una segunda pasada omite cada variante sin sobrescribirla.
    for options in variants:
        result = batch.process_folder(tmp_path, options=options)

        assert (result.processed, result.skipped) == (0, 1)

    for path, content in outputs.items():
        assert path.read_bytes() == content

    for path, content in originals.items():
        assert path.read_bytes() == content

    # Original, cuatro variantes y el manifiesto.
    assert len(list(tmp_path.iterdir())) == 6


def test_batch_existing_variant_is_not_overwritten(
    tmp_path, monkeypatch
):
    make_images(tmp_path, ["producto.jpg"])
    monkeypatch.setattr(batch, "process_image", options_aware_process)

    options = OutputOptions(800, 600, "transparent")
    existing = tmp_path / batch.output_name("producto", options)
    existing.write_bytes(b"PREVIO")

    result = batch.process_folder(tmp_path, options=options)

    # H-05: sin registro en el manifiesto, el archivo es un
    # histórico sin verificar; se conserva y no se da por válido.
    assert (result.processed, result.skipped, result.unverified) == (0, 0, 1)
    assert existing.read_bytes() == b"PREVIO"

    # La variante predeterminada es otro resultado.
    result = batch.process_folder(tmp_path)

    assert result.processed == 1
    assert_valid_png(tmp_path / "TRANSFORMADA_producto.png")


def test_transparent_variant_uses_atomic_write(
    tmp_path, monkeypatch, real_model_init
):
    originals = make_images(tmp_path, ["producto.jpg"])

    options = OutputOptions(800, 600, "transparent")
    final_name = batch.output_name("producto", options)

    stale = tmp_path / f"{final_name}.abc12_yz.tmp"
    stale.write_bytes(b"parcial")
    old = time.time() - 25 * 60 * 60
    os.utime(stale, (old, old))

    def fake_remove(image, session):
        # Producto opaco con borde semitransparente.
        cutout = Image.new("RGBA", image.size, (0, 0, 0, 0))
        cutout.paste((255, 0, 0, 128), (5, 5, 45, 45))
        cutout.paste((255, 0, 0, 255), (10, 10, 40, 40))
        return cutout

    writes = []
    real_save = batch.save_png_exclusive

    def spy_save(image, path):
        writes.append(path)
        real_save(image, path)

    monkeypatch.setattr(
        background_removal, "new_session", lambda model_name: object()
    )
    monkeypatch.setattr(background_removal, "remove", fake_remove)
    monkeypatch.setattr(batch, "process_image", pipeline.process_image)
    monkeypatch.setattr(batch, "save_png_exclusive", spy_save)

    result = batch.process_folder(tmp_path, options=options)

    output = tmp_path / final_name

    assert result.processed == 1
    assert result.temp_removed == 1
    assert writes == [output]
    assert list(tmp_path.glob("*.tmp")) == []

    with Image.open(output) as image:
        image.load()
        assert image.mode == "RGBA"
        assert image.size == (800, 600)
        assert image.getpixel((0, 0))[3] == 0

        alphas = image.getchannel("A").histogram()

    # Opaco en el centro y semitransparente en el borde.
    assert alphas[255] > 0
    assert any(alphas[1:255])

    for path, content in originals.items():
        assert path.read_bytes() == content


def color_segmentation(empty_when_dark=True):
    """
    Simula rembg: las fotografías oscuras no tienen producto
    detectable y devuelven una máscara completamente vacía.
    """

    def fake_remove_background(image, model_name="isnet-general-use"):
        cutout = Image.new("RGBA", image.size, (0, 0, 0, 0))

        if empty_when_dark and sum(image.convert("RGB").getpixel((0, 0))) < 60:
            return cutout

        cutout.paste((255, 0, 0, 255), (10, 10, 40, 40))
        return cutout

    return fake_remove_background


def make_batch_with_empty_segmentation(directory):
    originals = make_images(directory, ["a.png", "b.png", "c.png", "e.png"])

    dark = directory / "d_vacia.png"
    Image.new("RGB", (50, 50), "black").save(dark)
    originals[dark] = dark.read_bytes()

    return originals


@pytest.mark.parametrize(
    "options",
    [
        OutputOptions(),
        OutputOptions(background="transparent"),
        OutputOptions(800, 600, "white"),
        OutputOptions(640, 960, "transparent"),
    ],
)
def test_empty_segmentation_fails_and_batch_continues(
    tmp_path, monkeypatch, options
):
    originals = make_batch_with_empty_segmentation(tmp_path)

    monkeypatch.setattr(
        pipeline, "remove_background", color_segmentation()
    )
    monkeypatch.setattr(batch, "process_image", pipeline.process_image)

    result = batch.process_folder(tmp_path, options=options)

    assert (result.processed, result.failed, result.skipped) == (4, 1, 0)
    assert len(result.errors) == 1
    assert result.errors[0].startswith("d_vacia.png: ")
    assert "segmentación está vacía" in result.errors[0]

    assert not (tmp_path / batch.output_name("d_vacia", options)).exists()

    mode = "RGBA" if options.background == "transparent" else "RGB"

    for name in ("a", "b", "c", "e"):
        output = tmp_path / batch.output_name(name, options)

        with Image.open(output) as image:
            image.load()
            assert image.mode == mode
            assert image.size == (options.width, options.height)

    assert list(tmp_path.glob("*.tmp")) == []

    for path, content in originals.items():
        assert path.read_bytes() == content


def test_empty_segmentation_can_be_retried(tmp_path, monkeypatch):
    originals = make_batch_with_empty_segmentation(tmp_path)

    monkeypatch.setattr(batch, "process_image", pipeline.process_image)
    monkeypatch.setattr(
        pipeline, "remove_background", color_segmentation()
    )

    first = batch.process_folder(tmp_path)

    assert (first.processed, first.failed, first.skipped) == (4, 1, 0)

    # Otro modelo o una nueva ejecución sí detecta el producto.
    monkeypatch.setattr(
        pipeline,
        "remove_background",
        color_segmentation(empty_when_dark=False),
    )

    second = batch.process_folder(tmp_path)

    assert (second.processed, second.failed, second.skipped) == (1, 0, 4)
    assert_valid_png(tmp_path / "TRANSFORMADA_d_vacia.png")
    assert list(tmp_path.glob("*.tmp")) == []

    for path, content in originals.items():
        assert path.read_bytes() == content


SAME_STEM_COLORS = {
    "producto.jpg": (220, 30, 30),
    "producto.png": (30, 200, 30),
    "producto.webp": (30, 30, 220),
}


def make_colored(directory, colors):
    originals = {}

    for name, color in colors.items():
        path = directory / name
        Image.new("RGB", (60, 60), color).save(path)
        originals[path] = path.read_bytes()

    return originals


def color_copy_process(image, model_name, options=None):
    """Resultado del color del original: revela qué original lo generó."""

    options = options or OutputOptions()
    color = image.convert("RGB").getpixel((30, 30))
    mode = "RGBA" if options.background == "transparent" else "RGB"
    fill = color + (255,) if mode == "RGBA" else color

    return Image.new(mode, (options.width, options.height), fill)


def assert_output_color(path, color, size=(400, 400)):
    with Image.open(path) as image:
        image.load()
        assert image.format == "PNG"
        assert image.size == size
        pixel = image.convert("RGB").getpixel((size[0] // 2, size[1] // 2))

    # JPEG y WebP comprimen con pérdida.
    assert all(abs(a - b) <= 12 for a, b in zip(pixel, color))


def test_same_stem_originals_get_independent_outputs(tmp_path, monkeypatch):
    originals = make_colored(tmp_path, SAME_STEM_COLORS)
    monkeypatch.setattr(batch, "process_image", color_copy_process)

    # Misma carpeta como origen y destino.
    result = batch.process_folder(tmp_path)

    assert (result.processed, result.skipped, result.failed) == (3, 0, 0)
    assert result.warnings == []

    for name, color in SAME_STEM_COLORS.items():
        tag = name.rsplit(".", 1)[1]
        assert_output_color(
            tmp_path / f"TRANSFORMADA_producto__src-{tag}.png", color
        )

    assert not (tmp_path / "TRANSFORMADA_producto.png").exists()
    assert list(tmp_path.glob("*.tmp")) == []

    for path, content in originals.items():
        assert path.read_bytes() == content


def test_two_same_stem_originals_with_variants(tmp_path, monkeypatch):
    colors = {
        "producto.jpg": SAME_STEM_COLORS["producto.jpg"],
        "producto.png": SAME_STEM_COLORS["producto.png"],
    }
    originals = make_colored(tmp_path, colors)
    monkeypatch.setattr(batch, "process_image", color_copy_process)

    variants = [
        OutputOptions(),
        OutputOptions(background="transparent"),
        OutputOptions(800, 600, "white"),
        OutputOptions(800, 600, "transparent"),
    ]

    for options in variants:
        result = batch.process_folder(tmp_path, options=options)
        assert (result.processed, result.failed) == (2, 0)

    outputs = sorted(
        path.name for path in tmp_path.glob("TRANSFORMADA_*.png")
    )

    assert outputs == sorted([
        "TRANSFORMADA_producto__src-jpg.png",
        "TRANSFORMADA_producto__src-png.png",
        "TRANSFORMADA_producto__src-jpg__400x400_transparent.png",
        "TRANSFORMADA_producto__src-png__400x400_transparent.png",
        "TRANSFORMADA_producto__src-jpg__800x600_white.png",
        "TRANSFORMADA_producto__src-png__800x600_white.png",
        "TRANSFORMADA_producto__src-jpg__800x600_transparent.png",
        "TRANSFORMADA_producto__src-png__800x600_transparent.png",
    ])

    assert_output_color(
        tmp_path / "TRANSFORMADA_producto__src-png__800x600_white.png",
        colors["producto.png"],
        size=(800, 600),
    )

    snapshot = {
        path: path.read_bytes() for path in tmp_path.glob("TRANSFORMADA_*")
    }

    # Ejecución repetida: todo se omite y nada cambia.
    calls = []
    monkeypatch.setattr(batch, "prepare_model", calls.append)

    for options in variants:
        result = batch.process_folder(tmp_path, options=options)
        assert (result.processed, result.skipped, result.failed) == (0, 2, 0)

    assert calls == []

    for path, content in snapshot.items():
        assert path.read_bytes() == content

    for path, content in originals.items():
        assert path.read_bytes() == content


def test_existing_same_stem_output_is_not_overwritten(tmp_path, monkeypatch):
    make_colored(tmp_path, SAME_STEM_COLORS)
    monkeypatch.setattr(batch, "process_image", color_copy_process)

    existing = tmp_path / "TRANSFORMADA_producto__src-png.png"
    existing.write_bytes(b"PREVIO")

    result = batch.process_folder(tmp_path)

    # H-05: el archivo previo no tiene registro y no se da por válido.
    assert (result.processed, result.skipped, result.failed) == (2, 0, 0)
    assert result.unverified == 1
    assert existing.read_bytes() == b"PREVIO"


def test_existing_output_with_other_case_is_not_overwritten(
    tmp_path, monkeypatch
):
    make_colored(tmp_path, {"producto.jpg": (220, 30, 30)})
    monkeypatch.setattr(batch, "process_image", color_copy_process)

    existing = tmp_path / "transformada_PRODUCTO.png"
    existing.write_bytes(b"PREVIO")

    calls = []
    monkeypatch.setattr(batch, "prepare_model", calls.append)

    result = batch.process_folder(tmp_path)

    # El nombre coincide sin distinguir mayúsculas: es el mismo
    # destino, sin registro, así que queda sin verificar.
    assert (result.processed, result.skipped, result.unverified) == (0, 0, 1)
    assert calls == []
    assert existing.read_bytes() == b"PREVIO"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "producto.jpg", "transformada_PRODUCTO.png",
    ]


def test_src_like_original_gets_its_own_output(tmp_path, monkeypatch):
    colors = {
        "producto.jpg": (220, 30, 30),
        "producto.png": (30, 200, 30),
        "producto__src-jpg.png": (30, 30, 220),
    }
    originals = make_colored(tmp_path, colors)
    monkeypatch.setattr(batch, "process_image", color_copy_process)

    result = batch.process_folder(tmp_path)

    assert (result.processed, result.skipped, result.failed) == (3, 0, 0)

    expected = {
        "producto.jpg": "TRANSFORMADA_producto__src-jpg.png",
        "producto.png": "TRANSFORMADA_producto__src-png.png",
        "producto__src-jpg.png": "TRANSFORMADA_producto%5F%5Fsrc-jpg__esc.png",
    }

    assert sorted(path.name for path in tmp_path.glob("TRANSFORMADA_*")) == (
        sorted(expected.values())
    )

    for source, output in expected.items():
        assert_output_color(tmp_path / output, colors[source])

    assert list(tmp_path.glob("*.tmp")) == []

    for path, content in originals.items():
        assert path.read_bytes() == content


def test_new_original_makes_historic_result_ambiguous(
    tmp_path, monkeypatch
):
    make_colored(tmp_path, {"producto.jpg": (220, 30, 30)})
    monkeypatch.setattr(batch, "process_image", color_copy_process)

    first = batch.process_folder(tmp_path)
    historic = tmp_path / "TRANSFORMADA_producto.png"
    historic_bytes = historic.read_bytes()

    assert first.processed == 1

    make_colored(tmp_path, {"producto.png": (30, 200, 30)})

    second = batch.process_folder(tmp_path)

    assert (second.processed, second.skipped, second.failed) == (2, 0, 0)
    assert len(second.warnings) == 1
    assert "TRANSFORMADA_producto.png" in second.warnings[0]
    assert historic.read_bytes() == historic_bytes

    assert_output_color(
        tmp_path / "TRANSFORMADA_producto__src-jpg.png", (220, 30, 30)
    )
    assert_output_color(
        tmp_path / "TRANSFORMADA_producto__src-png.png", (30, 200, 30)
    )


def test_removed_original_does_not_inherit_historic_result(
    tmp_path, monkeypatch
):
    # Primera ejecución solo con producto.png: genera el histórico.
    make_colored(tmp_path, {"producto.png": (30, 200, 30)})
    monkeypatch.setattr(batch, "process_image", color_copy_process)
    batch.process_folder(tmp_path)

    # Aparece producto.jpg: ambos usan __src-.
    make_colored(tmp_path, {"producto.jpg": (220, 30, 30)})
    batch.process_folder(tmp_path)

    # Se elimina producto.png: el histórico (verde) no debe
    # atribuirse a producto.jpg (rojo).
    (tmp_path / "producto.png").unlink()

    result = batch.process_folder(tmp_path)

    assert (result.processed, result.skipped, result.failed) == (0, 1, 0)
    assert len(result.warnings) == 1
    assert_output_color(
        tmp_path / "TRANSFORMADA_producto__src-jpg.png", (220, 30, 30)
    )

    # Aunque falte su propio resultado, producto.jpg se regenera
    # en vez de darse por procesado con el histórico verde.
    (tmp_path / "TRANSFORMADA_producto__src-jpg.png").unlink()

    result = batch.process_folder(tmp_path)

    assert (result.processed, result.skipped, result.failed) == (1, 0, 0)
    assert_output_color(
        tmp_path / "TRANSFORMADA_producto__src-jpg.png", (220, 30, 30)
    )
    assert_output_color(
        tmp_path / "TRANSFORMADA_producto.png", (30, 200, 30)
    )


def test_discovery_order_does_not_change_processing(
    tmp_path, monkeypatch
):
    colors = {
        "producto.jpg": (220, 30, 30),
        "producto.png": (30, 200, 30),
        "Banner.webp": (30, 30, 220),
        "zeta.jpeg": (200, 200, 30),
    }

    runs = []
    real_discover = batch._discover_sources

    for reverse in (False, True):
        directory = tmp_path / f"orden_{reverse}"
        directory.mkdir()
        make_colored(directory, colors)

        order = []

        def recording_process(image, model_name, options=None):
            order.append(Path(image.filename).name)
            return color_copy_process(image, model_name, options)

        def discover(input_dir, reverse=reverse):
            found = sorted(real_discover(input_dir), key=lambda p: p.name)
            return list(reversed(found)) if reverse else found

        monkeypatch.setattr(batch, "_discover_sources", discover)
        monkeypatch.setattr(batch, "process_image", recording_process)

        result = batch.process_folder(directory)

        runs.append((
            order,
            sorted(path.name for path in directory.glob("TRANSFORMADA_*")),
            result,
        ))

    assert runs[0][0] == runs[1][0] == [
        "Banner.webp", "producto.jpg", "producto.png", "zeta.jpeg",
    ]
    assert runs[0][1] == runs[1][1]
    assert runs[0][2] == runs[1][2]


# --- Revisión correctiva de H-04 -----------------------------------

RED = (220, 30, 30)
GREEN = (30, 200, 30)
BLUE = (30, 30, 220)


def output_colors(directory):
    """Color central y tamaño de cada resultado, por nombre."""

    found = {}

    for path in sorted(directory.glob("TRANSFORMADA_*.png")):
        with Image.open(path) as image:
            image.load()
            size = image.size
            pixel = image.convert("RGB").getpixel(
                (size[0] // 2, size[1] // 2)
            )

        found[path.name] = (pixel, size)

    return found


def close(pixel, color):
    return all(abs(a - b) <= 12 for a, b in zip(pixel, color))


@pytest.mark.parametrize("transparent_first", [False, True])
def test_reserved_suffix_original_does_not_share_variant_result(
    tmp_path, monkeypatch, transparent_first
):
    originals = make_colored(
        tmp_path,
        {
            "producto.jpg": RED,
            "producto__800x600_transparent.jpg": GREEN,
        },
    )
    monkeypatch.setattr(batch, "process_image", color_copy_process)

    configurations = [OutputOptions(), OutputOptions(800, 600, "transparent")]

    if transparent_first:
        configurations.reverse()

    for options in configurations:
        result = batch.process_folder(tmp_path, options=options)

        # Ninguna imagen puede darse por procesada con el
        # resultado de la otra.
        assert (result.processed, result.skipped, result.failed) == (2, 0, 0)

    outputs = output_colors(tmp_path)

    assert len(outputs) == 4

    for color in (RED, GREEN):
        sizes = sorted(
            size for pixel, size in outputs.values() if close(pixel, color)
        )
        assert sizes == [(400, 400), (800, 600)]

    for path, content in originals.items():
        assert path.read_bytes() == content


def test_new_src_like_original_is_not_considered_processed(
    tmp_path, monkeypatch
):
    make_colored(tmp_path, {"producto.jpg": RED, "producto.png": GREEN})
    monkeypatch.setattr(batch, "process_image", color_copy_process)

    batch.process_folder(tmp_path)

    old_jpg = tmp_path / "TRANSFORMADA_producto__src-jpg.png"
    old_bytes = old_jpg.read_bytes()

    (tmp_path / "producto.jpg").unlink()
    (tmp_path / "producto.png").unlink()

    make_colored(tmp_path, {"producto__src-jpg.png": BLUE})

    result = batch.process_folder(tmp_path)

    assert (result.processed, result.skipped, result.failed) == (1, 0, 0)
    assert old_jpg.read_bytes() == old_bytes

    blue = [
        name
        for name, (pixel, _) in output_colors(tmp_path).items()
        if close(pixel, BLUE)
    ]
    assert len(blue) == 1
    assert blue[0] != old_jpg.name


def test_historic_variant_of_removed_original_is_not_inherited(
    tmp_path, monkeypatch
):
    transparent = OutputOptions(800, 600, "transparent")
    monkeypatch.setattr(batch, "process_image", color_copy_process)

    # 1. Solo producto.png, en dos variantes.
    make_colored(tmp_path, {"producto.png": GREEN})
    batch.process_folder(tmp_path)
    batch.process_folder(tmp_path, options=transparent)

    green_transparent = (
        tmp_path / "TRANSFORMADA_producto__800x600_transparent.png"
    )
    green_bytes = green_transparent.read_bytes()

    # 2. Aparece producto.jpg; solo la variante predeterminada.
    make_colored(tmp_path, {"producto.jpg": RED})
    batch.process_folder(tmp_path)

    # 3. Se elimina producto.png.
    (tmp_path / "producto.png").unlink()

    # 4. Variante transparente sobre producto.jpg.
    result = batch.process_folder(tmp_path, options=transparent)

    assert (result.processed, result.skipped, result.failed) == (1, 0, 0)
    assert green_transparent.read_bytes() == green_bytes
    assert any(green_transparent.name in item for item in result.warnings)

    red_transparent = [
        name
        for name, (pixel, size) in output_colors(tmp_path).items()
        if close(pixel, RED) and size == (800, 600)
    ]
    assert len(red_transparent) == 1


def test_legacy_reserved_suffix_result_is_reported_as_ambiguous(
    tmp_path, monkeypatch, capsys
):
    make_colored(
        tmp_path,
        {
            "producto.jpg": RED,
            "producto__800x600_transparent.jpg": GREEN,
        },
    )
    monkeypatch.setattr(batch, "process_image", color_copy_process)

    # Resultado generado por una versión anterior para el original
    # verde, con un nombre que ahora pediría producto.jpg.
    legacy = tmp_path / "TRANSFORMADA_producto__800x600_transparent.png"
    Image.new("RGB", (400, 400), GREEN).save(legacy)
    legacy_bytes = legacy.read_bytes()

    exit_code = batch_cli.main([
        "--input", str(tmp_path),
        "--background", "transparent",
        "--width", "800",
        "--height", "600",
    ])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Procesadas: 1" in captured.out
    assert "Omitidas:   0" in captured.out
    assert "Fallidas:   1" in captured.out
    assert "producto__800x600_transparent.jpg" in captured.err
    assert legacy.read_bytes() == legacy_bytes

    red = [
        name
        for name, (pixel, _) in output_colors(tmp_path).items()
        if close(pixel, RED)
    ]
    assert red == []


def make_normalization_pair(directory):
    composed = directory / "café.png"
    decomposed = directory / "café.png"

    Image.new("RGB", (60, 60), RED).save(composed)
    Image.new("RGB", (60, 60), GREEN).save(decomposed)

    if len(list(directory.iterdir())) != 2:
        pytest.skip("El sistema de archivos normaliza Unicode")

    return {path: path.read_bytes() for path in (composed, decomposed)}


def test_unresolvable_collision_does_not_affect_other_images(
    tmp_path, monkeypatch
):
    originals = make_normalization_pair(tmp_path)
    originals.update(make_colored(tmp_path, {"otro.png": BLUE}))
    monkeypatch.setattr(batch, "process_image", color_copy_process)

    result = batch.process_folder(tmp_path)

    assert (result.processed, result.skipped, result.failed) == (1, 0, 2)
    assert all("Renombre" in error for error in result.errors)

    outputs = output_colors(tmp_path)

    assert list(outputs) == ["TRANSFORMADA_otro.png"]
    assert close(outputs["TRANSFORMADA_otro.png"][0], BLUE)
    assert list(tmp_path.glob("*.tmp")) == []

    for path, content in originals.items():
        assert path.read_bytes() == content


def test_unresolvable_collisions_alone_do_not_initialize_model(
    tmp_path, monkeypatch
):
    make_normalization_pair(tmp_path)
    make_colored(tmp_path, {"otro.png": BLUE})
    (tmp_path / "TRANSFORMADA_otro.png").write_bytes(b"PREVIO")

    calls = []
    monkeypatch.setattr(batch, "prepare_model", calls.append)

    result = batch.process_folder(tmp_path)

    assert (result.processed, result.skipped, result.failed) == (0, 0, 2)
    assert result.unverified == 1
    assert calls == []


REPEATED_CONFIGURATIONS = [
    OutputOptions(),
    OutputOptions(800, 600, "transparent"),
    OutputOptions(800, 600, "white"),
    OutputOptions(background="transparent"),
]


@pytest.mark.parametrize(
    "colors",
    [
        # Tipo 1: el nombre del hermano contiene una variante.
        {"producto.jpg": RED, "producto__800x600_transparent.jpg": GREEN},
        # Tipo 2: el nombre del hermano contiene un bloque __src-.
        {
            "producto.jpg": RED,
            "producto.png": GREEN,
            "producto__src-jpg.png": BLUE,
        },
    ],
    ids=["variante", "src"],
)
def test_repeated_runs_with_reserved_suffix_siblings_are_stable(
    tmp_path, monkeypatch, colors
):
    originals = make_colored(tmp_path, colors)
    monkeypatch.setattr(batch, "process_image", color_copy_process)

    for options in REPEATED_CONFIGURATIONS:
        first = batch.process_folder(tmp_path, options=options)
        assert (first.processed, first.skipped, first.failed) == (
            len(colors), 0, 0,
        )

    snapshot = {
        path: path.read_bytes() for path in tmp_path.glob("TRANSFORMADA_*")
    }
    assert len(snapshot) == len(colors) * len(REPEATED_CONFIGURATIONS)

    # Cada resultado tiene el color de un único original.
    for color in colors.values():
        assert sum(
            close(pixel, color) for pixel, _ in output_colors(tmp_path).values()
        ) == len(REPEATED_CONFIGURATIONS)

    for _ in range(2):
        for options in REPEATED_CONFIGURATIONS:
            again = batch.process_folder(tmp_path, options=options)
            assert (again.processed, again.skipped, again.failed) == (
                0, len(colors), 0,
            )
            assert again.errors == []

    for path, content in snapshot.items():
        assert path.read_bytes() == content

    for path, content in originals.items():
        assert path.read_bytes() == content
