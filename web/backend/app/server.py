"""
Inicio del servidor local: python -m web.backend.app [--no-browser]

El socket se abre aquí, solo en loopback y antes de abrir el navegador: si el
puerto está ocupado no se abre nada ni se habla con otra aplicación.
"""

import argparse
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from .config import ConfigError, ensure_loopback, load_settings
from .connection import ConnectionManager
from .main import create_app
from .token_store import TokenStoreError, dpapi_store


def bind_socket(settings) -> socket.socket:
    host = ensure_loopback(settings.bind_host)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)

    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)

    try:
        sock.bind((host, settings.port))
    except OSError:
        sock.close()
        raise ConfigError(
            f"No se pudo abrir el puerto {settings.port} en {host}: "
            "¿la aplicación ya está abierta?"
        ) from None

    return sock


def open_browser_when_started(
    server, url: str, *, opener=webbrowser.open, sleep=time.sleep, attempts=300
) -> None:
    for _ in range(attempts):
        if server.should_exit:
            return

        if server.started:
            try:
                opener(url)
            except webbrowser.Error:
                print(f"Abra esta dirección en el navegador: {url}", file=sys.stderr)
            return

        sleep(0.1)


def serve(app, settings, *, open_browser: bool = True, quiet: bool = False) -> None:
    sock = bind_socket(settings)

    try:
        # quiet: sin el registro de cada solicitud (versión empaquetada).
        server = uvicorn.Server(uvicorn.Config(
            app, log_level="warning" if quiet else "info", access_log=not quiet
        ))

        if open_browser:
            threading.Thread(
                target=open_browser_when_started,
                args=(server, settings.app_url),
                daemon=True,
            ).start()

        server.run(sockets=[sock])
    finally:
        sock.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Drive Image Processor (web local)")
    parser.add_argument(
        "--no-browser", action="store_true", help="no abrir el navegador"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="no mostrar cada solicitud"
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        store = dpapi_store(settings.data_dir, settings.environment)
        manager = ConnectionManager(settings, store)
        serve(
            create_app(settings, manager),
            settings,
            open_browser=not args.no_browser,
            quiet=args.quiet,
        )
    except (ConfigError, TokenStoreError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    return 0
