
import ssl
from functools import partial

import httplib2
import pytest
from google.auth.exceptions import RefreshError

from drive_fakes import FakeDrive, folder, http_error, image
from image_processor.google_drive import client, queries
from image_processor.google_drive.errors import (
    DriveAccessError,
    DriveAuthError,
    DriveError,
    DriveNetworkError,
    DriveNotAFolderError,
    DriveNotFoundError,
    DriveRateLimitError,
    DriveResponseError,
    DriveTransientError,
)


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def execute(sleeps):
    """client.execute con esperas registradas en lugar de reales."""

    return partial(client.execute, sleep=sleeps.append)


def brand_folder(children):
    return FakeDrive(
        folders=[folder("carpetaA", "Marca A")],
        children={"carpetaA": children},
    )


# A. Conexión ----------------------------------------------------------

def test_verify_connection(execute):
    drive = FakeDrive()

    account = queries.verify_connection(drive, execute=execute)

    assert account == queries.DriveAccount("Cuenta de prueba", "prueba@example.com")
    assert drive.calls[0][0] == "about.get"


# 5, 6. Carpetas -------------------------------------------------------

def test_list_folders(execute):
    drive = FakeDrive(
        folders=[
            folder("f1", "Marca A"),
            folder("f2", "Fotos", parents=["f1"]),
            {"id": "f3", "name": "Compartida"},
        ]
    )

    folders = queries.list_folders(drive, execute=execute)

    assert folders == [
        queries.DriveFolder("f1", "Marca A", ("root",)),
        queries.DriveFolder("f2", "Fotos", ("f1",)),
        queries.DriveFolder("f3", "Compartida", ()),
    ]

    operation, kwargs = drive.calls[0]
    assert operation == "files.list"
    assert "mimeType = 'application/vnd.google-apps.folder'" in kwargs["q"]
    assert "trashed = false" in kwargs["q"]
    assert "parents" in kwargs["fields"]
    assert kwargs["supportsAllDrives"] is True


def test_duplicate_folder_names_keep_their_ids(execute):
    drive = FakeDrive(
        folders=[
            folder("idUno", "Marca"),
            folder("idDos", "Marca", parents=["otra"]),
        ]
    )

    folders = queries.list_folders(drive, execute=execute)

    assert [(item.id, item.name) for item in folders] == [
        ("idUno", "Marca"),
        ("idDos", "Marca"),
    ]


def test_folders_are_paginated(execute):
    drive = FakeDrive(
        folders=[folder(f"f{index}", f"Carpeta {index}") for index in range(7)]
    )

    folders = queries.list_folders(drive, execute=execute, page_size=3)

    assert len(folders) == 7
    assert [kwargs.get("pageToken") for _, kwargs in drive.calls] == [
        None, "p3", "p6",
    ]


# 7, 8. Carpeta vacía o inexistente ------------------------------------

def test_empty_folder(execute):
    listing = queries.list_images(brand_folder([]), "carpetaA", execute=execute)

    assert listing.folder.name == "Marca A"
    assert listing.images == []
    assert listing.skipped == []


def test_missing_folder(execute):
    with pytest.raises(DriveNotFoundError):
        queries.list_images(FakeDrive(), "noExiste", execute=execute)


def test_file_id_that_is_not_a_folder(execute):
    drive = FakeDrive(items={"foto1": image("foto1", "foto.jpg")})

    with pytest.raises(DriveNotAFolderError):
        queries.list_images(drive, "foto1", execute=execute)


def test_trashed_folder(execute):
    drive = FakeDrive(
        items={"vieja": {"id": "vieja", "name": "Vieja", "mimeType": queries.FOLDER_MIME, "trashed": True}}
    )

    with pytest.raises(DriveNotFoundError, match="papelera"):
        queries.list_images(drive, "vieja", execute=execute)


@pytest.mark.parametrize(
    "folder_id",
    ["", "x' or name contains '", "a b", "../carpeta", None],
)
def test_invalid_folder_id_is_rejected_before_any_request(execute, folder_id):
    drive = FakeDrive()

    with pytest.raises(ValueError):
        queries.list_images(drive, folder_id, execute=execute)

    assert drive.calls == []


# 9. 500 imágenes en varias páginas ------------------------------------

def test_five_hundred_images_over_several_pages(execute):
    children = [
        image(f"img{index:03d}", f"producto_{index:03d}.jpg")
        for index in range(500)
    ]
    drive = brand_folder(children)

    listing = queries.list_images(drive, "carpetaA", execute=execute, page_size=120)

    assert len(listing.images) == 500
    assert len({item.id for item in listing.images}) == 500
    assert [item.id for item in listing.images] == [item["id"] for item in children]

    tokens = [
        kwargs.get("pageToken")
        for operation, kwargs in drive.calls
        if operation == "files.list"
    ]
    assert tokens == [None, "p120", "p240", "p360", "p480"]


