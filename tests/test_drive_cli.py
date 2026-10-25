
import json
import logging
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path

import pytest
import requests
from google.oauth2.credentials import Credentials

from drive_fakes import ALLOWED, FakeDrive, folder, http_error, image
from image_processor import drive_cli
from image_processor.google_drive import auth, client


ACCESS = "ya29.SECRETO-ACCESO-CLI"
REFRESH = "1//SECRETO-REFRESCO-CLI"
CLIENT_SECRET = "SECRETO-CLIENTE-CLI"

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "image_processor"


def sample_drive():
    return FakeDrive(
        folders=[
            folder("idUno", "Marca"),
            folder("idDos", "Marca", parents=["otra"]),
            folder("idTres", "Catálogo"),
        ],
        children={
            "idUno": [
                image("img1", "producto.jpg", md5Checksum="a" * 32),
                image("img2", "TRANSFORMADA_producto.png", "image/png"),
                image("doc1", "notas.pdf", "application/pdf"),
            ],
            "idTres": [],
        },
    )


@pytest.fixture
def drive(monkeypatch, tmp_path):
    """CLI conectada a un Drive simulado, con un token válido."""

    fake = sample_drive()
    secrets = tmp_path / ".secrets"

    credentials = Credentials(
        token=ACCESS,
        refresh_token=REFRESH,
        token_uri="https://oauth2.googleapis.com/token",
        client_id="cliente.apps.googleusercontent.com",
        client_secret=CLIENT_SECRET,
        scopes=[auth.READONLY_SCOPE],
        expiry=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
    )
    auth.save_token(credentials, auth.secret_paths(secrets).token)

    monkeypatch.setattr(client, "build_service", lambda credentials: fake)
    monkeypatch.setattr(
        client, "execute", partial(client.execute, sleep=lambda seconds: None)
    )
    monkeypatch.setenv(auth.SECRETS_DIR_ENV, str(secrets))

    return fake


def run(*arguments):
    return drive_cli.main(list(arguments))


def test_auth_verifies_connection(drive, capsys):
    assert run("auth") == 0

    out = capsys.readouterr().out
    assert "Conexión verificada: Cuenta de prueba <prueba@example.com>" in out
    assert "drive.readonly" in out


def test_list_folders_shows_ids_and_marks_duplicates(drive, capsys):
    assert run("list-folders") == 0

    out = capsys.readouterr().out
    assert "Carpetas accesibles: 3" in out
    assert re.search(r"idUno\tMarca\troot\s+\(nombre repetido", out)
    assert re.search(r"idDos\tMarca\totra\s+\(nombre repetido", out)
    assert "idTres\tCatálogo\troot\n" in out


def test_list_images(drive, capsys):
    assert run("list-images", "--folder-id", "idUno") == 0

    out = capsys.readouterr().out
    assert "Carpeta: Marca (idUno)" in out
    assert "Imágenes admitidas: 2" in out
    assert "img1\tproducto.jpg\timage/jpeg\t1.024\t2026-10-20T12:00:00.000Z\t" + "a" * 32 in out
    assert "formato no admitido: 1" in out


def test_list_images_originals_only(drive, capsys):
    assert run("list-images", "--folder-id", "idUno", "--originals-only", "--show-skipped") == 0

    out = capsys.readouterr().out
    assert "Originales admitidas: 1" in out
    assert "TRANSFORMADA_producto.png\tresultado transformado" in out


def test_empty_folder_message(drive, capsys):
    assert run("list-images", "--folder-id", "idTres") == 0
    assert "no contiene imágenes admitidas" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments, code, message",
    [
        (("list-images", "--folder-id", "noExiste"), 4, "no existe"),
        (("list-images", "--folder-id", "img1"), 4, "no existe"),
        (("list-images", "--folder-id", "x' or '1'='1"), 2, "inválido"),
    ],
)
def test_list_images_errors(drive, capsys, arguments, code, message):
    assert run(*arguments) == code
    assert message in capsys.readouterr().err


def test_permission_rate_limit_and_network_codes(drive, capsys):
    drive.failures = [http_error(403, ["insufficientPermissions"])]
    assert run("list-folders") == 4

    drive.failures = [http_error(429)] * client.MAX_ATTEMPTS
    assert run("list-folders") == 5

    drive.failures = [ConnectionResetError()] * client.MAX_ATTEMPTS
    assert run("list-folders") == 5

    err = capsys.readouterr().err
    assert "denegó el acceso" in err
    assert "limitó las solicitudes" in err
    assert "No se pudo comunicar" in err


def test_missing_authorization_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(auth.SECRETS_DIR_ENV, str(tmp_path / "vacio"))

    assert run("list-folders") == 3
    assert "drive_cli auth" in capsys.readouterr().err


def test_revoked_authorization_code(drive, capsys):
    drive.failures = [http_error(401)]

    assert run("list-folders") == 3


def test_logout(drive, capsys, tmp_path):
    assert run("logout") == 0
    assert "Se eliminó la autorización local" in capsys.readouterr().out
    assert run("logout") == 0
    assert "No había" in capsys.readouterr().out


# 15. Sin credenciales en la salida ni en los registros -----------------

def test_commands_never_print_secrets(drive, capsys, caplog):
    caplog.set_level(logging.DEBUG)

    run("auth")
    run("list-folders")
    run("list-images", "--folder-id", "idUno", "--show-skipped")
    drive.failures = [http_error(429)] * client.MAX_ATTEMPTS
    run("list-folders")

    captured = capsys.readouterr()

    for secret in (ACCESS, REFRESH, CLIENT_SECRET):
        for text in (captured.out, captured.err, caplog.text):
            assert secret not in text


# 16. Ninguna operación de escritura en Drive ----------------------------

