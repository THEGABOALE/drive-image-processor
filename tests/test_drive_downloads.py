
import errno
import hashlib
import io
import json
import os
import ssl
import time
import unicodedata
from functools import partial

import pytest
from PIL import Image

from drive_fakes import FakeDrive, folder, http_error
from image_processor import batch, drive_cli
from image_processor.google_drive import auth, client, downloads


FOLDER = "carpetaPrueba"


def encoded(data_format, size=(64, 48), color=(200, 40, 40)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format=data_format)
    return buffer.getvalue()


def noisy_png(size=(120, 120)):
    """PNG que no se comprime: ocupa varios bloques de 1000 bytes."""

    buffer = io.BytesIO()
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(
        buffer, format="PNG"
    )
    return buffer.getvalue()


JPEG = encoded("JPEG")
PNG = encoded("PNG", color=(40, 200, 40))
WEBP = encoded("WEBP", color=(40, 40, 200))


def media_item(file_id, name, data, mime="image/jpeg", **extra):
    item = {
        "id": file_id,
        "name": name,
        "mimeType": mime,
        "size": str(len(data)),
        "md5Checksum": hashlib.md5(data).hexdigest(),
        "sha256Checksum": hashlib.sha256(data).hexdigest(),
        "version": "7",
        "headRevisionId": "rev-1",
        "modifiedTime": "2026-10-24T10:00:00.000Z",
        "trashed": False,
        "capabilities": {"canDownload": True},
    }
    item.update(extra)
    return item


def make_drive(*files):
    """files: (item, data). data None = sin contenido descargable."""

    drive = FakeDrive(folders=[folder(FOLDER, "Pruebas")])
    drive.children[FOLDER] = [item for item, _ in files]

    for item, data in files:
        drive.items[item["id"]] = item
        if data is not None:
            drive.media[item["id"]] = data

    return drive


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def download(tmp_path, sleeps):
    """download_folder con esperas registradas y destino temporal."""

    output = tmp_path / "descargas"

    def run(drive, **kwargs):
        kwargs.setdefault("sleep", sleeps.append)
        kwargs.setdefault("execute", partial(client.execute, sleep=sleeps.append))
        return downloads.download_folder(drive, FOLDER, output, **kwargs)

    run.output = output
    return run


def statuses(summary):
    return [(item.name, item.status) for item in summary.items]


def finals(output):
    """Archivos publicados (sin temporales ni registros)."""

    return sorted(
        path.name for path in output.iterdir()
        if path.is_file() and not path.name.endswith(".tmp")
    )


def record(output, name):
    return json.loads(
        (output / downloads.METADATA_DIR / f"{name}.json").read_text("utf-8")
    )


def assert_clean(output):
    """Sin temporales en el destino ni en los registros."""

    assert list(output.glob("*.tmp")) == []
    assert list(output.glob(".*.tmp")) == []
    assert list((output / downloads.METADATA_DIR).glob(".*.tmp")) == [] if (
        output / downloads.METADATA_DIR
    ).exists() else True


# 1, 2, 3. JPG, PNG y WebP -------------------------------------------------

@pytest.mark.parametrize(
    "name, data, mime, image_format",
    [
        ("producto.jpg", JPEG, "image/jpeg", "JPEG"),
        ("logo.PNG", PNG, "image/png", "PNG"),
        ("banner.webp", WEBP, "image/webp", "WEBP"),
        ("foto.jpeg", JPEG, "image/jpeg", "JPEG"),
    ],
)
def test_supported_formats_are_downloaded_and_verified(
    download, name, data, mime, image_format
):
    drive = make_drive((media_item("1AbC-_9", name, data, mime), data))

    summary = download(drive)

    expected = downloads.local_name(name, "1AbC-_9")
    assert statuses(summary) == [(name, downloads.DOWNLOADED)]
    assert summary.items[0].local_name == expected
    assert summary.bytes_downloaded == len(data)

    path = download.output / expected
    assert path.read_bytes() == data

    with Image.open(path) as image:
        assert image.format == image_format

    saved = record(download.output, expected)
    assert saved["status"] == downloads.COMPLETED
    assert saved["file_id"] == "1AbC-_9"
    assert saved["name"] == name
    assert saved["md5_checksum"] == hashlib.md5(data).hexdigest()
    assert saved["sha256"] == hashlib.sha256(data).hexdigest()
    assert saved["folder_id"] == FOLDER
    assert_clean(download.output)


# 4. Varios bloques -------------------------------------------------------

