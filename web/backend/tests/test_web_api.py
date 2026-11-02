import logging

import pytest
from conftest import (
    DESKTOP_SECRET,
    PICKER_API_KEY,
    PICKER_CLIENT_ID,
    PROJECT_NUMBER,
    read_credentials,
    write_credentials,
)
from drive_fakes import http_error
from fastapi.testclient import TestClient
from image_processor.google_drive import client as drive_client
from image_processor.google_drive.errors import DriveAuthError

from web.backend.app.main import create_app


def verify(client, source="origen", target="destino", **extra):
    return client.post(
        "/api/folders/verify", json={"source_id": source, "target_id": target, **extra}
    )


@pytest.fixture
def no_retry_sleep(monkeypatch):
    monkeypatch.setattr(drive_client.time, "sleep", lambda seconds: None)


# Estado --------------------------------------------------------------------

def test_status_disconnected(settings, make_manager, make_client):
    body = make_client(settings, make_manager()).get("/api/status").json()

    assert body["environment"] == "development"
    assert body["connection"]["state"] == "disconnected"
    assert body["connection"]["account"] is None


def test_status_connected_shows_sub_and_public_picker_config(
    settings, connected, make_client
):
    body = make_client(settings, connected).get("/api/status").json()

    assert body["connection"]["state"] == "connected"
    assert body["connection"]["account"]["sub"] == "sub-a"
    assert body["connection"]["account"]["email"] == "prueba@example.com"
    assert body["picker"] == {
        "client_id": PICKER_CLIENT_ID,
        "api_key": PICKER_API_KEY,
        "project_number": PROJECT_NUMBER,
    }


def test_responses_never_contain_secrets(settings, connected, make_client):
    client = make_client(settings, connected)
    text = client.get("/api/status").text + verify(client).text

    for secret in ("refresh-lectura", "refresh-escritura", DESKTOP_SECRET, "idtoken:"):
        assert secret not in text


def test_api_responses_are_not_cached(settings, connected, make_client):
    response = make_client(settings, connected).get("/api/status")

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


# Conexión y desconexión -----------------------------------------------------

def test_connect_and_disconnect(settings, make_manager, make_client):
    manager = make_manager(read_credentials(), write_credentials())
    client = make_client(settings, manager)

    started = client.post("/api/connection/start")
    assert started.status_code == 200
    assert started.json()["state"] == "connected"

    disconnected = client.post("/api/connection/disconnect")
    assert disconnected.status_code == 200
    assert disconnected.json()["state"] == "disconnected"
    assert client.get("/api/status").json()["connection"]["account"] is None


def test_connect_while_connected_is_a_conflict(settings, connected, make_client):
    response = make_client(settings, connected).post("/api/connection/start")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "already_connected"


def test_cancel_endpoint(settings, make_manager, make_client):
    response = make_client(settings, make_manager()).post("/api/connection/cancel")

    assert response.status_code == 200
    assert response.json()["state"] == "disconnected"


def test_denied_authorization_is_reported_in_status(settings, make_manager, make_client):
    from oauthlib.oauth2.rfc6749.errors import AccessDeniedError

    client = make_client(settings, make_manager(AccessDeniedError()))

    body = client.post("/api/connection/start").json()

    assert body["state"] == "disconnected"
    assert body["message"]


def test_account_mismatch_is_reported_in_status(settings, make_manager, make_client):
    manager = make_manager(read_credentials("sub-a"), write_credentials("sub-b"))

    body = make_client(settings, manager).post("/api/connection/start").json()

    assert body["error"] == "account_mismatch"
    assert body["account"] is None


# Validación de origen y destino --------------------------------------------

def test_verifies_source_and_target(settings, connected, make_client):
    response = verify(make_client(settings, connected))

    assert response.status_code == 200
    assert response.json() == {
        "source": {"id": "origen", "name": "Origen"},
        "target": {"id": "destino", "name": "Destino"},
        "same_folder": False,
    }


def test_verification_only_reads(settings, connected, make_client, drive):
    verify(make_client(settings, connected))

    operations = {call[0] for call in drive.read.calls + drive.write.calls}

    assert operations <= {"files.get", "about.get"}
    assert drive.created == []


def test_same_folder(settings, connected, make_client):
    response = verify(make_client(settings, connected), "destino", "destino")

    assert response.status_code == 200
    assert response.json()["same_folder"] is True
    assert response.json()["source"]["id"] == response.json()["target"]["id"]


def test_requires_a_connection(settings, make_manager, make_client):
    response = verify(make_client(settings, make_manager()))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_connected"


