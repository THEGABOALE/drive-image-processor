"""
GD-03B: subida de los PNG verificados por GD-03A.

El escenario completo usa los módulos reales: descarga de GD-02,
procesamiento de GD-03A (con la IA simulada) y subida, todo sobre
FakeDriveServer, que modela las dos credenciales: drive.readonly (ve
todo) y drive.file (solo lo creado por la aplicación y las carpetas
autorizadas). No hay red ni archivos reales de Google Drive.
"""

import copy
import hashlib
import io
import json
import ssl
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import pytest
from google.auth.credentials import AnonymousCredentials
from google.oauth2.credentials import Credentials
from PIL import Image, ImageDraw

from drive_fakes import FakeDriveServer, folder, http_error
from image_processor import batch, drive_cli, pipeline
from image_processor.google_drive import auth, client, downloads, uploads
from image_processor.google_drive import local_processing as lp
from image_processor.google_drive.errors import (
    DriveAccessError,
    DriveAuthError,
    DriveCertificateError,
    DriveWriteAccessError,
)
from image_processor.manifest import MANIFEST_NAME, Manifest, ManifestError
from image_processor.naming import with_revision
from image_processor.processing import OutputOptions


FOLDER = "1CarpetaPrueba"
OTHER = "1OtraCarpeta"
TARGET = "1DestinoAutorizado"
LOCKED = "1SinAccesoApp"

ACCESS = "ya29.SECRETO-ACCESO-SUBIDA"
REFRESH = "1//SECRETO-REFRESCO-SUBIDA"