def test_default_page_size_is_not_artificially_limited(execute):
    drive = brand_folder([image("a", "a.jpg")])

    queries.list_images(drive, "carpetaA", execute=execute)

    listing_call = [k for op, k in drive.calls if op == "files.list"][0]
    assert listing_call["pageSize"] == 1000


def test_repeated_page_token_is_an_error(execute):
    class LoopingDrive(FakeDrive):
        def respond(self, operation, kwargs):
            if operation == "files.list":
                return {"files": [], "nextPageToken": "siempre"}
            return super().respond(operation, kwargs)

    with pytest.raises(DriveResponseError):
        queries.list_folders(LoopingDrive(), execute=execute)


# 10, 11, 12. Formatos, transformadas y metadatos ----------------------

def test_supported_formats_filter(execute):
    drive = brand_folder([
        image("1", "a.jpg"),
        image("2", "b.JPEG"),
        image("3", "c.png", "image/png"),
        image("4", "d.webp", "image/webp"),
        image("5", "e.heic", "image/heic"),
        image("6", "f.gif", "image/gif"),
        image("7", "g.pdf", "application/pdf"),
        image("8", "h", "image/jpeg"),
        image("9", "i.jpg", "image/heic"),
        image("10", "Documento", "application/vnd.google-apps.document"),
        image("11", "atajo.jpg", "application/vnd.google-apps.shortcut"),
    ])

    listing = queries.list_images(drive, "carpetaA", execute=execute)

    assert [item.name for item in listing.images] == [
        "a.jpg", "b.JPEG", "c.png", "d.webp",
    ]
    assert {item.name: item.reason for item in listing.skipped} == {
        "e.heic": "formato no admitido",
        "f.gif": "formato no admitido",
        "g.pdf": "formato no admitido",
        "h": "formato no admitido",
        "i.jpg": "formato no admitido",
        "Documento": "formato no admitido",
        "atajo.jpg": "acceso directo (no se sigue)",
    }

    listing_call = [k for op, k in drive.calls if op == "files.list"][0]
    assert "'carpetaA' in parents" in listing_call["q"]
    assert "trashed = false" in listing_call["q"]


def test_transformed_results_excluded_only_for_originals(execute):
    children = [
        image("1", "producto.jpg"),
        image("2", "TRANSFORMADA_producto.png", "image/png"),
        image("3", "transformada_otro.png", "image/png"),
    ]

    everything = queries.list_images(brand_folder(children), "carpetaA", execute=execute)
    originals = queries.list_images(
        brand_folder(children), "carpetaA", originals_only=True, execute=execute
    )

    assert len(everything.images) == 3
    assert [item.name for item in originals.images] == ["producto.jpg"]
    assert [item.reason for item in originals.skipped] == [
        "resultado transformado", "resultado transformado",
    ]


def test_metadata_is_preserved(execute):
    drive = brand_folder([
        image(
            "1AbC-_9",
            "café pequeño.jpg",
            size="123456",
            modifiedTime="2026-10-23T08:15:30.123Z",
            md5Checksum="d41d8cd98f00b204e9800998ecf8427e",
        ),
        {"id": "2", "name": "sin_md5.png", "mimeType": "image/png"},
    ])

    listing = queries.list_images(drive, "carpetaA", execute=execute)

    assert listing.images == [
        queries.DriveImage(
            id="1AbC-_9",
            name="café pequeño.jpg",
            mime_type="image/jpeg",
            size=123456,
            modified_time="2026-10-23T08:15:30.123Z",
            md5_checksum="d41d8cd98f00b204e9800998ecf8427e",
        ),
        queries.DriveImage("2", "sin_md5.png", "image/png", None, None, None),
    ]

    fields = [k for op, k in drive.calls if op == "files.list"][0]["fields"]
    for name in ("id", "name", "mimeType", "size", "modifiedTime", "md5Checksum"):
        assert name in fields


@pytest.mark.parametrize(
    "item",
    [
        {"name": "sin_id.jpg", "mimeType": "image/jpeg"},
        {"id": "x", "mimeType": "image/jpeg"},
        {"id": "y", "name": "sin_tipo.jpg"},
        "no es un objeto",
    ],
)
def test_incomplete_metadata_is_reported(execute, item):
    listing = queries.list_images(brand_folder([item]), "carpetaA", execute=execute)

    assert listing.images == []
    assert [entry.reason for entry in listing.skipped] == ["metadatos incompletos"]


