"""Script de compilación del ejecutable. No ejecuta PyInstaller ni npm."""

import json
import os
import subprocess
import zipfile

import pytest
from conftest import DESKTOP_SECRET, REPO, desktop_config

from web.packaging import build


def config_dir(tmp_path, environment="production", **changes):
    suffix = "prod" if environment == "production" else "dev"
    project = f"drive-image-processor-{suffix}"
    folder = tmp_path / "config"
    folder.mkdir()
    (folder / "desktop_client.json").write_text(
        json.dumps(desktop_config(project_id=project)), encoding="utf-8"
    )
    values = {
        "DIP_ENV": environment,
        "GOOGLE_CLOUD_PROJECT_ID": project,
        "GOOGLE_CLOUD_PROJECT_NUMBER": "123456789012",
        "GOOGLE_DESKTOP_CLIENT_FILE": "desktop_client.json",
        "GOOGLE_PICKER_CLIENT_ID": "123456789012-web.apps.googleusercontent.com",
        "GOOGLE_PICKER_API_KEY": "clave",
    }
    values.update(changes)
    (folder / "app.env").write_text(
        "".join(f"{k}={v}\n" for k, v in values.items() if v is not None), encoding="utf-8"
    )
    return folder


# Validación de la configuración --------------------------------------------

def test_valid_production_configuration(tmp_path):
    checked = build.check_config(config_dir(tmp_path))

    assert checked.environment == "production"
    assert checked.client_file.name == "desktop_client.json"


def test_development_configuration_is_allowed_with_a_warning(tmp_path):
    checked = build.check_config(config_dir(tmp_path, "development"))

    assert checked.environment == "development"
    assert "usuarios de prueba" in checked.warning
    # El proyecto de desarrollo puede estar publicado: no se afirma que no lo esté.
    assert "modo de prueba" in checked.warning


@pytest.mark.parametrize(
    "changes, match",
    [
        ({"DIP_ENV": None}, "DIP_ENV"),
        ({"GOOGLE_DESKTOP_CLIENT_FILE": "otro.json"}, "otro.json"),
        ({"GOOGLE_DESKTOP_CLIENT_FILE": "../desktop_client.json"}, "carpeta"),
        ({"GOOGLE_CLOUD_PROJECT_ID": "drive-image-processor-dev"}, "entorno"),
        ({"GOOGLE_PICKER_CLIENT_ID": "999999999999-web.apps.googleusercontent.com"}, "GOOGLE_PICKER_CLIENT_ID"),
        ({"GOOGLE_PICKER_API_KEY": None}, "GOOGLE_PICKER_API_KEY"),
    ],
)
def test_invalid_configuration_is_rejected_before_building(tmp_path, changes, match):
    with pytest.raises(build.BuildError, match=match):
        build.check_config(config_dir(tmp_path, **changes))


def test_client_file_outside_the_configuration_folder_is_rejected(tmp_path):
    folder = config_dir(tmp_path, GOOGLE_DESKTOP_CLIENT_FILE="../afuera.json")
    (tmp_path / "afuera.json").write_text(
        json.dumps(desktop_config(project_id="drive-image-processor-prod")), encoding="utf-8"
    )

    with pytest.raises(build.BuildError, match="debe ser un archivo de la carpeta"):
        build.check_config(folder)


def test_missing_configuration_folder(tmp_path):
    with pytest.raises(build.BuildError, match="app.env"):
        build.check_config(tmp_path / "no-existe")


def test_errors_never_show_the_client_secret(tmp_path):
    folder = config_dir(tmp_path, GOOGLE_CLOUD_PROJECT_ID="drive-image-processor-otro")

    with pytest.raises(build.BuildError) as caught:
        build.check_config(folder)

    assert DESKTOP_SECRET not in str(caught.value)


def test_checking_never_writes_to_the_real_data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "real"))

    build.check_config(config_dir(tmp_path))

    assert not (tmp_path / "real").exists()


# PyInstaller ---------------------------------------------------------------

def test_pyinstaller_arguments(tmp_path):
    args = build.pyinstaller_args(tmp_path / "salida")

    assert args[0] == str(REPO / "web/packaging/entry.py")
    assert "--onedir" in args
    assert "--onefile" not in args
    assert args[args.index("--name") + 1] == "DriveImageProcessor"
    for package in ("rembg", "pymatting"):
        assert package in [args[i + 1] for i, a in enumerate(args) if a == "--collect-all"]
    data = [args[i + 1] for i, a in enumerate(args) if a == "--add-data"]
    assert any(item.endswith(";web/frontend/dist") for item in data)
    assert str(tmp_path / "salida") in args[args.index("--distpath") + 1]


def test_secrets_are_never_compiled_into_the_program(tmp_path):
    args = " ".join(build.pyinstaller_args(tmp_path / "salida"))

    assert ".env" not in args
    assert "desktop_client" not in args
    assert ".secrets" not in args


# Armado ----------------------------------------------------------------------

def fake_program(tmp_path):
    program = tmp_path / "salida" / "DriveImageProcessor"
    (program / "_internal").mkdir(parents=True)
    (program / "DriveImageProcessor.exe").write_bytes(b"MZ")
    return program


def test_assemble_copies_config_model_and_readme(tmp_path):
    program = fake_program(tmp_path)
    checked = build.check_config(config_dir(tmp_path))
    model = tmp_path / "isnet-general-use.onnx"
    model.write_bytes(b"modelo")

    build.assemble(program, checked, model, "isnet-general-use")

    assert (program / "config" / "app.env").exists()
    assert (program / "config" / "desktop_client.json").exists()
    assert (program / "rembg/models/isnet-general-use/isnet-general-use.onnx").read_bytes() == b"modelo"
    readme = (program / "LEEME.txt").read_text(encoding="utf-8")
    assert "DriveImageProcessor.exe" in readme
    assert "no está verificada" in readme