def test_download_in_several_chunks(download):
    data = noisy_png()
    drive = make_drive((media_item("multi", "grande.png", data, "image/png"), data))

    summary = download(drive, chunk_size=1000)

    assert summary.items[0].status == downloads.DOWNLOADED
    ranges = [byte_range for _, byte_range in drive.media_requests]
    assert len(ranges) == -(-len(data) // 1000)
    assert ranges[:2] == ["bytes=0-999", "bytes=1000-1999"]
    assert (download.output / summary.items[0].local_name).read_bytes() == data


def test_default_chunk_size_is_reasonable():
    assert downloads.DEFAULT_CHUNK_SIZE == 4 * 1024 * 1024
    assert downloads.DEFAULT_MAX_FILE_BYTES == 25 * 1024 * 1024


# 5, 6, 7. Tamaño, MD5 y contenido ----------------------------------------

@pytest.mark.parametrize(
    "declared, message",
    [(len(JPEG) + 10, "incompleta"), (len(JPEG) - 10, "más bytes")],
)
def test_declared_size_must_match(download, declared, message):
    item = media_item("tam", "producto.jpg", JPEG, size=str(declared))
    summary = download(make_drive((item, JPEG)))

    assert summary.items[0].status == downloads.FAILED
    assert message in summary.items[0].reason
    assert finals(download.output) == []
    assert_clean(download.output)


def test_wrong_md5_is_not_published(download):
    item = media_item("md5", "producto.jpg", JPEG, md5Checksum="0" * 32)
    summary = download(make_drive((item, JPEG)))

    assert summary.items[0].status == downloads.FAILED
    assert "MD5" in summary.items[0].reason
    assert finals(download.output) == []


def test_wrong_drive_sha256_is_not_published(download):
    item = media_item("sha", "producto.jpg", JPEG, sha256Checksum="0" * 64)
    summary = download(make_drive((item, JPEG)))

    assert "SHA-256" in summary.items[0].reason
    assert finals(download.output) == []


def test_corrupt_image_is_not_published(download):
    data = os.urandom(2000)
    summary = download(make_drive((media_item("roto", "roto.jpg", data), data)))

    assert summary.items[0].status == downloads.FAILED
    assert "no es una imagen válida" in summary.items[0].reason
    assert finals(download.output) == []
    assert_clean(download.output)


def test_truncated_image_is_not_published(download):
    data = JPEG[: len(JPEG) // 2]
    summary = download(make_drive((media_item("trunc", "trunc.jpg", data), data)))

    assert summary.items[0].status == downloads.FAILED
    assert finals(download.output) == []


def test_content_must_match_declared_type(download):
    summary = download(make_drive((media_item("tipo", "foto.jpg", PNG), PNG)))

    assert summary.items[0].status == downloads.FAILED
    assert "no coincide con el tipo declarado" in summary.items[0].reason


# 8, 9, 10, 11. Errores HTTP, SSL, red y reintentos -------------------------

@pytest.mark.parametrize("status, message", [(403, "denegó"), (404, "no existe")])
def test_http_error_during_download(download, sleeps, status, message):
    drive = make_drive((media_item("err", "producto.jpg", JPEG), JPEG))
    drive.media_failures = [http_error(status)]

    summary = download(drive)

    assert summary.items[0].status == downloads.FAILED
    assert message in summary.items[0].reason
    assert sleeps == []
    assert finals(download.output) == []


def test_ssl_error_is_not_retried(download, sleeps):
    drive = make_drive((media_item("ssl", "producto.jpg", JPEG), JPEG))
    drive.media_failures = [ssl.SSLCertVerificationError("certificado")]

    summary = download(drive)

    assert summary.items[0].status == downloads.FAILED
    assert "certificado SSL" in summary.items[0].reason
    assert len(drive.media_requests) == 1
    assert finals(download.output) == []


def test_network_failure_mid_transfer_resumes(download, sleeps):
    data = noisy_png()
    drive = make_drive((media_item("red", "red.png", data, "image/png"), data))
    drive.media_failures = [None, ConnectionResetError("reset"), None]

    summary = download(drive, chunk_size=1000)

    assert summary.items[0].status == downloads.DOWNLOADED
    assert (download.output / summary.items[0].local_name).read_bytes() == data
    ranges = [byte_range for _, byte_range in drive.media_requests]
    # El bloque fallido se pide de nuevo desde el mismo punto.
    assert ranges[1] == ranges[2] == "bytes=1000-1999"
    assert sleeps == [1.0]


def test_retries_are_bounded_per_file(download, sleeps):
    drive = make_drive((media_item("caida", "producto.jpg", JPEG), JPEG))
    drive.media_failures = [http_error(503)] * 20

    summary = download(drive)

    assert summary.items[0].status == downloads.FAILED
    assert len(drive.media_requests) == downloads.MAX_DOWNLOAD_RETRIES + 1
    assert sleeps == [1.0, 2.0, 4.0, 8.0]
    assert finals(download.output) == []


def test_rate_limit_retry_after_is_respected(download, sleeps):
    drive = make_drive((media_item("cuota", "producto.jpg", JPEG), JPEG))
    drive.media_failures = [http_error(429, headers={"retry-after": "3"})]

    assert download(drive).items[0].status == downloads.DOWNLOADED
    assert sleeps == [3.0]


def test_repeated_network_failures_stop_the_batch(download, sleeps):
    files = [
        (media_item(f"id{index}", f"foto{index}.jpg", JPEG), JPEG)
        for index in range(6)
    ]
    drive = make_drive(*files)
    drive.media_failures = [ConnectionResetError()] * 100

    summary = download(drive)

    assert summary.count(downloads.FAILED) == downloads.MAX_CONSECUTIVE_TRANSIENT_FAILURES
    assert summary.aborted is not None
    assert finals(download.output) == []


def test_revoked_authorization_stops_the_batch(download):
    drive = make_drive((media_item("aut", "producto.jpg", JPEG), JPEG))
    drive.media_failures = [http_error(401)]

    summary = download(drive)

    assert isinstance(summary.aborted, downloads.DriveAuthError)
    assert finals(download.output) == []


# 12, 13. Permisos y tamaño -------------------------------------------------

@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"capabilities": {"canDownload": False}}, "sin permiso de descarga"),
        ({"capabilities": {}}, "no informó si se puede descargar"),
        ({"md5Checksum": None}, "sin checksum MD5"),
        ({"size": None}, "tamaño no declarado"),
        ({"size": "0"}, "tamaño no declarado"),
        ({"trashed": True}, "papelera"),
        ({"mimeType": "image/gif"}, "formato no admitido"),
    ],
)
def test_files_rejected_before_download(download, changes, reason):
    item = media_item("rech", "producto.jpg", JPEG)
    item.update(changes)
    drive = make_drive((item, JPEG))
    # El listado solo filtra por tipo; files.get aporta el resto.
    drive.children[FOLDER] = [media_item("rech", "producto.jpg", JPEG)]

    summary = download(drive)

    assert statuses(summary) == [("producto.jpg", downloads.REJECTED)]
    assert reason in summary.items[0].reason
    assert drive.media_requests == []
    assert finals(download.output) == []


