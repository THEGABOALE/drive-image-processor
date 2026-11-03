"""
Vista previa y ejecución sobre el motor real: Drive simulado
(FakeDriveServer, con las dos credenciales) e IA simulada. No hay red.
"""

import hashlib
import io
import json
import logging
import threading

import pytest
from conftest import DESKTOP_SECRET, MemoryTokenStore, read_credentials, write_credentials
from drive_fakes import FakeDriveServer, folder, http_error
from image_processor import batch, pipeline
from image_processor.background_removal import ModelInitializationError
from image_processor.google_drive import client as drive_client
from image_processor.google_drive import downloads, sync
from image_processor.manifest import MANIFEST_NAME
from PIL import Image, ImageDraw

from web.backend.app.connection import ConnectionManager, NotConnectedError
from web.backend.app.jobs import (
    FAILED,
    FINISHED,
    INTERRUPTED,
    JobInProgressError,
    JobManager,
    PREVIEW,
    PREVIEW_MAX_AGE_SECONDS,
    RUN,
    RUNNING,
    PreviewNotUsableError,
    folder_dirs,
)

SOURCE = "origen"
TARGET = "destino"


def product():
    image = Image.new("RGB", (300, 200), "white")
    ImageDraw.Draw(image).rectangle((100, 50, 200, 150), fill=(220, 30, 30))
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
    monkeypatch.setattr(drive_client.time, "sleep", lambda seconds: None)
    return calls


@pytest.fixture
def drive():
    server = FakeDriveServer(
        folders=[
            folder(SOURCE, "Originales"),
            folder(TARGET, "Resultados"),
            folder("ajena", "Ajena"),
        ],
        app_folders={SOURCE, TARGET},
    )
    server.children = {SOURCE: [], TARGET: [], "ajena": []}
    return server


def add_photo(drive, file_id, name="foto.jpg", parent=SOURCE):
    data = product()
    item = {
        "id": file_id, "name": f"{file_id}-{name}", "mimeType": "image/jpeg",
        "parents": [parent], "trashed": False,
        "capabilities": {"canDownload": True},
        "size": str(len(data)),
        "md5Checksum": hashlib.md5(data).hexdigest(),
        "sha256Checksum": hashlib.sha256(data).hexdigest(),
        "version": "1", "headRevisionId": "rev-1",
        "modifiedTime": "2026-10-21T10:00:00.000Z",
    }
    drive.media[file_id] = data
    drive.items[file_id] = item
    drive.children[parent].append(item)
    return item


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def connection(settings, drive):
    def service_factory(credentials):
        return drive.read if credentials.refresh_token == "refresh-lectura" else drive.write

    from conftest import FlowScript, fake_verify_id_token

    manager = ConnectionManager(
        settings,
        MemoryTokenStore(),
        flow_factory=FlowScript(read_credentials(), write_credentials()),
        verify_id_token=fake_verify_id_token,
        service_factory=service_factory,
        start_thread=lambda target: target(),
    )
    manager.start()
    return manager


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def jobs(settings, connection, clock):
    return JobManager(settings, connection, start_thread=lambda target: target(), now=clock)


def current(jobs):
    return jobs.current("sub-a")


def preview(jobs, source=SOURCE, target=TARGET):
    return jobs.start_preview(source, target)


def previewed_and_run(jobs, source=SOURCE, target=TARGET):
    job = preview(jobs, source, target)
    return jobs.start_run(job["id"])


def uploaded(drive, target=TARGET):
    return [
        item for item in drive.children[target]
        if item["name"].endswith(".png")
    ]


def items_by_id(job):
    return {item["file_id"]: item for item in job["summary"]["items"]}


# Vista previa ---------------------------------------------------------------

def test_preview_reports_the_plan_without_changing_anything(jobs, drive, settings, ai):
    add_photo(drive, "foto1")
    add_photo(drive, "foto2")

    job = preview(jobs)

    assert job["kind"] == PREVIEW
    assert job["status"] == FINISHED
    assert job["summary"]["counts"]["planned"] == 2
    assert job["summary"]["can_run"] is True
    assert items_by_id(job)["foto1"]["name"] == "foto1-foto.jpg"
    assert items_by_id(job)["foto1"]["status"] == "planned"
    assert items_by_id(job)["foto1"]["actions"] == ["download"]
    assert items_by_id(job)["foto1"]["undetermined"] == ["processing", "upload"]
    # Ni Drive, ni carpetas locales del motor, ni modelo.
    assert drive.created == []
    assert drive.media_requests == []
    assert not folder_dirs(settings, "sub-a", SOURCE)[0].exists()
    assert not folder_dirs(settings, "sub-a", SOURCE)[1].exists()
    assert ai["prepare"] == 0


