"""
Arranque de la versión empaquetada (DriveImageProcessor.exe).

El paquete trae su configuración en config/ (app.env y el cliente de
escritorio) y el modelo de IA en rembg/. Esos valores reemplazan a las
variables del equipo: un paquete de producción nunca usa un proyecto de
desarrollo por accidente.

Este módulo no importa el motor: el entorno (por ejemplo, la carpeta del
modelo) tiene que estar listo antes de cargar rembg.

La compilación caché de numba queda junto al programa: el script de
compilación la genera al final, así el primer arranque en otro equipo no
compila todo de nuevo (salvo que su procesador sea distinto).
"""

import json
import os
import sys
import urllib.request
import webbrowser
from pathlib import Path

from dotenv import dotenv_values

from .config import ENVIRONMENTS, LOCAL_ORIGIN, PACKAGED_VARIABLE, ConfigError


# Debe coincidir con el modelo por defecto del motor.
DEFAULT_MODEL = "isnet-general-use"

CLIENT_FILE_VARIABLE = "GOOGLE_DESKTOP_CLIENT_FILE"

# Sin proxy: la aplicación solo se busca en este equipo.
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def prepare_environment(base: Path, environ) -> None:
    """Carga la configuración y el modelo del paquete en environ."""

    config_dir = Path(base) / "config"
    env_file = config_dir / "app.env"

    if not env_file.is_file():
        raise ConfigError(
            f"Falta la configuración del paquete ({env_file}). Vuelva a "
            "descomprimir la aplicación completa."
        )

    values = dotenv_values(env_file)

    if values.get("DIP_ENV") not in ENVIRONMENTS:
        raise ConfigError("La configuración del paquete no define DIP_ENV.")

    for name, value in values.items():
        if value is not None:
            environ[name] = value

    client_file = environ.get(CLIENT_FILE_VARIABLE)

    if client_file and not Path(client_file).is_absolute():
        environ[CLIENT_FILE_VARIABLE] = str(config_dir / client_file)

    environ["REMBG_HOME"] = str(Path(base) / "rembg")
    environ[PACKAGED_VARIABLE] = "1"


def already_running(url: str, *, urlopen=_LOCAL_OPENER.open) -> bool:
    """¿Esta aplicación ya responde en url?"""

    try:
        with urlopen(urllib.request.Request(f"{url}/api/status"), timeout=2) as response:
            body = json.loads(response.read())
    except (OSError, ValueError):
        return False

    return isinstance(body, dict) and "environment" in body and "connection" in body


def selftest(out) -> int:
    """Procesa una imagen de prueba con el modelo del paquete, sin Drive."""

    from PIL import Image, ImageDraw

    from image_processor import background_removal

    image = Image.new("RGB", (320, 240), (240, 240, 240))
    ImageDraw.Draw(image).ellipse((90, 50, 230, 190), fill=(200, 40, 40))

    try:
        cutout = background_removal.remove_background(image, DEFAULT_MODEL)
    except Exception as exc:
        print(f"No se pudo usar el modelo de IA ({type(exc).__name__}).", file=out)
        return 1

    print(f"OK: modelo {DEFAULT_MODEL}, imagen {cutout.size[0]}x{cutout.size[1]}.", file=out)
    return 0


def _wait_for_enter(wait) -> None:
    try:
        wait()
    except EOFError:
        pass


def run(
    argv,
    *,
    check_running=already_running,
    serve_main=None,
    opener=webbrowser.open,
    wait=input,
    out=sys.stdout,
) -> int:
    if "--selftest" in argv:
        return selftest(out)

    print("Drive Image Processor", file=out)

    if check_running(LOCAL_ORIGIN):
        print("La aplicación ya estaba abierta: se muestra en el navegador.", file=out)
        opener(LOCAL_ORIGIN)
        return 0

    print(
        "Iniciando... La primera vez puede tardar un minuto.\n"
        f"Se abrirá en el navegador: {LOCAL_ORIGIN}\n"
        "Para salir, cierre esta ventana.",
        file=out,
        flush=True,
    )

    if serve_main is None:
        from .server import main as serve_main

    code = serve_main(["--quiet", *argv])

    if code:
        print("\nNo se pudo iniciar la aplicación. Presione Enter para cerrar.", file=out)
        _wait_for_enter(wait)

    return code


def main() -> int:
    """Entrada del ejecutable: el paquete está junto a él."""

    try:
        prepare_environment(Path(sys.executable).parent, os.environ)
    except ConfigError as exc:
        print(f"Error: {exc}\nPresione Enter para cerrar.")
        _wait_for_enter(input)
        return 2

    return run(sys.argv[1:])