def product(color=(220, 30, 30), size=(300, 200)):
    image = Image.new("RGB", size, "white")
    width, height = size
    ImageDraw.Draw(image).rectangle(
        (width // 3, height // 4, 2 * width // 3, 3 * height // 4), fill=color
    )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def fake_cutout(image, model_name):
    rgba = image.convert("RGBA")
    rgba.putalpha(image.convert("L").point(lambda value: 255 if value < 200 else 0))
    return rgba


@pytest.fixture(autouse=True)
def fake_ai(monkeypatch):
    monkeypatch.setattr(batch, "prepare_model", lambda model_name: None)
    monkeypatch.setattr(pipeline, "remove_background", fake_cutout)


@pytest.fixture
def sleeps():
    return []


class World:
    """Drive simulado y las carpetas locales de GD-02 y GD-03A."""

    def __init__(self, tmp_path, sleeps):
        self.server = FakeDriveServer(
            folders=[
                folder(FOLDER, "Drive Image Processor - Test"),
                folder(OTHER, "Otra"),
                folder(TARGET, "Destino"),
                folder(LOCKED, "Sin acceso"),
            ],
            # La carpeta de prueba y el destino están autorizados para la
            # app; LOCKED no (límite de drive.file).
            app_folders=[FOLDER, TARGET],
        )
        self.server.children = {FOLDER: [], OTHER: [], TARGET: [], LOCKED: []}
        self.sleeps = sleeps
        self.downloads = tmp_path / "downloads" / "prueba"
        self.outputs = tmp_path / "outputs" / "gd03a-prueba"
        self.execute = partial(client.execute, sleep=sleeps.append)

    def add(self, file_id, name, color=(220, 30, 30), folder_id=FOLDER):
        data = product(color)
        item = {
            "id": file_id,
            "name": name,
            "mimeType": "image/jpeg",
            "size": str(len(data)),
            "md5Checksum": hashlib.md5(data).hexdigest(),
            "sha256Checksum": hashlib.sha256(data).hexdigest(),
            "version": "1",
            "headRevisionId": "rev-1",
            "modifiedTime": "2026-10-21T10:00:00.000Z",
            "parents": [folder_id],
            "trashed": False,
            "capabilities": {"canDownload": True},
        }
        self.server.items[file_id] = item
        self.server.children[folder_id].append(item)
        self.server.media[file_id] = data

    def prepare(self, folder_id=FOLDER, outputs=None, **options):
        """GD-02 + GD-03A reales sobre el Drive simulado."""

        downloads_dir = self.downloads.parent / folder_id
        outputs = outputs or self.outputs
        summary = downloads.download_folder(
            self.server.read, folder_id, downloads_dir, originals_only=True,
            sleep=self.sleeps.append, execute=self.execute,
        )
        assert summary.count(downloads.FAILED) == 0
        result = lp.process_downloads(downloads_dir, outputs, folder_id, **options)
        assert result.complete
        self.sleeps.clear()
        return result

    def upload(self, folder_id=FOLDER, **kwargs):
        kwargs.setdefault("sleep", self.sleeps.append)
        kwargs.setdefault("execute", self.execute)
        return uploads.upload_results(
            self.server.read, self.server.write, self.outputs, folder_id, **kwargs
        )

    def remote_children(self, folder_id=FOLDER):
        return [
            item for item in self.server.children[folder_id]
            if item["name"].startswith("TRANSFORMADA_")
        ]

    def originals(self):
        return {
            file_id: (copy.deepcopy(item), self.server.media.get(file_id))
            for file_id, item in self.server.items.items()
            if not item.get("name", "").startswith("TRANSFORMADA_")
        }

    def entry(self, file_id):
        [entry] = [
            entry for entry in Manifest.load(self.outputs, clean=False).entries()
            if entry.source.id == file_id
        ]
        return entry

    def ledger(self, file_id, target=FOLDER):
        path = uploads.ledger_path(self.outputs, target, self.entry(file_id).output_name)
        return json.loads(path.read_text("utf-8")) if path.exists() else None


@pytest.fixture
def world(tmp_path, sleeps):
    return World(tmp_path, sleeps)


def write_calls(world):
    return [operation for operation, _ in world.server.write.calls]


def creates(world):
    return [kwargs for operation, kwargs in world.server.write.calls if operation == "files.create"]


# 1, 2. Subida correcta y verificación ------------------------------------

def test_png_is_uploaded_to_the_original_folder(world):
    world.add("1Prod", "producto.jpg")
    world.prepare()

    summary = world.upload()

    [item] = summary.items
    assert item.status == uploads.UPLOADED
    assert summary.complete

    [remote] = world.remote_children()
    entry = world.entry("1Prod")
    local = world.outputs / entry.output_name

    assert remote["id"] == item.remote_id
    assert remote["name"] == "TRANSFORMADA_producto.png" == item.remote_name
    assert remote["parents"] == [FOLDER]
    assert remote["mimeType"] == "image/png"
    assert world.server.media[remote["id"]] == local.read_bytes()

    properties = remote["appProperties"]
    assert properties["dip_source_file_id"] == "1Prod"
    assert properties["dip_source_sha256"] == entry.source.sha256
    assert properties["dip_output_sha256"] == entry.output_sha256
    assert len(properties["dip_upload_key"]) == 32
    for key, value in properties.items():
        assert len(key.encode()) + len(value.encode()) <= 124

    # files.create con el ID preasignado y nada más.
    [create] = creates(world)
    assert create["body"]["id"] == remote["id"]
    assert create["body"]["parents"] == [FOLDER]
    assert create["body"]["mimeType"] == "image/png"
    assert set(write_calls(world)) == {
        "files.get", "files.list", "files.generateIds", "files.create",
    }


def test_upload_is_verified_by_size_and_checksums(world):
    world.add("1Verif", "producto.jpg")
    world.prepare()

    summary = world.upload()

    data = (world.outputs / world.entry("1Verif").output_name).read_bytes()
    ledger = world.ledger("1Verif")

    assert ledger["status"] == "completed"
    assert ledger["remote_id"] == summary.items[0].remote_id
    assert ledger["size"] == len(data)
    assert ledger["md5_checksum"] == hashlib.md5(data).hexdigest()
    assert ledger["output_sha256"] == hashlib.sha256(data).hexdigest()
    assert ledger["target_folder_id"] == FOLDER
    assert ledger["verified_at"]

    # La verificación posterior vuelve a consultar el PNG creado.
    gets = [k["fileId"] for op, k in world.server.write.calls if op == "files.get"]
    assert summary.items[0].remote_id in gets


def test_create_request_uses_a_resumable_png_upload():
    service = client.build_service(AnonymousCredentials())
    body = {"id": "1Gen", "name": "TRANSFORMADA_x.png", "parents": [FOLDER], "mimeType": "image/png"}

    request = uploads._create_request(service, body, b"\x89PNG datos", chunk_size=256 * 1024)

    assert request.method == "POST"
    assert "uploadType=resumable" in request.uri
    assert request.resumable.resumable() is True
    assert request.resumable.mimetype() == "image/png"
    assert request.resumable.size() == len(b"\x89PNG datos")
    assert json.loads(request.body) == body


# 3. Resultado local alterado ----------------------------------------------

def test_altered_local_result_is_rejected(world):
    world.add("1Alterado", "producto.jpg")
    world.prepare()
    (world.outputs / world.entry("1Alterado").output_name).write_bytes(b"editado")

    summary = world.upload()

    assert [item.status for item in summary.items] == [uploads.REJECTED]
    assert "no coincide" in summary.items[0].reason
    assert creates(world) == []
    assert world.remote_children() == []
    assert not summary.complete


def test_png_that_is_not_the_expected_image_is_rejected(world, monkeypatch):
    world.add("1Medida", "producto.jpg")
    world.prepare()
    entry = world.entry("1Medida")
    path = world.outputs / entry.output_name

    # Mismo SHA-256 registrado, pero no es un PNG de 400 x 400.
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(buffer, format="PNG")
    path.write_bytes(buffer.getvalue())
    document = json.loads((world.outputs / MANIFEST_NAME).read_text("utf-8"))
    document["entries"][0]["output_sha256"] = hashlib.sha256(buffer.getvalue()).hexdigest()
    (world.outputs / MANIFEST_NAME).write_text(json.dumps(document), "utf-8")

    summary = world.upload()

    assert summary.items[0].status == uploads.REJECTED
    assert creates(world) == []


# 4. Manifiesto corrupto -------------------------------------------------

def test_corrupt_manifest_stops_before_any_drive_call(world):
    world.add("1Manif", "producto.jpg")
    world.prepare()
    (world.outputs / MANIFEST_NAME).write_text("{dañado", "utf-8")

    with pytest.raises(ManifestError):
        world.upload()

    assert world.server.write.calls == []
    assert world.remote_children() == []


# 5. Archivo sin procedencia ---------------------------------------------

def test_png_without_provenance_is_rejected(world):
    world.add("1Valida", "producto.jpg")
    world.prepare()
    buffer = io.BytesIO()
    Image.new("RGB", (400, 400), "white").save(buffer, format="PNG")
    (world.outputs / "TRANSFORMADA_suelta.png").write_bytes(buffer.getvalue())

    summary = world.upload()

    statuses = {item.output_name: item.status for item in summary.items}
    assert statuses["TRANSFORMADA_suelta.png"] == uploads.REJECTED
    assert statuses[world.entry("1Valida").output_name] == uploads.UPLOADED
    assert [item["name"] for item in world.remote_children()] == ["TRANSFORMADA_producto.png"]


def test_results_of_the_local_batch_are_not_uploaded(world, tmp_path):
    world.outputs.mkdir(parents=True)
    Image.new("RGB", (300, 200), "red").save(world.outputs / "local.jpg")
    batch.process_folder(world.outputs, world.outputs)

    summary = world.upload()

    assert creates(world) == []
    assert all(item.status == uploads.REJECTED for item in summary.items)


# 6. Archivo de otra carpeta -------------------------------------------------

def test_results_of_another_drive_folder_are_rejected(world):
    world.add("1DeOtra", "otra.jpg", folder_id=OTHER)
    world.prepare(folder_id=OTHER)

    summary = world.upload(FOLDER)

    assert [item.status for item in summary.items] == [uploads.REJECTED]
    assert "otra carpeta" in summary.items[0].reason
    assert creates(world) == []


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda item: item.update(md5Checksum="f" * 32), "cambió en Drive"),
        (lambda item: item.update(parents=[OTHER]), "ya no está en la carpeta"),
        (lambda item: item.update(trashed=True), "ya no está en la carpeta"),
    ],
)
def test_result_must_match_a_current_original_of_the_folder(world, change, message):
    world.add("1Vigente", "producto.jpg")
    world.prepare()
    change(world.server.items["1Vigente"])

    summary = world.upload()

    assert summary.items[0].status == uploads.REJECTED
    assert message in summary.items[0].reason
    assert creates(world) == []


