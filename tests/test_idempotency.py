
import json
import os
from pathlib import Path

import pytest
from PIL import Image

from image_processor import batch, batch_cli, pipeline
from image_processor import manifest as manifest_module
from image_processor.errors import EmptySegmentationError
from image_processor.manifest import (
    COMPLETED,
    MANIFEST_NAME,
    PENDING,
    Manifest,
    ResultIdentity,
    file_sha256,
)
from image_processor.naming import with_revision
from image_processor.processing import OutputOptions


ISNET = "isnet-general-use"
BIREFNET = "birefnet-general-lite"

RED = (220, 30, 30)
GREEN = (30, 200, 30)
BLUE = (30, 30, 220)


@pytest.fixture(autouse=True)
def fake_ai(monkeypatch):
    """Modelo simulado: el resultado depende del original y del modelo."""

    loads = []

    def fake_process(image, model_name, options=None):
        options = options or OutputOptions()
        color = image.convert("RGB").getpixel((30, 30))

        if model_name == BIREFNET:
            color = tuple(255 - value for value in color)

        if options.background == "transparent":
            return Image.new(
                "RGBA", (options.width, options.height), color + (255,)
            )

        return Image.new("RGB", (options.width, options.height), color)

    monkeypatch.setattr(batch, "process_image", fake_process)
    monkeypatch.setattr(batch, "prepare_model", loads.append)

    return loads


def make(directory, colors):
    originals = {}

    for name, color in colors.items():
        path = directory / name
        Image.new("RGB", (60, 60), color).save(path)
        originals[path] = path.read_bytes()

    return originals


def read_manifest(directory):
    return json.loads((directory / MANIFEST_NAME).read_text("utf-8"))


def entries_by_name(directory):
    return {
        item["output_name"]: item
        for item in read_manifest(directory)["entries"]
    }


def snapshot(directory):
    return {
        path.name: path.read_bytes()
        for path in directory.iterdir()
        if path.name.startswith("TRANSFORMADA_")
    }


