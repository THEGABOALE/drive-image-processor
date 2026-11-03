"""Lanzador de la versión empaquetada (DriveImageProcessor.exe)."""

import io
import json
import subprocess
import sys

import pytest
from conftest import REPO, desktop_config

from web.backend.app import launcher


def bundle(tmp_path, env_lines=None, client_name="desktop_client.json"):
    base = tmp_path / "DriveImageProcessor"
    config = base / "config"
    config.mkdir(parents=True)
    (config / client_name).write_text(
        json.dumps(desktop_config(project_id="drive-image-processor-prod")), encoding="utf-8"
    )
    lines = env_lines if env_lines is not None else [
        "DIP_ENV=production",
        "GOOGLE_CLOUD_PROJECT_ID=drive-image-processor-prod",
        "GOOGLE_CLOUD_PROJECT_NUMBER=123456789012",
        f"GOOGLE_DESKTOP_CLIENT_FILE={client_name}",
        "GOOGLE_PICKER_CLIENT_ID=123456789012-web.apps.googleusercontent.com",
        "GOOGLE_PICKER_API_KEY=clave",
    ]
    (config / "app.env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return base


# Entorno del paquete ------------------------------------------------------------

def test_bundled_configuration_is_loaded(tmp_path):
    base = bundle(tmp_path)
    environ = {"LOCALAPPDATA": str(tmp_path / "local")}

    launcher.prepare_environment(base, environ)

    assert environ["DIP_ENV"] == "production"
    assert environ["GOOGLE_CLOUD_PROJECT_ID"] == "drive-image-processor-prod"


def test_client_file_is_resolved_inside_the_bundle(tmp_path):
    base = bundle(tmp_path)
    environ = {"LOCALAPPDATA": str(tmp_path / "local")}

    launcher.prepare_environment(base, environ)

    assert environ["GOOGLE_DESKTOP_CLIENT_FILE"] == str(base / "config" / "desktop_client.json")


def test_bundled_model_folder_is_used(tmp_path):
    base = bundle(tmp_path)
    environ = {"LOCALAPPDATA": str(tmp_path / "local")}

    launcher.prepare_environment(base, environ)

    assert environ["REMBG_HOME"] == str(base / "rembg")


def test_numba_keeps_the_cache_shipped_with_the_program(tmp_path):
    # Con NUMBA_CACHE_DIR numba ignoraría la caché que trae el paquete.
    base = bundle(tmp_path)
    environ = {"LOCALAPPDATA": str(tmp_path / "local")}

    launcher.prepare_environment(base, environ)

    assert "NUMBA_CACHE_DIR" not in environ


def test_launcher_model_is_the_engine_default():
    from image_processor.google_drive import local_processing

    assert launcher.DEFAULT_MODEL == local_processing.DEFAULT_MODEL


def test_main_reports_a_broken_bundle_and_waits(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(launcher.sys, "executable", str(tmp_path / "DriveImageProcessor.exe"))
    waited = []
    monkeypatch.setattr("builtins.input", lambda *a: waited.append(True))

    assert launcher.main() == 2
    assert "config" in capsys.readouterr().out
    assert waited == [True]


def test_main_prepares_the_bundle_before_running(tmp_path, monkeypatch):
    base = bundle(tmp_path)
    monkeypatch.setattr(launcher.sys, "executable", str(base / "DriveImageProcessor.exe"))
    monkeypatch.setattr(launcher.sys, "argv", ["DriveImageProcessor.exe", "--selftest"])
    environ = {"LOCALAPPDATA": str(tmp_path / "local")}
    monkeypatch.setattr(launcher.os, "environ", environ)
    seen = []
    monkeypatch.setattr(launcher, "run", lambda argv: seen.append((argv, dict(environ))) or 0)

    assert launcher.main() == 0
    argv, prepared = seen[0]
    assert argv == ["--selftest"]
    assert prepared["REMBG_HOME"] == str(base / "rembg")


def test_the_bundle_wins_over_variables_of_the_development_environment(tmp_path):
    # Un equipo con variables de desarrollo no cambia el proyecto del paquete.
    base = bundle(tmp_path)
    environ = {
        "LOCALAPPDATA": str(tmp_path / "local"),
        "DIP_ENV": "development",
        "GOOGLE_CLOUD_PROJECT_ID": "drive-image-processor-dev",
        "REMBG_HOME": "otra",
    }

    launcher.prepare_environment(base, environ)

    assert environ["DIP_ENV"] == "production"
    assert environ["GOOGLE_CLOUD_PROJECT_ID"] == "drive-image-processor-prod"
    assert environ["REMBG_HOME"] == str(base / "rembg")


def test_missing_bundled_configuration_is_reported(tmp_path):
    base = tmp_path / "vacío"
    base.mkdir()

    with pytest.raises(launcher.ConfigError, match="config"):
        launcher.prepare_environment(base, {"LOCALAPPDATA": str(tmp_path)})


def test_bundle_without_environment_is_rejected(tmp_path):
    base = bundle(tmp_path, env_lines=["GOOGLE_CLOUD_PROJECT_ID=x"])

    with pytest.raises(launcher.ConfigError, match="DIP_ENV"):
        launcher.prepare_environment(base, {"LOCALAPPDATA": str(tmp_path)})


def test_bundled_settings_pass_the_app_validation(tmp_path):
    from web.backend.app.config import load_settings

    base = bundle(tmp_path)
    environ = {"LOCALAPPDATA": str(tmp_path / "local")}
    launcher.prepare_environment(base, environ)

    settings = load_settings(environ, env_file=tmp_path / "no-existe.env")

    assert settings.environment == "production"
    assert settings.app_url == "http://localhost:8000"


def test_preparing_the_environment_never_imports_the_model():
    code = (
        "import sys; from web.backend.app import launcher; "
        "assert 'rembg' not in sys.modules and 'numba' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, result.stderr


# Arranque -----------------------------------------------------------------------

class Calls:
    def __init__(self, running=False, code=0):
        self.running = running
        self.code = code
        self.opened = []
        self.served = []
        self.waited = 0

    def check(self, url):
        return self.running

    def serve(self, argv):
        self.served.append(argv)
        return self.code

    def wait(self):
        self.waited += 1


def run(calls, argv=(), out=None):
    return launcher.run(
        list(argv),
        check_running=calls.check,
        serve_main=calls.serve,
        opener=calls.opened.append,
        wait=calls.wait,
        out=out or io.StringIO(),
    )


def test_starts_the_quiet_server():
    calls = Calls()

    assert run(calls) == 0
    assert calls.served == [["--quiet"]]
    assert calls.opened == []
    assert calls.waited == 0


def test_if_it_is_already_open_only_the_browser_opens():
    calls = Calls(running=True)
    out = io.StringIO()

    assert run(calls, out=out) == 0
    assert calls.opened == ["http://localhost:8000"]
    assert calls.served == []
    assert "ya estaba abierta" in out.getvalue()


def test_errors_wait_before_closing_the_window():
    calls = Calls(code=2)
    out = io.StringIO()

    assert run(calls, out=out) == 2
    assert calls.waited == 1
    assert "Enter" in out.getvalue()


def test_tells_how_to_close_the_application():
    out = io.StringIO()
    run(Calls(), out=out)

    assert "cierre esta ventana" in out.getvalue().lower()


def test_detects_the_application_by_its_status():
    class Response:
        status = 200

        def __init__(self, body):
            self.body = body

        def read(self):
            return json.dumps(self.body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    ours = launcher.already_running(
        "http://localhost:8000",
        urlopen=lambda request, timeout: Response({"environment": "production", "connection": {}}),
    )
    other = launcher.already_running(
        "http://localhost:8000", urlopen=lambda request, timeout: Response({"hola": 1})
    )

    def refused(request, timeout):
        raise OSError("conexión rechazada")

    assert ours is True
    assert other is False
    assert launcher.already_running("http://localhost:8000", urlopen=refused) is False


def test_selftest_runs_without_starting_the_server(monkeypatch):
    calls = Calls()
    ran = []
    monkeypatch.setattr(launcher, "selftest", lambda out: ran.append(True) or 0)

    assert run(calls, argv=["--selftest"]) == 0
    assert ran == [True]
    assert calls.served == []


def test_selftest_uses_the_engine_model(monkeypatch):
    from image_processor import background_removal

    used = []

    def cutout(image, model_name):
        used.append(model_name)
        return image.convert("RGBA")

    monkeypatch.setattr(background_removal, "remove_background", cutout)
    out = io.StringIO()

    assert launcher.selftest(out) == 0
    assert used == [launcher.DEFAULT_MODEL]
    assert "OK" in out.getvalue()


def test_selftest_reports_a_model_failure(monkeypatch):
    from image_processor import background_removal
    from image_processor.background_removal import ModelInitializationError

    def fail(image, model_name):
        raise ModelInitializationError("sin modelo")

    monkeypatch.setattr(background_removal, "remove_background", fail)
    out = io.StringIO()

    assert launcher.selftest(out) == 1
    assert "modelo" in out.getvalue().lower()


# Servidor silencioso ---------------------------------------------------------

def test_quiet_server_hides_the_access_log(settings, monkeypatch):
    from dataclasses import replace

    from web.backend.app import server

    configs = []

    class StubServer:
        started = True
        should_exit = False

        def __init__(self, config):
            configs.append(config)

        def run(self, sockets):
            pass

    monkeypatch.setattr(server.uvicorn, "Server", StubServer)

    server.serve(object(), replace(settings, port=0), open_browser=False, quiet=True)
    server.serve(object(), replace(settings, port=0), open_browser=False)

    assert configs[0].access_log is False
    assert configs[0].log_level == "warning"
    assert configs[1].access_log is True


def test_main_passes_quiet(settings, monkeypatch):
    from web.backend.app import server

    runs = []
    monkeypatch.setattr(server, "load_settings", lambda: settings)
    monkeypatch.setattr(server, "dpapi_store", lambda *a: object())
    monkeypatch.setattr(server, "ConnectionManager", lambda *a, **k: object())
    monkeypatch.setattr(server, "create_app", lambda *a, **k: object())
    monkeypatch.setattr(server, "serve", lambda *a, **k: runs.append(k.get("quiet")))

    assert server.main(["--quiet"]) == 0
    assert server.main([]) == 0
    assert runs == [True, False]