def test_file_over_size_limit_is_rejected(download):
    data = encoded("PNG", size=(400, 400))
    drive = make_drive((media_item("grande", "grande.png", data, "image/png"), data))

    summary = download(drive, max_file_bytes=len(data) - 1)

    assert summary.items[0].status == downloads.REJECTED
    assert "supera el límite" in summary.items[0].reason
    assert drive.media_requests == []


def test_decompression_bomb_protection_rejects(download, monkeypatch):
    # Se reduce el umbral de Pillow para no generar una imagen enorme.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    summary = download(make_drive((media_item("bomba", "bomba.jpg", JPEG), JPEG)))

    assert summary.items[0].status == downloads.REJECTED
    assert "desproporcionadamente grande" in summary.items[0].reason
    assert finals(download.output) == []
    assert_clean(download.output)


def test_listing_rejections_are_reported(download):
    drive = make_drive(
        (media_item("ok", "producto.jpg", JPEG), JPEG),
        (media_item("video", "clip.avi", b"x", "video/x-msvideo"), None),
        (media_item("res", "TRANSFORMADA_producto.png", PNG, "image/png"), PNG),
    )

    summary = download(drive, originals_only=True)

    assert sorted(statuses(summary)) == sorted([
        ("producto.jpg", downloads.DOWNLOADED),
        ("clip.avi", downloads.REJECTED),
        ("TRANSFORMADA_producto.png", downloads.REJECTED),
    ])
    assert [request[0] for request in drive.media_requests] == ["ok"]


# 14, 15, 16, 22. Nombres e identidad ---------------------------------------

def test_same_name_different_file_ids(download):
    other = encoded("JPEG", color=(10, 10, 10))
    drive = make_drive(
        (media_item("fileIdA", "producto.jpg", JPEG), JPEG),
        (media_item("fileIdB", "producto.jpg", other), other),
    )

    summary = download(drive)

    assert summary.count(downloads.DOWNLOADED) == 2
    names = {item.file_id: item.local_name for item in summary.items}
    assert names["fileIdA"] != names["fileIdB"]
    assert (download.output / names["fileIdA"]).read_bytes() == JPEG
    assert (download.output / names["fileIdB"]).read_bytes() == other
    assert record(download.output, names["fileIdB"])["file_id"] == "fileIdB"