def test_deleted_original_is_rejected(world):
    world.add("1Eliminado", "producto.jpg")
    world.prepare()
    del world.server.items["1Eliminado"]

    summary = world.upload()

    assert summary.items[0].status == uploads.REJECTED
    assert "ya no existe" in summary.items[0].reason


# 7. Enlace simbólico -------------------------------------------------------

def test_symlinked_result_is_rejected(world, monkeypatch):
    world.add("1Enlace", "producto.jpg")
    world.prepare()
    name = world.entry("1Enlace").output_name
    real_is_symlink = Path.is_symlink

    # En Windows crear enlaces requiere privilegios: se simula el enlace.
    monkeypatch.setattr(
        Path, "is_symlink", lambda self: self.name == name or real_is_symlink(self)
    )

    summary = world.upload()

    assert summary.items[0].status == uploads.REJECTED
    assert "enlace" in summary.items[0].reason
    assert creates(world) == []


# 8. Nombre remoto duplicado -------------------------------------------------

def test_two_originals_with_the_same_name_get_distinct_remote_names(world):
    world.add("1MismoA", "producto.jpg", (200, 20, 20))
    world.add("1MismoB", "producto.jpg", (20, 20, 200))
    world.prepare()

    summary = world.upload()

    assert [item.status for item in summary.items] == [uploads.UPLOADED] * 2
    names = {item.file_id: item.remote_name for item in summary.items}
    revision = world.entry("1MismoB").identity.revision()
    assert names == {
        "1MismoA": "TRANSFORMADA_producto.png",
        "1MismoB": with_revision("TRANSFORMADA_producto.png", revision),
    }

    # Cada PNG remoto conserva el fileId de su original.
    by_name = {item["name"]: item for item in world.remote_children()}
    assert by_name[names["1MismoA"]]["appProperties"]["dip_source_file_id"] == "1MismoA"
    assert by_name[names["1MismoB"]]["appProperties"]["dip_source_file_id"] == "1MismoB"


# 9. Repetición de la misma operación -------------------------------------

def test_repeating_the_upload_creates_no_duplicates(world):
    world.add("1Repite", "producto.jpg")
    world.prepare()
    first = world.upload()

    second = world.upload()

    assert [item.status for item in second.items] == [uploads.VERIFIED]
    assert second.items[0].remote_id == first.items[0].remote_id
    assert len(creates(world)) == 1
    assert len(world.remote_children()) == 1
    assert second.complete


def test_without_local_record_the_remote_result_is_found_by_identity(world):
    world.add("1SinRegistro", "producto.jpg")
    world.prepare()
    first = world.upload()
    uploads.ledger_path(world.outputs, FOLDER, world.entry("1SinRegistro").output_name).unlink()

    again = world.upload()

    assert again.items[0].status == uploads.VERIFIED
    assert again.items[0].remote_id == first.items[0].remote_id
    assert len(creates(world)) == 1
    assert world.ledger("1SinRegistro")["status"] == "completed"


def test_deleted_remote_result_is_not_reuploaded_automatically(world):
    world.add("1Borrado", "producto.jpg")
    world.prepare()
    first = world.upload()
    world.server.items[first.items[0].remote_id]["trashed"] = True

    again = world.upload()

    assert again.items[0].status == uploads.FAILED
    assert "papelera" in again.items[0].reason or "no existe" in again.items[0].reason
    assert len(creates(world)) == 1


# 10. Respuesta de subida perdida --------------------------------------------

def test_lost_response_is_resolved_without_duplicates(world):
    world.add("1Perdida", "producto.jpg")
    world.prepare()
    world.server.write.upload_failures = ["lost"]

    summary = world.upload()

    assert summary.items[0].status == uploads.UPLOADED
    # El reintento usó el mismo ID: Drive respondió 409 y no duplicó.
    attempts = world.server.write.upload_attempts
    assert len(attempts) == 2 and len(set(attempts)) == 1
    assert len(world.server.created) == 1
    assert len(world.remote_children()) == 1


