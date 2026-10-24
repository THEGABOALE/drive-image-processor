
import copy
import json
import os
import time
from pathlib import Path

import pytest

from image_processor import manifest as manifest_module
from image_processor.manifest import (
    COMPLETED,
    MANIFEST_NAME,
    PENDING,
    Manifest,
    ManifestEntry,
    ManifestError,
    ResultIdentity,
    SourceRecord,
    file_sha256,
    local_source_id,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


def identity(**changes):
    values = dict(
        source_kind="local",
        source_key="producto.jpg",
        source_sha256=SHA_A,
        model="isnet-general-use",
        width=400,
        height=400,
        background="white",
        pipeline_version="1",
    )
    values.update(changes)
    return ResultIdentity(**values)


def entry(name="TRANSFORMADA_producto.png", status=COMPLETED, **changes):
    return ManifestEntry(
        output_name=name,
        status=status,
        identity=identity(**changes),
        source=SourceRecord(
            kind="local",
            id="producto.jpg",
            name="producto.jpg",
            size=123,
            sha256=changes.get("source_sha256", SHA_A),
        ),
        output_sha256=SHA_B if status == COMPLETED else None,
        recorded_at="2026-10-23T12:00:00+00:00",
    )


def valid_document():
    manifest = Manifest(Path("."), [entry()])
    return json.loads(manifest.to_json())


def write_document(directory, document):
    (directory / MANIFEST_NAME).write_text(
        json.dumps(document), encoding="utf-8"
    )


def temp_files(directory):
    return list(directory.glob("*.tmp"))


def test_missing_manifest_is_empty(tmp_path):
    manifest = Manifest.load(tmp_path)

    assert manifest.entries() == []
    assert not (tmp_path / MANIFEST_NAME).exists()


def test_save_and_load_round_trip(tmp_path):
    manifest = Manifest(tmp_path)
    manifest.put(entry("TRANSFORMADA_zeta.png"))
    manifest.put(entry("TRANSFORMADA_alfa.png", status=PENDING))
    manifest.save()

    document = json.loads((tmp_path / MANIFEST_NAME).read_text("utf-8"))

    assert document["schema_version"] == 1
    assert document["generator"] == "drive-image-processor"
    assert [item["output_name"] for item in document["entries"]] == [
        "TRANSFORMADA_alfa.png",
        "TRANSFORMADA_zeta.png",
    ]

    loaded = Manifest.load(tmp_path)

    assert loaded.entries() == manifest.entries()
    assert loaded.get("transformada_ZETA.png") == entry("TRANSFORMADA_zeta.png")
    assert temp_files(tmp_path) == []


def test_document_has_no_absolute_paths(tmp_path):
    manifest = Manifest(tmp_path, [entry()])
    manifest.save()

    text = (tmp_path / MANIFEST_NAME).read_text("utf-8")

    assert str(tmp_path) not in text
    assert ":\\" not in text


@pytest.mark.parametrize(
    "content",
    [
        "esto no es JSON",
        "",
        "[]",
        "null",
    ],
)
def test_unreadable_manifest_is_rejected(tmp_path, content):
    path = tmp_path / MANIFEST_NAME
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ManifestError):
        Manifest.load(tmp_path)

    # Nunca se reinicia ni se modifica.
    assert path.read_text(encoding="utf-8") == content


def test_invalid_utf8_manifest_is_rejected(tmp_path):
    (tmp_path / MANIFEST_NAME).write_bytes(b"\xff\xfe\x00{")

    with pytest.raises(ManifestError):
        Manifest.load(tmp_path)


@pytest.mark.parametrize("version", [0, 2, "1", None, 1.0])
def test_unsupported_schema_version_is_rejected(tmp_path, version):
    document = valid_document()
    document["schema_version"] = version
    write_document(tmp_path, document)

    with pytest.raises(ManifestError, match="Versión de manifiesto"):
        Manifest.load(tmp_path)


def test_missing_schema_version_is_rejected(tmp_path):
    document = valid_document()
    del document["schema_version"]
    write_document(tmp_path, document)

    with pytest.raises(ManifestError, match="Versión de manifiesto"):
        Manifest.load(tmp_path)


