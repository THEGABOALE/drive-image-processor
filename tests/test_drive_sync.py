"""
GD-04: sincronización (sync) sobre los módulos reales de GD-01 a GD-03B.

FakeDriveServer modela las dos credenciales (drive.readonly ve todo;
drive.file solo lo que creó la aplicación y las carpetas autorizadas).
La IA se simula en pipeline.remove_background. No hay red.
"""

import copy
import errno
import hashlib
import io
import json
import os
import ssl
import time
from datetime import datetime, timedelta, timezone
from functools import partial

import pytest
from google.oauth2.credentials import Credentials
from PIL import Image, ImageDraw

from drive_fakes import FakeDriveServer, folder, http_error
from image_processor import batch, drive_cli, pipeline
from image_processor.background_removal import ModelInitializationError
from image_processor.google_drive import auth, client, downloads, sync, uploads
from image_processor.google_drive.errors import (
    DriveAccessError,
    DriveAuthError,
    DriveCertificateError,
    DriveQuotaError,
    DriveWriteAccessError,
)
from image_processor.manifest import MANIFEST_NAME
from image_processor.processing import OutputOptions


FOLDER = "1CarpetaOriginales"
TARGET = "1DestinoPropio"
LOCKED = "1SinAccesoApp"
OTHER = "1OtraCarpeta"

RED = (220, 30, 30)
BLUE = (30, 30, 220)
GREEN = (30, 160, 30)

ACCESS = "ya29.SECRETO-ACCESO-SYNC"
REFRESH = "1//SECRETO-REFRESCO-SYNC"


def product(color=RED, size=(300, 200)):
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
def ai(monkeypatch):
    calls = {"prepare": 0, "cutout": 0}

    def prepare(model_name):
        calls["prepare"] += 1

    def cutout(image, model_name):
        calls["cutout"] += 1
        return fake_cutout(image, model_name)

    monkeypatch.setattr(batch, "prepare_model", prepare)
    monkeypatch.setattr(pipeline, "remove_background", cutout)
    return calls


class Env:
    """Drive simulado y las carpetas locales de una sincronización."""

    def __init__(self, tmp_path):
        self.server = FakeDriveServer(
            folders=[
                folder(FOLDER, "Originales"),
                folder(TARGET, "Destino propio"),
                folder(LOCKED, "Sin acceso"),
                folder(OTHER, "Otra"),
            ],
            app_folders=[TARGET],
        )
        self.server.children = {FOLDER: [], TARGET: [], LOCKED: [], OTHER: []}
        self.downloads = tmp_path / "downloads"
        self.outputs = tmp_path / "outputs"
        self.sleeps = []
        self.execute = partial(client.execute, sleep=self.sleeps.append)

    # Drive -----------------------------------------------------------------

    def _content(self, item, data, version):
        item.update(
            size=str(len(data)),
            md5Checksum=hashlib.md5(data).hexdigest(),
            sha256Checksum=hashlib.sha256(data).hexdigest(),
            version=str(version),
            headRevisionId=f"rev-{version}",
            modifiedTime=f"2026-10-2{version}T10:00:00.000Z",
        )
        self.server.media[item["id"]] = data

    def add(self, file_id, name, color=RED):
        item = {
            "id": file_id, "name": name, "mimeType": "image/jpeg",
            "parents": [FOLDER], "trashed": False,
            "capabilities": {"canDownload": True},
        }
        self._content(item, product(color), 1)
        self.server.items[file_id] = item
        self.server.children[FOLDER].append(item)

    def change(self, file_id, color):
        item = self.server.items[file_id]
        self._content(item, product(color), int(item["version"]) + 1)

    def rename(self, file_id, name):
        item = self.server.items[file_id]
        item["name"] = name
        item["version"] = str(int(item["version"]) + 1)
        item["modifiedTime"] = f"2026-10-2{item['version']}T11:00:00.000Z"

    def corrupt_media(self, file_id):
        """Drive entrega bytes distintos de los declarados (MD5 falla)."""

        data = self.server.media[file_id]
        self.server.media[file_id] = bytes(reversed(data))

    def remove(self, file_id):
        item = self.server.items.pop(file_id)
        self.server.children[FOLDER].remove(item)

    # Ejecución ---------------------------------------------------------------

    def run(self, **kwargs):
        kwargs.setdefault("target_folder_id", TARGET)
        kwargs.setdefault("limit", None)
        return sync.sync(
            self.server.read, self.server.write,
            folder_id=FOLDER, downloads_dir=self.downloads, output_dir=self.outputs,
            execute=self.execute, sleep=self.sleeps.append, **kwargs,
        )

    # Observación -------------------------------------------------------------

    def remote(self, target=TARGET):
        return [item for item in self.server.children[target] if item["name"].endswith(".png")]

    def write_operations(self):
        return [operation for operation, _ in self.server.write.calls]

    def creates(self):
        return [kwargs for operation, kwargs in self.server.write.calls if operation == "files.create"]

    def media_ids(self):
        return [file_id for file_id, _ in self.server.media_requests]

    def manifest_bytes(self):
        path = self.outputs / MANIFEST_NAME
        return path.read_bytes() if path.exists() else None

    def local_state(self):
        state = {}
        for root in (self.downloads, self.outputs):
            if root.exists():
                for path in sorted(root.rglob("*")):
                    state[str(path)] = (
                        path.read_bytes() if path.is_file() else None,
                        path.stat().st_mtime_ns,
                    )
        return state

    def remote_state(self):
        return (
            copy.deepcopy(self.server.items),
            copy.deepcopy(self.server.children),
            list(self.server.generated),
            list(self.server.created),
        )

    def published_downloads(self):
        if not self.downloads.exists():
            return []
        return sorted(
            path.name for path in self.downloads.iterdir()
            if path.is_file() and not path.name.startswith(".")
        )


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def by_id(summary):
    return {item.file_id: item for item in summary.plan}