def center(path):
    with Image.open(path) as image:
        image.load()
        return image.convert("RGB").getpixel(
            (image.width // 2, image.height // 2)
        )


def close(pixel, color):
    return all(abs(a - b) <= 12 for a, b in zip(pixel, color))


def revision_name(base, source, model=ISNET):
    identity = ResultIdentity(
        source_sha256=file_sha256(source),
        model=model,
        width=400,
        height=400,
        background="white",
        pipeline_version=pipeline.PIPELINE_VERSION,
    )
    return with_revision(base, identity.revision())


# 1. Creación del manifiesto -----------------------------------------

def test_manifest_records_provenance(tmp_path):
    inputs = tmp_path / "marca"
    outputs = tmp_path / "resultados"
    inputs.mkdir()
    make(inputs, {"producto.jpg": RED})

    result = batch.process_folder(inputs, outputs)

    assert result.processed == 1

    document = read_manifest(outputs)
    assert document["schema_version"] == 1

    [entry] = document["entries"]
    output = outputs / "TRANSFORMADA_producto.png"

    assert entry["output_name"] == output.name
    assert entry["status"] == COMPLETED
    assert entry["output_sha256"] == file_sha256(output)
    assert entry["source"] == {
        "kind": "local",
        "id": "../marca/producto.jpg",
        "name": "producto.jpg",
        "size": (inputs / "producto.jpg").stat().st_size,
        "sha256": file_sha256(inputs / "producto.jpg"),
    }
    assert entry["model"] == ISNET
    assert entry["options"] == {
        "width": 400, "height": 400, "background": "white",
    }
    assert entry["pipeline_version"] == pipeline.PIPELINE_VERSION


# 3, 16, 17, 18. Segunda ejecución idéntica ---------------------------

def test_identical_run_is_verified_without_ai_or_rewrites(tmp_path, fake_ai):
    originals = make(tmp_path, {"a.jpg": RED, "b.png": GREEN})

    batch.process_folder(tmp_path)

    before = snapshot(tmp_path)
    manifest_bytes = (tmp_path / MANIFEST_NAME).read_bytes()
    mtimes = {
        name: (tmp_path / name).stat().st_mtime_ns for name in before
    }
    fake_ai.clear()

    result = batch.process_folder(tmp_path)

    assert (result.processed, result.skipped, result.failed) == (0, 2, 0)
    assert result.unverified == 0
    assert fake_ai == []
    assert snapshot(tmp_path) == before
    assert {
        name: (tmp_path / name).stat().st_mtime_ns for name in before
    } == mtimes
    assert (tmp_path / MANIFEST_NAME).read_bytes() == manifest_bytes

    for path, content in originals.items():
        assert path.read_bytes() == content


# 4. Original modificado ----------------------------------------------

def test_modified_original_gets_new_revision(tmp_path):
    make(tmp_path, {"producto.jpg": RED})
    batch.process_folder(tmp_path)

    first = tmp_path / "TRANSFORMADA_producto.png"
    first_bytes = first.read_bytes()

    # Mismo nombre, contenido distinto.
    make(tmp_path, {"producto.jpg": GREEN})
    revision = tmp_path / revision_name(first.name, tmp_path / "producto.jpg")

    result = batch.process_folder(tmp_path)

    assert (result.processed, result.skipped, result.failed) == (1, 0, 0)
    assert any("cambió" in warning for warning in result.warnings)
    assert first.read_bytes() == first_bytes
    assert close(center(first), RED)
    assert close(center(revision), GREEN)

    entries = entries_by_name(tmp_path)
    assert set(entries) == {first.name, revision.name}
    assert entries[revision.name]["source"]["sha256"] == file_sha256(
        tmp_path / "producto.jpg"
    )

    # El contenido nuevo ya está verificado.
    again = batch.process_folder(tmp_path)
    assert (again.processed, again.skipped) == (0, 1)

    # Si vuelve al contenido original, se reconoce el primero.
    make(tmp_path, {"producto.jpg": RED})
    back = batch.process_folder(tmp_path)

    assert (back.processed, back.skipped) == (0, 1)
    assert first.read_bytes() == first_bytes


# 5. Cambio de modelo -------------------------------------------------

def test_model_change_produces_independent_result(tmp_path):
    make(tmp_path, {"producto.jpg": RED})

    batch.process_folder(tmp_path, model_name=ISNET)

    isnet = tmp_path / "TRANSFORMADA_producto.png"
    isnet_bytes = isnet.read_bytes()

    result = batch.process_folder(tmp_path, model_name=BIREFNET)

    birefnet = tmp_path / revision_name(
        isnet.name, tmp_path / "producto.jpg", BIREFNET
    )

    assert result.processed == 1
    assert isnet.read_bytes() == isnet_bytes
    assert close(center(birefnet), tuple(255 - v for v in RED))

    entries = entries_by_name(tmp_path)
    assert entries[isnet.name]["model"] == ISNET
    assert entries[birefnet.name]["model"] == BIREFNET

    for model in (ISNET, BIREFNET, ISNET):
        again = batch.process_folder(tmp_path, model_name=model)
        assert (again.processed, again.skipped) == (0, 1)


# 6, 7. Cambio de fondo y de dimensiones ------------------------------

def test_background_and_dimensions_are_separate_results(tmp_path):
    make(tmp_path, {"producto.jpg": RED})

    variants = [
        OutputOptions(),
        OutputOptions(background="transparent"),
        OutputOptions(800, 600, "white"),
        OutputOptions(800, 600, "transparent"),
    ]

    for options in variants:
        result = batch.process_folder(tmp_path, options=options)
        assert result.processed == 1

    entries = entries_by_name(tmp_path)

    assert sorted(
        (item["options"]["width"], item["options"]["height"],
         item["options"]["background"])
        for item in entries.values()
    ) == [
        (400, 400, "transparent"),
        (400, 400, "white"),
        (800, 600, "transparent"),
        (800, 600, "white"),
    ]
    assert not any("__rev-" in name for name in entries)

    before = snapshot(tmp_path)

    for options in variants:
        again = batch.process_folder(tmp_path, options=options)
        assert (again.processed, again.skipped) == (0, 1)

    assert snapshot(tmp_path) == before


# 8. Resultado eliminado ----------------------------------------------

def test_deleted_result_is_regenerated(tmp_path):
    make(tmp_path, {"producto.jpg": RED})
    batch.process_folder(tmp_path)

    output = tmp_path / "TRANSFORMADA_producto.png"
    output.unlink()

    result = batch.process_folder(tmp_path)

    assert (result.processed, result.failed) == (1, 0)
    assert any("no existe" in warning for warning in result.warnings)
    assert entries_by_name(tmp_path)[output.name]["output_sha256"] == (
        file_sha256(output)
    )


# 9. Resultado modificado externamente --------------------------------

def test_externally_modified_result_is_not_overwritten(tmp_path, capsys):
    make(tmp_path, {"producto.jpg": RED})
    batch.process_folder(tmp_path)

    output = tmp_path / "TRANSFORMADA_producto.png"
    Image.new("RGB", (400, 400), "black").save(output)
    altered = output.read_bytes()

    exit_code = batch_cli.main(["--input", str(tmp_path)])

    assert exit_code == 1
    assert "modificado fuera del procesador" in capsys.readouterr().err
    assert output.read_bytes() == altered
    assert snapshot(tmp_path) == {output.name: altered}


# 10, 11. Manifiesto dañado o incompatible ----------------------------

@pytest.mark.parametrize(
    "content",
    [
        "{esto no es JSON",
        json.dumps({"schema_version": 99, "generator": "x", "entries": []}),
    ],
    ids=["corrupto", "version"],
)
def test_invalid_manifest_stops_batch(tmp_path, fake_ai, capsys, content):
    originals = make(tmp_path, {"producto.jpg": RED})
    (tmp_path / MANIFEST_NAME).write_text(content, encoding="utf-8")

    with pytest.raises(manifest_module.ManifestError):
        batch.process_folder(tmp_path)

    exit_code = batch_cli.main(["--input", str(tmp_path)])

    assert exit_code == 5
    assert "manifiesto" in capsys.readouterr().err
    assert fake_ai == []
    assert (tmp_path / MANIFEST_NAME).read_text(encoding="utf-8") == content
    assert snapshot(tmp_path) == {}

    for path, data in originals.items():
        assert path.read_bytes() == data


# 12. Interrupción durante la escritura del manifiesto ----------------

def test_failed_manifest_write_does_not_publish(tmp_path, monkeypatch):
    make(tmp_path, {"a.jpg": RED, "b.jpg": GREEN})
    batch.process_folder(tmp_path)

    make(tmp_path, {"c.jpg": BLUE})
    manifest_bytes = (tmp_path / MANIFEST_NAME).read_bytes()

    def failing_replace(source, target):
        raise OSError("disco lleno")

    monkeypatch.setattr(manifest_module.os, "replace", failing_replace)

    result = batch.process_folder(tmp_path)

    monkeypatch.undo()

    # La imagen nueva no se publica sin su registro previo.
    assert (result.processed, result.skipped, result.failed) == (0, 2, 1)
    assert not (tmp_path / "TRANSFORMADA_c.png").exists()
    assert (tmp_path / MANIFEST_NAME).read_bytes() == manifest_bytes
    assert list(tmp_path.glob("*.tmp")) == []

    retry = batch.process_folder(tmp_path)
    assert (retry.processed, retry.skipped) == (1, 2)


# 13, 20. PNG publicado sin registro completo -------------------------

def interrupt_on_save(monkeypatch, call_number):
    real_save = Manifest.save
    calls = []

    def save(self):
        calls.append(self)
        if len(calls) == call_number:
            raise KeyboardInterrupt
        real_save(self)

    monkeypatch.setattr(Manifest, "save", save)


def test_published_png_without_completed_entry_is_recovered(
    tmp_path, monkeypatch, fake_ai
):
    make(tmp_path, {"producto.jpg": RED})

    # Primera fase guardada; la interrupción llega al completar.
    interrupt_on_save(monkeypatch, 2)

    with pytest.raises(KeyboardInterrupt):
        batch.process_folder(tmp_path)

    monkeypatch.undo()
    monkeypatch.setattr(batch, "prepare_model", fake_ai.append)
    fake_ai.clear()

    output = tmp_path / "TRANSFORMADA_producto.png"
    published = output.read_bytes()

    assert entries_by_name(tmp_path)[output.name]["status"] == PENDING

    result = batch.process_folder(tmp_path)

    assert (result.processed, result.skipped, result.recovered) == (0, 0, 1)
    assert fake_ai == []
    assert output.read_bytes() == published

    entry = entries_by_name(tmp_path)[output.name]
    assert entry["status"] == COMPLETED
    assert entry["output_sha256"] == file_sha256(output)

    again = batch.process_folder(tmp_path)
    assert (again.skipped, again.recovered) == (1, 0)


def test_interruption_before_publishing_is_retried(tmp_path, monkeypatch):
    make(tmp_path, {"producto.jpg": RED})

    def interrupted(image, model_name, options=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(batch, "process_image", interrupted)

    with pytest.raises(KeyboardInterrupt):
        batch.process_folder(tmp_path)

    monkeypatch.undo()

    assert not (tmp_path / "TRANSFORMADA_producto.png").exists()
    assert entries_by_name(tmp_path)["TRANSFORMADA_producto.png"]["status"] == (
        PENDING
    )


def test_pending_entry_with_foreign_file_is_not_adopted(tmp_path, monkeypatch):
    make(tmp_path, {"producto.jpg": RED})
    interrupt_on_save(monkeypatch, 2)

    with pytest.raises(KeyboardInterrupt):
        batch.process_folder(tmp_path)

    monkeypatch.undo()

    # El archivo pendiente no tiene el formato de la variante.
    output = tmp_path / "TRANSFORMADA_producto.png"
    output.write_bytes(b"no es un PNG")

    result = batch.process_folder(tmp_path)

    assert (result.recovered, result.failed) == (0, 1)
    assert output.read_bytes() == b"no es un PNG"


# 14. Resultado histórico sin procedencia verificable -----------------

def test_historic_result_is_unverified(tmp_path, fake_ai, capsys):
    make(tmp_path, {"producto.jpg": RED})

    historic = tmp_path / "TRANSFORMADA_producto.png"
    Image.new("RGB", (400, 400), RED).save(historic)
    historic_bytes = historic.read_bytes()

    exit_code = batch_cli.main(["--input", str(tmp_path)])

    captured = capsys.readouterr()

    assert exit_code == 4
    assert "Sin verificar: 1" in captured.out
    assert "--regenerate-unverified" in captured.err
    assert fake_ai == []
    assert historic.read_bytes() == historic_bytes
    assert not (tmp_path / MANIFEST_NAME).exists()


def test_regenerate_unverified_keeps_historic_file(tmp_path):
    source = make(tmp_path, {"producto.jpg": RED})

    historic = tmp_path / "TRANSFORMADA_producto.png"
    historic.write_bytes(b"HISTORICO")

    result = batch.process_folder(tmp_path, regenerate_unverified=True)

    revision = tmp_path / revision_name(historic.name, tmp_path / "producto.jpg")

    assert (result.processed, result.unverified) == (1, 0)
    assert historic.read_bytes() == b"HISTORICO"
    assert close(center(revision), RED)
    assert historic.name not in entries_by_name(tmp_path)

    # Ya verificada, no hace falta la opción.
    again = batch.process_folder(tmp_path)
    assert (again.processed, again.skipped, again.unverified) == (0, 1, 0)

    for path, data in source.items():
        assert path.read_bytes() == data


# 15. Colisiones y separadores especiales de H-04 ---------------------

def test_h04_names_are_recorded_and_verified(tmp_path):
    colors = {
        "producto.jpg": RED,
        "producto.png": GREEN,
        "producto__src-jpg.png": BLUE,
    }
    make(tmp_path, colors)

    result = batch.process_folder(tmp_path)

    assert result.processed == 3

    expected = {
        "TRANSFORMADA_producto__src-jpg.png": "producto.jpg",
        "TRANSFORMADA_producto__src-png.png": "producto.png",
        "TRANSFORMADA_producto%5F%5Fsrc-jpg__esc.png": "producto__src-jpg.png",
    }

    entries = entries_by_name(tmp_path)
    assert {name: item["source"]["name"] for name, item in entries.items()} == (
        expected
    )

    again = batch.process_folder(tmp_path)
    assert (again.processed, again.skipped, again.failed) == (0, 3, 0)

    # Solo el original modificado obtiene una revisión.
    make(tmp_path, {"producto__src-jpg.png": RED})
    changed = batch.process_folder(tmp_path)

    assert (changed.processed, changed.skipped) == (1, 2)
    assert sum("__rev-" in name for name in entries_by_name(tmp_path)) == 1


def test_legacy_src_result_of_other_original_is_never_adopted(tmp_path):
    """
    Regresión de la auditoría: TRANSFORMADA_producto__src-jpg.png lo
    generó una versión anterior para producto__src-jpg.png, que ahora
    recibe un nombre codificado. producto.jpg no puede adoptarlo.
    """
    make(
        tmp_path,
        {
            "producto.jpg": RED,
            "producto.png": GREEN,
            "producto__src-jpg.png": BLUE,
        },
    )

    legacy = tmp_path / "TRANSFORMADA_producto__src-jpg.png"
    Image.new("RGB", (400, 400), BLUE).save(legacy)
    legacy_bytes = legacy.read_bytes()

    first = batch.process_folder(tmp_path)

    assert first.processed == 2
    assert first.failed == 1

    # Ya existe el resultado codificado de producto__src-jpg.png;
    # sin manifiesto, el legado sigue sin poder atribuirse.
    second = batch.process_folder(tmp_path)

    assert (second.processed, second.skipped, second.failed) == (0, 2, 0)
    assert second.unverified == 1
    assert legacy.read_bytes() == legacy_bytes
    assert legacy.name not in entries_by_name(tmp_path)

    third = batch.process_folder(tmp_path, regenerate_unverified=True)

    assert third.processed == 1
    revision = tmp_path / revision_name(legacy.name, tmp_path / "producto.jpg")

    assert close(center(revision), RED)
    assert close(center(legacy), BLUE)
    assert entries_by_name(tmp_path)[revision.name]["source"]["name"] == (
        "producto.jpg"
    )


# 19. Continuación después de errores individuales --------------------

def test_failed_image_has_no_entry_and_is_retried(tmp_path, monkeypatch):
    originals = make(tmp_path, {"a.jpg": RED, "b.jpg": (5, 5, 5)})
    real_process = batch.process_image

    def failing_dark(image, model_name, options=None):
        if sum(image.convert("RGB").getpixel((30, 30))) < 60:
            raise EmptySegmentationError("segmentación vacía")
        return real_process(image, model_name, options)

    monkeypatch.setattr(batch, "process_image", failing_dark)

    result = batch.process_folder(tmp_path)

    assert (result.processed, result.failed) == (1, 1)
    assert set(entries_by_name(tmp_path)) == {"TRANSFORMADA_a.png"}
    assert not (tmp_path / "TRANSFORMADA_b.png").exists()

    monkeypatch.setattr(batch, "process_image", real_process)

    retry = batch.process_folder(tmp_path)

    assert (retry.processed, retry.skipped, retry.failed) == (1, 1, 0)

    for path, data in originals.items():
        assert path.read_bytes() == data


def test_failed_regeneration_keeps_previous_entry(tmp_path, monkeypatch):
    make(tmp_path, {"producto.jpg": RED})
    batch.process_folder(tmp_path)

    output = tmp_path / "TRANSFORMADA_producto.png"
    previous = entries_by_name(tmp_path)[output.name]
    output.unlink()

    def failing(image, model_name, options=None):
        raise RuntimeError("fallo simulado")

    monkeypatch.setattr(batch, "process_image", failing)

    result = batch.process_folder(tmp_path)

    assert result.failed == 1
    assert entries_by_name(tmp_path)[output.name] == previous