def corrupt(change):
    document = valid_document()
    change(document)
    return document


def set_entry(field, value):
    def change(document):
        document["entries"][0][field] = value

    return change


def set_nested(group, field, value):
    def change(document):
        document["entries"][0][group][field] = value

    return change


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(extra=1),
        lambda d: d.update(generator="otro"),
        lambda d: d.update(entries={}),
        lambda d: d["entries"].append(copy.deepcopy(d["entries"][0])),
        lambda d: d["entries"].append(
            dict(
                copy.deepcopy(d["entries"][0]),
                output_name="transformada_PRODUCTO.png",
            )
        ),
        lambda d: d["entries"][0].update(extra=1),
        lambda d: d["entries"][0].pop("model"),
        set_entry("output_name", "../TRANSFORMADA_x.png"),
        set_entry("output_name", "sub/TRANSFORMADA_x.png"),
        set_entry("output_name", "sub\\TRANSFORMADA_x.png"),
        set_entry("output_name", "producto.png"),
        set_entry("output_name", "TRANSFORMADA_x.jpg"),
        set_entry("status", "done"),
        set_entry("output_sha256", None),
        set_entry("output_sha256", "A" * 64),
        set_entry("output_sha256", "a" * 63),
        set_entry("model", ""),
        set_entry("pipeline_version", 1),
        set_nested("source", "sha256", "x"),
        set_nested("source", "size", -1),
        set_nested("source", "size", True),
        set_nested("options", "width", True),
        set_nested("options", "height", 0),
        set_nested("options", "background", "azul"),
    ],
)
def test_invalid_manifest_content_is_rejected(tmp_path, change):
    write_document(tmp_path, corrupt(change))

    with pytest.raises(ManifestError, match="Manifiesto inválido"):
        Manifest.load(tmp_path)


def test_pending_entry_keeps_expected_output_hash(tmp_path):
    # Evidencia registrada antes de publicar el PNG.
    document = valid_document()
    document["entries"][0]["status"] = PENDING
    write_document(tmp_path, document)

    [loaded] = Manifest.load(tmp_path).entries()

    assert loaded.status == PENDING
    assert loaded.output_sha256 == SHA_B


def test_pending_entry_without_hash_is_readable(tmp_path):
    # Versión anterior: se lee, pero no permite recuperar.
    document = valid_document()
    document["entries"][0]["status"] = PENDING
    document["entries"][0]["output_sha256"] = None
    write_document(tmp_path, document)

    [loaded] = Manifest.load(tmp_path).entries()

    assert loaded.output_sha256 is None


def test_pending_entry_with_invalid_hash_is_rejected(tmp_path):
    document = valid_document()
    document["entries"][0]["status"] = PENDING
    document["entries"][0]["output_sha256"] = "x"
    write_document(tmp_path, document)

    with pytest.raises(ManifestError):
        Manifest.load(tmp_path)


@pytest.mark.parametrize(
    "target",
    ["fsync", "replace"],
)
def test_failed_save_keeps_previous_manifest(tmp_path, monkeypatch, target):
    previous = Manifest(tmp_path, [entry()])
    previous.save()
    previous_bytes = (tmp_path / MANIFEST_NAME).read_bytes()

    def failing(*args, **kwargs):
        raise OSError("disco lleno")

    monkeypatch.setattr(manifest_module.os, target, failing)

    updated = Manifest(tmp_path, [entry(), entry("TRANSFORMADA_otro.png")])

    with pytest.raises(ManifestError, match="No se pudo guardar"):
        updated.save()

    monkeypatch.undo()

    assert (tmp_path / MANIFEST_NAME).read_bytes() == previous_bytes
    assert temp_files(tmp_path) == []