def two_originals(env):
    env.add("1A", "a.jpg", RED)
    env.add("1B", "b.jpg", BLUE)


# Primera sincronización y repetición --------------------------------------

def test_first_sync_downloads_processes_and_uploads(env, ai):
    two_originals(env)

    summary = env.run()

    assert summary.complete
    assert [item.file_id for item in summary.plan] == ["1A", "1B"]
    assert [item.status for item in summary.plan] == [sync.COMPLETE] * 2
    assert sorted(item["name"] for item in env.remote()) == [
        "TRANSFORMADA_a.png", "TRANSFORMADA_b.png",
    ]
    assert ai["prepare"] == 1

    for item in summary.plan:
        assert item.download.status == downloads.DOWNLOADED
        assert item.revision.local_name == downloads.local_name(item.original.name, item.file_id)
        assert item.result.identity.source_key == item.file_id
        assert item.result.identity.source_sha256 == item.revision.sha256
        assert item.upload.status == uploads.UPLOADED
        assert item.upload.output_name == item.result.output_name

    # Nunca se crean carpetas: solo PNG.
    assert {kwargs["body"]["mimeType"] for kwargs in env.creates()} == {"image/png"}


def test_second_sync_without_changes_does_nothing_new(env, ai):
    two_originals(env)
    env.run()
    media, created, generated = len(env.media_ids()), list(env.server.created), list(env.server.generated)
    ai["prepare"] = ai["cutout"] = 0
    manifest = env.manifest_bytes()

    summary = env.run()

    assert summary.complete
    assert len(env.media_ids()) == media
    assert env.server.created == created and env.server.generated == generated
    assert ai == {"prepare": 0, "cutout": 0}
    assert env.manifest_bytes() == manifest
    for item in summary.plan:
        assert item.download.status == downloads.VERIFIED
        assert item.processing == batch.VERIFIED
        assert item.upload.status == uploads.VERIFIED


def test_new_original_is_the_only_one_processed(env, ai):
    two_originals(env)
    env.run()
    media = len(env.media_ids())
    ai["prepare"] = ai["cutout"] = 0

    env.add("1C", "c.jpg", GREEN)
    summary = env.run()

    assert summary.complete
    assert env.media_ids()[media:] == ["1C"]
    assert ai["cutout"] == 1
    statuses = {item.file_id: item.upload.status for item in summary.plan}
    assert statuses == {"1A": uploads.VERIFIED, "1B": uploads.VERIFIED, "1C": uploads.UPLOADED}


def test_modified_original_gets_a_new_result_and_reports_the_old_one(env):
    env.add("1A", "a.jpg", RED)
    env.run()
    [old] = env.remote()
    old_state = copy.deepcopy(old)

    env.change("1A", BLUE)
    summary = env.run()

    [item] = summary.plan
    assert item.status == sync.COMPLETE
    assert item.download.status == downloads.DOWNLOADED
    assert ".rev-" in item.revision.local_name
    assert item.upload.status == uploads.UPLOADED
    assert item.upload.remote_name.startswith("TRANSFORMADA_a__rev-")
    assert [(related.id, related.kind) for related in item.related] == [
        (old["id"], uploads.PREVIOUS_REVISION),
    ]
    # El PNG anterior no se modifica ni se elimina.
    assert env.server.items[old["id"]] == old_state
    assert len(env.remote()) == 2


# Caso A: la descarga de la revisión nueva falla -----------------------------

def test_case_a_failed_download_never_falls_back_to_the_old_revision(env, ai):
    env.add("1A", "a.jpg", RED)
    env.run()
    manifest = env.manifest_bytes()
    old_copy = env.published_downloads()
    ai["prepare"] = ai["cutout"] = 0

    env.change("1A", BLUE)
    env.corrupt_media("1A")
    remote = env.remote_state()
    summary = env.run()

    [item] = summary.plan
    assert item.status == sync.FAILED
    assert item.stage == sync.DOWNLOAD
    assert "MD5" in item.reason
    assert item.revision is None and item.result is None and item.upload is None
    # Ni procesamiento ni subida de la revisión anterior.
    assert ai == {"prepare": 0, "cutout": 0}
    assert env.manifest_bytes() == manifest
    assert env.remote_state() == remote
    assert env.published_downloads() == old_copy
    assert not summary.complete


