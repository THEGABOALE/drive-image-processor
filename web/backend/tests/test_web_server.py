"""Inicio del servidor local y separación entre la CLI y la aplicación web."""

import ast
import socket
import sys
import threading

import pytest
from conftest import REPO, read_credentials, write_credentials

from image_processor.google_drive import auth
from web.backend.app import server
from web.backend.app.config import ConfigError, load_settings


# Inicio y navegador ---------------------------------------------------------

def test_refuses_a_non_loopback_bind(settings):
    from dataclasses import replace

    with pytest.raises(ConfigError, match="loopback"):
        server.bind_socket(replace(settings, bind_host="0.0.0.0"))


def test_binds_only_to_loopback(settings):
    from dataclasses import replace

    sock = server.bind_socket(replace(settings, port=0))
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()


def test_busy_port_is_reported_clearly(settings):
    from dataclasses import replace

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]

        with pytest.raises(ConfigError, match=str(port)):
            server.bind_socket(replace(settings, port=port))


class FakeServer:
    def __init__(self, started=True):
        self.started = started
        self.should_exit = False


def test_browser_opens_once_the_server_started():
    opened = []
    fake = FakeServer()

    server.open_browser_when_started(
        fake, "http://localhost:5173", opener=opened.append, sleep=lambda s: None
    )

    assert opened == ["http://localhost:5173"]


def test_browser_waits_for_the_server():
    opened = []
    fake = FakeServer(started=False)
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            fake.started = True

    server.open_browser_when_started(
        fake, "http://localhost:8000", opener=opened.append, sleep=sleep
    )

    assert opened == ["http://localhost:8000"]
    assert len(waits) == 3


def test_browser_does_not_open_if_the_server_never_starts():
    opened = []
    fake = FakeServer(started=False)

    server.open_browser_when_started(
        fake, "http://localhost:8000", opener=opened.append, sleep=lambda s: None,
        attempts=5,
    )

    assert opened == []


def test_browser_does_not_open_if_the_server_is_exiting():
    opened = []
    fake = FakeServer(started=False)
    fake.should_exit = True

    server.open_browser_when_started(
        fake, "http://localhost:8000", opener=opened.append, sleep=lambda s: None
    )

    assert opened == []


def test_browser_failure_is_controlled():
    import webbrowser

    def fail(url):
        raise webbrowser.Error("sin navegador")

    server.open_browser_when_started(
        FakeServer(), "http://localhost:8000", opener=fail, sleep=lambda s: None
    )


def test_main_reports_configuration_errors(make_environ, monkeypatch, capsys):
    monkeypatch.setattr(
        server, "load_settings", lambda: (_ for _ in ()).throw(ConfigError("falta algo"))
    )

    assert server.main([]) == 2
    assert "falta algo" in capsys.readouterr().err


def test_main_starts_the_server_without_browser(settings, monkeypatch):
    runs = []
    monkeypatch.setattr(server, "load_settings", lambda: settings)
    monkeypatch.setattr(server, "dpapi_store", lambda *a: object())
    monkeypatch.setattr(server, "ConnectionManager", lambda *a, **k: object())
    monkeypatch.setattr(server, "create_app", lambda *a, **k: object())
    monkeypatch.setattr(
        server, "serve", lambda *a, **k: runs.append(k.get("open_browser"))
    )

    assert server.main(["--no-browser"]) == 0
    assert runs == [False]


def test_serve_opens_the_browser_and_closes_the_socket(settings, monkeypatch):
    from dataclasses import replace

    calls = {}

    class StubServer:
        started = True
        should_exit = False

        def __init__(self, config):
            calls["config"] = config

        def run(self, sockets):
            calls["sockets"] = sockets
            calls["bound"] = sockets[0].getsockname()[0]

    done = threading.Event()

    def open_browser(uv, url):
        calls["url"] = url
        done.set()

    monkeypatch.setattr(server.uvicorn, "Server", StubServer)
    monkeypatch.setattr(server, "open_browser_when_started", open_browser)

    server.serve(object(), replace(settings, port=0))

    assert done.wait(5)
    assert calls["url"] == settings.app_url
    assert calls["bound"] == "127.0.0.1"
    assert calls["sockets"][0].fileno() == -1