@pytest.mark.parametrize(
    "name",
    [
        "../../escape.jpg",
        "..\\..\\escape.jpg",
        "/etc/passwd.jpg",
        "C:\\Windows\\win.jpg",
        "CON.jpg",
        "nul.JPG",
        "a<b>c:d\"e|f?g*.jpg",
        "control\x00\x1f.jpg",
        " .oculto. .jpg",
        "doble__guion--bajo.jpg",
        "....jpg",
    ],
)
def test_malicious_names_stay_inside_output(download, name):
    drive = make_drive((media_item("mal", name, JPEG), JPEG))

    summary = download(drive)

    item = summary.items[0]
    assert item.status == downloads.DOWNLOADED

    local = download.output / item.local_name
    assert local.parent.resolve() == download.output.resolve()
    assert local.read_bytes() == JPEG

    stem = item.local_name.split("--")[0]
    assert stem.split(".")[0].upper() not in {"CON", "NUL"}
    assert "__" not in stem
    assert not item.local_name.startswith(".")
    assert record(download.output, item.local_name)["name"] == name

    escaped = download.output.parent.parent / "escape.jpg"
    assert not escaped.exists()


def test_case_and_unicode_differences_never_collide(download):
    composed = "caf\u00e9.jpg"
    decomposed = "cafe\u0301.jpg"
    files = [
        (media_item("abcD", "Producto.jpg", JPEG), JPEG),
        (media_item("abcd", "producto.jpg", PNG, "image/jpeg"), None),
        (media_item("uni1", composed, JPEG), JPEG),
        (media_item("uni2", decomposed, JPEG), JPEG),
    ]
    drive = make_drive(*files)
    drive.media["abcd"] = JPEG
    drive.items["abcd"] = media_item("abcd", "producto.jpg", JPEG)
    drive.children[FOLDER][1] = drive.items["abcd"]

    summary = download(drive)

    assert summary.count(downloads.DOWNLOADED) == 4
    locals_ = [item.local_name for item in summary.items]
    assert len({name.casefold() for name in locals_}) == 4
    assert all(unicodedata.is_normalized("NFC", name) for name in locals_)


@pytest.mark.parametrize("file_id", ["1AbC-_9", "abcD", "ABCD", "a-b_C", "0OoO"])
def test_file_id_identity_is_preserved_exactly(download, file_id):
    drive = make_drive((media_item(file_id, "foto.jpg", JPEG), JPEG))

    summary = download(drive)

    item = summary.items[0]
    assert item.file_id == file_id
    assert record(download.output, item.local_name)["file_id"] == file_id

    encoded_id = item.local_name.split("--", 1)[1].rsplit(".", 1)[0]
    assert encoded_id == encoded_id.lower()
    assert downloads.decode_file_id(encoded_id) == file_id


def test_encoded_ids_are_injective_under_casefold():
    ids = ["abcD", "abcd", "ABCD", "aBcD", "a~bc", "a-b", "a_b", "A-B"]
    valid = [file_id for file_id in ids if "~" not in file_id]

    names = {downloads.encode_file_id(file_id).casefold() for file_id in valid}
    assert len(names) == len(valid)

    with pytest.raises(ValueError):
        downloads.encode_file_id("a~bc")


# 17, 18. Archivos preexistentes y ejecuciones repetidas --------------------

def test_existing_foreign_file_is_not_overwritten(download):
    drive = make_drive((media_item("ajeno", "producto.jpg", JPEG), JPEG))
    name = downloads.local_name("producto.jpg", "ajeno")
    download.output.mkdir(parents=True)
    (download.output / name).write_bytes(b"ARCHIVO DEL USUARIO")

    summary = download(drive)

    assert summary.items[0].status == downloads.FAILED
    assert "sin registro de procedencia" in summary.items[0].reason
    assert (download.output / name).read_bytes() == b"ARCHIVO DEL USUARIO"
    assert drive.media_requests == []


def test_second_run_verifies_without_downloading(download):
    drive = make_drive(
        (media_item("a", "a.jpg", JPEG), JPEG),
        (media_item("b", "b.png", PNG, "image/png"), PNG),
    )

    download(drive)
    before = {
        name: (download.output / name).stat().st_mtime_ns
        for name in finals(download.output)
    }
    drive.media_requests.clear()

    summary = download(drive)

    assert summary.count(downloads.VERIFIED) == 2
    assert summary.bytes_downloaded == 0
    assert drive.media_requests == []
    assert {
        name: (download.output / name).stat().st_mtime_ns
        for name in finals(download.output)
    } == before


def test_locally_modified_download_is_not_overwritten(download):
    drive = make_drive((media_item("mod", "producto.jpg", JPEG), JPEG))
    name = download(drive).items[0].local_name
    (download.output / name).write_bytes(PNG)

    summary = download(drive)

    assert summary.items[0].status == downloads.FAILED
    assert "no coincide con su registro" in summary.items[0].reason
    assert (download.output / name).read_bytes() == PNG


