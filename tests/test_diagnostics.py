
from PIL import Image

from image_processor import diagnostics, diagnostics_cli


def fake_remove_background(image, model_name):
    cutout = Image.new(
        "RGBA",
        image.size,
        (0, 0, 0, 0),
    )

    cutout.paste(
        (255, 0, 0, 255),
        (10, 20, 50, 60),
    )

    # Ruido casi invisible que debe ignorarse
    # al calcular el área visible.
    cutout.putpixel(
        (image.width - 1, 0),
        (0, 0, 0, 1),
    )

    return cutout


def test_diagnostic_generates_all_images(monkeypatch):
    monkeypatch.setattr(
        diagnostics,
        "remove_background",
        fake_remove_background,
    )

    original = Image.new("RGB", (100, 80), "blue")
    original_bytes = original.tobytes()

    result = diagnostics.diagnose_image(original)

    assert result.mask.size == (100, 80)
    assert result.cutout.mode == "RGBA"
    assert result.final.size == (400, 400)
    assert result.comparison.size == (1200, 432)

    assert original.tobytes() == original_bytes


def test_diagnostic_ignores_low_alpha_noise(monkeypatch):
    monkeypatch.setattr(
        diagnostics,
        "remove_background",
        fake_remove_background,
    )

    original = Image.new("RGB", (100, 80), "blue")

    result = diagnostics.diagnose_image(original)

    assert result.bounding_box == (10, 20, 50, 60)
    assert result.coverage_percent == 20.0

    # La máscara original conserva el píxel débil.
    assert result.mask.getpixel((99, 0)) == 1


def test_diagnostic_cli_exports_files(tmp_path, monkeypatch):
    monkeypatch.setattr(
        diagnostics,
        "remove_background",
        fake_remove_background,
    )

    source = tmp_path / "producto.jpg"
    Image.new("RGB", (100, 80), "blue").save(source)

    output = tmp_path / "diagnostics"
    original_bytes = source.read_bytes()

    arguments = [
        "--input", str(source),
        "--output", str(output),
    ]

    assert diagnostics_cli.main(arguments) == 0

    assert len(list(output.glob("*.png"))) == 4
    assert source.read_bytes() == original_bytes

    # Ejecutarlo de nuevo no debe sobrescribir.
    assert diagnostics_cli.main(arguments) == 2
    assert len(list(output.glob("*.png"))) == 4


def test_diagnostic_cli_rejects_missing_file(
    tmp_path, capsys
):
    code = diagnostics_cli.main([
        "--input",
        str(tmp_path / "no_existe.jpg"),
    ])

    assert code == 2
    assert "[ERROR]" in capsys.readouterr().err