def test_only_read_operations_are_used(drive):
    run("auth")
    run("list-folders")
    run("list-images", "--folder-id", "idUno")
    run("list-images", "--folder-id", "idUno", "--originals-only")
    run("list-images", "--folder-id", "noExiste")

    allowed = {f"{kind}.{method}" for kind, methods in ALLOWED.items() for method in methods}
    used = {operation for operation, _ in drive.calls}

    assert used <= allowed
    assert used == {"about.get", "files.list", "files.get"}


def test_fake_drive_rejects_write_operations():
    drive = FakeDrive()

    for operation in ("create", "update", "delete", "copy", "emptyTrash", "get_media", "export"):
        with pytest.raises(AssertionError, match="no permitida"):
            getattr(drive.files(), operation)

    with pytest.raises(AssertionError, match="no permitido"):
        drive.permissions()


def test_drive_code_has_no_write_or_download_calls():
    sources = [PACKAGE / "drive_cli.py", *sorted((PACKAGE / "google_drive").glob("*.py"))]
    forbidden = re.compile(
        r"\.(create|update|delete|copy|emptyTrash|trash|untrash|"
        r"get_media|export|export_media|watch|generateIds|permissions|"
        r"revisions|comments|replies)\(|MediaFileUpload|MediaIoBaseUpload|"
        r"MediaIoBaseDownload|MediaUpload"
    )

    for source in sources:
        code = source.read_text(encoding="utf-8")
        assert forbidden.search(code) is None, source.name

    assert auth.SCOPES == ("https://www.googleapis.com/auth/drive.readonly",)


def test_local_processor_does_not_import_drive():
    for name in ("processing.py", "pipeline.py", "batch.py"):
        code = (PACKAGE / name).read_text(encoding="utf-8")
        assert "google" not in code
        assert "drive" not in code.lower()


# Seguridad: los secretos quedan fuera de Git -------------------------

@pytest.mark.skipif(shutil.which("git") is None, reason="git no disponible")
def test_secret_files_are_ignored_by_git():
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=REPO, capture_output=True, text=True,
    )

    if inside.returncode != 0:
        pytest.skip("no es un repositorio git")

    secret_paths = [
        ".secrets/token.json",
        ".secrets/credentials.json",
        ".secrets/client_secret.json",
        ".secrets/.token.json.abc12345.tmp",
        ".secrets/token.damaged-20261024T120000Z-abcd1234.json",
        "otra_carpeta/token.damaged-20261024T120000Z-abcd1234.json",
        "client_secret_123-abc.apps.googleusercontent.com.json",
        "credentials.json",
        "token.json",
        "otra_carpeta/token.json",
        "otra_carpeta/credentials-drive.json",
        "drive.token.json",
    ]
    code_paths = [
        "image_processor/google_drive/auth.py",
        "tests/test_drive_cli.py",
        "requirements.txt",
    ]

    def ignored(path):
        return subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", path], cwd=REPO
        ).returncode == 0

    assert [path for path in secret_paths if not ignored(path)] == []
    assert [path for path in code_paths if ignored(path)] == []

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True
    ).stdout.splitlines()

    assert [path for path in tracked if re.search(r"secret|token|credential", path, re.I) and path.endswith(".json")] == []


# --- Revisión GD-01: comando auth ----------------------------------------

class LoginFlow:
    def __init__(self, credentials=None, error=None):
        self.credentials = credentials
        self.error = error

    def run_local_server(self, **kwargs):
        if self.error is not None:
            raise self.error
        return self.credentials


def login_credentials():
    return Credentials(
        token=ACCESS,
        refresh_token=REFRESH,
        token_uri="https://oauth2.googleapis.com/token",
        client_id="cliente.apps.googleusercontent.com",
        client_secret=CLIENT_SECRET,
        scopes=[auth.READONLY_SCOPE],
        expiry=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
    )


def use_login_flow(monkeypatch, flow):
    real = auth.get_credentials
    monkeypatch.setattr(
        auth,
        "get_credentials",
        partial(real, flow_factory=lambda path, scopes: flow),
    )


def prepare_damaged_login(drive, tmp_path):
    paths = auth.secret_paths(tmp_path / ".secrets")
    damaged = f'{{"refresh_token": "{REFRESH}"'.encode("utf-8")
    paths.token.write_bytes(damaged)
    paths.client_secret.write_text("{}", encoding="utf-8")
    return paths, damaged


def test_auth_recovers_from_damaged_token(drive, tmp_path, monkeypatch, capsys):
    paths, damaged = prepare_damaged_login(drive, tmp_path)
    use_login_flow(monkeypatch, LoginFlow(login_credentials()))

    # Las consultas siguen rechazando el token dañado.
    assert run("list-folders") == 3

    assert run("auth") == 0

    captured = capsys.readouterr()
    [backup] = sorted(paths.directory.glob("token.damaged-*.json"))

    assert backup.read_bytes() == damaged
    assert f"[AVISO] El token anterior estaba dañado; se conservó sin cambios como {backup.name}" in captured.out
    assert "Conexión verificada" in captured.out
    assert run("list-folders") == 0

    for secret in (ACCESS, REFRESH, CLIENT_SECRET):
        assert secret not in captured.out + captured.err


def test_auth_network_error_has_no_traceback(drive, tmp_path, monkeypatch, capsys):
    paths, damaged = prepare_damaged_login(drive, tmp_path)
    use_login_flow(
        monkeypatch,
        LoginFlow(error=requests.exceptions.SSLError(f"CERTIFICATE_VERIFY_FAILED {ACCESS}")),
    )

    assert run("auth") == 5

    err = capsys.readouterr().err
    assert "certificado SSL" in err
    assert "Traceback" not in err
    assert ACCESS not in err
    assert paths.token.read_bytes() == damaged
