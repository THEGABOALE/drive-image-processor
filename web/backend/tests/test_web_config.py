import json

import pytest
from conftest import DESKTOP_SECRET, PROJECT_NUMBER, desktop_config

from web.backend.app.config import (
    ConfigError,
    ensure_loopback,
    load_settings,
)


def load(environ, tmp_path):
    return load_settings(environ, env_file=tmp_path / "no-existe.env")


# Separación de entornos ---------------------------------------------------

def test_development_and_production_use_separate_data_folders(make_environ, tmp_path):
    dev = load(make_environ("development"), tmp_path)
    prod = load(make_environ("production"), tmp_path)

    assert dev.environment == "development"
    assert prod.environment == "production"
    assert dev.data_dir != prod.data_dir
    assert dev.data_dir.name == "development"
    assert prod.data_dir.name == "production"
    assert dev.data_dir.parent == prod.data_dir.parent
    assert dev.data_dir.parent.name == "DriveImageProcessor"
    assert dev.data_dir.parent.parent == tmp_path / "LocalAppData"


def test_production_only_allows_the_local_origin(make_environ, tmp_path):
    dev = load(make_environ("development"), tmp_path)
    prod = load(make_environ("production"), tmp_path)

    assert dev.allowed_origins == {"http://localhost:5173", "http://localhost:8000"}
    assert prod.allowed_origins == {"http://localhost:8000"}
    assert dev.app_url == "http://localhost:5173"
    assert prod.app_url == "http://localhost:8000"


def test_packaged_development_opens_the_local_page(make_environ, tmp_path):
    settings = load(make_environ("development", DIP_PACKAGED="1"), tmp_path)

    assert settings.app_url == "http://localhost:8000"
    assert settings.packaged
    # Los orígenes de desarrollo no cambian.
    assert "http://localhost:5173" in settings.allowed_origins


def test_running_from_the_repository_is_not_packaged(make_environ, tmp_path):
    assert not load(make_environ("development"), tmp_path).packaged
    assert not load(make_environ("development", DIP_PACKAGED="0"), tmp_path).packaged


def test_unknown_environment_is_rejected(make_environ, tmp_path):
    with pytest.raises(ConfigError, match="DIP_ENV"):
        load(make_environ(DIP_ENV="staging"), tmp_path)


@pytest.mark.parametrize(
    "environment, project",
    [
        ("development", "drive-image-processor-prod"),
        ("production", "drive-image-processor-dev"),
    ],
)
def test_project_of_the_other_environment_is_rejected(
    make_environ, tmp_path, environment, project
):
    environ = make_environ(
        environment,
        client=desktop_config(project_id=project),
        GOOGLE_CLOUD_PROJECT_ID=project,
    )

    with pytest.raises(ConfigError, match="otro entorno"):
        load(environ, tmp_path)


def test_env_file_of_each_environment_is_read_and_real_environment_wins(
    make_environ, tmp_path
):
    environ = make_environ()
    env_file = tmp_path / ".env.development.local"
    env_file.write_text(
        f"GOOGLE_PICKER_API_KEY=clave-del-archivo\n"
        f"GOOGLE_PICKER_CLIENT_ID={environ.pop('GOOGLE_PICKER_CLIENT_ID')}\n",
        encoding="utf-8",
    )

    settings = load_settings(environ, env_file=env_file)

    # La variable real tiene prioridad; lo demás viene del archivo.
    assert settings.picker_api_key == environ["GOOGLE_PICKER_API_KEY"]
    assert settings.picker_client_id.endswith("-web.apps.googleusercontent.com")


def test_default_env_file_depends_on_the_environment(make_environ, tmp_path, monkeypatch):
    from web.backend.app import config

    monkeypatch.setattr(config, "BACKEND_DIR", tmp_path)
    environ = make_environ("production")
    key = environ.pop("GOOGLE_PICKER_API_KEY")
    (tmp_path / ".env.dev.local").write_text(
        "GOOGLE_PICKER_API_KEY=clave-de-desarrollo\n", encoding="utf-8"
    )
    (tmp_path / ".env.production.local").write_text(
        f"GOOGLE_PICKER_API_KEY={key}\n", encoding="utf-8"
    )

    assert load_settings(environ).picker_api_key == key

    environ = make_environ("development")
    environ.pop("GOOGLE_PICKER_API_KEY")
    assert load_settings(environ).picker_api_key == "clave-de-desarrollo"