def test_uncertain_upload_is_resolved_on_the_next_run(world, monkeypatch):
    world.add("1Incierta", "producto.jpg")
    world.prepare()
    world.server.write.upload_failures = ["lost"] + [ConnectionResetError()] * 10
    real_create = world.server.create

    def create_then_lose_reads(kwargs):
        # Drive crea el archivo, pero ni la respuesta ni la consulta
        # posterior llegan: el estado queda incierto.
        item = real_create(kwargs)
        world.server.write.failures = [http_error(503)] * client.MAX_ATTEMPTS
        return item

    monkeypatch.setattr(world.server, "create", create_then_lose_reads)

    summary = world.upload()

    assert summary.items[0].status == uploads.UNCERTAIN
    assert not summary.complete
    ledger = world.ledger("1Incierta")
    assert ledger["status"] == "pending"
    reserved = ledger["remote_id"]

    world.server.write.upload_failures = []
    again = world.upload()

    assert again.items[0].status == uploads.RECOVERED
    assert again.items[0].remote_id == reserved
    assert len(world.server.created) == 1
    assert world.ledger("1Incierta")["status"] == "completed"


def test_pending_upload_that_never_reached_drive_is_retried_with_the_same_id(world):
    world.add("1Pendiente", "producto.jpg")
    world.prepare()
    world.server.write.upload_failures = [ConnectionResetError()] * 10
    world.server.read.failures = []

    first = world.upload()
    reserved = world.ledger("1Pendiente")["remote_id"]
    assert first.items[0].status == uploads.UNCERTAIN
    assert world.server.created == []

    world.server.write.upload_failures = []
    again = world.upload()

    assert again.items[0].status == uploads.UPLOADED
    assert again.items[0].remote_id == reserved
    assert world.server.generated == [reserved]


# 11, 12. Permisos y autorización ------------------------------------------

def test_permission_error_aborts_without_retries(world):
    world.add("1Permiso", "a.jpg")
    world.add("1Permiso2", "b.jpg")
    world.prepare()
    world.server.write.upload_failures = [http_error(403, ["insufficientFilePermissions"])]

    summary = world.upload()

    assert isinstance(summary.aborted, DriveAccessError)
    assert len(world.server.write.upload_attempts) == 1
    assert world.sleeps == []
    assert world.remote_children() == []


def test_storage_quota_error_has_a_clear_message(world):
    world.add("1Cuota", "producto.jpg")
    world.prepare()
    world.server.write.upload_failures = [http_error(403, ["storageQuotaExceeded"])]

    summary = world.upload()

    assert isinstance(summary.aborted, DriveAccessError)
    assert "almacenamiento" in str(summary.aborted)


def test_revoked_write_authorization_aborts(world):
    world.add("1Revocada", "producto.jpg")
    world.prepare()
    world.server.write.upload_failures = [http_error(401)]

    summary = world.upload()

    assert isinstance(summary.aborted, DriveAuthError)
    assert "auth-write" in str(summary.aborted)
    assert world.remote_children() == []


# 13. 429 y 5xx -------------------------------------------------------------

def test_rate_limit_and_server_errors_are_retried_with_the_same_id(world):
    world.add("1Reintento", "producto.jpg")
    world.prepare()
    world.server.write.upload_failures = [http_error(429), http_error(503), http_error(500)]

    summary = world.upload()

    assert summary.items[0].status == uploads.UPLOADED
    attempts = world.server.write.upload_attempts
    assert len(attempts) == 4 and len(set(attempts)) == 1
    assert world.sleeps == [1.0, 2.0, 4.0]
    assert len(world.remote_children()) == 1


def test_repeated_network_failures_stop_the_batch(world):
    for index in range(5):
        world.add(f"1Red{index}", f"foto{index}.jpg")
    world.prepare()
    world.server.write.upload_failures = [ConnectionResetError()] * 100

    summary = world.upload()

    assert summary.aborted is not None
    assert summary.count(uploads.UNCERTAIN) == uploads.MAX_CONSECUTIVE_TRANSIENT_FAILURES
    assert world.server.created == []


# 14. Certificado SSL inválido -----------------------------------------------

def test_invalid_ssl_certificate_is_not_retried(world):
    world.add("1Ssl", "producto.jpg")
    world.prepare()
    world.server.write.upload_failures = [ssl.SSLCertVerificationError("certificado")]

    summary = world.upload()

    assert isinstance(summary.aborted, DriveCertificateError)
    assert "certificado" in str(summary.aborted).lower()
    assert len(world.server.write.upload_attempts) == 1
    assert world.sleeps == []


# 15. PNG remoto con checksum incorrecto ---------------------------------------

def test_remote_file_with_wrong_checksum_is_never_completed(world):
    world.add("1Corrupto", "producto.jpg")
    world.prepare()
    world.server.md5_override["1Gen0000Id"] = "0" * 32

    summary = world.upload()

    assert summary.items[0].status == uploads.FAILED
    assert "MD5" in summary.items[0].reason
    assert world.ledger("1Corrupto")["status"] == "mismatch"

    # Repetir no crea otro archivo ni lo da por bueno.
    again = world.upload()
    assert again.items[0].status == uploads.FAILED
    assert len(world.server.created) == 1


