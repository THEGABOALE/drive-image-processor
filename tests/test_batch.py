
import pytest
from PIL import Image

from image_processor import batch


@pytest.fixture(autouse=True)
def mock_ai(monkeypatch):
    """Evita ejecutar o descargar modelos durante las pruebas."""

    def fake_process(image, model_name):
        return Image.new("RGB", (400, 400), "white")

    monkeypatch.setattr(
        batch, "process_image", fake_process
    )


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