# Caso B: varias revisiones y configuraciones del mismo fileId -------------

def test_case_b_only_the_selected_revision_and_configuration_is_used(env):
    transparent = OutputOptions(background="transparent")
    env.add("1A", "a.jpg", RED)
    env.run()                                   # revisión 1, blanco
    env.run(options=transparent)                # revisión 1, transparente
    env.change("1A", BLUE)
    env.run(options=transparent)                # revisión 2, transparente
    before = {item["id"]: copy.deepcopy(item) for item in env.remote()}
    created = len(env.server.created)

    summary = env.run()                         # revisión 2, blanco

    [item] = summary.plan
    assert item.status == sync.COMPLETE
    assert ".rev-" in item.revision.local_name
    assert item.result.identity.source_sha256 == item.revision.sha256
    assert (item.result.identity.background, item.result.identity.width) == ("white", 400)

    # Solo se sube el resultado de la revisión 2 en blanco.
    assert len(env.server.created) == created + 1
    [new] = [kwargs for kwargs in env.creates()][-1:]
    assert new["body"]["appProperties"]["dip_source_sha256"] == item.revision.sha256
    assert summary.upload.items == [item.upload]
    assert item.upload.output_name == item.result.output_name

    kinds = sorted(related.kind for related in item.related)
    assert kinds == [
        uploads.OTHER_CONFIGURATION, uploads.PREVIOUS_REVISION, uploads.PREVIOUS_REVISION,
    ]
    # Los PNG anteriores no cambian.
    for remote_id, state in before.items():
        assert env.server.items[remote_id] == state


# Caso C: el original cambia entre el listado y la descarga --------------

def test_case_c_content_change_after_listing_is_not_mixed_into_the_plan(env, monkeypatch, ai):
    two_originals(env)
    real_list = sync.queries.list_images

    def list_then_change(*args, **kwargs):
        listing = real_list(*args, **kwargs)
        env.change("1A", GREEN)          # cambia después del listado
        return listing

    monkeypatch.setattr(sync.queries, "list_images", list_then_change)

    summary = env.run()

    item = by_id(summary)["1A"]
    assert item.status == sync.FAILED and item.stage == sync.DOWNLOAD
    assert "después del listado" in item.reason
    assert "1A" not in env.media_ids()
    assert item.revision is None
    assert by_id(summary)["1B"].status == sync.COMPLETE
    assert [remote["name"] for remote in env.remote()] == ["TRANSFORMADA_b.png"]

    monkeypatch.setattr(sync.queries, "list_images", real_list)
    again = env.run()
    assert again.complete
    assert by_id(again)["1A"].revision.md5_checksum == env.server.items["1A"]["md5Checksum"]


# Renombres -----------------------------------------------------------------

def test_rename_with_identical_content_reuses_everything(env, ai):
    env.add("1A", "a.jpg", RED)
    first = env.run()
    [remote] = env.remote()
    remote_state = copy.deepcopy(remote)
    media, created = len(env.media_ids()), list(env.server.created)
    ai["prepare"] = ai["cutout"] = 0

    env.rename("1A", "a-renombrada.jpg")
    summary = env.run()

    [item] = summary.plan
    assert item.status == sync.COMPLETE
    assert item.download.status == downloads.VERIFIED
    assert item.revision.local_name == first.plan[0].revision.local_name
    assert any("renombró" in note for note in item.notes)
    assert len(env.media_ids()) == media
    assert ai == {"prepare": 0, "cutout": 0}
    assert env.server.created == created
    assert item.upload.status == uploads.VERIFIED
    # El PNG remoto conserva su nombre: nunca se renombra.
    assert env.server.items[remote["id"]] == remote_state
    assert remote_state["name"] == "TRANSFORMADA_a.png"


def test_rename_with_content_change_downloads_and_uploads_a_new_result(env):
    env.add("1A", "a.jpg", RED)
    env.run()
    [old] = env.remote()
    old_state = copy.deepcopy(old)

    env.rename("1A", "a-nueva.jpg")
    env.change("1A", BLUE)
    summary = env.run()

    [item] = summary.plan
    assert item.download.status == downloads.DOWNLOADED
    assert item.revision.local_name == downloads.local_name("a-nueva.jpg", "1A")
    assert item.upload.status == uploads.UPLOADED
    assert item.upload.remote_name == "TRANSFORMADA_a-nueva.png"
    assert [related.kind for related in item.related] == [uploads.PREVIOUS_REVISION]
    assert env.server.items[old["id"]] == old_state
    assert downloads.local_name("a.jpg", "1A") in env.published_downloads()


# Límite y orden ----------------------------------------------------------

def test_limit_is_applied_once_and_a_failed_photo_is_not_replaced(env):
    for file_id, name in (("1D", "d.jpg"), ("1B", "b.jpg"), ("1C", "c.jpg"), ("1A", "a.jpg")):
        env.add(file_id, name)
    env.corrupt_media("1A")

    summary = env.run(limit=2)

    assert [item.file_id for item in summary.plan] == ["1A", "1B"]
    assert by_id(summary)["1A"].status == sync.FAILED
    assert by_id(summary)["1B"].status == sync.COMPLETE
    assert set(env.media_ids()) == {"1A", "1B"}
    assert [remote["name"] for remote in env.remote()] == ["TRANSFORMADA_b.png"]
    assert summary.upload.items == [by_id(summary)["1B"].upload]