# Clientes OAuth ------------------------------------------------------------

def test_desktop_client_must_really_be_desktop(make_environ, tmp_path):
    environ = make_environ(client=desktop_config(kind="web"))

    with pytest.raises(ConfigError, match="escritorio"):
        load(environ, tmp_path)


@pytest.mark.parametrize("content", ["no es json", "[]", '{"installed": {}}'])
def test_invalid_desktop_client_file_is_rejected_without_its_content(
    make_environ, tmp_path, content
):
    environ = make_environ()
    path = tmp_path / "desktop_dev.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigError) as caught:
        load(environ, tmp_path)

    assert content not in str(caught.value) or content == "[]"


def test_config_errors_never_show_the_client_secret(make_environ, tmp_path):
    environ = make_environ(GOOGLE_CLOUD_PROJECT_ID="drive-image-processor-otro")

    with pytest.raises(ConfigError) as caught:
        load(environ, tmp_path)

    assert DESKTOP_SECRET not in str(caught.value)


def test_missing_desktop_client_file_is_reported(make_environ, tmp_path):
    with pytest.raises(ConfigError, match="GOOGLE_DESKTOP_CLIENT_FILE"):
        load(make_environ(GOOGLE_DESKTOP_CLIENT_FILE=str(tmp_path / "x.json")), tmp_path)


def test_desktop_client_must_belong_to_the_configured_project(make_environ, tmp_path):
    environ = make_environ(
        client=desktop_config(project_id="drive-image-processor-otro-dev")
    )

    with pytest.raises(ConfigError, match="proyecto"):
        load(environ, tmp_path)


def test_web_client_must_belong_to_the_same_project_number(make_environ, tmp_path):
    environ = make_environ(
        GOOGLE_PICKER_CLIENT_ID="999999999999-web.apps.googleusercontent.com"
    )

    with pytest.raises(ConfigError, match="GOOGLE_PICKER_CLIENT_ID"):
        load(environ, tmp_path)


def test_desktop_client_id_must_belong_to_the_same_project_number(make_environ, tmp_path):
    environ = make_environ(
        client=desktop_config(client_id="999999999999-d.apps.googleusercontent.com")
    )

    with pytest.raises(ConfigError, match="escritorio"):
        load(environ, tmp_path)


def test_web_and_desktop_clients_must_be_different(make_environ, tmp_path):
    environ = make_environ()
    client = json.loads(
        (tmp_path / "desktop_dev.json").read_text(encoding="utf-8")
    )["installed"]["client_id"]

    with pytest.raises(ConfigError, match="distintos"):
        load({**environ, "GOOGLE_PICKER_CLIENT_ID": client}, tmp_path)


@pytest.mark.parametrize(
    "name",
    [
        "GOOGLE_CLOUD_PROJECT_ID",
        "GOOGLE_CLOUD_PROJECT_NUMBER",
        "GOOGLE_DESKTOP_CLIENT_FILE",
        "GOOGLE_PICKER_CLIENT_ID",
        "GOOGLE_PICKER_API_KEY",
        "LOCALAPPDATA",
    ],
)
def test_missing_variable_is_named(make_environ, tmp_path, name):
    with pytest.raises(ConfigError, match=name):
        load(make_environ(**{name: None}), tmp_path)


def test_project_number_must_be_numeric(make_environ, tmp_path):
    with pytest.raises(ConfigError, match="GOOGLE_CLOUD_PROJECT_NUMBER"):
        load(make_environ(GOOGLE_CLOUD_PROJECT_NUMBER="abc"), tmp_path)


def test_project_number_of_fixtures_is_the_client_prefix():
    assert desktop_config()["installed"]["client_id"].startswith(f"{PROJECT_NUMBER}-")


# Solo loopback --------------------------------------------------------------

def test_settings_always_bind_to_loopback_even_if_the_environment_asks_otherwise(
    make_environ, tmp_path
):
    settings = load(make_environ(DIP_BIND_HOST="0.0.0.0", HOST="0.0.0.0"), tmp_path)

    assert settings.bind_host == "127.0.0.1"
    assert settings.port == 8000


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "example.com", ""])
def test_non_loopback_hosts_are_rejected(host):
    with pytest.raises(ConfigError, match="loopback"):
        ensure_loopback(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_loopback_hosts_are_accepted(host):
    assert ensure_loopback(host) == host
