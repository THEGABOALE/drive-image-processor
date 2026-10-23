
import pytest
from PIL import Image

from image_processor import background_removal, batch, batch_cli, pipeline


def fake_process(image, model_name, options=None):
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


def options_aware_process(image, model_name, options=None):
    if options.background == "transparent":
        return Image.new(
            "RGBA", (options.width, options.height), (255, 0, 0, 128)
        )

    return Image.new("RGB", (options.width, options.height), "white")


def test_cli_custom_background_and_dimensions(
    tmp_path, monkeypatch, capsys
):
    input_dir = tmp_path / "inputs"
    output_dir = tmp_path / "outputs"
    input_dir.mkdir()

    original = input_dir / "producto.jpg"
    Image.new("RGB", (800, 600), "red").save(original)
    original_bytes = original.read_bytes()

    monkeypatch.setattr(batch, "process_image", options_aware_process)

    exit_code = batch_cli.main([
        "--input", str(input_dir),
        "--output", str(output_dir),
        "--background", "transparent",
        "--width", "800",
        "--height", "600",
    ])

    assert exit_code == 0
    assert "800 x 600 px, fondo transparent" in capsys.readouterr().out

    output = output_dir / "TRANSFORMADA_producto__800x600_transparent.png"

    with Image.open(output) as image:
        image.load()
        assert image.mode == "RGBA"
        assert image.size == (800, 600)

    # Sin parámetros nuevos: variante predeterminada, sin colisión.
    exit_code = batch_cli.main([
        "--input", str(input_dir),
        "--output", str(output_dir),
    ])

    assert exit_code == 0

    with Image.open(output_dir / "TRANSFORMADA_producto.png") as image:
        assert image.mode == "RGB"
        assert image.size == (400, 400)

    assert original.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "arguments",
    [
        ["--width", "0"],
        ["--height", "-5"],
        ["--width", "6001"],
        ["--width", "5000", "--height", "5000"],
    ],
)
def test_cli_rejects_invalid_dimensions(
    tmp_path, monkeypatch, capsys, arguments
):
    Image.new("RGB", (50, 50), "red").save(tmp_path / "producto.jpg")

    calls = []
    monkeypatch.setattr(batch, "prepare_model", calls.append)

    output_dir = tmp_path / "outputs"

    exit_code = batch_cli.main([
        "--input", str(tmp_path),
        "--output", str(output_dir),
        *arguments,
    ])

    assert exit_code == 2
    assert "[ERROR]" in capsys.readouterr().err
    assert calls == []
    assert not output_dir.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--background", "azul"],
        ["--width", "ancho"],
    ],
)
def test_cli_rejects_invalid_argument_values(tmp_path, arguments):
    with pytest.raises(SystemExit) as error:
        batch_cli.main(["--input", str(tmp_path), *arguments])

    assert error.value.code == 2


@pytest.mark.parametrize(
    "arguments, output_name",
    [
        ([], "TRANSFORMADA_valida.png"),
        (
            ["--background", "transparent", "--width", "800", "--height", "600"],
            "TRANSFORMADA_valida__800x600_transparent.png",
        ),
    ],
)
def test_cli_reports_empty_segmentation(
    tmp_path, monkeypatch, capsys, arguments, output_name
):
    valid = tmp_path / "valida.png"
    empty = tmp_path / "vacia.png"
    Image.new("RGB", (50, 50), "red").save(valid)
    Image.new("RGB", (50, 50), "black").save(empty)
    originals = {path: path.read_bytes() for path in (valid, empty)}

    def fake_remove_background(image, model_name="isnet-general-use"):
        cutout = Image.new("RGBA", image.size, (0, 0, 0, 0))

        if image.convert("RGB").getpixel((0, 0)) != (0, 0, 0):
            cutout.paste((255, 0, 0, 255), (10, 10, 40, 40))

        return cutout

    monkeypatch.setattr(
        pipeline, "remove_background", fake_remove_background
    )
    monkeypatch.setattr(batch, "process_image", pipeline.process_image)

    exit_code = batch_cli.main(["--input", str(tmp_path), *arguments])

    captured = capsys.readouterr()

    # Código 1: fallos individuales, no de inicialización (3).
    assert exit_code == 1
    assert "Procesadas: 1" in captured.out
    assert "Fallidas:   1" in captured.out
    assert "vacia.png" in captured.err
    assert "segmentación está vacía" in captured.err

    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [output_name, "vacia.png", "valida.png"]
    )

    for path, content in originals.items():
        assert path.read_bytes() == content


def test_cli_reports_ambiguous_historic_result(tmp_path, monkeypatch, capsys):
    for name in ("producto.jpg", "producto.png"):
        Image.new("RGB", (50, 50), "red").save(tmp_path / name)

    historic = tmp_path / "TRANSFORMADA_producto.png"
    historic.write_bytes(b"PREVIO")

    monkeypatch.setattr(batch, "process_image", fake_process)

    exit_code = batch_cli.main(["--input", str(tmp_path)])

    captured = capsys.readouterr()

    # Una advertencia no es un fallo.
    assert exit_code == 0
    assert "Procesadas: 2" in captured.out
    assert "Advertencias:" in captured.out
    assert "TRANSFORMADA_producto.png: resultado ambiguo" in captured.err
    assert historic.read_bytes() == b"PREVIO"


def test_cli_reports_unresolvable_collision(tmp_path, monkeypatch, capsys):
    # Mismo nombre en NFC y NFD: NTFS los admite juntos.
    for name in ("café.png", "café.png", "otro.png"):
        Image.new("RGB", (50, 50), "red").save(tmp_path / name)

    if len(list(tmp_path.iterdir())) != 3:
        pytest.skip("El sistema de archivos normaliza Unicode")

    monkeypatch.setattr(batch, "process_image", fake_process)

    exit_code = batch_cli.main(["--input", str(tmp_path)])

    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Procesadas: 1" in captured.out
    assert "Fallidas:   2" in captured.out
    assert captured.err.count("Renombre uno de los originales") == 2