def test_upload_stage_only_uses_results_of_the_plan(env):
    two_originals(env)
    # 1A se sube; la subida de 1B se rechaza y detiene el lote.
    env.server.write.upload_failures = [None, http_error(403, ["insufficientFilePermissions"])]
    first = env.run()
    assert by_id(first)["1B"].status == sync.FAILED
    env.server.write.upload_failures = []
    created = list(env.server.created)

    # Con el límite en 1, el resultado local de 1B queda fuera del plan.
    summary = env.run(limit=1)

    assert [item.file_id for item in summary.plan] == ["1A"]
    assert env.server.created == created
    assert [item.file_id for item in summary.upload.items] == ["1A"]


def test_invalid_limit(env):
    with pytest.raises(ValueError):
        env.run(limit=0)


# Interrupciones y recuperación ----------------------------------------------

def test_download_with_pending_record_is_recovered_next_run(env, monkeypatch):
    two_originals(env)
    real_write = downloads._write_record
    state = {"failed": False}

    def flaky(output_dir, name, record):
        if record["file_id"] == "1A" and record["status"] == "completed" and not state["failed"]:
            state["failed"] = True
            raise PermissionError(errno.EACCES, "denegado")
        return real_write(output_dir, name, record)

    with monkeypatch.context() as patch:
        patch.setattr(downloads, "_write_record", flaky)
        summary = env.run()

    assert by_id(summary)["1A"].status == sync.PENDING
    assert by_id(summary)["1B"].status == sync.COMPLETE
    assert summary.stopped is None
    media_for_a = env.media_ids().count("1A")

    again = env.run()

    assert again.complete
    assert by_id(again)["1A"].download.status == downloads.RECOVERED
    assert env.media_ids().count("1A") == media_for_a


def test_interrupted_processing_is_resumed(env, monkeypatch, ai):
    two_originals(env)
    state = {"calls": 0}

    def interrupted(image, model_name):
        state["calls"] += 1
        if state["calls"] == 2:
            raise KeyboardInterrupt
        return fake_cutout(image, model_name)

    with monkeypatch.context() as patch:
        patch.setattr(pipeline, "remove_background", interrupted)
        with pytest.raises(KeyboardInterrupt):
            env.run()

    assert env.remote() == []
    ai["cutout"] = 0

    summary = env.run()

    assert summary.complete
    assert ai["cutout"] == 1
    assert len(env.remote()) == 2
    assert len(env.server.created) == 2


def test_uncertain_upload_is_resolved_without_duplicates(env):
    env.add("1A", "a.jpg")
    env.server.write.upload_failures = [ConnectionResetError()] * 10

    summary = env.run()

    [item] = summary.plan
    assert item.status == sync.PENDING and item.stage == sync.UPLOAD
    reserved = list(env.server.generated)

    env.server.write.upload_failures = []
    again = env.run()

    assert again.complete
    assert by_id(again)["1A"].upload.remote_id == reserved[0]
    assert env.server.generated == reserved
    assert len(env.remote()) == 1


# Errores que detienen la sincronización -----------------------------------

def response(error):
    return error.resp, error.content


@pytest.mark.parametrize(
    "failure, error",
    [
        (response(http_error(403, ["dailyLimitExceeded"])), DriveQuotaError),
        (response(http_error(401)), DriveAuthError),
        (ssl.SSLCertVerificationError("certificado"), DriveCertificateError),
    ],
)
def test_fatal_download_errors_stop_the_sync(env, ai, failure, error):
    two_originals(env)
    env.server.media_failures = [failure]

    summary = env.run()

    assert isinstance(summary.stopped, error)
    assert summary.stopped_stage == sync.DOWNLOAD
    assert by_id(summary)["1B"].status == sync.NOT_STARTED
    assert env.media_ids() == ["1A"]
    assert ai["prepare"] == 0
    assert not {"files.create", "files.generateIds"} & set(env.write_operations())


def test_a_stop_during_downloads_keeps_finished_downloads_unprocessed(env, ai):
    two_originals(env)
    # 1A se descarga bien; la descarga de 1B agota la cuota.
    env.server.media_failures = [None, response(http_error(403, ["dailyLimitExceeded"]))]

    summary = env.run()

    assert isinstance(summary.stopped, DriveQuotaError)
    a = by_id(summary)["1A"]
    assert a.revision is not None
    assert (a.status, a.stage) == (sync.NOT_STARTED, sync.PROCESSING)
    assert ai["prepare"] == 0 and ai["cutout"] == 0
    assert not env.outputs.exists()
    assert env.server.created == [] and env.server.generated == []


def test_quota_during_upload_stops_the_remaining_uploads(env):
    two_originals(env)
    env.server.write.upload_failures = [http_error(403, ["dailyLimitExceeded"])]

    summary = env.run()

    assert isinstance(summary.stopped, DriveQuotaError)
    assert summary.stopped_stage == sync.UPLOAD
    assert by_id(summary)["1A"].status == sync.FAILED
    assert by_id(summary)["1B"].status == sync.NOT_STARTED
    assert env.server.created == []