# 16. Límite ---------------------------------------------------------------

def test_limit_counts_the_selected_results(world):
    for name, file_id in (("c.jpg", "1C"), ("a.jpg", "1A"), ("b.jpg", "1B")):
        world.add(file_id, name)
    world.prepare()

    summary = world.upload(limit=2)

    assert [item.file_id for item in summary.items] == ["1A", "1B"]
    assert len(world.remote_children()) == 2

    with pytest.raises(ValueError):
        world.upload(limit=0)


# 17. Vista previa ---------------------------------------------------------

def test_dry_run_writes_nothing(world):
    world.add("1Vista", "producto.jpg")
    world.prepare()
    before = sorted(p.relative_to(world.outputs).as_posix() for p in world.outputs.rglob("*"))

    summary = world.upload(dry_run=True)

    assert summary.items[0].status == uploads.PLANNED
    assert summary.items[0].remote_name == "TRANSFORMADA_producto.png"
    assert not {"files.create", "files.generateIds"} & set(write_calls(world))
    assert world.server.generated == [] and world.server.created == []
    after = sorted(p.relative_to(world.outputs).as_posix() for p in world.outputs.rglob("*"))
    assert after == before


def test_dry_run_works_without_write_authorization(world):
    world.add("1VistaSola", "producto.jpg")
    world.prepare()

    summary = uploads.upload_results(
        world.server.read, None, world.outputs, FOLDER, dry_run=True,
        sleep=world.sleeps.append, execute=world.execute,
    )

    assert summary.items[0].status == uploads.PLANNED
    assert summary.write_access is None


# 18. Carpetas de destino ---------------------------------------------------

def test_different_target_folders_are_independent(world):
    world.add("1Destinos", "producto.jpg")
    world.prepare()

    own = world.upload()
    other = world.upload(target_folder_id=TARGET)

    assert own.items[0].status == other.items[0].status == uploads.UPLOADED
    assert own.items[0].remote_id != other.items[0].remote_id
    assert len(world.remote_children(FOLDER)) == 1
    assert len(world.remote_children(TARGET)) == 1
    assert world.ledger("1Destinos", FOLDER)["target_folder_id"] == FOLDER
    assert world.ledger("1Destinos", TARGET)["target_folder_id"] == TARGET

    assert world.upload().items[0].status == uploads.VERIFIED
    assert world.upload(target_folder_id=TARGET).items[0].status == uploads.VERIFIED
    assert len(world.server.created) == 2


def test_folder_without_drive_file_access_stops_before_uploading(world):
    world.add("1Bloqueada", "producto.jpg")
    world.prepare()

    with pytest.raises(DriveWriteAccessError, match="drive.file"):
        world.upload(target_folder_id=LOCKED)

    assert not {"files.create", "files.generateIds"} & set(write_calls(world))
    assert world.server.children[LOCKED] == []


def test_target_that_is_not_a_folder_is_refused(world):
    world.add("1NoCarpeta", "producto.jpg")
    world.prepare()

    with pytest.raises(drive_cli.DriveNotAFolderError):
        world.upload(target_folder_id="1NoCarpeta")

    assert creates(world) == []


# 19. Originales intactos -------------------------------------------------

def test_originals_are_never_modified(world):
    world.add("1Original", "producto.jpg")
    world.add("1Original2", "otra.jpg")
    world.prepare()
    before = world.originals()
    local_before = {
        p.name: p.read_bytes() for p in world.downloads.parent.rglob("*") if p.is_file()
    }

    world.upload()
    world.upload()

    assert world.originals() == before
    assert {
        p.name: p.read_bytes() for p in world.downloads.parent.rglob("*") if p.is_file()
    } == local_before
    for view in (world.server.read, world.server.write):
        assert all(
            operation in ("files.list", "files.get", "files.get_media", "files.create",
                          "files.generateIds", "about.get")
            for operation, _ in view.calls
        )


# 20. Token de solo lectura -------------------------------------------------

def make_credentials(scopes):
    return Credentials(
        token=ACCESS,
        refresh_token=REFRESH,
        token_uri="https://oauth2.googleapis.com/token",
        client_id="cliente.apps.googleusercontent.com",
        client_secret="SECRETO-CLIENTE",
        scopes=list(scopes),
        expiry=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
    )


class FakeFlow:
    def __init__(self, credentials):
        self.credentials = credentials

    def run_local_server(self, **kwargs):
        return self.credentials


def test_write_authorization_never_touches_the_read_only_token(tmp_path):
    paths = auth.secret_paths(tmp_path / ".secrets")
    auth.save_token(make_credentials([auth.READONLY_SCOPE]), paths.token)
    (paths.directory / "credentials.json").write_text("{}", "utf-8")
    read_before = paths.token.read_bytes()
    requested = []

    def factory(path, scopes):
        requested.append(tuple(scopes))
        return FakeFlow(make_credentials([auth.WRITE_SCOPE]))

    credentials = auth.get_credentials(
        paths, interactive=True, profile=auth.WRITE, flow_factory=factory
    )

    assert requested == [(auth.WRITE_SCOPE,)]
    assert credentials.scopes == [auth.WRITE_SCOPE]
    assert paths.token.read_bytes() == read_before
    assert paths.write_token.name == "token_write.json"
    assert json.loads(paths.write_token.read_text("utf-8"))["scopes"] == [auth.WRITE_SCOPE]

    # La autorización de lectura sigue funcionando.
    assert auth.get_credentials(paths).scopes == [auth.READONLY_SCOPE]
    assert auth.get_credentials(paths, profile=auth.WRITE).scopes == [auth.WRITE_SCOPE]


