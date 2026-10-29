"""
Selección de carpetas con Google Picker (variante de escritorio).

El flujo real de PickerFlow se prueba con un cliente ficticio y sin red
(se sustituye solo el intercambio del código por tokens). Las demás
pruebas simulan el navegador y usan FakeDriveServer. Nunca se contacta
con Google.
"""

from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from google_auth_oauthlib.flow import WSGITimeoutError
from oauthlib.oauth2.rfc6749.errors import OAuth2Error

from image_processor import drive_cli
from image_processor.google_drive import auth, picker, queries
from image_processor.google_drive.errors import (
    DriveAuthError,
    DriveNetworkError,
    DriveNotAFolderError,
    DriveWriteAccessError,
)
from test_drive_sync import (  # noqa: F401 (fixtures)
    ACCESS,
    FOLDER,
    LOCKED,
    REFRESH,
    TARGET,
    ai,
    by_id,
    cli_env,
    env,
    make_credentials,
    two_originals,
)
from drive_fakes import http_error


CLIENT_CONFIG = {
    "installed": {
        "client_id": "cliente.apps.googleusercontent.com",
        "client_secret": "SECRETO-CLIENTE",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

REDIRECT = "https://localhost:8765/"


# La redirección de Google Picker ------------------------------------------

def test_picked_ids_are_read_from_the_redirect():
    assert picker.picked_file_ids(f"{REDIRECT}?picked_file_ids=1Carpeta&code=c&scope=s") == ["1Carpeta"]
    assert picker.picked_file_ids(f"{REDIRECT}?picked_file_ids=1A%2C1B&code=c") == ["1A", "1B"]


@pytest.mark.parametrize(
    "query",
    ["error=access_denied", "code=c&scope=s", "picked_file_ids=&code=c"],
    ids=["cancelado", "sin-ids", "vacio"],
)
def test_a_cancelled_or_empty_selection_picks_nothing(query):
    with pytest.raises(picker.PickerCancelled):
        picker.picked_file_ids(f"{REDIRECT}?{query}")


def test_the_flow_requests_the_official_desktop_picker():
    flow = picker.PickerFlow.from_client_config(CLIENT_CONFIG, [auth.WRITE_SCOPE])
    flow.redirect_uri = "http://localhost:8765/"

    url, _ = flow.authorization_url(**picker.PICKER_PARAMS)

    parts = urlsplit(url)
    params = parse_qs(parts.query)
    assert parts.netloc == "accounts.google.com"
    assert params["trigger_onepick"] == ["true"]
    assert params["prompt"] == ["consent"]
    assert params["allow_folder_selection"] == ["true"]
    assert params["mimetypes"] == [queries.FOLDER_MIME]
    # Exactamente drive.file: el Picker de escritorio no admite otros.
    assert params["scope"] == [auth.WRITE_SCOPE]
    assert "include_granted_scopes" not in params
    assert "allow_multiple" not in params


@pytest.fixture
def flow(monkeypatch):
    flow = picker.PickerFlow.from_client_config(CLIENT_CONFIG, [auth.WRITE_SCOPE])
    flow.redirect_uri = "http://localhost:8765/"
    exchanges = []

    def fake_exchange(token_url, **kwargs):
        exchanges.append(kwargs)
        flow.oauth2session.token = {"access_token": ACCESS, "token_type": "Bearer"}
        return flow.oauth2session.token

    monkeypatch.setattr(flow.oauth2session, "fetch_token", fake_exchange)
    flow.exchanges = exchanges
    return flow


def test_the_flow_keeps_the_picked_folder(flow):
    flow.fetch_token(authorization_response=f"{REDIRECT}?picked_file_ids=1Carpeta&code=c&scope={auth.WRITE_SCOPE}")

    assert flow.picked_ids == ["1Carpeta"]
    assert len(flow.exchanges) == 1


def test_a_cancelled_picker_never_exchanges_the_code(flow):
    with pytest.raises(picker.PickerCancelled):
        flow.fetch_token(authorization_response=f"{REDIRECT}?error=access_denied")

    assert flow.exchanges == []
    assert flow.picked_ids is None


# pick_folder ------------------------------------------------------------------

class FakePickerFlow:
    """Navegador simulado: devuelve la selección o falla como Google."""

    def __init__(self, ids=("1Elegida",), scopes=(auth.WRITE_SCOPE,), error=None):
        self.ids, self.scopes, self.error = ids, scopes, error
        self.picked_ids = None
        self.kwargs = None

    def run_local_server(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        self.picked_ids = list(self.ids)
        return make_credentials(self.scopes)


def factory(flow, calls):
    def build(path, scopes):
        calls.append((path, tuple(scopes)))
        return flow
    return build


@pytest.fixture
def paths(tmp_path):
    paths = auth.secret_paths(tmp_path / ".secrets")
    auth.save_token(make_credentials([auth.READONLY_SCOPE]), paths.token)
    paths.client_secret.write_text("{}", "utf-8")
    return paths


def test_pick_folder_uses_drive_file_and_the_picker_parameters(paths, capsys):
    read_token = paths.token.read_bytes()
    fake, calls = FakePickerFlow(), []

    picked = picker.pick_folder(paths, "la carpeta de origen", flow_factory=factory(fake, calls))

    assert picked.id == "1Elegida"
    assert calls == [(str(paths.client_secret), (auth.WRITE_SCOPE,))]
    for key, value in picker.PICKER_PARAMS.items():
        assert fake.kwargs[key] == value
    assert fake.kwargs["port"] == 0
    assert "la carpeta de origen" in fake.kwargs["authorization_prompt_message"]
    # La autorización de escritura se guarda; la de lectura no cambia.
    assert auth.get_credentials(paths, profile=auth.WRITE).scopes == [auth.WRITE_SCOPE]
    assert paths.token.read_bytes() == read_token
    out, err = capsys.readouterr()
    assert ACCESS not in out + err and REFRESH not in out + err


def test_pick_folder_in_a_preview_saves_nothing(paths):
    picker.pick_folder(paths, "el destino", persist=False, flow_factory=factory(FakePickerFlow(), []))

    assert not paths.write_token.exists()


@pytest.mark.parametrize(
    "error, expected",
    [
        (picker.PickerCancelled("cancelado"), picker.PickerCancelled),
        (WSGITimeoutError("tiempo"), picker.PickerCancelled),
        (requests.exceptions.ConnectionError("sin red"), DriveNetworkError),
        (OAuth2Error("rechazada"), DriveAuthError),
        (Warning("Scope has changed"), DriveAuthError),
    ],
    ids=["cancelado", "tiempo", "red", "oauth", "scope"],
)
def test_pick_folder_errors_are_controlled_and_save_nothing(paths, error, expected):
    with pytest.raises(expected):
        picker.pick_folder(paths, "el origen", flow_factory=factory(FakePickerFlow(error=error), []))

    assert not paths.write_token.exists()


def test_a_broader_scope_is_rejected_and_not_saved(paths):
    fake = FakePickerFlow(scopes=["https://www.googleapis.com/auth/drive"])

    with pytest.raises(DriveAuthError, match="drive.file"):
        picker.pick_folder(paths, "el origen", flow_factory=factory(fake, []))

    assert not paths.write_token.exists()


@pytest.mark.parametrize("ids", [("1A", "1B"), ("../fuera",)], ids=["dos", "invalido"])
def test_the_selection_must_be_one_valid_folder_id(paths, ids):
    with pytest.raises(picker.PickerSelectionError):
        picker.pick_folder(paths, "el origen", flow_factory=factory(FakePickerFlow(ids=ids), []))

    assert not paths.write_token.exists()


def test_pick_folder_needs_the_oauth_client(tmp_path):
    paths = auth.secret_paths(tmp_path / ".secrets")

    with pytest.raises(DriveAuthError, match="cliente OAuth"):
        picker.pick_folder(paths, "el origen", flow_factory=factory(FakePickerFlow(), []))


# Verificación con la API de Drive -------------------------------------------

def verify(env, source, target, dry_run=False):
    return picker.verify_selection(
        env.server.read, env.server.write, source, target, dry_run=dry_run, execute=env.execute
    )


def test_verified_selection_of_source_and_target(env):
    selection = verify(env, FOLDER, TARGET)

    assert (selection.source.id, selection.target.folder.id) == (FOLDER, TARGET)
    assert selection.target.write_access is True
    assert not selection.same_folder
    assert not {"files.create", "files.generateIds"} & set(env.write_operations())


def test_verified_selection_of_the_same_folder(env):
    env.server.app_access.add(FOLDER)   # elegida con Picker: drive.file tiene acceso

    selection = verify(env, FOLDER, FOLDER)

    assert selection.same_folder and selection.target.write_access is True


@pytest.mark.parametrize("role", ["source", "target"])
def test_an_item_that_is_not_a_folder_is_rejected(env, role):
    env.add("1A", "a.jpg")
    source, target = ("1A", TARGET) if role == "source" else (FOLDER, "1A")

    with pytest.raises(DriveNotAFolderError):
        verify(env, source, target)


def test_a_target_without_drive_file_access_is_rejected(env):
    with pytest.raises(DriveWriteAccessError, match="drive.file"):
        verify(env, FOLDER, LOCKED)

    preview = verify(env, FOLDER, LOCKED, dry_run=True)
    assert preview.target.write_access is False


def test_a_permission_403_on_the_target_is_rejected(env):
    env.server.write.failures = [http_error(403, ["insufficientFilePermissions"])]

    with pytest.raises(DriveWriteAccessError, match="403"):
        verify(env, FOLDER, TARGET)


# CLI -----------------------------------------------------------------------

@pytest.fixture
def picks(monkeypatch):
    state = {"queue": [], "calls": []}

    def fake_pick(paths, purpose, *, persist=True, flow_factory=None):
        state["calls"].append((purpose, persist))
        outcome = state["queue"].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return picker.PickedFolder(outcome, make_credentials([auth.WRITE_SCOPE]))

    monkeypatch.setattr(picker, "pick_folder", fake_pick)
    return state


def test_pick_folders_selects_source_and_target(cli_env, picks, capsys):
    picks["queue"] = [FOLDER, TARGET]

    assert drive_cli.main(["pick-folders"]) == 0

    out = capsys.readouterr().out
    assert f"Origen:  Originales ({FOLDER})" in out
    assert f"Destino: Destino propio ({TARGET})" in out
    assert f"sync --folder-id {FOLDER} --target-folder-id {TARGET}" in out
    assert [purpose for purpose, _ in picks["calls"]] == [
        "la carpeta de origen (originales)", "la carpeta de destino (resultados)",
    ]
    assert not {"files.create", "files.generateIds"} & set(cli_env.write_operations())


def test_pick_folders_with_the_same_folder_picks_once(cli_env, picks, capsys):
    cli_env.server.app_access.add(FOLDER)
    picks["queue"] = [FOLDER]

    assert drive_cli.main(["pick-folders", "--same-folder"]) == 0

    out = capsys.readouterr().out
    assert len(picks["calls"]) == 1
    assert "misma carpeta" in out
    assert f"--folder-id {FOLDER} --target-folder-id {FOLDER}" in out


def test_pick_folders_cancelled_starts_nothing(cli_env, picks, capsys):
    picks["queue"] = [picker.PickerCancelled("Se canceló la selección en Google Picker.")]

    assert drive_cli.main(["pick-folders"]) == 10

    assert "canceló" in capsys.readouterr().err
    assert cli_env.server.read.calls == [] and cli_env.server.write.calls == []


@pytest.mark.parametrize(
    "queue, code, message",
    [
        (["1A", TARGET], 4, "no corresponde a una carpeta"),
        ([FOLDER, "1A"], 4, "no corresponde a una carpeta"),
        ([FOLDER, LOCKED], 4, "drive.file"),
        ([picker.PickerSelectionError("elija una sola carpeta")], 2, "una sola carpeta"),
    ],
    ids=["origen-archivo", "destino-archivo", "destino-sin-acceso", "varios"],
)
def test_pick_folders_rejects_invalid_selections(cli_env, picks, capsys, queue, code, message):
    cli_env.add("1A", "a.jpg")
    picks["queue"] = list(queue)

    assert drive_cli.main(["pick-folders"]) == code
    assert message in capsys.readouterr().err


def picker_sync(env, *flags):
    return drive_cli.main([
        "sync", *flags, "--downloads", str(env.downloads), "--output", str(env.outputs), "--all",
    ])


def test_sync_with_picked_source_and_target(cli_env, picks, capsys):
    two_originals(cli_env)
    picks["queue"] = [FOLDER, TARGET]

    assert picker_sync(cli_env, "--pick-source", "--pick-target") == 0

    assert sorted(item["name"] for item in cli_env.remote(TARGET)) == [
        "TRANSFORMADA_a.png", "TRANSFORMADA_b.png",
    ]
    assert [persist for _, persist in picks["calls"]] == [True, True]
    assert "Carpetas elegidas con Google Picker" in capsys.readouterr().out


def test_sync_in_the_same_folder_picks_once_and_never_duplicates(cli_env, picks, capsys):
    cli_env.server.app_access.add(FOLDER)
    two_originals(cli_env)
    picks["queue"] = [FOLDER]

    assert picker_sync(cli_env, "--pick-source", "--same-folder") == 0
    assert len(picks["calls"]) == 1
    assert picks["calls"][0][0] == "la carpeta de origen y destino"
    created, media = list(cli_env.server.created), len(cli_env.media_ids())
    assert sorted(item["name"] for item in cli_env.remote(FOLDER)) == [
        "TRANSFORMADA_a.png", "TRANSFORMADA_b.png",
    ]

    picks["queue"] = [FOLDER]
    assert picker_sync(cli_env, "--pick-source", "--same-folder") == 0

    out = capsys.readouterr().out
    assert "Originales seleccionados:   2" in out
    assert "PNG subidos ahora:          0" in out
    assert cli_env.server.created == created
    assert len(cli_env.media_ids()) == media


def test_sync_pick_cancelled_starts_nothing(cli_env, picks):
    two_originals(cli_env)
    picks["queue"] = [picker.PickerCancelled("cancelado")]

    assert picker_sync(cli_env, "--pick-source", "--pick-target") == 10
    assert cli_env.server.read.calls == [] and cli_env.server.write.calls == []
    assert not cli_env.downloads.exists() and not cli_env.outputs.exists()


@pytest.mark.parametrize(
    "queue, code",
    [(["1A", TARGET], 4), ([FOLDER, LOCKED], 4), ([FOLDER, "1A"], 4)],
    ids=["origen-archivo", "destino-sin-acceso", "destino-archivo"],
)
def test_sync_with_an_invalid_picked_folder_starts_nothing(cli_env, picks, queue, code):
    two_originals(cli_env)
    picks["queue"] = list(queue)

    assert picker_sync(cli_env, "--pick-source", "--pick-target") == code
    assert cli_env.media_ids() == []
    assert not cli_env.downloads.exists() and not cli_env.outputs.exists()
    assert cli_env.server.created == [] and cli_env.server.generated == []


def test_sync_preview_never_saves_the_picker_authorization(cli_env, picks):
    two_originals(cli_env)
    picks["queue"] = [FOLDER, TARGET]

    assert picker_sync(cli_env, "--pick-source", "--pick-target", "--dry-run") == 0
    assert [persist for _, persist in picks["calls"]] == [False, False]
    assert cli_env.server.created == [] and cli_env.server.generated == []


def test_sync_with_an_explicit_source_and_the_same_folder(cli_env):
    cli_env.server.app_access.add(FOLDER)
    two_originals(cli_env)

    assert drive_cli.main([
        "sync", "--folder-id", FOLDER, "--same-folder",
        "--downloads", str(cli_env.downloads), "--output", str(cli_env.outputs), "--all",
    ]) == 0
    assert len(cli_env.remote(FOLDER)) == 2


# Misma carpeta: los PNG generados nunca son originales -----------------------

def test_generated_pngs_renamed_or_copied_are_not_reprocessed(env, ai):
    env.server.app_access.add(FOLDER)
    two_originals(env)
    assert env.run(target_folder_id=FOLDER).complete
    by_name = {item["name"]: item for item in env.remote(FOLDER)}

    # El usuario renombra un resultado y copia otro (mismo contenido, otro ID).
    by_name["TRANSFORMADA_a.png"]["name"] = "a-final.png"
    copied = dict(by_name["TRANSFORMADA_b.png"], id="1CopiaPng", name="copia.png")
    env.server.items["1CopiaPng"] = copied
    env.server.children[FOLDER].append(copied)
    env.server.media["1CopiaPng"] = env.server.media[by_name["TRANSFORMADA_b.png"]["id"]]
    created, media = list(env.server.created), len(env.media_ids())
    ai["prepare"] = ai["cutout"] = 0

    for dry_run in (True, False):
        summary = env.run(target_folder_id=FOLDER, dry_run=dry_run)

        # Solo los originales: ni el PNG renombrado ni la copia.
        assert [item.file_id for item in summary.plan] == ["1A", "1B"]
        assert any("PNG generado por esta aplicación" in warning and ": 2." in warning
                   for warning in summary.warnings)
        # GD-03B: el resultado renombrado ya no tiene el nombre registrado;
        # se informa y no se sube otro (nunca hay duplicados).
        a, b = by_id(summary)["1A"], by_id(summary)["1B"]
        assert (a.status, a.stage) == ("failed", "upload")
        assert "nombre esperado" in a.reason
        assert b.status == "complete"

    assert env.server.created == created
    assert len(env.media_ids()) == media
    assert ai == {"prepare": 0, "cutout": 0}