# Separación entre la CLI y la aplicación web --------------------------------

def _imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_engine_and_cli_never_import_the_web_app():
    for path in (REPO / "image_processor").rglob("*.py"):
        assert not any(
            name == "web" or name.startswith("web.") for name in _imports(path)
        ), path


def test_cli_environment_variable_is_not_used_by_the_web_app(make_environ, tmp_path):
    secrets_dir = tmp_path / "secretos-cli"
    environ = make_environ(**{auth.SECRETS_DIR_ENV: str(secrets_dir)})

    settings = load_settings(environ, env_file=tmp_path / "x.env")

    assert not settings.data_dir.is_relative_to(secrets_dir)


def test_web_data_folder_is_outside_the_repository_and_the_cli_secrets(settings):
    assert not settings.data_dir.is_relative_to(REPO)
    assert not settings.data_dir.is_relative_to(auth.secret_paths().directory.resolve())


def test_connecting_and_disconnecting_never_touch_the_cli_tokens(
    make_environ, tmp_path, make_manager
):
    secrets = tmp_path / ".secrets"
    secrets.mkdir()
    client = secrets / "credentials.json"
    token = secrets / "token.json"
    write_token = secrets / "token_write.json"
    environ = make_environ()
    client.write_bytes(
        (tmp_path / "desktop_dev.json").read_bytes()
    )
    token.write_text("token de la CLI", encoding="utf-8")
    write_token.write_text("token de escritura de la CLI", encoding="utf-8")
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (client, token, write_token)}

    settings = load_settings(
        {**environ, "GOOGLE_DESKTOP_CLIENT_FILE": str(client)},
        env_file=tmp_path / "x.env",
    )
    manager = make_manager(
        read_credentials(), write_credentials(), settings_=settings
    )
    manager.start()
    manager.services()
    manager.disconnect()

    after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}
    assert after == before
    assert sorted(p.name for p in secrets.iterdir()) == [
        "credentials.json", "token.json", "token_write.json",
    ]


def test_disconnect_never_revokes_at_google(connected):
    # La revocación quita todos los scopes del proyecto (también los de la
    # CLI): desconectar solo borra lo local. El código no conoce ese endpoint.
    source = (REPO / "web/backend/app/connection.py").read_text(encoding="utf-8")

    assert "revoke" not in source.lower()
    assert "oauth2.googleapis.com" not in source


def test_web_backend_never_reads_the_cli_token_paths():
    for path in (REPO / "web/backend/app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for needle in ("token.json", "token_write.json", "load_token", "save_token",
                       "secret_paths("):
            assert needle not in source, (path, needle)


def test_web_backend_has_no_web_client_secret():
    for path in (REPO / "web").rglob("*"):
        if path.suffix not in {".py", ".ts", ".tsx", ".json", ".example"}:
            continue
        if "node_modules" in path.parts or "tests" in path.parts or path.name == "package-lock.json":
            continue
        assert "WEB_CLIENT_SECRET" not in path.read_text(encoding="utf-8"), path


def test_running_the_module_does_not_need_web_deps_for_the_engine():
    code = "import image_processor.drive_cli, sys; assert 'fastapi' not in sys.modules"
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, result.stderr


def test_env_files_are_ignored_by_git():
    import subprocess

    for name in (
        "web/backend/.env.development.local",
        "web/backend/.env.production.local",
        "web/frontend/.env.local",
    ):
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", name], cwd=REPO, timeout=30
        )
        assert ignored.returncode == 0, name


def test_env_example_has_no_values():
    example = REPO / "web/backend/.env.example"

    for line in example.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition("=")
        assert value == "" or name == "DIP_ENV", line