def test_deleted_download_is_downloaded_again(download):
    drive = make_drive((media_item("del", "producto.jpg", JPEG), JPEG))
    name = download(drive).items[0].local_name
    (download.output / name).unlink()

    summary = download(drive)

    assert summary.items[0].status == downloads.DOWNLOADED
    assert summary.items[0].local_name == name
    assert (download.output / name).read_bytes() == JPEG


def test_damaged_or_foreign_record_is_not_trusted(download):
    drive = make_drive((media_item("reg", "producto.jpg", JPEG), JPEG))
    name = download(drive).items[0].local_name
    path = download.output / downloads.METADATA_DIR / f"{name}.json"

    path.write_text("{dañado", encoding="utf-8")
    assert "dañado" in download(drive).items[0].reason

    other = record_with(download.output, name, drive, file_id="otroId")
    path.write_text(json.dumps(other), encoding="utf-8")
    assert "otro archivo de Drive" in download(drive).items[0].reason
    assert (download.output / name).read_bytes() == JPEG


def record_with(output, name, drive, **changes):
    item = drive.items["reg"]
    data = {
        "schema_version": 1, "generator": "drive-image-processor",
        "status": "completed", "file_id": "reg", "name": item["name"],
        "mime_type": item["mimeType"], "size": int(item["size"]),
        "md5_checksum": item["md5Checksum"],
        "sha256": hashlib.sha256(JPEG).hexdigest(),
        "drive_sha256": None, "version": "7", "head_revision_id": "rev-1",
        "modified_time": None, "folder_id": FOLDER, "local_name": name,
        "recorded_at": "2026-10-24T00:00:00+00:00",
    }
    data.update(changes)
    return data


def test_interrupted_transfer_leaves_nothing_and_reruns(download):
    data = noisy_png()
    drive = make_drive((media_item("int", "int.png", data, "image/png"), data))
    drive.media_failures = [None, KeyboardInterrupt()]

    with pytest.raises(KeyboardInterrupt):
        download(drive, chunk_size=1000)

    assert finals(download.output) == []
    assert_clean(download.output)

    summary = download(drive, chunk_size=1000)

    assert summary.items[0].status == downloads.DOWNLOADED
    assert (download.output / summary.items[0].local_name).read_bytes() == data


def test_interruption_before_publishing_reruns(download, monkeypatch):
    drive = make_drive((media_item("pub", "producto.jpg", JPEG), JPEG))

    def interrupted(tmp, final):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(downloads, "publish_file_exclusive", interrupted)

        with pytest.raises(KeyboardInterrupt):
            download(drive)

    name = downloads.local_name("producto.jpg", "pub")
    assert finals(download.output) == []
    assert record(download.output, name)["status"] == downloads.PENDING
    assert_clean(download.output)

    summary = download(drive)

    assert summary.items[0].status == downloads.DOWNLOADED
    assert record(download.output, name)["status"] == downloads.COMPLETED


def test_interruption_after_publishing_is_recovered(download, monkeypatch):
    drive = make_drive((media_item("rec", "producto.jpg", JPEG), JPEG))
    real_write = downloads._write_record
    calls = []

    def interrupted(output, name, data):
        calls.append(data["status"])
        if data["status"] == downloads.COMPLETED:
            raise KeyboardInterrupt
        real_write(output, name, data)

    with monkeypatch.context() as patch:
        patch.setattr(downloads, "_write_record", interrupted)

        with pytest.raises(KeyboardInterrupt):
            download(drive)

    name = downloads.local_name("producto.jpg", "rec")
    assert (download.output / name).read_bytes() == JPEG
    drive.media_requests.clear()

    summary = download(drive)

    assert summary.items[0].status == downloads.RECOVERED
    assert drive.media_requests == []
    assert record(download.output, name)["status"] == downloads.COMPLETED


def test_unregistered_download_is_reported(download, monkeypatch):
    drive = make_drive((media_item("unr", "producto.jpg", JPEG), JPEG))
    real_write = downloads._write_record

    def failing(output, name, data):
        if data["status"] == downloads.COMPLETED:
            raise OSError("disco lleno")
        real_write(output, name, data)

    monkeypatch.setattr(downloads, "_write_record", failing)

    summary = download(drive)

    assert summary.items[0].status == downloads.DOWNLOADED
    assert summary.unregistered == 1