@pytest.mark.parametrize(
    "source, target",
    [
        ("con espacios", "destino"),
        ("origen", "../../etc"),
        ("origen'", "destino"),
        ("", "destino"),
        ("origen", ""),
    ],
)
def test_invalid_ids_are_rejected_without_calling_drive(
    settings, connected, make_client, drive, source, target
):
    drive.read.calls.clear()
    response = verify(make_client(settings, connected), source, target)

    assert response.status_code == 422
    assert drive.read.calls == []
    assert drive.write.calls == []


def test_only_folder_ids_are_accepted_in_the_body(settings, connected, make_client):
    # Nunca se acepta un token del navegador, ni por error.
    response = verify(make_client(settings, connected), access_token="token-navegador")

    assert response.status_code == 422
    assert "token-navegador" not in response.text


def test_not_a_folder(settings, connected, make_client):
    response = verify(make_client(settings, connected), "foto", "destino")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "not_a_folder"


def test_folder_not_found(settings, connected, make_client):
    response = verify(make_client(settings, connected), "inexistente", "destino")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "folder_not_found"


# Cuenta incorrecta: Drive decide, con las credenciales del backend ----------

def test_source_picked_by_another_account_is_rejected(settings, connected, make_client):
    # "ajena" es legible con drive.readonly, pero el drive.file del backend
    # no la ve: nadie con esta cuenta la autorizó en Picker.
    response = verify(make_client(settings, connected), "ajena", "destino")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "source_not_authorized"
    assert "cuenta" in response.json()["error"]["message"]


def test_target_picked_by_another_account_is_rejected(settings, connected, make_client):
    response = verify(make_client(settings, connected), "origen", "ajena")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "target_not_authorized"


def test_same_folder_picked_by_another_account_is_rejected(
    settings, connected, make_client
):
    response = verify(make_client(settings, connected), "ajena", "ajena")

    assert response.status_code == 409


def test_folder_only_visible_in_the_other_account_is_not_found(
    settings, connected, make_client, drive
):
    drive.items.pop("ajena")
    drive.folders[:] = [f for f in drive.folders if f["id"] != "ajena"]

    response = verify(make_client(settings, connected), "ajena", "destino")

    assert response.status_code == 404


# Permisos insuficientes -----------------------------------------------------

def test_target_without_permission_to_add_files(settings, connected, make_client, drive):
    drive.items["destino"]["capabilities"] = {"canAddChildren": False}

    response = verify(make_client(settings, connected))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "target_not_authorized"


def test_access_denied_on_source(settings, connected, make_client, drive, no_retry_sleep):
    drive.read.failures.append(http_error(403, ["insufficientFilePermissions"]))

    response = verify(make_client(settings, connected))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "access_denied"


def test_rejected_authorization_requires_reconnecting(
    settings, connected, make_client, drive
):
    drive.read.failures.append(http_error(401))
    client = make_client(settings, connected)

    response = verify(client)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "reconnect_required"
    # El mensaje es de la aplicación, no el comando de la CLI.
    assert "drive_cli" not in response.text
    assert client.get("/api/status").json()["connection"]["state"] == "reconnect_required"


def test_expired_authorization_while_building(settings, make_manager, make_client):
    manager = make_manager(read_credentials(), write_credentials())
    manager.start()

    def fail(credentials):
        raise DriveAuthError("expiró")

    manager._service_factory = fail

    response = verify(make_client(settings, manager))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "reconnect_required"


def test_quota_exhausted(settings, connected, make_client, drive):
    drive.read.failures.append(http_error(403, ["dailyLimitExceeded"]))

    response = verify(make_client(settings, connected))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "quota_exceeded"


def test_network_error(settings, connected, make_client, drive, no_retry_sleep):
    drive.read.failures.extend([ConnectionError("x")] * drive_client.MAX_ATTEMPTS)

    response = verify(make_client(settings, connected))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "network_error"


def test_certificate_error(settings, connected, make_client, drive):
    import ssl

    drive.read.failures.append(ssl.SSLCertVerificationError("mal"))

    response = verify(make_client(settings, connected))

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "secure_connection_error"


def test_unexpected_error_does_not_leak_details(
    settings, connected, make_client, drive, caplog
):
    drive.read.failures.append(RuntimeError("refresh-lectura en el detalle"))

    with caplog.at_level(logging.DEBUG):
        response = verify(make_client(settings, connected))

    assert response.status_code == 500
    assert "refresh-lectura" not in response.text
    assert "refresh-lectura" not in caplog.text


# Host, origen y CSRF --------------------------------------------------------

def bare_client(settings, manager, base_url="http://localhost:8000"):
    return TestClient(
        create_app(settings, manager), base_url=base_url, raise_server_exceptions=False
    )