def test_partial_failures_do_not_stop_independent_photos(env):
    two_originals(env)
    env.corrupt_media("1A")

    summary = env.run()

    assert summary.stopped is None
    assert by_id(summary)["1A"].status == sync.FAILED
    assert by_id(summary)["1B"].status == sync.COMPLETE
    assert not summary.complete


def test_model_initialization_error_stops_before_uploading(env, monkeypatch):
    two_originals(env)

    def failing(model_name):
        raise ModelInitializationError("sin caché de modelos")

    monkeypatch.setattr(batch, "prepare_model", failing)

    summary = env.run()

    assert isinstance(summary.stopped, ModelInitializationError)
    assert summary.stopped_stage == sync.PROCESSING
    assert all(item.status == sync.NOT_STARTED for item in summary.plan)
    assert env.server.created == [] and env.server.generated == []


# Disco local lleno -----------------------------------------------------------

def enospc():
    return OSError(errno.ENOSPC, "No space left on device")


def windows_disk_full():
    error = OSError(None, "disco lleno")
    error.winerror = 112        # ERROR_DISK_FULL, sin errno
    return error


def windows_handle_disk_full():
    error = OSError(errno.EINVAL, "disco lleno")
    error.winerror = 39         # ERROR_HANDLE_DISK_FULL: Python asigna EINVAL
    return error


class _FailingFile:
    def __init__(self, file, error):
        self._file = file
        self._error = error

    def write(self, data):
        raise self._error

    def __getattr__(self, name):
        return getattr(self._file, name)

    def __enter__(self):
        self._file.__enter__()
        return self

    def __exit__(self, *exc):
        return self._file.__exit__(*exc)


def fail_temp_writes(patch, error, times=None):
    real_fdopen = os.fdopen
    state = {"left": times}

    def fdopen(fd, mode="r", *args, **kwargs):
        file = real_fdopen(fd, mode, *args, **kwargs)
        if "w" in mode and (state["left"] is None or state["left"] > 0):
            if state["left"] is not None:
                state["left"] -= 1
            return _FailingFile(file, error)
        return file

    patch.setattr(downloads.os, "fdopen", fdopen)


@pytest.mark.parametrize("make_error", [enospc, windows_disk_full, windows_handle_disk_full])
def test_disk_full_while_writing_the_temp_stops_the_sync(env, monkeypatch, ai, make_error):
    two_originals(env)

    with monkeypatch.context() as patch:
        fail_temp_writes(patch, make_error())
        summary = env.run()

    stopped = summary.stopped
    assert isinstance(stopped, downloads.LocalDiskFullError)
    assert (stopped.file_id, stopped.phase) == ("1A", downloads.PHASE_TEMP)
    assert summary.stopped_stage == sync.DOWNLOAD
    item = by_id(summary)["1A"]
    assert item.status == sync.FAILED and "espacio" in item.reason
    assert by_id(summary)["1B"].status == sync.NOT_STARTED
    assert env.media_ids() == ["1A"]
    # Ni descarga publicada ni temporales; ni procesamiento ni subida.
    assert env.published_downloads() == []
    assert list(env.downloads.glob(".*.tmp")) == []
    assert ai["prepare"] == 0
    assert not env.outputs.exists()
    assert not {"files.create", "files.generateIds"} & set(env.write_operations())


def test_disk_full_on_the_completed_record_is_recoverable(env, monkeypatch, ai):
    two_originals(env)
    real_write = downloads._write_record

    def full_on_completion(output_dir, name, record):
        if record["status"] == "completed":
            raise enospc()
        return real_write(output_dir, name, record)

    with monkeypatch.context() as patch:
        patch.setattr(downloads, "_write_record", full_on_completion)
        summary = env.run()

    stopped = summary.stopped
    assert isinstance(stopped, downloads.LocalDiskFullError)
    assert (stopped.file_id, stopped.phase) == ("1A", downloads.PHASE_COMPLETED_RECORD)
    name = downloads.local_name("a.jpg", "1A")
    assert env.published_downloads() == [name]
    record = json.loads((env.downloads / ".drive" / f"{name}.json").read_text("utf-8"))
    assert record["status"] == "pending"
    assert by_id(summary)["1A"].status == sync.PENDING
    assert by_id(summary)["1B"].status == sync.NOT_STARTED
    assert env.media_ids() == ["1A"]
    assert ai["prepare"] == 0 and env.server.created == []

    again = env.run()

    assert again.complete
    assert by_id(again)["1A"].download.status == downloads.RECOVERED
    assert env.media_ids().count("1A") == 1
    assert sorted(env.published_downloads()) == sorted([name, downloads.local_name("b.jpg", "1B")])
    assert len(env.remote()) == 2


