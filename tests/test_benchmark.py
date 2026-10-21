
from types import SimpleNamespace

import pytest
from PIL import Image

from image_processor import benchmark, benchmark_cli


@pytest.fixture
def fake_models(monkeypatch):
    loads = []

    def fake_prepare(model_name):
        loads.append(model_name)

    def fake_diagnose(image, model_name):
        return SimpleNamespace(
            final=Image.new("RGB", (400, 400), "white"),
            comparison=Image.new(
                "RGB", (1200, 432), "white"
            ),
            coverage_percent=25.0,
            bounding_box=(10, 10, 90, 90),
        )

    monkeypatch.setattr(
        benchmark, "prepare_model", fake_prepare
    )

    monkeypatch.setattr(
        benchmark, "diagnose_image", fake_diagnose
    )

    return loads


def test_benchmark_two_models(tmp_path, fake_models):
    source = tmp_path / "inputs"
    output = tmp_path / "benchmark"
    source.mkdir()

    for name in ("a.jpg", "b.jpg"):
        Image.new(
            "RGB", (100, 100), "red"
        ).save(source / name)

    rows, times = benchmark.evaluate_folder(
        source,
        output,
        ["isnet-general-use", "birefnet-general-lite"],
    )

    assert len(rows) == 4
    assert all(row.status == "ok" for row in rows)
    assert len(times) == 2
    assert len(fake_models) == 2

    assert (output / "reporte.json").exists()
    assert (output / "reporte.csv").exists()

    assert len(list(output.rglob("*.png"))) == 8


def test_benchmark_continues_after_error(
    tmp_path, fake_models
):
    source = tmp_path / "inputs"
    source.mkdir()

    Image.new("RGB", (100, 100)).save(
        source / "valida.jpg"
    )

    (source / "corrupta.jpg").write_text(
        "No es una imagen",
        encoding="utf-8",
    )

    rows, _ = benchmark.evaluate_folder(
        source,
        tmp_path / "results",
        ["isnet-general-use"],
    )

    assert len(rows) == 2
    assert sum(row.status == "ok" for row in rows) == 1
    assert sum(row.status == "error" for row in rows) == 1


def test_benchmark_rejects_nonempty_output(
    tmp_path, fake_models
):
    source = tmp_path / "inputs"
    source.mkdir()

    Image.new("RGB", (100, 100)).save(
        source / "producto.png"
    )

    output = tmp_path / "results"
    output.mkdir()

    (output / "existente.txt").write_text("No borrar")

    with pytest.raises(ValueError):
        benchmark.evaluate_folder(
            source,
            output,
            ["isnet-general-use"],
        )

    assert (output / "existente.txt").exists()


def test_benchmark_cli_invalid_folder(
    tmp_path, capsys
):
    code = benchmark_cli.main([
        "--input", str(tmp_path / "no_existe"),
        "--output", str(tmp_path / "results"),
    ])

    assert code == 2
    assert "[ERROR]" in capsys.readouterr().err