def test_preview_shows_the_folder_names(jobs, drive):
    job = preview(jobs)

    assert job["source"] == {"id": SOURCE, "name": "Originales"}
    assert job["target"] == {"id": TARGET, "name": "Resultados"}
    assert job["same_folder"] is False


def test_empty_folder_has_nothing_to_run(jobs):
    job = preview(jobs)

    assert job["status"] == FINISHED
    assert job["summary"]["items"] == []
    assert job["summary"]["can_run"] is False
    with pytest.raises(PreviewNotUsableError) as caught:
        jobs.start_run(job["id"])
    assert caught.value.code == "nothing_to_run"


def test_preview_uses_defaults_of_the_engine_for_all_images(settings, connection):
    calls = []

    def fake_sync(read, write, **kwargs):
        calls.append(kwargs)
        return sync.sync(read, write, **kwargs)

    jobs = JobManager(
        settings, connection, start_thread=lambda t: t(), runner=fake_sync
    )
    preview(jobs)

    assert calls[0]["limit"] is None
    assert calls[0]["dry_run"] is True
    assert calls[0]["folder_id"] == SOURCE
    assert calls[0]["target_folder_id"] == TARGET
    assert "options" not in calls[0] and "model_name" not in calls[0]


# Ejecución -------------------------------------------------------------------

def test_run_downloads_processes_and_uploads(jobs, drive, ai):
    add_photo(drive, "foto1")
    add_photo(drive, "foto2")

    job = previewed_and_run(jobs)

    assert job["kind"] == RUN
    assert job["status"] == FINISHED
    assert job["summary"]["counts"]["complete"] == 2
    assert job["summary"]["uploaded_now"] == 2
    assert job["summary"]["complete"] is True
    assert job["summary"]["can_run"] is False
    assert len(uploaded(drive)) == 2
    assert ai["cutout"] == 2


def test_run_never_changes_or_deletes_the_originals(jobs, drive):
    add_photo(drive, "foto1")
    before = json.dumps(drive.items["foto1"], sort_keys=True)

    previewed_and_run(jobs)

    assert json.dumps(drive.items["foto1"], sort_keys=True) == before
    operations = {op for op, _ in drive.write.calls + drive.read.calls}
    assert not operations & {"files.update", "files.delete", "files.trash"}


def test_second_run_does_not_duplicate(jobs, drive, ai):
    add_photo(drive, "foto1")
    previewed_and_run(jobs)

    job = preview(jobs)
    assert job["summary"]["counts"]["complete"] == 1
    assert job["summary"]["can_run"] is False
    assert len(uploaded(drive)) == 1
    assert ai["cutout"] == 1


def test_only_the_new_photo_is_processed_later(jobs, drive, ai):
    add_photo(drive, "foto1")
    previewed_and_run(jobs)
    add_photo(drive, "foto2")

    job = previewed_and_run(jobs)

    assert job["summary"]["uploaded_now"] == 1
    assert job["summary"]["counts"]["complete"] == 2
    assert len(uploaded(drive)) == 2
    assert ai["cutout"] == 2


def test_same_folder_never_processes_its_own_results(jobs, drive, ai):
    add_photo(drive, "foto1")

    job = previewed_and_run(jobs, SOURCE, SOURCE)

    assert job["same_folder"] is True
    assert job["summary"]["uploaded_now"] == 1
    assert ai["cutout"] == 1

    again = preview(jobs, SOURCE, SOURCE)

    assert len(again["summary"]["items"]) == 1
    assert again["summary"]["can_run"] is False


def test_one_failed_photo_does_not_stop_the_others(jobs, drive):
    add_photo(drive, "foto1")
    add_photo(drive, "foto2")
    drive.media["foto1"] = bytes(reversed(drive.media["foto1"]))

    job = previewed_and_run(jobs)

    items = items_by_id(job)
    assert items["foto1"]["status"] == "failed"
    assert items["foto1"]["stage"] == "download"
    assert items["foto1"]["reason"]
    assert items["foto2"]["status"] == "complete"
    assert job["status"] == FINISHED
    assert job["summary"]["complete"] is False
    assert job["summary"]["counts"]["failed"] == 1