def test_other_local_errors_are_not_disk_full(env, monkeypatch):
    two_originals(env)

    with monkeypatch.context() as patch:
        fail_temp_writes(patch, PermissionError(errno.EACCES, "denegado"), times=1)
        summary = env.run()

    assert summary.stopped is None
    assert by_id(summary)["1A"].status == sync.FAILED
    assert by_id(summary)["1B"].status == sync.COMPLETE


# Destino sin acceso para drive.file ---------------------------------------

def test_target_without_drive_file_access_stops_before_downloading(env, ai):
    two_originals(env)

    with pytest.raises(DriveWriteAccessError):
        env.run(target_folder_id=LOCKED)

    assert env.media_ids() == []
    assert not env.downloads.exists() and not env.outputs.exists()
    assert ai["prepare"] == 0


def test_permission_403_on_the_target_stops_before_downloading(env):
    two_originals(env)
    env.server.write.failures = [http_error(403, ["insufficientFilePermissions"])]

    with pytest.raises(DriveWriteAccessError):
        env.run()

    assert env.media_ids() == []


def test_dry_run_reports_a_target_without_access(env):
    two_originals(env)

    summary = env.run(target_folder_id=LOCKED, dry_run=True)

    assert summary.target.write_access is False
    assert "drive.file" in summary.target.note
    assert all(item.status == sync.PLANNED for item in summary.plan)


# Vista previa ----------------------------------------------------------------

def old_temp(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"temporal abandonado")
    past = time.time() - 3 * 24 * 3600
    os.utime(path, (past, past))
    return path


def test_dry_run_of_a_new_folder_writes_nothing(env, ai):
    two_originals(env)
    remote = env.remote_state()

    summary = env.run(dry_run=True)

    assert [item.status for item in summary.plan] == [sync.PLANNED] * 2
    for item in summary.plan:
        assert item.actions == [sync.DOWNLOAD]
        assert item.undetermined == [sync.PROCESSING, sync.UPLOAD]
    assert not env.downloads.exists() and not env.outputs.exists()
    assert ai["prepare"] == 0
    assert env.remote_state() == remote
    assert set(env.write_operations()) <= {"files.get", "files.list"}


def test_dry_run_after_changes_writes_nothing(env, ai):
    two_originals(env)
    env.run()
    env.change("1A", GREEN)
    temp = old_temp(env.downloads, ".a--1~a.jpg.abcdefgh.tmp")
    manifest_temp = old_temp(env.outputs, f"{MANIFEST_NAME}.abcdefgh.tmp")
    local = env.local_state()
    remote = env.remote_state()
    ai["prepare"] = ai["cutout"] = 0

    summary = env.run(dry_run=True)

    a, b = by_id(summary)["1A"], by_id(summary)["1B"]
    assert a.status == sync.PLANNED and a.download.status == downloads.PLANNED
    assert a.undetermined == [sync.PROCESSING, sync.UPLOAD]
    assert b.status == sync.COMPLETE
    # Nada cambió: ni archivos, ni temporales, ni Drive, ni el modelo.
    assert env.local_state() == local
    assert temp.exists() and manifest_temp.exists()
    assert env.remote_state() == remote
    assert ai == {"prepare": 0, "cutout": 0}


def test_dry_run_with_a_new_configuration_plans_processing(env, ai):
    env.add("1A", "a.jpg")
    env.run()
    local = env.local_state()

    summary = env.run(dry_run=True, options=OutputOptions(background="transparent"))

    [item] = summary.plan
    assert item.status == sync.PLANNED
    assert item.download.status == downloads.VERIFIED
    assert item.actions == [sync.PROCESSING]
    assert item.undetermined == [sync.UPLOAD]
    assert env.local_state() == local


def test_dry_run_without_write_authorization(env):
    two_originals(env)

    summary = sync.sync(
        env.server.read, None, folder_id=FOLDER, downloads_dir=env.downloads,
        output_dir=env.outputs, target_folder_id=TARGET, limit=None,
        dry_run=True, execute=env.execute,
    )

    assert summary.target.write_access is None
    assert env.server.write.calls == []


def test_real_run_requires_the_write_authorization(env):
    with pytest.raises(DriveAuthError, match="auth-write"):
        sync.sync(
            env.server.read, None, folder_id=FOLDER, downloads_dir=env.downloads,
            output_dir=env.outputs, target_folder_id=TARGET, limit=None,
        )


# Advertencias: lo que no pertenece al plan --------------------------------

def test_loose_files_and_deleted_originals_are_warnings(env):
    two_originals(env)
    env.run()
    env.remove("1B")
    (env.downloads / "suelta.jpg").write_bytes(product())

    summary = env.run()

    assert summary.complete
    assert [item.file_id for item in summary.plan] == ["1A"]
    assert any("procedencia" in warning for warning in summary.warnings)
    assert any("ya no están" in warning for warning in summary.warnings)
    assert (env.downloads / "suelta.jpg").read_bytes() == product()


# Validación local previa ---------------------------------------------------

def test_damaged_manifest_stops_before_any_drive_call(env):
    env.add("1A", "a.jpg")
    env.outputs.mkdir()
    (env.outputs / MANIFEST_NAME).write_text("{dañado", "utf-8")

    with pytest.raises(sync.ManifestError):
        env.run()

    assert env.server.read.calls == [] and env.server.write.calls == []