@pytest.mark.parametrize(
    "scopes",
    [
        ["https://www.googleapis.com/auth/drive"],
        [auth.WRITE_SCOPE, "https://www.googleapis.com/auth/drive"],
        [auth.READONLY_SCOPE],
        [auth.WRITE_SCOPE, auth.READONLY_SCOPE],
    ],
)
def test_write_token_must_have_exactly_drive_file(tmp_path, scopes):
    paths = auth.secret_paths(tmp_path / ".secrets")
    auth.save_token(make_credentials(scopes), paths.write_token)

    with pytest.raises(DriveAuthError, match="drive.file"):
        auth.get_credentials(paths, profile=auth.WRITE)


def test_broader_scope_granted_in_the_browser_is_not_saved(tmp_path):
    paths = auth.secret_paths(tmp_path / ".secrets")
    paths.directory.mkdir(parents=True)
    (paths.directory / "credentials.json").write_text("{}", "utf-8")

    with pytest.raises(DriveAuthError, match="drive.file"):
        auth.get_credentials(
            paths, interactive=True, profile=auth.WRITE,
            flow_factory=lambda path, scopes: FakeFlow(
                make_credentials(["https://www.googleapis.com/auth/drive"])
            ),
        )

    assert not paths.write_token.exists()


def test_read_only_profile_is_unchanged():
    assert auth.SCOPES == (auth.READONLY_SCOPE,)
    assert auth.READ.scopes == (auth.READONLY_SCOPE,)
    assert auth.WRITE.scopes == ("https://www.googleapis.com/auth/drive.file",)
    assert "https://www.googleapis.com/auth/drive" not in auth.WRITE.scopes


# 21. Colisión con un resultado ajeno ----------------------------------------

def foreign(world, name, file_id):
    item = {
        "id": file_id, "name": name, "mimeType": "image/png",
        "parents": [FOLDER], "size": "10", "md5Checksum": "1" * 32, "trashed": False,
    }
    world.server.items[file_id] = item
    world.server.children[FOLDER].append(item)
    return copy.deepcopy(item)


def test_foreign_file_with_the_same_name_is_never_touched(world):
    world.add("1Colision", "producto.jpg")
    world.prepare()
    original = foreign(world, "TRANSFORMADA_producto.png", "1ArchivoAjeno")

    summary = world.upload()

    revision = world.entry("1Colision").identity.revision()
    assert summary.items[0].status == uploads.UPLOADED
    assert summary.items[0].remote_name == with_revision("TRANSFORMADA_producto.png", revision)
    assert world.server.items["1ArchivoAjeno"] == original
    assert len(world.remote_children()) == 2


def test_collision_on_both_names_is_rejected(world):
    world.add("1Doble", "producto.jpg")
    world.prepare()
    revision = world.entry("1Doble").identity.revision()
    foreign(world, "TRANSFORMADA_producto.png", "1AjenoUno")
    foreign(world, with_revision("TRANSFORMADA_producto.png", revision), "1AjenoDos")

    summary = world.upload()

    assert summary.items[0].status == uploads.REJECTED
    assert "nombre" in summary.items[0].reason
    assert creates(world) == []


def test_collision_listing_reads_every_page(world, monkeypatch):
    world.add("1Paginas", "producto.jpg")
    world.prepare()
    for index in range(5):
        foreign(world, f"relleno{index}.png", f"1Relleno{index}")
    foreign(world, "TRANSFORMADA_producto.png", "1AjenoFinal")
    monkeypatch.setattr(uploads, "PAGE_SIZE", 2)

    summary = world.upload()

    assert summary.items[0].remote_name != "TRANSFORMADA_producto.png"


# 22. Compatibilidad con GD-01, GD-02 y GD-03A --------------------------------

def test_uploaded_results_do_not_disturb_gd01_gd02_and_gd03a(world):
    world.add("1Compat", "producto.jpg")
    first = world.prepare()
    world.upload()

    # GD-01: los resultados subidos no son originales.
    from image_processor.google_drive import queries
    listing = queries.list_images(world.server.read, FOLDER, originals_only=True, execute=world.execute)
    assert [image.id for image in listing.images] == ["1Compat"]

    # GD-02 y GD-03A: repetir no descarga ni procesa nada nuevo.
    again = world.prepare()
    assert again.batch.processed == 0 and again.batch.skipped == 1
    assert [p.file_id for p in again.prepared] == [p.file_id for p in first.prepared]
    assert world.upload().items[0].status == uploads.VERIFIED


# Comando -------------------------------------------------------------------

@pytest.fixture
def cli_world(world, monkeypatch, tmp_path):
    secrets = tmp_path / ".secrets"
    paths = auth.secret_paths(secrets)
    auth.save_token(make_credentials([auth.READONLY_SCOPE]), paths.token)
    auth.save_token(make_credentials([auth.WRITE_SCOPE]), paths.write_token)
    monkeypatch.setenv(auth.SECRETS_DIR_ENV, str(secrets))

    def build(credentials):
        return world.server.write if auth.WRITE_SCOPE in credentials.scopes else world.server.read

    monkeypatch.setattr(client, "build_service", build)
    monkeypatch.setattr(client, "execute", world.execute)
    monkeypatch.setattr(uploads.time, "sleep", world.sleeps.append)
    world.paths = paths
    return world