# Debe partir de una vista previa ---------------------------------------------

def test_run_requires_the_current_preview(jobs, drive):
    add_photo(drive, "foto1")
    preview(jobs)

    with pytest.raises(PreviewNotUsableError) as caught:
        jobs.start_run("a" * 32)

    assert caught.value.code == "preview_not_current"
    assert drive.created == []


def test_new_preview_replaces_the_previous(jobs, drive):
    add_photo(drive, "foto1")
    first = preview(jobs)
    preview(jobs)

    with pytest.raises(PreviewNotUsableError):
        jobs.start_run(first["id"])


def test_a_preview_runs_only_once(jobs, drive):
    add_photo(drive, "foto1")
    job = preview(jobs)
    jobs.start_run(job["id"])

    with pytest.raises(PreviewNotUsableError):
        jobs.start_run(job["id"])


def test_a_run_cannot_be_used_as_a_preview(jobs, drive):
    add_photo(drive, "foto1")
    job = previewed_and_run(jobs)

    with pytest.raises(PreviewNotUsableError) as caught:
        jobs.start_run(job["id"])

    assert caught.value.code == "preview_not_current"


def test_run_uses_the_folders_of_the_preview(jobs, drive):
    add_photo(drive, "foto1")
    job = preview(jobs, SOURCE, SOURCE)

    result = jobs.start_run(job["id"])

    assert result["source"]["id"] == SOURCE
    assert result["target"]["id"] == SOURCE
    assert uploaded(drive, SOURCE)
    assert uploaded(drive, TARGET) == []


def test_expired_preview_cannot_run(jobs, drive, clock):
    add_photo(drive, "foto1")
    job = preview(jobs)
    clock.now += PREVIEW_MAX_AGE_SECONDS + 1

    with pytest.raises(PreviewNotUsableError) as caught:
        jobs.start_run(job["id"])

    assert caught.value.code == "preview_expired"
    assert drive.created == []


def test_preview_just_within_the_limit_can_run(jobs, drive, clock):
    add_photo(drive, "foto1")
    job = preview(jobs)
    clock.now += PREVIEW_MAX_AGE_SECONDS - 1

    assert jobs.start_run(job["id"])["status"] == FINISHED


def test_failed_preview_cannot_run(jobs, drive):
    job = preview(jobs, "ajena", TARGET)

    assert job["status"] == FAILED
    with pytest.raises(PreviewNotUsableError):
        jobs.start_run(job["id"])


def test_preview_stopped_by_an_error_cannot_run(jobs, drive, monkeypatch):
    add_photo(drive, "foto1")
    real = downloads.download_folder

    def stopped(*args, **kwargs):
        result = real(*args, **kwargs)
        result.aborted = downloads.LocalDiskFullError("foto1", "foto1.jpg", downloads.PHASE_LOCAL)
        return result

    monkeypatch.setattr(downloads, "download_folder", stopped)
    job = preview(jobs)

    assert job["summary"]["stopped"]["code"] == "local_disk_full"
    assert job["summary"]["can_run"] is False


def test_only_previews_with_something_to_do_can_run():
    from web.backend.app.jobs import _can_run

    assert _can_run(PREVIEW, None, {"planned": 0, "pending": 1}) is True
    assert _can_run(PREVIEW, None, {"planned": 1, "pending": 0}) is True
    assert _can_run(PREVIEW, None, {"planned": 0, "pending": 0}) is False
    assert _can_run(RUN, None, {"planned": 1, "pending": 1}) is False
    assert _can_run(PREVIEW, {"code": "x"}, {"planned": 1, "pending": 0}) is False


# Permisos y cuenta -----------------------------------------------------------

def test_requires_a_connection(settings, make_manager):
    jobs = JobManager(settings, make_manager(), start_thread=lambda t: t())

    with pytest.raises(NotConnectedError):
        jobs.start_preview(SOURCE, TARGET)


def test_folder_not_authorized_for_the_account_fails_before_anything(jobs, drive):
    add_photo(drive, "foto1", parent="ajena")

    job = preview(jobs, "ajena", TARGET)

    assert job["status"] == FAILED
    assert job["error"]["code"] == "source_not_authorized"
    assert drive.created == []