# CLI ---------------------------------------------------------------------

def make_credentials(scopes, expired=False):
    offset = timedelta(hours=-1 if expired else 1)
    return Credentials(
        token=ACCESS, refresh_token=REFRESH,
        token_uri="https://oauth2.googleapis.com/token",
        client_id="cliente.apps.googleusercontent.com",
        client_secret="SECRETO-CLIENTE",
        scopes=list(scopes),
        expiry=datetime.now(timezone.utc).replace(tzinfo=None) + offset,
    )


@pytest.fixture
def cli_env(env, monkeypatch, tmp_path):
    paths = auth.secret_paths(tmp_path / ".secrets")
    auth.save_token(make_credentials([auth.READONLY_SCOPE]), paths.token)
    auth.save_token(make_credentials([auth.WRITE_SCOPE]), paths.write_token)
    monkeypatch.setenv(auth.SECRETS_DIR_ENV, str(paths.directory))

    def build(credentials):
        return env.server.write if auth.WRITE_SCOPE in credentials.scopes else env.server.read

    monkeypatch.setattr(client, "build_service", build)
    monkeypatch.setattr(client, "execute", env.execute)
    monkeypatch.setattr(uploads.time, "sleep", env.sleeps.append)
    env.paths = paths
    return env


def sync_cli(env, *extra, selection=("--all",), target=TARGET):
    arguments = [
        "sync", "--folder-id", FOLDER,
        "--downloads", str(env.downloads), "--output", str(env.outputs),
        *(("--target-folder-id", target) if target else ()),
        *selection, *extra,
    ]
    return drive_cli.main(arguments)


def test_cli_complete_and_repeated_sync(cli_env, capsys):
    two_originals(cli_env)

    assert sync_cli(cli_env) == 0
    out = capsys.readouterr().out
    assert "RESUMEN DE LA SINCRONIZACIÓN" in out
    assert "PNG subidos ahora:          2" in out

    assert sync_cli(cli_env, selection=("--limit", "2")) == 0
    assert "PNG subidos ahora:          0" in capsys.readouterr().out
    assert len(cli_env.server.created) == 2


def test_cli_partial_sync_exits_with_6(cli_env, capsys):
    two_originals(cli_env)
    cli_env.corrupt_media("1A")

    assert sync_cli(cli_env) == 6
    assert "Sincronización parcial" in capsys.readouterr().out


@pytest.mark.parametrize(
    "setup, code, message",
    [
        (lambda e: e.server.media_failures.append(response(http_error(403, ["dailyLimitExceeded"]))), 5, "cuota"),
        (lambda e: e.server.media_failures.append(ssl.SSLCertVerificationError("x")), 5, "certificado"),
        (lambda e: e.server.media_failures.append(response(http_error(401))), 3, "autoriz"),
        (lambda e: e.server.write.failures.append(http_error(403, ["insufficientFilePermissions"])), 4, "403"),
    ],
)
def test_cli_fatal_error_codes(cli_env, capsys, setup, code, message):
    two_originals(cli_env)
    setup(cli_env)

    assert sync_cli(cli_env) == code
    _, err = capsys.readouterr()
    assert message in err
    assert cli_env.server.created == []
    for secret in (ACCESS, REFRESH, "SECRETO-CLIENTE"):
        assert secret not in err


def test_cli_disk_full_exits_with_9(cli_env, capsys, monkeypatch):
    two_originals(cli_env)
    fail_temp_writes(monkeypatch, enospc())

    assert sync_cli(cli_env) == 9
    err = capsys.readouterr().err
    assert "espacio" in err and "a.jpg" in err and "descarga" in err


def test_cli_model_initialization_error_exits_with_7(cli_env, monkeypatch):
    two_originals(cli_env)

    def failing(model_name):
        raise ModelInitializationError("sin caché")

    monkeypatch.setattr(batch, "prepare_model", failing)

    assert sync_cli(cli_env) == 7
    assert cli_env.server.created == []


def test_cli_target_without_access_exits_with_4(cli_env, capsys):
    two_originals(cli_env)

    assert sync_cli(cli_env, target=LOCKED) == 4
    assert "drive.file" in capsys.readouterr().err
    assert cli_env.media_ids() == []


def test_cli_damaged_manifest_exits_with_8(cli_env):
    cli_env.add("1A", "a.jpg")
    cli_env.outputs.mkdir()
    (cli_env.outputs / MANIFEST_NAME).write_text("{", "utf-8")

    assert sync_cli(cli_env) == 8
    assert cli_env.server.read.calls == []


def test_cli_missing_write_authorization(cli_env, capsys):
    two_originals(cli_env)
    cli_env.paths.write_token.unlink()

    assert sync_cli(cli_env) == 3
    assert sync_cli(cli_env, "--dry-run") == 0
    out = capsys.readouterr().out
    assert "auth-write" in out and "no verificado" in out


@pytest.mark.parametrize(
    "selection",
    [(), ("--limit", "1", "--all")],
)
def test_cli_requires_exactly_one_of_limit_and_all(cli_env, selection):
    with pytest.raises(SystemExit) as error:
        sync_cli(cli_env, selection=selection)
    assert error.value.code == 2