def test_stale_temps_are_removed_and_recent_kept(download):
    download.output.mkdir(parents=True)
    old = download.output / ".foto--x.jpg.abc12_yz.tmp"
    recent = download.output / ".foto--y.jpg.zyx21_ab.tmp"
    foreign = download.output / "notas.tmp"

    for path in (old, recent, foreign):
        path.write_bytes(b"parcial")

    stale = time.time() - 25 * 3600
    os.utime(old, (stale, stale))
    os.utime(foreign, (stale, stale))

    summary = download(make_drive())

    assert summary.temp_removed == 1
    assert not old.exists()
    assert recent.exists() and foreign.exists()


# 19. Cambios en Drive ----------------------------------------------------

@pytest.mark.parametrize(
    "field, value",
    [("md5Checksum", "f" * 32), ("headRevisionId", "rev-2"), ("size", "1")],
)
def test_remote_change_during_download(download, field, value):
    drive = make_drive((media_item("cambia", "producto.jpg", JPEG), JPEG))

    def change(file_id, request_number):
        drive.items[file_id] = dict(drive.items[file_id], **{field: value})

    drive.on_media = change

    summary = download(drive)

    assert summary.items[0].status == downloads.FAILED
    assert "cambió en Drive durante la descarga" in summary.items[0].reason
    assert finals(download.output) == []
    assert_clean(download.output)


def test_remote_content_change_between_runs_keeps_previous(download):
    drive = make_drive((media_item("ver", "producto.jpg", JPEG), JPEG))
    first = download(drive).items[0].local_name

    drive.items["ver"] = media_item("ver", "producto.jpg", PNG, "image/jpeg")
    new = encoded("JPEG", color=(1, 2, 3))
    drive.items["ver"] = media_item("ver", "producto.jpg", new, headRevisionId="rev-2")
    drive.media["ver"] = new

    summary = download(drive)

    item = summary.items[0]
    assert item.status == downloads.DOWNLOADED
    assert ".rev-" in item.local_name
    assert (download.output / first).read_bytes() == JPEG
    assert (download.output / item.local_name).read_bytes() == new

    again = download(drive)
    assert again.items[0].status == downloads.VERIFIED


# 20, 21. Límite y carpeta vacía ---------------------------------------------

def test_limit_is_respected(download):
    files = [
        (media_item(f"id{index}", f"foto{index}.jpg", JPEG), JPEG)
        for index in range(7)
    ]
    drive = make_drive(*files)

    summary = download(drive, limit=3)

    assert summary.count(downloads.DOWNLOADED) == 3
    assert sorted({file_id for file_id, _ in drive.media_requests}) == [
        "id0", "id1", "id2",
    ]
    assert len(finals(download.output)) == 3


def test_invalid_limits(download):
    with pytest.raises(ValueError):
        download(make_drive(), limit=0)

    with pytest.raises(ValueError):
        download(make_drive(), max_file_bytes=0)


def test_empty_folder(download):
    summary = download(make_drive())

    assert summary.items == []
    assert finals(download.output) == []


# 23. Ninguna operación de escritura en Drive --------------------------------

def test_only_read_operations_are_used(download):
    drive = make_drive(
        (media_item("a", "a.jpg", JPEG), JPEG),
        (media_item("b", "b.jpg", JPEG, md5Checksum="0" * 32), JPEG),
    )

    download(drive)
    download(drive)

    used = {operation for operation, _ in drive.calls}
    assert used <= {"files.list", "files.get", "files.get_media", "about.get"}


# 25. Compatibilidad con el motor local --------------------------------------

def test_downloads_feed_the_local_engine(download, monkeypatch):
    drive = make_drive(
        (media_item("fileIdA", "producto.jpg", JPEG), JPEG),
        (media_item("fileIdB", "producto.jpg", encoded("JPEG", color=(9, 9, 9))), None),
    )
    drive.media["fileIdB"] = encoded("JPEG", color=(9, 9, 9))
    drive.items["fileIdB"] = media_item("fileIdB", "producto.jpg", drive.media["fileIdB"])
    drive.children[FOLDER][1] = drive.items["fileIdB"]

    download(drive)

    monkeypatch.setattr(batch, "prepare_model", lambda model_name: None)
    monkeypatch.setattr(
        batch,
        "process_image",
        lambda image, model_name, options=None: Image.new("RGB", (400, 400), "white"),
    )

    result = batch.process_folder(download.output)

    assert (result.processed, result.failed, result.unverified) == (2, 0, 0)

    outputs = sorted(path.name for path in download.output.glob("TRANSFORMADA_*"))
    assert len(outputs) == 2
    assert all("__esc" not in name for name in outputs)