def test_target_without_access_fails_before_anything(jobs, drive):
    add_photo(drive, "foto1")

    job = preview(jobs, SOURCE, "ajena")

    assert job["status"] == FAILED
    assert job["error"]["code"] == "target_not_authorized"
    assert "drive_cli" not in job["error"]["message"]


def test_access_lost_between_preview_and_run_stops_before_downloading(jobs, drive):
    add_photo(drive, "foto1")
    job = preview(jobs)
    drive.app_access.discard(TARGET)

    result = jobs.start_run(job["id"])

    assert result["status"] == FAILED
    assert result["error"]["code"] == "target_not_authorized"
    assert drive.media_requests == []


def test_rejected_authorization_requires_reconnecting(jobs, drive, connection):
    drive.read.failures.append(http_error(401))

    job = preview(jobs)

    assert job["status"] == FAILED
    assert job["error"]["code"] == "reconnect_required"
    assert "drive_cli" not in job["error"]["message"]
    assert connection.status().state.value == "reconnect_required"


def test_authorization_lost_during_the_run_requires_reconnecting(jobs, drive, connection, monkeypatch):
    add_photo(drive, "foto1")
    job = preview(jobs)
    real = downloads.download_folder

    def stopped(*args, **kwargs):
        from image_processor.google_drive.errors import DriveAuthError

        result = real(*args, **kwargs)
        result.aborted = DriveAuthError("Vuelva a autorizar con: python -m image_processor.drive_cli auth")
        return result

    monkeypatch.setattr(downloads, "download_folder", stopped)

    result = jobs.start_run(job["id"])

    assert result["status"] == FINISHED
    assert result["summary"]["stopped"]["code"] == "reconnect_required"
    assert result["summary"]["stopped"]["stage"] == "download"
    assert "drive_cli" not in json.dumps(result)
    assert connection.status().state.value == "reconnect_required"


def test_accounts_never_share_local_folders(settings):
    assert folder_dirs(settings, "sub-a", SOURCE) != folder_dirs(settings, "sub-b", SOURCE)
    assert folder_dirs(settings, "sub-a", SOURCE) != folder_dirs(settings, "sub-a", TARGET)
    for path in folder_dirs(settings, "sub-a", SOURCE):
        assert path.is_relative_to(settings.data_dir)
        assert len(str(path.relative_to(settings.data_dir))) <= 60


def test_local_state_of_the_engine_stays_in_the_data_folder(jobs, drive, settings):
    add_photo(drive, "foto1")

    previewed_and_run(jobs)

    downloads_dir, output_dir = folder_dirs(settings, "sub-a", SOURCE)
    assert (output_dir / MANIFEST_NAME).exists()
    assert any(p.is_file() for p in downloads_dir.iterdir())


# Progreso ---------------------------------------------------------------------

def test_progress_counts_each_stage(jobs, drive):
    add_photo(drive, "foto1")
    add_photo(drive, "foto2")

    job = previewed_and_run(jobs)

    progress = job["progress"]
    assert progress["total"] == 2
    assert progress["download"] == {"done": 2, "failed": 0}
    assert progress["processing"] == {"done": 2, "failed": 0}
    assert progress["upload"] == {"done": 2, "failed": 0}


def test_progress_is_visible_while_running(settings, connection, drive):
    add_photo(drive, "foto1")
    seen = []

    def spy(read, write, **kwargs):
        on_event = kwargs["on_event"]

        def wrapped(stage, *details):
            on_event(stage, *details)
            seen.append((stage, jobs.current("sub-a")["progress"]["current"], jobs.current("sub-a")["status"]))

        kwargs["on_event"] = wrapped
        return sync.sync(read, write, **kwargs)

    jobs = JobManager(settings, connection, start_thread=lambda t: t(), runner=spy)
    previewed_and_run(jobs)

    stages = [s for s, _, status in seen if status == RUNNING]
    assert "download" in stages and "processing" in stages and "upload" in stages
    names = {name for _, name, status in seen if status == RUNNING}
    assert "foto1-foto.jpg" in names


def test_files_that_never_were_part_of_the_plan_do_not_count(jobs, drive):
    add_photo(drive, "foto1")
    job = preview(jobs)
    live = jobs._job

    jobs._on_event(live, sync.DOWNLOAD, (downloads.DownloadItem(None, "x", downloads.REJECTED),))

    assert live["progress"]["download"] == job["progress"]["download"]
    assert live["progress"]["current"] is None