def test_interrupted_save_keeps_previous_manifest(tmp_path, monkeypatch):
    previous = Manifest(tmp_path, [entry()])
    previous.save()
    previous_bytes = (tmp_path / MANIFEST_NAME).read_bytes()

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(manifest_module.os, "replace", interrupted)

    with pytest.raises(KeyboardInterrupt):
        Manifest(tmp_path, [entry("TRANSFORMADA_otro.png")]).save()

    monkeypatch.undo()

    assert (tmp_path / MANIFEST_NAME).read_bytes() == previous_bytes
    assert temp_files(tmp_path) == []
    assert Manifest.load(tmp_path).entries() == previous.entries()


def test_serialization_failure_keeps_previous_manifest(tmp_path, monkeypatch):
    previous = Manifest(tmp_path, [entry()])
    previous.save()
    previous_bytes = (tmp_path / MANIFEST_NAME).read_bytes()

    def failing_dumps(*args, **kwargs):
        raise TypeError("no serializable")

    monkeypatch.setattr(manifest_module.json, "dumps", failing_dumps)

    with pytest.raises(TypeError):
        Manifest(tmp_path, [entry("TRANSFORMADA_otro.png")]).save()

    assert (tmp_path / MANIFEST_NAME).read_bytes() == previous_bytes
    assert temp_files(tmp_path) == []


def test_replace_retries_permission_error(tmp_path, monkeypatch):
    real_replace = os.replace
    attempts = []
    sleeps = []

    def flaky_replace(source, target):
        attempts.append(target)
        if len(attempts) <= 2:
            raise PermissionError("retenido por otro programa")
        real_replace(source, target)

    monkeypatch.setattr(manifest_module.os, "replace", flaky_replace)
    monkeypatch.setattr(manifest_module, "sleep", sleeps.append)

    Manifest(tmp_path, [entry()]).save()

    assert len(attempts) == 3
    assert sleeps == [0.1, 0.2]
    assert Manifest.load(tmp_path).entries() == [entry()]


def test_stale_manifest_temps_are_removed(tmp_path):
    old = tmp_path / f"{MANIFEST_NAME}.abc12_yz.tmp"
    recent = tmp_path / f"{MANIFEST_NAME}.zyx21_ab.tmp"
    unrelated = tmp_path / "notas.tmp"

    for path in (old, recent, unrelated):
        path.write_text("{")

    stale = time.time() - 25 * 60 * 60
    os.utime(old, (stale, stale))
    os.utime(unrelated, (stale, stale))

    Manifest.load(tmp_path)

    assert not old.exists()
    assert recent.exists()
    assert unrelated.exists()


def test_revision_depends_on_content_model_and_pipeline():
    base = identity()

    assert base.revision() == identity().revision()
    assert len(base.revision()) == 12

    assert base.revision() != identity(source_sha256=SHA_B).revision()
    assert base.revision() != identity(model="birefnet-general-lite").revision()
    assert base.revision() != identity(pipeline_version="2").revision()

    # H05-R2: otro original con los mismos bytes es otra identidad.
    other = identity(source_key="../marca_b/producto.jpg")
    assert other != base
    assert base.revision() != other.revision()
    assert base.revision() != identity(source_kind="drive").revision()

    # Las opciones ya distinguen el nombre base.
    assert base.revision() == identity(width=800).revision()


def test_identity_uses_normalized_source_id(tmp_path):
    entry_a = entry()
    document = json.loads(Manifest(tmp_path, [entry_a]).to_json())
    document["entries"][0]["source"]["id"] = "PRODUCTO.JPG"
    write_document(tmp_path, document)

    [loaded] = Manifest.load(tmp_path).entries()

    # Mismo original en Windows: la identidad no depende de mayúsculas.
    assert loaded.identity == entry_a.identity
    assert loaded.source.id == "PRODUCTO.JPG"


def test_local_source_id_is_relative(tmp_path):
    inputs = tmp_path / "marca" / "fotos"
    outputs = tmp_path / "marca" / "resultados"

    assert local_source_id(inputs / "producto.jpg", inputs) == "producto.jpg"
    assert (
        local_source_id(inputs / "producto.jpg", outputs)
        == "../fotos/producto.jpg"
    )


def test_file_sha256(tmp_path):
    path = tmp_path / "datos.bin"
    path.write_bytes(b"abc")

    assert file_sha256(path) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