# CLI ---------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch, tmp_path):
    drive = make_drive(
        (media_item("a1", "producto.jpg", JPEG), JPEG),
        (media_item("b2", "roto.jpg", os.urandom(500)), None),
        (media_item("v", "clip.avi", b"x", "video/x-msvideo"), None),
    )
    drive.media["b2"] = os.urandom(int(drive.items["b2"]["size"]))
    drive.items["b2"]["md5Checksum"] = hashlib.md5(drive.media["b2"]).hexdigest()
    drive.items["b2"]["sha256Checksum"] = hashlib.sha256(drive.media["b2"]).hexdigest()

    monkeypatch.setattr(auth, "get_credentials", lambda paths, **kwargs: object())
    monkeypatch.setattr(client, "build_service", lambda credentials: drive)
    monkeypatch.setattr(client, "execute", partial(client.execute, sleep=lambda s: None))
    monkeypatch.setattr(
        downloads,
        "download_folder",
        partial(downloads.download_folder, sleep=lambda seconds: None),
    )

    def run(*arguments):
        return drive_cli.main([
            "download", "--folder-id", FOLDER,
            "--output", str(tmp_path / "salida"), *arguments,
        ])

    run.drive = drive
    run.output = tmp_path / "salida"
    return run


def test_cli_download_summary(cli, capsys):
    assert cli("--show-skipped") == 6

    captured = capsys.readouterr()
    out = captured.out

    assert "Descargadas:                1" in out
    assert "Ya verificadas y omitidas:  0" in out
    assert "Rechazadas:                 1" in out
    assert "formato no admitido: 1" in out
    assert "Fallidas (no publicadas):   1" in out
    assert f"Bytes descargados:          {len(JPEG):,}".replace(",", ".") in out
    assert "[RECHAZADA] clip.avi" in out
    assert "[ERROR] roto.jpg" in captured.err
    assert "No se publicó ningún archivo" in captured.err


def test_cli_second_run_and_limit(cli, capsys):
    cli("--limit", "1")
    capsys.readouterr()

    assert cli("--limit", "1") == 0
    out = capsys.readouterr().out
    assert "Ya verificadas y omitidas:  1" in out
    assert "Descargadas:                0" in out


@pytest.mark.parametrize(
    "arguments", [("--limit", "0"), ("--max-file-mb", "0"), ("--max-file-mb", "5000")]
)
def test_cli_invalid_arguments(cli, capsys, arguments):
    assert cli(*arguments) == 2


def test_cli_max_file_mb(cli, capsys):
    assert cli("--max-file-mb", "0.0001") == 0
    assert "supera el límite" in capsys.readouterr().out


def test_cli_stops_on_repeated_network_errors(cli, capsys):
    for index in range(3):
        item = media_item(f"extra{index}", f"extra{index}.jpg", JPEG)
        cli.drive.items[item["id"]] = item
        cli.drive.media[item["id"]] = JPEG
        cli.drive.children[FOLDER].append(item)

    cli.drive.media_failures = [ConnectionResetError()] * 100

    assert cli() == 5

    captured = capsys.readouterr()
    assert "fallos de red seguidos" in captured.err
    assert "Fallidas (no publicadas):   3" in captured.out
    assert "Traceback" not in captured.err


def test_cli_individual_failures_exit_code(cli, capsys):
    cli.drive.media_failures = [ConnectionResetError()] * 100

    # Dos fallos de red no alcanzan para detener el lote.
    assert cli() == 6
    assert "Fallidas (no publicadas):   2" in capsys.readouterr().out


# --- Revisión GD-02 ----------------------------------------------------

class _FailingFile:
    """Archivo local cuya escritura falla (disco lleno, permisos)."""

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


def fail_local_writes(monkeypatch, error):
    real_fdopen = os.fdopen

    def fdopen(fd, mode="r", *args, **kwargs):
        file = real_fdopen(fd, mode, *args, **kwargs)
        return _FailingFile(file, error) if "w" in mode else file

    monkeypatch.setattr(downloads.os, "fdopen", fdopen)


# GD02-R1: errores locales de escritura -----------------------------------

@pytest.mark.parametrize(
    "error, message",
    [
        (OSError(errno.ENOSPC, "No space left on device"), "espacio"),
        (PermissionError(errno.EACCES, "Permission denied"), "permiso"),
    ],
)
def test_local_write_errors_are_not_network_errors(
    download, sleeps, monkeypatch, error, message
):
    files = [
        (media_item(f"loc{index}", f"foto{index}.jpg", JPEG), JPEG)
        for index in range(4)
    ]
    drive = make_drive(*files)

    with monkeypatch.context() as patch:
        fail_local_writes(patch, error)
        summary = download(drive)

    # Cada imagen falla por la escritura local, sin reintentos de red.
    assert summary.count(downloads.FAILED) == 4
    assert all("local" in item.reason and message in item.reason for item in summary.items)
    assert all("red" not in item.reason for item in summary.items)
    assert sleeps == []
    assert len(drive.media_requests) == 4

    # No se confunde con tres fallos de red seguidos.
    assert summary.aborted is None
    assert finals(download.output) == []
    assert_clean(download.output)