def test_failures_are_counted_in_their_stage(jobs, drive):
    add_photo(drive, "foto1")
    add_photo(drive, "foto2")
    drive.media["foto1"] = bytes(reversed(drive.media["foto1"]))

    job = previewed_and_run(jobs)

    assert job["progress"]["download"] == {"done": 2, "failed": 1}
    assert job["progress"]["processing"]["done"] == 1


# Errores que detienen la ejecución ------------------------------------------

def test_model_error_stops_before_uploading(jobs, drive, monkeypatch):
    add_photo(drive, "foto1")
    job = preview(jobs)

    def fail(model_name):
        raise ModelInitializationError("sin modelo")

    monkeypatch.setattr(batch, "prepare_model", fail)

    result = jobs.start_run(job["id"])

    assert result["status"] == FINISHED
    assert result["summary"]["stopped"]["code"] == "model_error"
    assert result["summary"]["stopped"]["stage"] == "processing"
    assert result["summary"]["complete"] is False
    assert uploaded(drive) == []


def test_quota_during_download_is_reported(jobs, drive):
    add_photo(drive, "foto1")
    job = preview(jobs)
    drive.media_failures.append(http_error(403, ["dailyLimitExceeded"]))

    result = jobs.start_run(job["id"])

    assert result["summary"]["stopped"]["code"] == "quota_exceeded"


def test_damaged_manifest_fails_without_changes(jobs, drive, settings):
    add_photo(drive, "foto1")
    _, output_dir = folder_dirs(settings, "sub-a", SOURCE)
    output_dir.mkdir(parents=True)
    (output_dir / MANIFEST_NAME).write_text("no es json", encoding="utf-8")

    job = preview(jobs)

    assert job["status"] == FAILED
    assert job["error"]["code"] == "manifest_error"
    assert (output_dir / MANIFEST_NAME).read_text(encoding="utf-8") == "no es json"


def test_unexpected_error_does_not_leak_details(settings, connection, caplog):
    def explode(read, write, **kwargs):
        raise RuntimeError("refresh-lectura en el detalle")

    jobs = JobManager(settings, connection, start_thread=lambda t: t(), runner=explode)

    with caplog.at_level(logging.DEBUG):
        job = preview(jobs)

    assert job["status"] == FAILED
    assert job["error"]["code"] == "unexpected_error"
    assert "refresh-lectura" not in json.dumps(job)
    assert "refresh-lectura" not in caplog.text


# Un trabajo a la vez ----------------------------------------------------------

@pytest.fixture
def slow_jobs(settings, connection):
    release = threading.Event()
    started = threading.Event()
    threads = []

    def runner(read, write, **kwargs):
        started.set()
        release.wait(5)
        return sync.sync(read, write, **kwargs)

    def start_thread(target):
        thread = threading.Thread(target=target)
        threads.append(thread)
        thread.start()

    jobs = JobManager(settings, connection, start_thread=start_thread, runner=runner)
    jobs.release = release
    jobs.started = started
    yield jobs
    release.set()
    for thread in threads:
        thread.join(5)


def test_only_one_job_at_a_time(slow_jobs):
    job = slow_jobs.start_preview(SOURCE, TARGET)
    assert job["status"] == RUNNING
    assert slow_jobs.started.wait(5)

    with pytest.raises(JobInProgressError):
        slow_jobs.start_preview(SOURCE, TARGET)

    with pytest.raises(JobInProgressError):
        slow_jobs.start_run(job["id"])

    slow_jobs.release.set()


def test_a_new_job_can_start_when_the_previous_finished(slow_jobs):
    slow_jobs.release.set()
    slow_jobs.start_preview(SOURCE, TARGET)

    for _ in range(500):
        if slow_jobs.current("sub-a")["status"] != RUNNING:
            break
        threading.Event().wait(0.01)

    assert slow_jobs.current("sub-a")["status"] == FINISHED
    slow_jobs.start_preview(SOURCE, TARGET)


def test_asking_for_another_account_does_not_lose_the_active_job(slow_jobs):
    job = slow_jobs.start_preview(SOURCE, TARGET)
    assert slow_jobs.started.wait(5)

    assert slow_jobs.current("sub-b") is None
    assert slow_jobs.current("sub-a")["id"] == job["id"]
    assert slow_jobs.current("sub-a")["status"] == RUNNING