def test_cli_requires_the_target_folder(cli_env):
    with pytest.raises(SystemExit) as error:
        sync_cli(cli_env, target=None)
    assert error.value.code == 2


@pytest.mark.parametrize("arguments", [("--limit", "0"), ("--max-file-mb", "0", "--all"), ("--width", "10", "--all")])
def test_cli_invalid_arguments(cli_env, arguments):
    selection = () if arguments[0] == "--limit" else ()
    assert sync_cli(cli_env, *arguments, selection=selection) == 2
    assert cli_env.server.read.calls == []


def test_cli_folders_inside_the_secrets_directory_are_refused(cli_env):
    cli_env.downloads = cli_env.paths.directory / "descargas"

    assert sync_cli(cli_env) == 2
    assert not cli_env.downloads.exists()


def test_cli_dry_run_shows_known_and_undetermined_actions(cli_env, capsys):
    two_originals(cli_env)
    local = cli_env.local_state()
    remote = cli_env.remote_state()

    assert sync_cli(cli_env, "--dry-run") == 0

    out = capsys.readouterr().out
    assert "VISTA PREVIA" in out
    assert "no determinable sin ejecutar: procesamiento, subida" in out
    assert "No determinables:           2" in out
    assert cli_env.local_state() == local
    assert cli_env.remote_state() == remote


# Tokens: persistencia en ejecución real, ninguna en la vista previa -------

def test_tokens_persist_only_in_real_runs(cli_env, monkeypatch):
    two_originals(cli_env)
    auth.save_token(make_credentials([auth.READONLY_SCOPE], expired=True), cli_env.paths.token)
    auth.save_token(make_credentials([auth.WRITE_SCOPE], expired=True), cli_env.paths.write_token)
    before = (cli_env.paths.token.read_bytes(), cli_env.paths.write_token.read_bytes())

    def refresh(self, request):
        self.token = "ya29.RENOVADO"
        self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", refresh)

    assert sync_cli(cli_env, "--dry-run") == 0
    assert (cli_env.paths.token.read_bytes(), cli_env.paths.write_token.read_bytes()) == before

    assert sync_cli(cli_env) == 0
    after = (cli_env.paths.token.read_bytes(), cli_env.paths.write_token.read_bytes())
    assert after[0] != before[0] and after[1] != before[1]
    assert all(b"RENOVADO" in token for token in after)


def test_persist_flags_follow_the_mode(cli_env, monkeypatch):
    two_originals(cli_env)
    seen = []
    real = auth.get_credentials

    def spy(paths, **kwargs):
        seen.append((kwargs.get("profile"), kwargs.get("persist")))
        return real(paths, **kwargs)

    monkeypatch.setattr(auth, "get_credentials", spy)

    sync_cli(cli_env, "--dry-run")
    sync_cli(cli_env)

    assert seen == [
        (auth.READ, False), (auth.WRITE, False),
        (auth.READ, True), (auth.WRITE, True),
    ]


# Acceso denegado durante la descarga: detiene la sincronización ---------

def deny_metadata(env, file_id):
    # files.get del original responde 403; el listado no cambia.
    env.server.items[file_id] = http_error(403, ["insufficientFilePermissions"])


def deny_content(env, file_id):
    env.server.media_failures = [response(http_error(403, ["insufficientFilePermissions"]))]


def metadata_requests(env, file_id):
    return [
        kwargs for operation, kwargs in env.server.read.calls
        if operation == "files.get" and kwargs.get("fileId") == file_id
    ]


@pytest.mark.parametrize("deny", [deny_metadata, deny_content], ids=["metadatos", "contenido"])
def test_access_denied_during_download_stops_the_sync(env, ai, deny):
    two_originals(env)
    deny(env, "1A")

    summary = env.run()

    assert isinstance(summary.stopped, DriveAccessError)
    assert not isinstance(summary.stopped, DriveWriteAccessError)
    assert "403" in str(summary.stopped)
    assert summary.stopped_stage == sync.DOWNLOAD
    # La segunda imagen no se consulta ni se descarga.
    assert metadata_requests(env, "1B") == []
    assert "1B" not in env.media_ids()
    assert by_id(summary)["1B"].status == sync.NOT_STARTED
    # Ni procesamiento ni subida.
    assert ai == {"prepare": 0, "cutout": 0}
    assert not {"files.generateIds", "files.create"} & set(env.write_operations())
    assert env.server.generated == [] and env.server.created == []
    assert not summary.complete


@pytest.mark.parametrize("deny", [deny_metadata, deny_content], ids=["metadatos", "contenido"])
def test_cli_access_denied_during_download_exits_with_4(cli_env, capsys, deny):
    two_originals(cli_env)
    deny(cli_env, "1A")

    assert sync_cli(cli_env) == 4

    out, err = capsys.readouterr()
    assert "La sincronización se detuvo en descarga" in err
    assert "403" in err
    assert "Sincronización parcial" not in out
    assert "1B" not in cli_env.media_ids()
    assert cli_env.server.created == []