@pytest.mark.parametrize(
    "host",
    ["evil.example.com", "evil.example.com:8000", "127.0.0.1:8000", "localhost:9999", ""],
)
def test_foreign_host_is_rejected(settings, connected, host):
    # Evita DNS rebinding: un dominio ajeno que resuelve a 127.0.0.1.
    response = bare_client(settings, connected).get(
        "/api/status", headers={"Host": host}
    )

    assert response.status_code == 400
    assert "csrf_token" not in response.text


def test_foreign_origin_cannot_read_status(settings, connected):
    response = bare_client(settings, connected).get(
        "/api/status", headers={"Origin": "https://evil.example.com"}
    )

    assert response.status_code == 403
    assert "csrf_token" not in response.text
    assert "access-control-allow-origin" not in response.headers


def test_cross_site_fetch_metadata_is_rejected(settings, connected):
    response = bare_client(settings, connected).get(
        "/api/status", headers={"Sec-Fetch-Site": "cross-site"}
    )

    assert response.status_code == 403


def test_same_site_other_port_is_rejected(settings, connected):
    response = bare_client(settings, connected).get(
        "/api/status", headers={"Sec-Fetch-Site": "same-site"}
    )

    assert response.status_code == 403


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_same_origin_requests_are_accepted(settings, connected, site):
    response = bare_client(settings, connected).get(
        "/api/status", headers={"Sec-Fetch-Site": site}
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    ["/api/connection/start", "/api/connection/cancel", "/api/connection/disconnect",
     "/api/folders/verify"],
)
def test_post_without_origin_is_rejected(settings, connected, make_client, path):
    client = make_client(settings, connected)
    del client.headers["Origin"]

    response = client.post(path, json={})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_origin"
    assert connected.status().state.value == "connected"


def test_post_with_foreign_origin_is_rejected(settings, connected, make_client):
    client = make_client(settings, connected)
    client.headers["Origin"] = "https://evil.example.com"

    response = client.post("/api/connection/disconnect")

    assert response.status_code == 403
    assert connected.status().state.value == "connected"


def test_null_origin_is_rejected(settings, connected, make_client):
    client = make_client(settings, connected)
    client.headers["Origin"] = "null"

    assert client.post("/api/connection/disconnect").status_code == 403


@pytest.mark.parametrize("token", [None, "", "token-incorrecto"])
def test_post_without_valid_csrf_token_is_rejected(
    settings, connected, make_client, token
):
    client = make_client(settings, connected)
    del client.headers["X-CSRF-Token"]
    headers = {} if token is None else {"X-CSRF-Token": token}

    response = client.post("/api/connection/disconnect", headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_csrf_token"
    assert connected.status().state.value == "connected"


def test_csrf_token_differs_on_each_start(settings, connected):
    first = bare_client(settings, connected).get("/api/status").json()["csrf_token"]
    second = bare_client(settings, connected).get("/api/status").json()["csrf_token"]

    assert first != second
    assert len(first) >= 32


def test_form_post_is_not_accepted(settings, connected, make_client):
    # Un formulario de otro sitio no puede enviar JSON ni el token CSRF.
    client = make_client(settings, connected)

    response = client.post(
        "/api/folders/verify",
        data={"source_id": "origen", "target_id": "destino"},
    )

    assert response.status_code == 422


def test_no_cors_headers_even_for_allowed_origins(settings, connected, make_client):
    response = make_client(settings, connected).get("/api/status")

    assert "access-control-allow-origin" not in response.headers


def test_preflight_gets_no_permission(settings, connected):
    response = bare_client(settings, connected).options(
        "/api/connection/disconnect",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


def test_production_rejects_the_development_origin(make_environ, tmp_path, make_manager):
    from web.backend.app.config import load_settings

    prod = load_settings(make_environ("production"), env_file=tmp_path / "x.env")
    client = bare_client(prod, make_manager(settings_=prod))

    assert client.get("/api/status", headers={"Origin": "http://localhost:5173"}).status_code == 403
    assert client.get("/api/status", headers={"Host": "localhost:5173"}).status_code == 400
    assert client.get("/api/status", headers={"Origin": "http://localhost:8000"}).status_code == 200


# Frontend compilado --------------------------------------------------------

def test_serves_the_built_frontend_from_the_same_origin(
    settings, make_manager, tmp_path
):
    from dataclasses import replace

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>Aplicación</h1>", encoding="utf-8")

    client = bare_client(replace(settings, frontend_dist=dist), make_manager())

    assert "Aplicación" in client.get("/").text
    assert client.get("/", headers={"Host": "evil.example.com"}).status_code == 400
    assert client.get("/api/status").status_code == 200


def test_works_without_a_built_frontend(settings, make_manager, tmp_path):
    from dataclasses import replace

    client = bare_client(
        replace(settings, frontend_dist=tmp_path / "no-existe"), make_manager()
    )

    assert client.get("/api/status").status_code == 200
    assert client.get("/").status_code == 404
