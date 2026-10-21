
from PIL import Image

from image_processor import batch, batch_cli


def fake_process(image, model_name):
    return Image.new("RGB", (400, 400), "white")


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