def upload_cli(world, *extra, limit="1"):
    arguments = ["upload", "--folder-id", FOLDER, "--input", str(world.outputs)]
    if limit is not None:
        arguments += ["--limit", limit]
    return drive_cli.main(arguments + list(extra))


def test_cli_upload_and_repeat(cli_world, capsys):
    cli_world.add("1Cli", "producto.jpg")
    cli_world.prepare()
    read_token = cli_world.paths.token.read_bytes()

    assert upload_cli(cli_world) == 0
    out = capsys.readouterr().out
    for label in ("Subidas:", "Verificadas previamente:", "Rechazadas:", "Fallidas:", "Pendientes:"):
        assert label in out
    assert "TRANSFORMADA_producto.png" in out

    assert upload_cli(cli_world) == 0
    assert len(cli_world.server.created) == 1
    assert cli_world.paths.token.read_bytes() == read_token


def test_cli_limit_is_required(cli_world):
    with pytest.raises(SystemExit) as error:
        upload_cli(cli_world, limit=None)
    assert error.value.code == 2


def test_cli_dry_run(cli_world, capsys):
    cli_world.add("1CliVista", "producto.jpg")
    cli_world.prepare()

    assert upload_cli(cli_world, "--dry-run") == 0
    assert "VISTA PREVIA" in capsys.readouterr().out
    assert cli_world.server.created == [] and cli_world.server.generated == []


def test_cli_dry_run_without_write_token(cli_world, capsys):
    cli_world.add("1CliSinEscritura", "producto.jpg")
    cli_world.prepare()
    cli_world.paths.write_token.unlink()

    assert upload_cli(cli_world, "--dry-run") == 0
    assert "auth-write" in capsys.readouterr().out
    assert upload_cli(cli_world) == 3


@pytest.mark.parametrize(
    "failure, code, message",
    [
        (http_error(403, ["insufficientFilePermissions"]), 4, "403"),
        (http_error(401), 3, "auth-write"),
        (ssl.SSLCertVerificationError("x"), 5, "certificado"),
    ],
)
def test_cli_error_codes(cli_world, capsys, failure, code, message):
    cli_world.add("1CliError", "producto.jpg")
    cli_world.prepare()
    cli_world.server.write.upload_failures = [failure]

    assert upload_cli(cli_world) == code
    assert message in capsys.readouterr().err


def test_cli_folder_without_access_and_corrupt_manifest(cli_world, capsys):
    cli_world.add("1CliBloq", "producto.jpg")
    cli_world.prepare()

    assert upload_cli(cli_world, "--target-folder-id", LOCKED) == 4
    assert "drive.file" in capsys.readouterr().err

    (cli_world.outputs / MANIFEST_NAME).write_text("{", "utf-8")
    assert upload_cli(cli_world) == 8


def test_cli_partial_upload_exit_code(cli_world, capsys):
    cli_world.add("1CliParcial", "producto.jpg")
    cli_world.prepare()
    (cli_world.outputs / cli_world.entry("1CliParcial").output_name).write_bytes(b"x")

    assert upload_cli(cli_world) == 6


def test_cli_never_prints_secrets(cli_world, capsys):
    cli_world.add("1CliSecreto", "producto.jpg")
    cli_world.prepare()

    upload_cli(cli_world, "--dry-run")
    upload_cli(cli_world)
    drive_cli.main(["auth-write"])
    out, err = capsys.readouterr()

    for secret in (ACCESS, REFRESH, "SECRETO-CLIENTE"):
        assert secret not in out and secret not in err


def test_cli_auth_write_reports_the_minimal_scope(cli_world, capsys):
    read_token = cli_world.paths.token.read_bytes()

    assert drive_cli.main(["auth-write"]) == 0

    out = capsys.readouterr().out
    assert "drive.file" in out
    assert cli_world.paths.token.read_bytes() == read_token


# GD03B-R1: subidas reanudables sin progreso -------------------------------

def local_png(world, file_id):
    return (world.outputs / world.entry(file_id).output_name).read_bytes()


