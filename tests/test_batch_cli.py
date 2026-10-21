
import pytest
from PIL import Image

from image_processor import background_removal, batch, batch_cli


def fake_process(image, model_name):
    return Image.new("RGB", (400, 400), "white")


@pytest.fixture(autouse=True)
def mock_model_init(monkeypatch):
    """Evita inicializar o descargar modelos reales."""

    monkeypatch.setattr(
        batch, "prepare_model", lambda model_name: None
    )


def test_cli_processes_folder(tmp_path, monkeypatch, capsys):
    input_dir = tmp_path / "inputs"
    output_dir = tmp_path / "outputs"
    input_dir.mkdir()

    original = input_dir / "producto.jpg"
    Image.new("RGB", (800, 600), "red").save(original)

    original_bytes = original.read_bytes()

    monkeypatch.setattr(batch, "process_image", fake_process)

    exit_code = batch_cli.main([
        "--input", str(input_dir),
        "--output", str(output_dir),
    ])

    assert exit_code == 0
    assert (output_dir / "TRANSFORMADA_producto.png").exists()
    assert original.read_bytes() == original_bytes
    assert "Procesadas: 1" in capsys.readouterr().out


def test_cli_invalid_input(tmp_path, capsys):
    invalid_dir = tmp_path / "no_existe"

    exit_code = batch_cli.main([
        "--input", str(invalid_dir),
    ])

    assert exit_code == 2
    assert "[ERROR]" in capsys.readouterr().err


def test_cli_reports_processing_errors(tmp_path, capsys):
    invalid_image = tmp_path / "corrupta.jpg"
    invalid_image.write_text("No es una imagen")

    exit_code = batch_cli.main([
        "--input", str(tmp_path),
    ])

    assert exit_code == 1
    assert "Fallidas:   1" in capsys.readouterr().out


def test_cli_reports_model_initialization_failure(
    tmp_path, monkeypatch, capsys
):
    original = tmp_path / "producto.jpg"
    Image.new("RGB", (50, 50), "red").save(original)
    original_bytes = original.read_bytes()

    def failing_new_session(model_name):
        raise OSError("SSL: CERTIFICATE_VERIFY_FAILED (simulado)")

    background_removal._get_session.cache_clear()

    monkeypatch.setattr(
        batch, "prepare_model", background_removal.prepare_model
    )
    monkeypatch.setattr(
        background_removal, "new_session", failing_new_session
    )

    try:
        exit_code = batch_cli.main(["--input", str(tmp_path)])
    finally:
        background_removal._get_session.cache_clear()

    captured = capsys.readouterr()

    assert exit_code == 3
    assert captured.err.count("No se pudo inicializar el modelo") == 1
    assert "isnet-general-use" in captured.err
    assert "CERTIFICATE_VERIFY_FAILED" in captured.err
    assert "RESUMEN DEL LOTE" not in captured.out

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "producto.jpg"
    ]
    assert original.read_bytes() == original_bytes