def test_disconnect_is_blocked_while_a_job_runs(slow_jobs, connection):
    slow_jobs.start_preview(SOURCE, TARGET)
    assert slow_jobs.started.wait(5)

    with pytest.raises(JobInProgressError):
        slow_jobs.while_idle(connection.disconnect)

    assert connection.status().state.value == "connected"


def test_while_idle_runs_when_nothing_is_active(jobs, connection):
    jobs.while_idle(connection.disconnect)

    assert connection.status().state.value == "disconnected"


def test_returned_job_cannot_alter_the_state(jobs, drive):
    add_photo(drive, "foto1")
    job = preview(jobs)
    job["status"] = "alterado"
    job["summary"]["can_run"] = False

    assert current(jobs)["status"] == FINISHED
    assert current(jobs)["summary"]["can_run"] is True

    read = current(jobs)
    read["status"] = "alterado"
    read["summary"]["can_run"] = False

    assert current(jobs)["status"] == FINISHED
    assert current(jobs)["summary"]["can_run"] is True


# Persistencia e interrupciones ---------------------------------------------

def test_no_job_yet(jobs):
    assert current(jobs) is None
    assert jobs.current(None) is None


def test_summary_survives_a_restart(jobs, drive, settings, connection):
    add_photo(drive, "foto1")
    job = previewed_and_run(jobs)

    restarted = JobManager(settings, connection, start_thread=lambda t: t())

    assert restarted.current("sub-a") == job


def test_job_saved_as_running_comes_back_interrupted(settings, connection, drive):
    add_photo(drive, "foto1")
    release = threading.Event()
    started = threading.Event()

    def runner(read, write, **kwargs):
        started.set()
        release.wait(5)
        return sync.sync(read, write, **kwargs)

    threads = []

    def start_thread(target):
        thread = threading.Thread(target=target)
        threads.append(thread)
        thread.start()

    jobs = JobManager(settings, connection, start_thread=start_thread, runner=runner)
    jobs.start_preview(SOURCE, TARGET)
    assert started.wait(5)

    # Otra instancia lee el disco como después de cerrar la aplicación.
    restarted = JobManager(settings, connection, start_thread=lambda t: t())
    job = restarted.current("sub-a")

    release.set()
    threads[0].join(5)

    assert job["status"] == INTERRUPTED
    assert job["error"]["code"] == "interrupted"
    with pytest.raises(PreviewNotUsableError):
        restarted.start_run(job["id"])
    # La interrupción deja de ser un trabajo activo.
    restarted.start_preview(SOURCE, TARGET)


def test_interrupted_run_resumes_without_duplicates(jobs, drive, settings, connection, ai):
    add_photo(drive, "foto1")
    previewed_and_run(jobs)

    restarted = JobManager(settings, connection, start_thread=lambda t: t())
    job = restarted.start_preview(SOURCE, TARGET)

    assert job["summary"]["counts"]["complete"] == 1
    assert len(uploaded(drive)) == 1


def test_another_account_does_not_see_the_job(jobs, drive):
    add_photo(drive, "foto1")
    preview(jobs)

    assert jobs.current("sub-b") is None


def test_damaged_saved_job_is_ignored(jobs, drive, settings):
    add_photo(drive, "foto1")
    preview(jobs)
    path = next(settings.data_dir.rglob("last_job.json"))
    path.write_text("no es json", encoding="utf-8")

    restarted = JobManager(settings, jobs._connection, start_thread=lambda t: t())

    assert restarted.current("sub-a") is None


def test_failure_saving_the_job_does_not_stop_it(jobs, drive, monkeypatch, caplog):
    from web.backend.app import jobs as module

    def fail(path, content):
        raise OSError("sin disco")

    monkeypatch.setattr(module, "write_atomic", fail)
    add_photo(drive, "foto1")

    with caplog.at_level(logging.ERROR):
        job = preview(jobs)

    assert job["status"] == FINISHED


def test_saved_job_has_no_secrets(jobs, drive, settings):
    add_photo(drive, "foto1")
    previewed_and_run(jobs)

    content = next(settings.data_dir.rglob("last_job.json")).read_text(encoding="utf-8")

    for secret in ("refresh-lectura", "refresh-escritura", DESKTOP_SECRET, "sub-a"):
        assert secret not in content


# API -----------------------------------------------------------------------

