"""
Consulta de solo lectura de Google Drive.

    python -m image_processor.drive_cli auth
    python -m image_processor.drive_cli list-folders
    python -m image_processor.drive_cli list-images --folder-id ID [--originals-only]
    python -m image_processor.drive_cli logout

Códigos de salida:

0  Operación completada.
1  Error inesperado de Google Drive.
2  Argumentos inválidos.
3  Autorización ausente, expirada, revocada o insuficiente.
4  Sin permiso, inexistente o no es una carpeta.
5  Error de red o límite de solicitudes después de los reintentos.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from .google_drive import auth, client, queries
from .google_drive.errors import (
    DriveAccessError,
    DriveAuthError,
    DriveError,
    DriveNotAFolderError,
    DriveNotFoundError,
    DriveTransientError,
)


def _connect(secrets_dir, interactive=False):
    paths = auth.secret_paths(secrets_dir)
    notices = []
    credentials = auth.get_credentials(
        paths, interactive=interactive, notices=notices
    )

    for notice in notices:
        print(f"[AVISO] {notice}")

    return paths, client.build_service(credentials)


def _format_size(size: int | None) -> str:
    return "?" if size is None else f"{size:,}".replace(",", ".")


def _command_auth(args) -> int:
    paths, service = _connect(args.secrets_dir, interactive=True)
    account = queries.verify_connection(service)

    who = account.display_name or "(sin nombre)"
    email = f" <{account.email}>" if account.email else ""

    print(f"Conexión verificada: {who}{email}")
    print(f"Permiso: {auth.READONLY_SCOPE} (solo lectura)")
    print(f"Autorización guardada en: {paths.token}")
    return 0


def _command_logout(args) -> int:
    paths = auth.secret_paths(args.secrets_dir)

    if auth.logout(paths):
        print(f"Se eliminó la autorización local: {paths.token}")
        print(
            "Para revocar el acceso en Google, use la configuración de "
            "seguridad de su cuenta (aplicaciones de terceros)."
        )
    else:
        print("No había una autorización local guardada.")

    return 0


def _command_list_folders(args) -> int:
    _, service = _connect(args.secrets_dir)
    folders = queries.list_folders(service)

    if not folders:
        print("La cuenta no tiene carpetas accesibles.")
        return 0

    names = Counter(folder.name.casefold() for folder in folders)

    print(f"Carpetas accesibles: {len(folders)}\n")
    print("ID\tNOMBRE\tCARPETA SUPERIOR")

    for folder in folders:
        parents = ", ".join(folder.parents) or "-"
        repeated = (
            "  (nombre repetido: use el ID)"
            if names[folder.name.casefold()] > 1
            else ""
        )
        print(f"{folder.id}\t{folder.name}\t{parents}{repeated}")

    return 0


def _command_list_images(args) -> int:
    _, service = _connect(args.secrets_dir)
    listing = queries.list_images(
        service, args.folder_id, originals_only=args.originals_only
    )

    folder = listing.folder
    kind = "originales" if args.originals_only else "imágenes"

    print(f"Carpeta: {folder.name} ({folder.id})")

    if not listing.images:
        print(f"La carpeta no contiene {kind} admitidas.")
    else:
        print(f"{kind.capitalize()} admitidas: {len(listing.images)}\n")
        print("ID\tNOMBRE\tMIME\tBYTES\tMODIFICADO\tMD5")

        for image in listing.images:
            print(
                f"{image.id}\t{image.name}\t{image.mime_type}\t"
                f"{_format_size(image.size)}\t{image.modified_time or '?'}\t"
                f"{image.md5_checksum or '-'}"
            )

    if listing.skipped:
        print("\nArchivos no incluidos:")

        for reason, count in sorted(
            Counter(item.reason for item in listing.skipped).items()
        ):
            print(f"  - {reason}: {count}")

        if args.show_skipped:
            for item in listing.skipped:
                print(
                    f"    {item.id or '?'}\t{item.name or '?'}\t{item.reason}"
                )

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Consulta de solo lectura de Google Drive"
    )
    parser.add_argument(
        "--secrets-dir",
        type=Path,
        default=None,
        help=(
            "Carpeta privada con credentials.json y token.json "
            f"(por defecto, ${auth.SECRETS_DIR_ENV} o .secrets)"
        ),
    )

    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "auth", help="Autoriza la cuenta en el navegador y verifica la conexión"
    ).set_defaults(handler=_command_auth)

    commands.add_parser(
        "logout", help="Elimina la autorización guardada localmente"
    ).set_defaults(handler=_command_logout)

    commands.add_parser(
        "list-folders", help="Lista las carpetas accesibles con su ID"
    ).set_defaults(handler=_command_list_folders)

    images = commands.add_parser(
        "list-images", help="Lista las imágenes de una carpeta (sin subcarpetas)"
    )
    images.add_argument("--folder-id", required=True, help="ID de la carpeta")
    images.add_argument(
        "--originals-only",
        action="store_true",
        help="Excluye los resultados TRANSFORMADA_",
    )
    images.add_argument(
        "--show-skipped",
        action="store_true",
        help="Muestra cada archivo no incluido y el motivo",
    )
    images.set_defaults(handler=_command_list_images)

    args = parser.parse_args(argv)

    try:
        return args.handler(args)

    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    except DriveAuthError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 3

    except (DriveAccessError, DriveNotFoundError, DriveNotAFolderError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 4

    except DriveTransientError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 5

    except DriveError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