def test_partial_progress_completes_the_upload(world):
    world.add("1Bloques", "producto.jpg")
    world.prepare()
    world.server.write.chunked = True
    data = local_png(world, "1Bloques")
    assert len(data) > 4 * 256

    summary = world.upload(chunk_size=256)

    item = summary.items[0]
    assert item.status == uploads.UPLOADED
    assert len(world.server.write.upload_attempts) == -(-len(data) // 256)
    assert world.server.write.upload_sessions == [item.remote_id]
    assert world.server.media[item.remote_id] == data


@pytest.mark.parametrize(
    "responses",
    [
        pytest.param([0] * 50, id="sin-progreso"),
        pytest.param(["none"] * 50, id="sin-estado"),
        pytest.param([300, 200, 200, 200] + [200] * 50, id="retroceso"),
    ],
)
def test_upload_without_progress_stops_and_stays_recoverable(world, responses):
    world.add("1Quieta", "producto.jpg")
    world.prepare()
    world.server.write.chunk_responses = list(responses)

    summary = world.upload(chunk_size=256)

    item = summary.items[0]
    assert item.status == uploads.UNCERTAIN
    assert "sin progreso" in item.reason
    assert not summary.complete
    assert len(world.server.write.upload_attempts) <= uploads.MAX_STALLED_CHUNKS + 1
    # Ni reintentos a ciegas ni un ID nuevo: el registro sigue pendiente.
    assert world.server.write.upload_sessions == [item.remote_id]
    assert world.server.created == []
    assert world.sleeps == []
    ledger = world.ledger("1Quieta")
    assert ledger["status"] == "pending"
    assert world.server.generated == [ledger["remote_id"]]

    world.server.write.chunk_responses = []
    again = world.upload(chunk_size=256)

    assert again.items[0].status == uploads.UPLOADED
    assert again.items[0].remote_id == ledger["remote_id"]
    assert world.server.generated == [ledger["remote_id"]]


def test_endless_tiny_progress_is_bounded(world):
    world.add("1Goteo", "producto.jpg")
    world.prepare()
    size = len(local_png(world, "1Goteo"))
    world.server.write.chunk_responses = list(range(1, 400))

    summary = world.upload(chunk_size=256)

    item = summary.items[0]
    assert item.status == uploads.UNCERTAIN
    assert "demasiadas respuestas parciales" in item.reason
    assert len(world.server.write.upload_attempts) <= 2 * -(-size // 256) + uploads.MAX_STALLED_CHUNKS
    assert world.server.created == []
    assert world.ledger("1Goteo")["status"] == "pending"


def test_retryable_error_between_chunks_restarts_with_the_same_id(world):
    world.add("1Corte503", "producto.jpg")
    world.prepare()
    world.server.write.chunked = True
    world.server.write.upload_failures = [None, http_error(503)]

    summary = world.upload(chunk_size=256)

    item = summary.items[0]
    assert item.status == uploads.UPLOADED
    assert world.server.write.upload_sessions == [item.remote_id] * 2
    assert world.sleeps == [1.0]
    assert world.server.media[item.remote_id] == local_png(world, "1Corte503")


def test_stalled_uploads_count_as_network_failures(world):
    for index in range(5):
        world.add(f"1Lenta{index}", f"foto{index}.jpg")
    world.prepare()
    world.server.write.chunk_responses = [0] * 400

    summary = world.upload(chunk_size=256)

    assert summary.aborted is not None
    assert summary.count(uploads.UNCERTAIN) == uploads.MAX_CONSECUTIVE_TRANSIENT_FAILURES
    assert world.server.created == []


# GD03B-R3: vista previa sin exigir acceso de escritura ---------------------

def test_dry_run_with_write_token_reports_missing_folder_access(world):
    world.add("1VistaBloq", "producto.jpg")
    world.prepare()

    summary = world.upload(dry_run=True, target_folder_id=LOCKED)

    assert [item.status for item in summary.items] == [uploads.PLANNED]
    assert summary.write_access is False
    assert "drive.file" in summary.write_access_note
    assert not {"files.create", "files.generateIds"} & set(write_calls(world))
    assert world.server.generated == [] and world.server.created == []
    assert not (world.outputs / uploads.LEDGER_DIR).exists()


def test_dry_run_without_write_token_on_a_locked_folder(world):
    world.add("1VistaSinToken", "producto.jpg")
    world.prepare()

    summary = uploads.upload_results(
        world.server.read, None, world.outputs, FOLDER, target_folder_id=LOCKED,
        dry_run=True, sleep=world.sleeps.append, execute=world.execute,
    )

    assert [item.status for item in summary.items] == [uploads.PLANNED]
    assert summary.write_access is None
    assert "no se verificó" in summary.write_access_note
    assert world.server.write.calls == []
    assert not (world.outputs / uploads.LEDGER_DIR).exists()


def test_dry_run_with_access_reports_it_as_verified_but_not_guaranteed(world):
    world.add("1VistaOk", "producto.jpg")
    world.prepare()

    summary = world.upload(dry_run=True)

    assert summary.write_access is True
    assert "no garantiza" in summary.write_access_note


def test_real_upload_still_checks_access_before_creating(world):
    world.add("1RealBloq", "producto.jpg")
    world.prepare()
    world.upload(dry_run=True, target_folder_id=LOCKED)

    with pytest.raises(DriveWriteAccessError):
        world.upload(target_folder_id=LOCKED)

    assert world.server.generated == [] and world.server.created == []


def test_cli_dry_run_on_a_locked_folder(cli_world, capsys):
    cli_world.add("1CliVistaBloq", "producto.jpg")
    cli_world.prepare()

    assert upload_cli(cli_world, "--dry-run", "--target-folder-id", LOCKED) == 0
    out = capsys.readouterr().out
    assert "sin acceso" in out and "no garantiza" in out

    cli_world.paths.write_token.unlink()
    assert upload_cli(cli_world, "--dry-run", "--target-folder-id", LOCKED) == 0
    assert "no verificado" in capsys.readouterr().out
    assert upload_cli(cli_world, "--target-folder-id", LOCKED) == 3


def test_cli_dry_run_never_modifies_the_tokens(cli_world, monkeypatch):
    cli_world.add("1CliTokens", "producto.jpg")
    cli_world.prepare()
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    for scopes, path in (
        ([auth.READONLY_SCOPE], cli_world.paths.token),
        ([auth.WRITE_SCOPE], cli_world.paths.write_token),
    ):
        expired = make_credentials(scopes)
        expired.expiry = past
        auth.save_token(expired, path)
    before = (cli_world.paths.token.read_bytes(), cli_world.paths.write_token.read_bytes())

    def refresh(self, request):
        self.token = "ya29.RENOVADO"
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", refresh)

    assert upload_cli(cli_world, "--dry-run") == 0
    assert (cli_world.paths.token.read_bytes(), cli_world.paths.write_token.read_bytes()) == before
