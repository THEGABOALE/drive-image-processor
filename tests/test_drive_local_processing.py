"""
GD-03A: procesamiento local de las imágenes descargadas por GD-02.

Las descargas se generan con el GD-02 real sobre FakeDrive, así que
los registros de .drive/ son los que produce la aplicación. La IA se
simula en pipeline.remove_background: el centrado y el lienzo son
los reales de normalize_image.
"""

import hashlib
import io
import json
import os
import re
from functools import partial
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from drive_fakes import FakeDrive, folder
from image_processor import background_removal, batch, drive_cli, manifest, pipeline
from image_processor.background_removal import ModelInitializationError
from image_processor.google_drive import auth, client, downloads
from image_processor.google_drive import local_processing as lp
from image_processor.manifest import MANIFEST_NAME, Manifest, ManifestError
from image_processor.naming import output_name
from image_processor.processing import OutputOptions


PACKAGE = Path(__file__).resolve().parent.parent / "image_processor"

FOLDER = "1CarpetaPrueba"
OTHER_FOLDER = "1OtraCarpeta"

RED = (220, 30, 30)


def product(data_format="JPEG", size=(300, 200), color=RED):
    """Producto oscuro sobre fondo blanco."""

    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    width, height = size
    draw.rectangle(
        (width // 3, height // 4, 2 * width // 3, 3 * height // 4), fill=color
    )

    buffer = io.BytesIO()
    image.save(buffer, format=data_format)
    return buffer.getvalue()


MIMES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


def fake_cutout(image, model_name):
    """Segmentación simulada: opaco donde el píxel no es claro."""

    rgba = image.convert("RGBA")
    rgba.putalpha(image.convert("L").point(lambda value: 255 if value < 200 else 0))
    return rgba


@pytest.fixture(autouse=True)
def fake_ai(monkeypatch):
    calls = {"prepare": [], "cutout": []}

    def prepare(model_name):
        calls["prepare"].append(model_name)

    def cutout(image, model_name):
        calls["cutout"].append(model_name)
        return fake_cutout(image, model_name)

    monkeypatch.setattr(batch, "prepare_model", prepare)
    monkeypatch.setattr(pipeline, "remove_background", cutout)
    return calls


class Remote:
    """Carpetas de Drive simuladas y descargas reales de GD-02."""

    def __init__(self):
        self.drive = FakeDrive(
            folders=[folder(FOLDER, "Pruebas"), folder(OTHER_FOLDER, "Otra")]
        )
        self.drive.children = {FOLDER: [], OTHER_FOLDER: []}

    def add(self, file_id, name, data=None, folder_id=FOLDER, version="1"):
        mime = MIMES[Path(name).suffix.lower()]
        data = data if data is not None else product(FORMATS[mime])
        item = {
            "id": file_id,
            "name": name,
            "mimeType": mime,
            "trashed": False,
            "capabilities": {"canDownload": True},
        }
        self.drive.items[file_id] = item
        self.drive.children[folder_id].append(item)
        self.change(file_id, data, version)
        return item

    def change(self, file_id, data, version):
        self.drive.items[file_id].update(
            size=str(len(data)),
            md5Checksum=hashlib.md5(data).hexdigest(),
            sha256Checksum=hashlib.sha256(data).hexdigest(),
            version=version,
            headRevisionId=f"rev-{version}",
            modifiedTime=f"2026-10-2{version}T10:00:00.000Z",
        )
        self.drive.media[file_id] = data

    def download(self, output, folder_id=FOLDER):
        summary = downloads.download_folder(
            self.drive,
            folder_id,
            output,
            sleep=lambda seconds: None,
            execute=partial(client.execute, sleep=lambda seconds: None),
        )
        assert summary.count(downloads.FAILED) == 0
        return summary


@pytest.fixture
def remote():
    return Remote()


@pytest.fixture
def dirs(tmp_path):
    class Dirs:
        input = tmp_path / "downloads" / "prueba"
        output = tmp_path / "outputs" / "prueba"

    return Dirs


def run(dirs, folder_id=FOLDER, **kwargs):
    return lp.process_downloads(dirs.input, dirs.output, folder_id, **kwargs)


def cli(dirs, *extra, folder_id=FOLDER):
    return drive_cli.main([
        "process-local",
        "--folder-id", folder_id,
        "--input", str(dirs.input),
        "--output", str(dirs.output),
        *extra,
    ])


def image_files(directory):
    return sorted(
        path.name for path in directory.iterdir()
        if path.is_file() and not path.name.startswith(".")
    )


def pngs(directory):
    if not directory.exists():
        return []
    return [name for name in image_files(directory) if name.startswith("TRANSFORMADA_")]


def manifest_document(directory):
    return json.loads((directory / MANIFEST_NAME).read_text("utf-8"))


def entries_by_file_id(directory):
    return {
        entry["source"]["id"]: entry
        for entry in manifest_document(directory)["entries"]
        if entry["source"]["kind"] == "drive"
    }


def local_name(file_id, remote_name):
    return downloads.local_name(remote_name, file_id)


def expected_output(file_id, remote_name, options=OutputOptions()):
    return output_name(Path(local_name(file_id, remote_name)).stem, options)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(directory):
    """Contenido y fecha de cada archivo, registros incluidos."""

    return {
        path.relative_to(directory).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def record_path(dirs, file_id, remote_name):
    return dirs.input / downloads.METADATA_DIR / f"{local_name(file_id, remote_name)}.json"


def edit_record(dirs, of_file, remote_name, **changes):
    path = record_path(dirs, of_file, remote_name)
    record = json.loads(path.read_text("utf-8"))
    record.update(changes)
    path.write_text(json.dumps(record), encoding="utf-8")


# 1. JPG normal -------------------------------------------------------

def test_jpg_download_is_processed_with_its_drive_identity(remote, dirs):
    remote.add("1AbC-_9z", "producto.jpg")
    remote.download(dirs.input)

    result = run(dirs)

    name = expected_output("1AbC-_9z", "producto.jpg")
    assert result.batch.processed == 1
    assert result.complete
    assert pngs(dirs.output) == [name]

    entry = entries_by_file_id(dirs.output)["1AbC-_9z"]
    record = json.loads(record_path(dirs, "1AbC-_9z", "producto.jpg").read_text("utf-8"))

    assert entry["status"] == manifest.COMPLETED
    assert entry["output_name"] == name
    assert entry["output_sha256"] == sha256(dirs.output / name)
    assert entry["model"] == "isnet-general-use"
    assert entry["options"] == {"width": 400, "height": 400, "background": "white"}
    assert entry["pipeline_version"] == pipeline.PIPELINE_VERSION
    assert entry["source"] == {
        "kind": "drive",
        "id": "1AbC-_9z",
        "name": "producto.jpg",
        "size": record["size"],
        "sha256": record["sha256"],
        "drive": {
            "folder_id": FOLDER,
            "md5_checksum": record["md5_checksum"],
            "head_revision_id": "rev-1",
            "version": "1",
            "modified_time": record["modified_time"],
            "local_name": local_name("1AbC-_9z", "producto.jpg"),
        },
    }


# 2. PNG y WebP -------------------------------------------------------

@pytest.mark.parametrize("name", ["logo.png", "banner.webp", "foto.JPEG"])
def test_png_webp_and_jpeg_downloads_are_processed(remote, dirs, name):
    remote.add("1Formato", name)
    remote.download(dirs.input)

    result = run(dirs)

    assert result.batch.processed == 1
    assert pngs(dirs.output) == [expected_output("1Formato", name)]


# 3, 4, 5. Fondo blanco, transparente y dimensiones -------------------

def test_default_result_is_white_400_png(remote, dirs):
    remote.add("1Blanco", "producto.jpg")
    remote.download(dirs.input)

    run(dirs)

    with Image.open(dirs.output / expected_output("1Blanco", "producto.jpg")) as image:
        assert image.format == "PNG"
        assert (image.size, image.mode) == ((400, 400), "RGB")
        assert image.getpixel((0, 0)) == (255, 255, 255)
        red, green, blue = image.getpixel((200, 200))
        assert red > 150 and green < 90 and blue < 90


def test_transparent_background(remote, dirs):
    remote.add("1Transp", "producto.png")
    remote.download(dirs.input)
    options = OutputOptions(background="transparent")

    result = run(dirs, options=options)

    name = expected_output("1Transp", "producto.png", options)
    assert result.batch.processed == 1

    with Image.open(dirs.output / name) as image:
        assert (image.size, image.mode) == ((400, 400), "RGBA")
        assert image.getpixel((0, 0))[3] == 0
        assert image.getpixel((200, 200))[3] == 255


def test_custom_dimensions(remote, dirs):
    remote.add("1Medidas", "producto.jpg")
    remote.download(dirs.input)
    options = OutputOptions(width=800, height=600)

    run(dirs, options=options)

    name = expected_output("1Medidas", "producto.jpg", options)
    assert name.endswith("__800x600_white.png")

    with Image.open(dirs.output / name) as image:
        assert image.size == (800, 600)

    assert entries_by_file_id(dirs.output)["1Medidas"]["options"] == {
        "width": 800, "height": 600, "background": "white",
    }


# 6. Dos archivos remotos con el mismo nombre -------------------------

def test_two_remote_files_with_the_same_name_stay_distinguishable(remote, dirs):
    remote.add("1Mismo-A", "producto.jpg", product(color=(200, 20, 20)))
    remote.add("1Mismo-B", "producto.jpg", product(color=(20, 20, 200)))
    remote.download(dirs.input)

    result = run(dirs)

    assert result.batch.processed == 2
    entries = entries_by_file_id(dirs.output)
    assert entries["1Mismo-A"]["output_name"] == expected_output("1Mismo-A", "producto.jpg")
    assert entries["1Mismo-B"]["output_name"] == expected_output("1Mismo-B", "producto.jpg")
    assert entries["1Mismo-A"]["output_name"] != entries["1Mismo-B"]["output_name"]

    prepared = {item.file_id: item for item in result.prepared}
    assert {item.remote_name for item in prepared.values()} == {"producto.jpg"}
    assert prepared["1Mismo-A"].output_name == entries["1Mismo-A"]["output_name"]
    assert prepared["1Mismo-B"].output_name == entries["1Mismo-B"]["output_name"]


# 7. El fileId se conserva exacto -------------------------------------

def test_file_ids_are_never_case_folded(remote, dirs):
    remote.add("1caseX", "producto.jpg", product(color=(200, 20, 20)))
    remote.add("1casex", "producto.jpg", product(color=(20, 20, 200)))
    remote.download(dirs.input)

    result = run(dirs)

    assert result.batch.processed == 2
    assert set(entries_by_file_id(dirs.output)) == {"1caseX", "1casex"}
    assert {item.file_id for item in result.prepared} == {"1caseX", "1casex"}

    # Tras releer el manifiesto, siguen siendo identidades distintas.
    loaded = Manifest.load(dirs.output)
    keys = {entry.identity.source_key for entry in loaded.entries()}
    assert keys == {"1caseX", "1casex"}

    assert manifest.identity_key("drive", "1AbC") == "1AbC"
    assert manifest.identity_key("local", "Foto.JPG") == "foto.jpg"

    # Una segunda ejecución verifica cada una sin confundirlas.
    again = run(dirs)
    assert (again.batch.processed, again.batch.skipped) == (0, 2)


# 8. El original descargado se conserva -------------------------------

def test_downloads_and_their_records_are_not_modified(remote, dirs):
    remote.add("1Intacta", "producto.jpg")
    remote.add("1Intacta2", "otra.png")
    remote.download(dirs.input)
    before = snapshot(dirs.input)

    run(dirs)
    run(dirs)
    cli(dirs, "--dry-run")

    assert snapshot(dirs.input) == before


# 9. Sin registro de procedencia --------------------------------------

def test_image_without_provenance_is_rejected(remote, dirs, capsys):
    remote.add("1Valida", "producto.jpg")
    remote.download(dirs.input)
    (dirs.input / "suelta.jpg").write_bytes(product())

    result = run(dirs)

    assert result.batch.processed == 1
    assert [item.name for item in result.discovery.rejected] == ["suelta.jpg"]
    assert "procedencia" in result.discovery.rejected[0].reason
    assert not result.complete
    assert not any("suelta" in name for name in pngs(dirs.output))

    assert cli(dirs) == 6
    assert "suelta.jpg" in capsys.readouterr().out


# 10. Registro dañado -------------------------------------------------

def _invalid_json(dirs):
    record_path(dirs, "1Danada", "producto.jpg").write_text("{no es json", "utf-8")


def _foreign_file_id(dirs):
    edit_record(dirs, "1Danada", "producto.jpg", file_id="1OtroArchivo")


def _bad_file_id(dirs):
    edit_record(dirs, "1Danada", "producto.jpg", file_id="../../etc")


def _pending(dirs):
    edit_record(dirs, "1Danada", "producto.jpg", status="pending")


def _bad_md5(dirs):
    edit_record(dirs, "1Danada", "producto.jpg", md5_checksum="no-es-md5")


def _bad_folder(dirs):
    edit_record(dirs, "1Danada", "producto.jpg", folder_id=None)


def _bad_mime(dirs):
    edit_record(dirs, "1Danada", "producto.jpg", mime_type="image/png")


def _extra_field(dirs):
    edit_record(dirs, "1Danada", "producto.jpg", ruta="../../fuera.jpg")


@pytest.mark.parametrize(
    "damage",
    [_invalid_json, _foreign_file_id, _bad_file_id, _pending, _bad_md5,
     _bad_folder, _bad_mime, _extra_field],
)
def test_damaged_record_is_rejected(remote, dirs, damage):
    remote.add("1Danada", "producto.jpg")
    remote.download(dirs.input)
    damage(dirs)

    result = run(dirs)

    assert result.batch.processed == 0
    assert len(result.discovery.rejected) == 1
    assert result.discovery.sources == []
    assert pngs(dirs.output) == []
    assert not result.complete


def test_record_copied_to_another_download_is_rejected(remote, dirs):
    remote.add("1Primera", "a.jpg")
    remote.add("1Segunda", "b.jpg")
    remote.download(dirs.input)

    # El registro de "a" se copia como si fuera el de "b".
    a = record_path(dirs, "1Primera", "a.jpg")
    b = record_path(dirs, "1Segunda", "b.jpg")
    record = json.loads(a.read_text("utf-8"))
    record["local_name"] = local_name("1Segunda", "b.jpg")
    b.write_text(json.dumps(record), encoding="utf-8")

    result = run(dirs)

    assert [source.file_id for source in result.discovery.sources] == ["1Primera"]
    assert [item.name for item in result.discovery.rejected] == [local_name("1Segunda", "b.jpg")]


# 11, 12. Entrada alterada y SHA-256 incompatible ---------------------

def test_altered_download_is_rejected(remote, dirs):
    remote.add("1Alterada", "producto.jpg")
    remote.download(dirs.input)
    (dirs.input / local_name("1Alterada", "producto.jpg")).write_bytes(
        product(color=(10, 200, 10))
    )

    result = run(dirs)

    assert result.batch.processed == 0
    assert "no coincide" in result.discovery.rejected[0].reason
    assert pngs(dirs.output) == []


@pytest.mark.parametrize(
    "field, value",
    [("sha256", "0" * 64), ("md5_checksum", "0" * 32), ("drive_sha256", "1" * 64), ("size", 1)],
)
def test_incompatible_recorded_hashes_are_rejected(remote, dirs, field, value):
    remote.add("1Hash", "producto.jpg")
    remote.download(dirs.input)
    edit_record(dirs, "1Hash", "producto.jpg", **{field: value})

    result = run(dirs)

    assert result.batch.processed == 0
    assert len(result.discovery.rejected) == 1
    assert pngs(dirs.output) == []


def test_input_changed_during_batch_is_not_processed(remote, dirs, monkeypatch):
    remote.add("1Cambia", "producto.jpg")
    remote.download(dirs.input)
    path = dirs.input / local_name("1Cambia", "producto.jpg")

    original_discover = lp.discover_downloads

    def discover_then_alter(*args, **kwargs):
        discovery = original_discover(*args, **kwargs)
        path.write_bytes(product(color=(10, 200, 10)))
        return discovery

    monkeypatch.setattr(lp, "discover_downloads", discover_then_alter)

    result = run(dirs)

    assert result.batch.processed == 0
    assert result.batch.failed == 1
    assert "cambió" in result.batch.errors[0]
    assert pngs(dirs.output) == []


# 13. Resultado anterior válido ---------------------------------------

def test_valid_previous_result_is_verified_without_ai(remote, dirs, fake_ai):
    remote.add("1Previo", "producto.jpg")
    remote.download(dirs.input)
    run(dirs)
    before = snapshot(dirs.output)
    fake_ai["prepare"].clear()
    fake_ai["cutout"].clear()

    result = run(dirs)

    assert (result.batch.processed, result.batch.skipped) == (0, 1)
    assert fake_ai == {"prepare": [], "cutout": []}
    assert snapshot(dirs.output) == before
    assert [item.ready for item in result.prepared] == [True]


# 14. Resultado existente alterado ------------------------------------

def test_altered_existing_result_is_not_overwritten(remote, dirs):
    remote.add("1Tocada", "producto.jpg")
    remote.download(dirs.input)
    run(dirs)

    path = dirs.output / expected_output("1Tocada", "producto.jpg")
    path.write_bytes(b"editado a mano")

    result = run(dirs)

    assert result.batch.failed == 1
    assert path.read_bytes() == b"editado a mano"
    assert result.prepared[0].ready is False
    assert not result.complete
    assert cli(dirs) == 6


# 15. Reanudación tras una interrupción -------------------------------

def test_resume_after_interruption_between_publish_and_record(remote, dirs, monkeypatch, fake_ai):
    remote.add("1Corte", "producto.jpg")
    remote.download(dirs.input)

    real_save = Manifest.save
    calls = []

    def save_then_cut(self):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        real_save(self)

    with monkeypatch.context() as patch:
        patch.setattr(Manifest, "save", save_then_cut)
        with pytest.raises(KeyboardInterrupt):
            run(dirs)

    name = expected_output("1Corte", "producto.jpg")
    assert pngs(dirs.output) == [name]
    assert entries_by_file_id(dirs.output)["1Corte"]["status"] == manifest.PENDING
    fake_ai["cutout"].clear()

    result = run(dirs)

    assert result.batch.recovered == 1
    assert fake_ai["cutout"] == []
    assert entries_by_file_id(dirs.output)["1Corte"]["status"] == manifest.COMPLETED
    assert [item.ready for item in result.prepared] == [True]


def test_resume_after_interruption_during_processing(remote, dirs, monkeypatch, fake_ai):
    for index in range(3):
        remote.add(f"1Lote{index}", f"foto{index}.jpg")
    remote.download(dirs.input)

    def cut_on_second(image, model_name):
        if len(fake_ai["cutout"]) == 1:
            raise KeyboardInterrupt
        fake_ai["cutout"].append(model_name)
        return fake_cutout(image, model_name)

    with monkeypatch.context() as patch:
        patch.setattr(pipeline, "remove_background", cut_on_second)
        with pytest.raises(KeyboardInterrupt):
            run(dirs)

    assert len(pngs(dirs.output)) == 1
    fake_ai["cutout"].clear()

    result = run(dirs)

    assert (result.batch.processed, result.batch.skipped) == (2, 1)
    assert len(fake_ai["cutout"]) == 2
    assert all(item.ready for item in result.prepared)


# 16. Nueva revisión remota -------------------------------------------

def test_new_remote_revision_gets_its_own_result(remote, dirs):
    remote.add("1Revision", "producto.jpg", product(color=(200, 20, 20)), version="1")
    remote.download(dirs.input)
    run(dirs)
    old_name = expected_output("1Revision", "producto.jpg")
    old_bytes = (dirs.output / old_name).read_bytes()

    remote.change("1Revision", product(color=(20, 20, 200)), version="2")
    remote.download(dirs.input)

    result = run(dirs)

    assert result.batch.processed == 1
    assert [item.name for item in result.discovery.superseded] == [
        local_name("1Revision", "producto.jpg")
    ]

    # El resultado anterior se conserva con su registro.
    assert (dirs.output / old_name).read_bytes() == old_bytes
    document = manifest_document(dirs.output)
    revisions = sorted(
        (entry["source"]["drive"]["version"], entry["output_name"])
        for entry in document["entries"]
    )
    assert len(revisions) == 2 and revisions[0] == ("1", old_name)
    new_name = revisions[1][1]
    assert new_name != old_name

    # GD-03B: el resultado vigente del fileId es el de la nueva revisión.
    [prepared] = result.prepared
    assert (prepared.file_id, prepared.output_name, prepared.ready) == (
        "1Revision", new_name, True,
    )
    assert prepared.md5_checksum == hashlib.md5(
        remote.drive.media["1Revision"]
    ).hexdigest()


# 17. Error de inicialización del modelo ------------------------------

def test_model_initialization_error_processes_nothing(remote, dirs, monkeypatch, capsys):
    remote.add("1Modelo", "producto.jpg")
    remote.download(dirs.input)

    def failing(model_name):
        raise ModelInitializationError("sin caché de modelos")

    monkeypatch.setattr(batch, "prepare_model", failing)

    with pytest.raises(ModelInitializationError):
        run(dirs)

    assert pngs(dirs.output) == []
    assert cli(dirs) == 7
    assert "sin caché de modelos" in capsys.readouterr().err
    assert pngs(dirs.output) == []


# 18. Máscara vacía ---------------------------------------------------

def test_empty_mask_is_a_segmentation_failure(remote, dirs, capsys):
    # Un producto casi blanco: la segmentación simulada queda vacía.
    remote.add("1Vacia", "vacia.jpg", product(color=(250, 250, 250)))
    remote.add("1Llena", "llena.jpg")
    remote.download(dirs.input)

    result = run(dirs)

    assert result.batch.processed == 1
    assert result.batch.segmentation_failed == 1
    assert pngs(dirs.output) == [expected_output("1Llena", "llena.jpg")]

    assert cli(dirs) == 6
    out = capsys.readouterr().out
    assert "Fallos de segmentación:" in out


# 19. Límite ----------------------------------------------------------

def test_limit_selects_the_first_downloads_in_order(remote, dirs, fake_ai):
    for name, file_id in (("c.jpg", "1C"), ("a.jpg", "1A"), ("b.jpg", "1B")):
        remote.add(file_id, name)
    remote.download(dirs.input)

    result = run(dirs, limit=2)

    assert result.batch.processed == 2
    assert [item.file_id for item in result.prepared] == ["1A", "1B"]
    assert fake_ai["prepare"] == ["isnet-general-use"]

    with pytest.raises(ValueError):
        run(dirs, limit=0)


# 20. Carpeta vacía ---------------------------------------------------

def test_empty_folder(dirs, fake_ai, capsys):
    dirs.input.mkdir(parents=True)
    (dirs.input / "notas.txt").write_text("sin imágenes", "utf-8")

    result = run(dirs)

    assert result.batch.processed == 0
    assert result.complete
    assert fake_ai["prepare"] == []

    assert cli(dirs) == 0
    assert "No hay imágenes descargadas" in capsys.readouterr().out


def test_missing_input_folder_is_an_argument_error(dirs, capsys):
    assert cli(dirs) == 2
    assert not dirs.output.exists()


# 21. No se sobrescriben resultados ajenos ----------------------------

def test_foreign_result_with_the_same_name_is_not_overwritten(remote, dirs):
    remote.add("1Ajeno", "producto.jpg")
    remote.download(dirs.input)
    dirs.output.mkdir(parents=True)
    name = expected_output("1Ajeno", "producto.jpg")
    (dirs.output / name).write_bytes(b"resultado de otra herramienta")

    result = run(dirs)

    assert result.batch.unverified == 1
    assert result.batch.processed == 0
    assert (dirs.output / name).read_bytes() == b"resultado de otra herramienta"
    assert result.prepared[0].ready is False
    assert cli(dirs) == 6


def test_output_with_results_of_another_drive_folder_is_refused(remote, dirs, capsys):
    remote.add("1DeOtra", "otra.jpg", folder_id=OTHER_FOLDER)
    remote.add("1DeEsta", "esta.jpg")
    other_input = dirs.input.parent / "otra"
    remote.download(other_input, folder_id=OTHER_FOLDER)
    remote.download(dirs.input)

    lp.process_downloads(other_input, dirs.output, OTHER_FOLDER)
    before = snapshot(dirs.output)

    with pytest.raises(lp.ForeignFolderError):
        run(dirs)

    assert cli(dirs) == 8
    assert "otra carpeta" in capsys.readouterr().err
    assert snapshot(dirs.output) == before


def test_downloads_of_another_folder_are_excluded(remote, dirs):
    remote.add("1DeOtra", "otra.jpg", folder_id=OTHER_FOLDER)
    remote.add("1DeEsta", "esta.jpg")
    remote.download(dirs.input, folder_id=OTHER_FOLDER)
    remote.download(dirs.input)

    result = run(dirs)

    assert [source.file_id for source in result.discovery.sources] == ["1DeEsta"]
    assert [item.file_id for item in result.discovery.other_folder] == ["1DeOtra"]
    assert result.complete
    assert set(entries_by_file_id(dirs.output)) == {"1DeEsta"}


@pytest.mark.skipif(os.name == "nt", reason="crear enlaces requiere privilegios en Windows")
def test_symlinked_download_is_rejected(remote, dirs, tmp_path):
    remote.add("1Enlace", "producto.jpg")
    remote.download(dirs.input)
    name = local_name("1Enlace", "producto.jpg")
    outside = tmp_path / "fuera.jpg"
    (dirs.input / name).rename(outside)
    (dirs.input / name).symlink_to(outside)

    result = run(dirs)

    assert result.batch.processed == 0
    assert len(result.discovery.rejected) == 1


# 22. Sin llamadas a Google Drive -------------------------------------

def test_processing_never_contacts_drive_or_reads_secrets(remote, dirs, monkeypatch, tmp_path):
    remote.add("1SinRed", "producto.jpg")
    remote.download(dirs.input)

    def forbidden(*args, **kwargs):
        raise AssertionError("process-local no debe usar Google Drive")

    monkeypatch.setattr(client, "build_service", forbidden)
    monkeypatch.setattr(client, "execute", forbidden)
    monkeypatch.setattr(auth, "get_credentials", forbidden)
    monkeypatch.setattr(auth, "load_token", forbidden)
    monkeypatch.setattr(downloads, "download_folder", forbidden)
    secrets = tmp_path / "secretos"
    monkeypatch.setenv(auth.SECRETS_DIR_ENV, str(secrets))

    assert cli(dirs) == 0
    assert not secrets.exists()

    code = (PACKAGE / "google_drive" / "local_processing.py").read_text("utf-8")
    for name in ("build_service", "get_credentials", "load_token", "execute(",
                 ".files()", "googleapiclient", "download_folder"):
        assert name not in code


def test_output_inside_secrets_directory_is_refused(remote, dirs, monkeypatch, tmp_path):
    remote.add("1Secreto", "producto.jpg")
    remote.download(dirs.input)
    secrets = tmp_path / "secretos"
    secrets.mkdir()
    monkeypatch.setenv(auth.SECRETS_DIR_ENV, str(secrets))
    dirs.output = secrets / "resultados"

    assert cli(dirs) == 2
    assert list(secrets.iterdir()) == []


# 23. Una sola inicialización del modelo por lote ---------------------

def test_model_is_initialized_once_per_batch(remote, dirs, monkeypatch):
    for index in range(4):
        remote.add(f"1Uno{index}", f"foto{index}.jpg")
    remote.download(dirs.input)

    sessions = []
    background_removal._get_session.cache_clear()
    monkeypatch.setattr(batch, "prepare_model", background_removal.prepare_model)
    monkeypatch.setattr(
        background_removal, "new_session", lambda name: sessions.append(name) or object()
    )

    try:
        result = run(dirs)
    finally:
        background_removal._get_session.cache_clear()

    assert result.batch.processed == 4
    assert sessions == ["isnet-general-use"]


# 24. Integración con H-04 y H-05 -------------------------------------

def test_local_batch_result_with_the_same_name_keeps_its_owner(remote, dirs):
    """
    Un resultado del lote local (origen "local") ocupa el nombre base:
    el de Drive recibe un nombre de revisión y ninguno se sobrescribe.
    """
    remote.add("1Mixto", "producto.jpg")
    remote.download(dirs.input)

    local = batch.process_folder(dirs.input, dirs.output)
    assert local.processed == 1
    base = expected_output("1Mixto", "producto.jpg")
    local_bytes = (dirs.output / base).read_bytes()

    result = run(dirs)

    assert result.batch.processed == 1
    entry = entries_by_file_id(dirs.output)["1Mixto"]
    assert entry["output_name"].startswith(base[:-len(".png")] + "__rev-")
    assert (dirs.output / base).read_bytes() == local_bytes
    assert result.prepared[0].output_name == entry["output_name"]

    kinds = sorted(e["source"]["kind"] for e in manifest_document(dirs.output)["entries"])
    assert kinds == ["drive", "local"]


def test_model_and_options_changes_are_independent_results(remote, dirs):
    remote.add("1Variantes", "producto.jpg")
    remote.download(dirs.input)

    run(dirs)
    run(dirs, options=OutputOptions(background="transparent"))
    run(dirs, model_name="birefnet-general-lite")

    names = pngs(dirs.output)
    assert len(names) == 3
    document = manifest_document(dirs.output)
    assert {(e["model"], e["options"]["background"]) for e in document["entries"]} == {
        ("isnet-general-use", "white"),
        ("isnet-general-use", "transparent"),
        ("birefnet-general-lite", "white"),
    }


def test_same_input_and_output_folder(remote, dirs):
    remote.add("1Misma", "producto.jpg")
    remote.download(dirs.input)
    dirs.output = dirs.input

    first = run(dirs)
    second = run(dirs)

    assert first.batch.processed == 1
    assert (second.batch.processed, second.batch.skipped) == (0, 1)
    assert second.discovery.rejected == []


# Vista previa ----------------------------------------------------------

def test_dry_run_writes_nothing_and_loads_no_model(remote, dirs, fake_ai, capsys):
    remote.add("1Vista", "producto.jpg")
    remote.download(dirs.input)

    result = run(dirs, dry_run=True)

    assert result.batch.planned == 1
    assert result.batch.processed == 0
    assert fake_ai == {"prepare": [], "cutout": []}
    assert not dirs.output.exists()

    assert cli(dirs, "--dry-run") == 0
    assert "VISTA PREVIA" in capsys.readouterr().out
    assert not dirs.output.exists()


# Comando ----------------------------------------------------------------

def test_cli_summary_and_exit_code_for_a_complete_batch(remote, dirs, capsys):
    remote.add("1Resumen", "producto.jpg")
    remote.download(dirs.input)

    assert cli(dirs, "--background", "transparent", "--width", "500", "--height", "300") == 0
    out = capsys.readouterr().out

    assert "1Resumen" in out
    for label in ("Procesadas:", "Verificadas previamente:", "Rechazadas:",
                  "Fallos de segmentación:", "Resultados pendientes:", "PNG generados:"):
        assert label in out

    [name] = pngs(dirs.output)
    assert name.endswith("__500x300_transparent.png")

    assert cli(dirs, "--background", "transparent", "--width", "500", "--height", "300") == 0
    assert re.search(r"Verificadas previamente:\s+1\n", capsys.readouterr().out)


@pytest.mark.parametrize("arguments", [["--width", "10"], ["--height", "7000"], ["--limit", "0"]])
def test_cli_rejects_invalid_options(remote, dirs, arguments):
    remote.add("1Opciones", "producto.jpg")
    remote.download(dirs.input)

    assert cli(dirs, *arguments) == 2
    assert pngs(dirs.output) == []


@pytest.mark.parametrize("arguments", [["--background", "rojo"], ["--model", "otro"]])
def test_cli_only_accepts_existing_backgrounds_and_models(dirs, arguments):
    with pytest.raises(SystemExit) as error:
        cli(dirs, *arguments)

    assert error.value.code == 2


def test_cli_rejects_invalid_folder_id(dirs, capsys):
    dirs.input.mkdir(parents=True)
    assert cli(dirs, folder_id="../otra") == 2


# Manifiesto: fuentes de Drive -------------------------------------------

@pytest.fixture
def processed_manifest(remote, dirs):
    remote.add("1Manifiesto", "producto.jpg")
    remote.download(dirs.input)
    run(dirs)
    return dirs.output


def _rewrite(directory, change):
    document = manifest_document(directory)
    change(document["entries"][0]["source"])
    (directory / MANIFEST_NAME).write_text(json.dumps(document), "utf-8")


@pytest.mark.parametrize(
    "change",
    [
        lambda source: source.update(id="../fuera"),
        lambda source: source.pop("drive"),
        lambda source: source["drive"].update(local_name="../fuera.jpg"),
        lambda source: source["drive"].update(local_name="sub\\fuera.jpg"),
        lambda source: source["drive"].update(folder_id=""),
        lambda source: source["drive"].update(md5_checksum="x"),
        lambda source: source["drive"].update(extra=1),
        lambda source: source.update(kind="local"),
    ],
)
def test_invalid_drive_source_in_manifest_is_rejected(processed_manifest, change):
    _rewrite(processed_manifest, change)

    with pytest.raises(ManifestError):
        Manifest.load(processed_manifest)


def test_drive_manifest_round_trip(processed_manifest):
    loaded = Manifest.load(processed_manifest)
    [entry] = loaded.entries()

    assert entry.source.kind == "drive"
    assert entry.source.drive.folder_id == FOLDER
    assert entry.identity.source_key == "1Manifiesto"
    assert json.loads(loaded.to_json()) == manifest_document(processed_manifest)


# GD-04: selección explícita de descargas ------------------------------------

def test_selection_processes_exactly_the_given_download(remote, dirs):
    remote.add("1Sel", "producto.jpg", product(color=(200, 20, 20)), version="1")
    remote.download(dirs.input)
    remote.change("1Sel", product(color=(20, 20, 200)), version="2")
    remote.download(dirs.input)
    old = local_name("1Sel", "producto.jpg")

    result = run(dirs, selection=[("1Sel", old)])

    # La revisión pedida, aunque haya otra con una versión mayor.
    assert [source.path.name for source in result.selected] == [old]
    assert result.complete
    entry = entries_by_file_id(dirs.output)["1Sel"]
    assert entry["source"]["drive"]["local_name"] == old
    assert entry["source"]["drive"]["version"] == "1"


def test_selection_reports_a_download_that_is_not_available(remote, dirs):
    remote.add("1Hay", "producto.jpg")
    remote.download(dirs.input)

    result = run(dirs, selection=[("1Falta", "falta--1~falta.jpg")])

    assert result.selected == []
    assert [item.file_id for item in result.unavailable] == ["1Falta"]
    assert not result.complete
    assert pngs(dirs.output) == []


def test_selection_only_counts_its_own_rejections(remote, dirs):
    remote.add("1Bien", "producto.jpg")
    remote.download(dirs.input)
    (dirs.input / "suelta.jpg").write_bytes(product())

    result = run(dirs, selection=[("1Bien", local_name("1Bien", "producto.jpg"))])

    assert result.discovery.rejected
    assert result.rejected == []
    assert result.complete


def test_selection_and_limit_do_not_combine(dirs):
    with pytest.raises(ValueError):
        run(dirs, limit=1, selection=[])