def test_development_readme_only_asks_for_access_when_google_blocks_the_account(tmp_path):
    program = fake_program(tmp_path)
    model = tmp_path / "m.onnx"
    model.write_bytes(b"x")

    build.assemble(program, build.check_config(config_dir(tmp_path, "development")), model,
                   "isnet-general-use")

    readme = (program / "LEEME.txt").read_text(encoding="utf-8")
    assert "solo funciona con las cuentas" not in readme
    assert "usuario de prueba" in readme


def test_production_readme_has_no_test_user_note(tmp_path):
    program = fake_program(tmp_path)
    model = tmp_path / "m.onnx"
    model.write_bytes(b"x")

    build.assemble(program, build.check_config(config_dir(tmp_path)), model, "isnet-general-use")

    assert "usuario de prueba" not in (program / "LEEME.txt").read_text(encoding="utf-8")


def test_readme_describes_the_current_application():
    # Lo lee quien recibe el .zip: el tamaño y el fondo ya no son fijos.
    readme = (REPO / "web" / "packaging" / "LEEME.txt").read_text(encoding="utf-8")
    text = readme.lower()

    assert "400x400, fondo blanco)" not in text
    assert "tamaño" in text and "transparente" in text
    assert "vista previa" in text
    assert "volver a generar" in text


def test_only_the_needed_configuration_is_copied(tmp_path):
    program = fake_program(tmp_path)
    folder = config_dir(tmp_path)
    (folder / "token.json").write_text("token de la CLI", encoding="utf-8")
    (folder / "notas.txt").write_text("privado", encoding="utf-8")
    model = tmp_path / "m.onnx"
    model.write_bytes(b"x")

    build.assemble(program, build.check_config(folder), model, "isnet-general-use")

    assert sorted(p.name for p in (program / "config").iterdir()) == [
        "app.env", "desktop_client.json",
    ]


def test_the_bundled_env_has_only_the_known_variables(tmp_path):
    program = fake_program(tmp_path)
    folder = config_dir(tmp_path, OTRA_VARIABLE="secreto")
    model = tmp_path / "m.onnx"
    model.write_bytes(b"x")

    build.assemble(program, build.check_config(folder), model, "isnet-general-use")

    bundled = (program / "config" / "app.env").read_text(encoding="utf-8")
    assert "OTRA_VARIABLE" not in bundled
    assert "GOOGLE_PICKER_API_KEY=clave" in bundled


def test_zip_contains_the_program_folder(tmp_path):
    program = fake_program(tmp_path)
    (program / "LEEME.txt").write_text("hola", encoding="utf-8")

    archive = build.make_zip(program, tmp_path / "DriveImageProcessor.zip")

    with zipfile.ZipFile(archive) as content:
        names = content.namelist()
    assert "DriveImageProcessor/DriveImageProcessor.exe" in names
    assert "DriveImageProcessor/LEEME.txt" in names


def test_missing_model_is_reported(tmp_path):
    with pytest.raises(build.BuildError, match="modelo"):
        build.find_model("isnet-general-use", home=tmp_path)


def test_empty_model_file_is_rejected(tmp_path):
    model = tmp_path / "models" / "isnet-general-use" / "isnet-general-use.onnx"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"")

    with pytest.raises(build.BuildError, match="modelo"):
        build.find_model("isnet-general-use", home=tmp_path)


def test_model_is_found_in_the_rembg_cache(tmp_path):
    model = tmp_path / "models" / "isnet-general-use" / "isnet-general-use.onnx"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"x" * 10)

    assert build.find_model("isnet-general-use", home=tmp_path) == model


# Dependencias del frontend ---------------------------------------------------

def frontend_folder(tmp_path, *, installed=True, record=True, lock_newer=False):
    folder = tmp_path / "frontend"
    folder.mkdir()
    lock = folder / "package-lock.json"
    lock.write_text("{}", encoding="utf-8")
    if installed:
        (folder / "node_modules").mkdir()
    if installed and record:
        hidden = folder / "node_modules" / ".package-lock.json"
        hidden.write_text("{}", encoding="utf-8")
        # npm deja ese registro al instalar: más nuevo que el lock, salvo que
        # el lock haya cambiado después (por ejemplo, al traer otra versión).
        older, newer = (hidden, lock) if lock_newer else (lock, hidden)
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))
    return folder


def test_frontend_without_dependencies_needs_install(tmp_path):
    assert build.needs_install(frontend_folder(tmp_path, installed=False))


def test_frontend_without_npm_record_needs_install(tmp_path):
    assert build.needs_install(frontend_folder(tmp_path, record=False))


def test_frontend_with_a_newer_lock_needs_install(tmp_path):
    # Las dependencias quedaron de una versión anterior: compilar fallaría.
    assert build.needs_install(frontend_folder(tmp_path, lock_newer=True))


def test_frontend_installed_from_the_current_lock_is_reused(tmp_path):
    assert not build.needs_install(frontend_folder(tmp_path))


# Git -------------------------------------------------------------------------

def test_build_outputs_and_configuration_are_ignored_by_git():
    for name in (
        "web/packaging/config/app.env",
        "web/packaging/config/desktop_client.json",
        "web/packaging/dist/DriveImageProcessor/DriveImageProcessor.exe",
        "web/packaging/build/x",
        "web/packaging/DriveImageProcessor.zip",
    ):
        ignored = subprocess.run(["git", "check-ignore", "-q", name], cwd=REPO, timeout=30)
        assert ignored.returncode == 0, name