@pytest.fixture
def api(settings, connection, jobs, make_client):
    return make_client(settings, connection, jobs=jobs)


def test_api_current_job_is_empty_at_first(api):
    assert api.get("/api/jobs/current").json() == {"job": None}


def test_api_current_job_when_disconnected(settings, make_manager, make_client):
    manager = make_manager()
    client = make_client(settings, manager)

    assert client.get("/api/jobs/current").json() == {"job": None}


def test_api_preview_then_run(api, drive):
    add_photo(drive, "foto1")

    response = api.post(
        "/api/jobs/preview", json={"source_id": SOURCE, "target_id": TARGET}
    )
    assert response.status_code == 200
    job = response.json()
    assert job["summary"]["can_run"] is True
    assert api.get("/api/jobs/current").json()["job"] == job

    response = api.post(f"/api/jobs/{job['id']}/run")
    assert response.status_code == 200
    assert response.json()["kind"] == RUN
    assert len(uploaded(drive)) == 1


def test_api_run_needs_no_folder_ids_and_ignores_none(api, drive):
    add_photo(drive, "foto1")
    job = api.post(
        "/api/jobs/preview", json={"source_id": SOURCE, "target_id": TARGET}
    ).json()

    response = api.post(
        f"/api/jobs/{job['id']}/run", json={"target_id": "ajena"}
    )

    assert response.status_code == 200
    assert response.json()["target"]["id"] == TARGET


@pytest.mark.parametrize(
    "body",
    [
        {"source_id": SOURCE},
        {"source_id": "a'b", "target_id": TARGET},
        {"source_id": SOURCE, "target_id": TARGET, "access_token": "token-navegador"},
    ],
)
def test_api_preview_rejects_invalid_bodies(api, drive, body):
    response = api.post("/api/jobs/preview", json=body)

    assert response.status_code == 422
    assert "token-navegador" not in response.text
    assert api.get("/api/jobs/current").json() == {"job": None}


def test_api_preview_requires_a_connection(settings, make_manager, make_client):
    manager = make_manager()
    jobs = JobManager(settings, manager, start_thread=lambda t: t())
    client = make_client(settings, manager, jobs=jobs)

    response = client.post(
        "/api/jobs/preview", json={"source_id": SOURCE, "target_id": TARGET}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_connected"


@pytest.mark.parametrize("job_id", ["corto", "z" * 32, "a" * 33])
def test_api_run_validates_the_id(api, job_id):
    assert api.post(f"/api/jobs/{job_id}/run").status_code == 422


def test_api_run_of_an_unknown_preview_is_a_conflict(api):
    response = api.post(f"/api/jobs/{'a' * 32}/run")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "preview_not_current"


def test_api_conflict_when_a_job_is_running(settings, connection, slow_jobs, make_client):
    client = make_client(settings, connection, jobs=slow_jobs)
    selection = {"source_id": SOURCE, "target_id": TARGET}

    assert client.post("/api/jobs/preview", json=selection).json()["status"] == RUNNING
    assert slow_jobs.started.wait(5)

    response = client.post("/api/jobs/preview", json=selection)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_in_progress"

    response = client.post("/api/connection/disconnect")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_in_progress"
    assert connection.status().state.value == "connected"


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/jobs/preview", {"source_id": SOURCE, "target_id": TARGET}),
        (f"/api/jobs/{'a' * 32}/run", None),
    ],
)
def test_api_jobs_are_protected_against_csrf(api, drive, path, body):
    token = api.headers.pop("X-CSRF-Token")

    response = api.post(path, json=body)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_csrf_token"
    assert drive.created == []
    api.headers["X-CSRF-Token"] = token


def test_api_jobs_reject_foreign_origins(api, drive):
    api.headers["Origin"] = "https://evil.example.com"

    assert api.get("/api/jobs/current").status_code == 403
    assert api.post(
        "/api/jobs/preview", json={"source_id": SOURCE, "target_id": TARGET}
    ).status_code == 403


def test_api_responses_have_no_secrets(api, drive):
    add_photo(drive, "foto1")
    job = api.post(
        "/api/jobs/preview", json={"source_id": SOURCE, "target_id": TARGET}
    )
    run = api.post(f"/api/jobs/{job.json()['id']}/run")

    for secret in ("refresh-lectura", "refresh-escritura", DESKTOP_SECRET):
        assert secret not in job.text + run.text