def test_invalid_size_is_unknown(execute):
    listing = queries.list_images(
        brand_folder([image("1", "a.jpg", size="no-es-un-número")]),
        "carpetaA",
        execute=execute,
    )

    assert listing.images[0].size is None


# 13. Errores 403, 404 y 429 -------------------------------------------

def failing_request(*outcomes):
    """Solicitud que falla con las excepciones dadas y luego responde."""

    class Request:
        calls = 0

        def execute(self):
            Request.calls += 1
            outcome = outcomes[min(Request.calls, len(outcomes)) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    return Request()


def test_403_permission_denied_is_not_retried(execute, sleeps):
    request = failing_request(http_error(403, ["insufficientPermissions"]))

    with pytest.raises(DriveAccessError, match="insufficientPermissions"):
        execute(request)

    assert type(request).calls == 1
    assert sleeps == []


def test_404_is_not_retried(execute, sleeps):
    request = failing_request(http_error(404, ["notFound"]))

    with pytest.raises(DriveNotFoundError):
        execute(request)

    assert type(request).calls == 1


def test_401_is_an_authentication_error(execute, sleeps):
    request = failing_request(http_error(401, ["authError"]))

    with pytest.raises(DriveAuthError):
        execute(request)

    assert type(request).calls == 1


def test_revoked_credentials_during_request(execute):
    with pytest.raises(DriveAuthError):
        execute(failing_request(RefreshError("invalid_grant")))


def test_429_is_retried_with_exponential_backoff(execute, sleeps):
    request = failing_request(http_error(429, ["rateLimitExceeded"]))

    with pytest.raises(DriveRateLimitError):
        execute(request)

    assert type(request).calls == client.MAX_ATTEMPTS
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_429_then_success(execute, sleeps):
    request = failing_request(http_error(429), {"ok": True})

    assert execute(request) == {"ok": True}
    assert sleeps == [1.0]


def test_retry_after_header_is_respected(execute, sleeps):
    request = failing_request(
        http_error(429, headers={"retry-after": "7"}), {"ok": True}
    )

    execute(request)

    assert sleeps == [7.0]


def test_403_quota_is_retried(execute, sleeps):
    request = failing_request(
        http_error(403, ["userRateLimitExceeded"]), {"ok": True}
    )

    assert execute(request) == {"ok": True}
    assert sleeps == [1.0]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_errors_are_transient(execute, sleeps, status):
    request = failing_request(http_error(status))

    with pytest.raises(DriveTransientError):
        execute(request)

    assert len(sleeps) == client.MAX_ATTEMPTS - 1


def test_other_client_errors_are_not_retried(execute, sleeps):
    request = failing_request(http_error(400, ["invalid"]))

    with pytest.raises(DriveError) as error:
        execute(request)

    assert not isinstance(error.value, DriveTransientError)
    assert sleeps == []


# 14. Fallos de red ----------------------------------------------------

@pytest.mark.parametrize(
    "failure",
    [
        ConnectionResetError("reset"),
        TimeoutError("tiempo agotado"),
        httplib2.ServerNotFoundError("sin DNS"),
        OSError("red inaccesible"),
    ],
)
def test_network_failures_are_retried(execute, sleeps, failure):
    request = failing_request(failure)

    with pytest.raises(DriveNetworkError):
        execute(request)

    assert type(request).calls == client.MAX_ATTEMPTS


def test_network_failure_then_success(execute, sleeps):
    request = failing_request(ConnectionResetError(), TimeoutError(), {"ok": 1})

    assert execute(request) == {"ok": 1}
    assert sleeps == [1.0, 2.0]


def test_ssl_certificate_errors_are_not_retried(execute, sleeps):
    request = failing_request(ssl.SSLCertVerificationError("certificado"))

    with pytest.raises(DriveNetworkError, match="no se desactiva"):
        execute(request)

    assert type(request).calls == 1


def test_listing_survives_transient_failures(execute, sleeps):
    drive = brand_folder([image("1", "a.jpg")])
    drive.failures = [None, http_error(503), ConnectionResetError()]

    listing = queries.list_images(drive, "carpetaA", execute=execute)

    assert [item.id for item in listing.images] == ["1"]
    assert sleeps == [1.0, 2.0]


def test_build_service_uses_bundled_discovery(monkeypatch):
    captured = {}

    def fake_build(*args, **kwargs):
        captured.update(kwargs, args=args)
        return "servicio"

    monkeypatch.setattr(client, "build", fake_build)

    assert client.build_service("credenciales") == "servicio"
    assert captured["args"] == ("drive", "v3")
    assert captured["static_discovery"] is True
    assert captured["credentials"] == "credenciales"
