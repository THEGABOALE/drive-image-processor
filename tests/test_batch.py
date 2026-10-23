
import errno
import os
import time

import pytest
from PIL import Image

from image_processor import background_removal, batch, pipeline
from image_processor.background_removal import ModelInitializationError
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

    assert len(list(tmp_path.iterdir())) == 5


def test_batch_existing_variant_is_not_overwritten(
    tmp_path, monkeypatch
):
    make_images(tmp_path, ["producto.jpg"])
    monkeypatch.setattr(batch, "process_image", options_aware_process)

    options = OutputOptions(800, 600, "transparent")
    existing = tmp_path / batch.output_name("producto", options)
    existing.write_bytes(b"PREVIO")

    result = batch.process_folder(tmp_path, options=options)

    assert (result.processed, result.skipped) == (0, 1)
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
