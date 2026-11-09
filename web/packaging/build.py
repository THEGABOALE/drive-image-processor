"""
Compila DriveImageProcessor.exe y lo deja en un .zip.

    python web/packaging/build.py --config web/packaging/config

La carpeta de configuración (fuera de Git) tiene app.env, con las variables
de web/backend/.env.example más DIP_ENV, y el JSON del cliente de escritorio
nombrado en GOOGLE_DESKTOP_CLIENT_FILE. Se valida con las mismas reglas de la
aplicación antes de compilar.

El paquete lleva esa configuración en texto: el cliente de escritorio, cuyo
secreto Google no considera confidencial en aplicaciones instaladas, y la clave
de API de Picker, que es pública y está restringida. Nunca lleva tokens.

Pasos: frontend (npm), modelo de IA, PyInstaller (una carpeta, con consola),
configuración, una prueba del ejecutable que además deja la caché de numba, y
el .zip.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

REPO = Path(__file__).resolve().parents[2]
PACKAGING = REPO / "web" / "packaging"
FRONTEND = REPO / "web" / "frontend"
NAME = "DriveImageProcessor"
CLIENT_NAME = "desktop_client.json"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from web.backend.app.config import ConfigError, load_settings  # noqa: E402
from web.backend.app.launcher import CLIENT_FILE_VARIABLE, DEFAULT_MODEL  # noqa: E402

# Lo único que se copia al paquete.
_VARIABLES = (
    "DIP_ENV",
    "GOOGLE_CLOUD_PROJECT_ID",
    "GOOGLE_CLOUD_PROJECT_NUMBER",
    CLIENT_FILE_VARIABLE,
    "GOOGLE_PICKER_CLIENT_ID",
    "GOOGLE_PICKER_API_KEY",
)


class BuildError(Exception):
    """La compilación no puede seguir. Nunca incluye secretos."""


@dataclass(frozen=True)
class CheckedConfig:
    environment: str
    client_file: Path
    values: dict = field(repr=False)
    warning: str | None = None


def check_config(folder: Path) -> CheckedConfig:
    folder = Path(folder)
    env_file = folder / "app.env"

    if not env_file.is_file():
        raise BuildError(f"No se encontró {env_file} (app.env).")

    values = dotenv_values(env_file)

    if not values.get("DIP_ENV"):
        raise BuildError("app.env debe definir DIP_ENV (development o production).")

    name = values.get(CLIENT_FILE_VARIABLE)

    if not name:
        raise BuildError(f"app.env debe definir {CLIENT_FILE_VARIABLE}.")

    client_file = (folder / name).resolve()

    if client_file.parent != folder.resolve():
        raise BuildError(
            f"{CLIENT_FILE_VARIABLE} debe ser un archivo de la carpeta de configuración."
        )

    if not client_file.is_file():
        raise BuildError(f"No se encontró {name} en la carpeta de configuración.")

    selected = {key: values[key] for key in _VARIABLES if values.get(key)}

    with tempfile.TemporaryDirectory() as tmp:
        environ = {**selected, CLIENT_FILE_VARIABLE: str(client_file), "LOCALAPPDATA": tmp}
        try:
            settings = load_settings(environ, env_file=Path(tmp) / "sin-archivo.env")
        except ConfigError as exc:
            raise BuildError(str(exc)) from None

    warning = None

    if settings.environment == "development":
        warning = (
            "Configuración de desarrollo: el ejecutable solo funcionará para los "
            "usuarios de prueba del proyecto."
        )

    return CheckedConfig(settings.environment, client_file, selected, warning)


def pyinstaller_args(output: Path) -> list[str]:
    output = Path(output)
    args = [
        str(PACKAGING / "entry.py"),
        "--name", NAME,
        "--onedir",
        "--console",
        "--noconfirm",
        "--clean",
        "--paths", str(REPO),
        "--distpath", str(output / "dist"),
        "--workpath", str(output / "build"),
        "--specpath", str(output / "build"),
        "--exclude-module", "pytest",
        "--exclude-module", "PyInstaller",
    ]

    for package in ("rembg", "pymatting"):
        args += ["--collect-all", package]

    for package in ("googleapiclient", "jsonschema_specifications"):
        args += ["--collect-data", package]

    for package in ("uvicorn", "web", "image_processor"):
        args += ["--collect-submodules", package]

    args += ["--add-data", f"{FRONTEND / 'dist'};web/frontend/dist"]
    return args


def find_model(model_name: str, *, home: Path | None = None) -> Path:
    if home is None:
        from rembg.sessions.base import BaseSession

        home = Path(BaseSession.rembg_home())

    path = Path(home) / "models" / model_name / f"{model_name}.onnx"

    if not path.is_file() or path.stat().st_size == 0:
        raise BuildError(
            f"No se encontró el modelo {model_name} en {path.parent}. Se descarga al "
            "procesar una imagen por primera vez en este equipo."
        )

    return path


def assemble(program: Path, config: CheckedConfig, model: Path, model_name: str) -> None:
    """Agrega al programa compilado la configuración, el modelo y el LEEME."""

    program = Path(program)
    config_dir = program / "config"
    config_dir.mkdir(exist_ok=True)

    # config.values ya tiene solo las variables conocidas (check_config).
    values = {**config.values, CLIENT_FILE_VARIABLE: CLIENT_NAME}
    (config_dir / "app.env").write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    shutil.copyfile(config.client_file, config_dir / CLIENT_NAME)

    model_dir = program / "rembg" / "models" / model_name
    model_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(model, model_dir / f"{model_name}.onnx")

    readme = (PACKAGING / "LEEME.txt").read_text(encoding="utf-8")

    if config.environment == "development":
        readme += (
            "\nVersión de desarrollo: solo funciona con las cuentas agregadas como "
            "usuarios de prueba del proyecto de Google Cloud.\n"
        )

    (program / "LEEME.txt").write_text(readme, encoding="utf-8")


def make_zip(program: Path, archive: Path) -> Path:
    program = Path(program)
    archive = Path(archive)

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as content:
        for path in sorted(program.rglob("*")):
            if path.is_file():
                content.write(path, path.relative_to(program.parent).as_posix())

    return archive


def _run(command: list[str], **kwargs) -> None:
    try:
        subprocess.run(command, check=True, **kwargs)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BuildError(f"Falló: {' '.join(command[:3])} ({type(exc).__name__}).") from None


def needs_install(frontend: Path) -> bool:
    """
    Si hay que instalar las dependencias del frontend antes de compilar.

    npm deja node_modules/.package-lock.json al instalar. Si falta, o si
    package-lock.json cambió después (otra versión del repositorio con otras
    dependencias), lo instalado no corresponde y la compilación fallaría.
    """
    record = frontend / "node_modules" / ".package-lock.json"
    if not record.is_file():
        return True
    return (frontend / "package-lock.json").stat().st_mtime > record.stat().st_mtime


def build(config_folder: Path, output: Path) -> Path:
    config = check_config(config_folder)

    if config.warning:
        print(f"[AVISO] {config.warning}")

    npm = "npm.cmd" if os.name == "nt" else "npm"
    if needs_install(FRONTEND):
        _run([npm, "ci"], cwd=FRONTEND)
    _run([npm, "run", "build"], cwd=FRONTEND)

    from image_processor import background_removal

    # Descarga el modelo a la caché de rembg si todavía no está.
    background_removal.prepare_model(DEFAULT_MODEL)
    model = find_model(DEFAULT_MODEL)

    import PyInstaller.__main__

    PyInstaller.__main__.run(pyinstaller_args(output))

    program = Path(output) / "dist" / NAME
    assemble(program, config, model, DEFAULT_MODEL)

    print("Probando el ejecutable (también prepara la caché de numba)...")
    _run([str(program / f"{NAME}.exe"), "--selftest"])

    archive = make_zip(program, Path(output) / f"{NAME}-{config.environment}.zip")
    print(f"Listo: {archive} ({archive.stat().st_size / 1e6:.0f} MB)")
    return archive


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compila DriveImageProcessor.exe")
    parser.add_argument("--config", type=Path, default=PACKAGING / "config")
    parser.add_argument("--output", type=Path, default=PACKAGING)
    args = parser.parse_args(argv)

    try:
        build(args.config, args.output)
    except BuildError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