def test_genuine_network_errors_keep_their_classification(download, sleeps):
    drive = make_drive((media_item("red2", "producto.jpg", JPEG), JPEG))
    drive.media_failures = [ConnectionResetError(), OSError("red inaccesible")]

    summary = download(drive)

    assert summary.items[0].status == downloads.DOWNLOADED
    assert sleeps == [1.0, 2.0]


# GD02-R2: correspondencia MIME/extensión ---------------------------------

@pytest.mark.parametrize(
    "name, mime",
    [
        ("producto.png", "image/jpeg"),
        ("producto.jpg", "image/png"),
        ("producto.jpeg", "image/webp"),
        ("producto.webp", "image/png"),
    ],
)
def test_mime_and_extension_must_correspond(download, name, mime):
    drive = make_drive((media_item("mix", name, JPEG, mime), JPEG))

    summary = download(drive)

    assert summary.items[0].status == downloads.REJECTED
    assert "no corresponde" in summary.items[0].reason
    assert mime in summary.items[0].reason
    assert drive.media_requests == []
    assert finals(download.output) == []


@pytest.mark.parametrize(
    "name, data, mime",
    [
        ("a.jpg", JPEG, "image/jpeg"),
        ("b.JPEG", JPEG, "image/jpeg"),
        ("c.png", PNG, "image/png"),
        ("d.WEBP", WEBP, "image/webp"),
    ],
)
def test_matching_mime_and_extension_are_accepted(download, name, data, mime):
    drive = make_drive((media_item("ok", name, data, mime), data))

    assert download(drive).items[0].status == downloads.DOWNLOADED


# GD02-R3: respuestas sin progreso ----------------------------------------

def empty_chunk(total):
    import httplib2

    response = httplib2.Response({
        "status": "206",
        "content-range": f"bytes 0-999/{total}",
    })
    return response, b""


def test_response_without_progress_is_aborted(download, sleeps):
    data = noisy_png()
    drive = make_drive((media_item("quieto", "quieto.png", data, "image/png"), data))
    drive.media_failures = [empty_chunk(len(data))] * 100

    summary = download(drive, chunk_size=1000)

    item = summary.items[0]
    assert item.status == downloads.FAILED
    assert "sin progreso" in item.reason
    assert len(drive.media_requests) == downloads.MAX_STALLED_CHUNKS
    assert sleeps == []
    assert finals(download.output) == []
    assert_clean(download.output)


def test_stall_after_partial_progress_is_aborted(download, sleeps):
    data = noisy_png()
    drive = make_drive((media_item("medio", "medio.png", data, "image/png"), data))
    drive.media_failures = [None] + [empty_chunk(len(data))] * 100

    summary = download(drive, chunk_size=1000)

    assert summary.items[0].status == downloads.FAILED
    assert len(drive.media_requests) == 1 + downloads.MAX_STALLED_CHUNKS
    assert finals(download.output) == []
    assert_clean(download.output)


def test_transient_errors_do_not_count_as_stalls(download, sleeps):
    data = noisy_png()
    drive = make_drive((media_item("vale", "vale.png", data, "image/png"), data))
    # Tres errores transitorios seguidos (dentro del límite de
    # reintentos) no son "respuestas sin progreso".
    drive.media_failures = [
        None, http_error(503), http_error(503), ConnectionResetError(), None,
    ]

    summary = download(drive, chunk_size=1000)

    assert summary.items[0].status == downloads.DOWNLOADED
    assert (download.output / summary.items[0].local_name).read_bytes() == data
    assert sleeps == [1.0, 2.0, 4.0]


def test_endless_tiny_progress_is_bounded(download, sleeps):
    import httplib2

    data = noisy_png()
    drive = make_drive((media_item("goteo", "goteo.png", data, "image/png"), data))
    served = {"offset": 0}

    def one_byte(file_id, number):
        offset = served["offset"]
        served["offset"] += 1
        drive.media_failures.insert(0, (
            httplib2.Response({
                "status": "206",
                "content-range": f"bytes {offset}-{offset}/{len(data)}",
            }),
            data[offset:offset + 1],
        ))

    drive.on_media = one_byte

    summary = download(drive, chunk_size=1000)

    assert summary.items[0].status == downloads.FAILED
    assert "sin progreso" in summary.items[0].reason or "demasiados" in summary.items[0].reason
    assert len(drive.media_requests) < len(data)
    assert finals(download.output) == []
