"""
Consulta y descarga de solo lectura de Google Drive.

    python -m image_processor.drive_cli auth
    python -m image_processor.drive_cli list-folders
    python -m image_processor.drive_cli list-images --folder-id ID [--originals-only]
    python -m image_processor.drive_cli download --folder-id ID --output DIR [--limit N]
    python -m image_processor.drive_cli process-local --folder-id ID --input DIR --output DIR
    python -m image_processor.drive_cli auth-write
    python -m image_processor.drive_cli upload --folder-id ID --input DIR --limit N [--dry-run]
    python -m image_processor.drive_cli logout [--write]

process-local trabaja solo con archivos locales: no se conecta a
Drive ni lee la autorización.

auth-write y upload usan una autorización de escritura separada
(drive.file, en token_write.json). La de solo lectura no se modifica.

Códigos de salida:

0  Operación completada.
1  Error inesperado de Google Drive.
2  Argumentos o carpetas inválidos.
3  Autorización ausente, expirada, revocada o insuficiente.
4  Sin permiso, inexistente o no es una carpeta.
5  Error de red o límite de frecuencia de solicitudes después de los
   reintentos, cuota de la API agotada (sin reintentos) o conexión
   segura (TLS) fallida.
6  download: alguna imagen no se pudo descargar o verificar, o su
   registro quedó pendiente.
   process-local: lote parcial (imágenes rechazadas o fallidas, o
   resultados sin verificar o con el registro pendiente).
   upload: algún PNG se rechazó, falló o quedó con estado incierto.
7  process-local: no se pudo inicializar el modelo; no se procesó nada.
8  process-local: el manifiesto del destino está dañado o el destino
   contiene resultados de otra carpeta de Drive; no se procesó nada.
   upload: el manifiesto de los resultados está dañado; no se subió nada.

En upload, 3 incluye la autorización de escritura ausente o revocada
y 4 una carpeta de destino sin acceso para drive.file.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from . import batch
from .background_removal import SUPPORTED_MODELS, ModelInitializationError
from .google_drive import auth, client, downloads, local_processing, queries, uploads
from .google_drive.errors import (
    DriveAccessError,
    DriveAuthError,
    DriveError,
    DriveNotAFolderError,
    DriveNotFoundError,
    DriveTransientError,
)
from .manifest import ManifestError
from .processing import (
    BACKGROUNDS,
    DEFAULT_BACKGROUND,
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    MAX_SIDE,
    MIN_SIDE,
    OutputOptions,
)


def _connect(secrets_dir, interactive=False, profile=auth.READ, persist=True):
    paths = auth.secret_paths(secrets_dir)
    notices = []
    credentials = auth.get_credentials(
        paths, interactive=interactive, notices=notices, profile=profile,
        persist=persist,
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


def _command_auth_write(args) -> int:
    paths, service = _connect(args.secrets_dir, interactive=True, profile=auth.WRITE)
    account = queries.verify_connection(service)

    who = account.display_name or "(sin nombre)"
    email = f" <{account.email}>" if account.email else ""

    print(f"Autorización de escritura verificada: {who}{email}")
    print(
        f"Permiso: {auth.WRITE_SCOPE} (solo crea archivos propios; no ve "
        "ni modifica los demás)"
    )
    print(f"Autorización guardada en: {paths.write_token}")
    print(f"La autorización de solo lectura ({paths.token}) no se modificó.")
    return 0


def _command_logout(args) -> int:
    paths = auth.secret_paths(args.secrets_dir)
    profile = auth.WRITE if args.write else auth.READ

    if auth.logout(paths, profile):
        print(f"Se eliminó la autorización local: {profile.token_path(paths)}")
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


_LABELS = {
    downloads.DOWNLOADED: "[OK]",
    downloads.VERIFIED: "[VERIFICADA]",
    downloads.RECOVERED: "[RECUPERADA]",
    downloads.REJECTED: "[RECHAZADA]",
    downloads.FAILED: "[ERROR]",
}


def _command_download(args) -> int:
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit debe ser al menos 1.")

    if not 0 < args.max_file_mb <= 1024:
        raise ValueError("--max-file-mb debe estar entre 0 y 1024.")

    _, service = _connect(args.secrets_dir)

    def show(item):
        if item.status == downloads.REJECTED and not args.show_skipped:
            return

        name = item.name or "?"
        line = f"{_LABELS[item.status]} {name} ({item.file_id or '?'})"

        if item.local_name:
            line += f" -> {item.local_name}"

        if item.status == downloads.FAILED:
            line += f": {item.reason} No se publicó ningún archivo."
        elif item.reason:
            line += f": {item.reason}"

        print(line, file=sys.stderr if item.status == downloads.FAILED else sys.stdout)

    summary = downloads.download_folder(
        service,
        args.folder_id,
        args.output,
        limit=args.limit,
        originals_only=args.originals_only,
        max_file_bytes=int(args.max_file_mb * downloads.MIB),
        on_item=show,
    )

    rejected = Counter(
        item.reason for item in summary.items if item.status == downloads.REJECTED
    )

    print(f"\nCarpeta: {summary.folder.name} ({summary.folder.id})")
    print(f"Destino: {args.output}")
    print("\n===== RESUMEN DE LA DESCARGA =====")
    print(f"Descargadas:                {summary.count(downloads.DOWNLOADED)}")
    print(f"Ya verificadas y omitidas:  {summary.count(downloads.VERIFIED)}")

    if summary.count(downloads.RECOVERED):
        print(f"Registros recuperados:      {summary.count(downloads.RECOVERED)}")

    print(f"Rechazadas:                 {sum(rejected.values())}")

    for reason, count in sorted(rejected.items()):
        print(f"  - {reason}: {count}")

    print(f"Fallidas (no publicadas):   {summary.count(downloads.FAILED)}")
    print(f"Bytes descargados:          {_format_size(summary.bytes_downloaded)}")

    if summary.unregistered:
        print(f"Con registro pendiente:     {summary.unregistered}")

    if summary.temp_removed:
        print(f"Temporales antiguos eliminados: {summary.temp_removed}")

    print(
        "\nCada archivo publicado se verificó por tamaño, MD5 de Drive y "
        "Pillow. Las descargas incompletas nunca se publican."
    )

    if summary.aborted is not None:
        raise summary.aborted

    if summary.count(downloads.FAILED) or summary.unregistered:
        return 6

    return 0


_PROCESS_LABELS = {
    batch.PROCESSED: "[OK]",
    batch.VERIFIED: "[VERIFICADA]",
    batch.RECOVERED: "[RECUPERADA]",
    batch.UNVERIFIED: "[SIN VERIFICAR]",
    batch.FAILED: "[ERROR]",
    batch.SEGMENTATION_FAILED: "[SEGMENTACIÓN VACÍA]",
    batch.PLANNED: "[SE PROCESARÍA]",
}


def _is_inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def _command_process_local(args) -> int:
    options = OutputOptions(
        width=args.width, height=args.height, background=args.background
    )

    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit debe ser al menos 1.")

    queries.validate_file_id(args.folder_id)

    # Nada se lee ni se escribe en la carpeta de secretos.
    secrets = auth.secret_paths(args.secrets_dir).directory

    for label, path in (("--input", args.input), ("--output", args.output)):
        if _is_inside(path, secrets):
            raise ValueError(
                f"{label} no puede estar dentro de la carpeta de secretos "
                f"({secrets})."
            )

    if not args.input.is_dir():
        raise ValueError(f"No existe la carpeta de descargas: {args.input}")

    print("\nProcesamiento local de descargas de Drive (sin conexión a Drive)")
    print(f"Carpeta de Drive: {args.folder_id}")
    print(f"Descargas: {args.input}")
    print(f"Destino:   {args.output}")
    print(
        f"Resultado: {options.width} x {options.height} px, fondo "
        f"{options.background}, modelo {args.model}"
    )

    if args.dry_run:
        print("VISTA PREVIA: no se inicializa el modelo ni se escribe nada.")

    print()

    def show(source, item):
        line = (
            f"{_PROCESS_LABELS[item.status]} {source.remote_name} "
            f"({source.file_id})"
        )

        if item.output_name:
            line += f" -> {item.output_name}"

        if item.message and item.status != batch.RECOVERED:
            line += f": {item.message}"

        failed = item.status in (batch.FAILED, batch.SEGMENTATION_FAILED)
        print(line, file=sys.stderr if failed else sys.stdout)

    try:
        result = local_processing.process_downloads(
            args.input,
            args.output,
            args.folder_id,
            model_name=args.model,
            options=options,
            limit=args.limit,
            dry_run=args.dry_run,
            on_item=show,
        )
    except (ManifestError, local_processing.ForeignFolderError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        print("No se procesó ninguna imagen.", file=sys.stderr)
        return 8
    except ModelInitializationError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        print(
            "No se procesó ninguna imagen. Verifique la conexión de red o "
            "la caché de modelos.",
            file=sys.stderr,
        )
        return 7

    discovery = result.discovery
    summary = result.batch

    for item in discovery.rejected:
        print(f"[RECHAZADA] {item.name}: {item.reason}")

    if not discovery.sources and not discovery.rejected:
        print(
            "No hay imágenes descargadas con procedencia de GD-02 para "
            "esta carpeta de Drive."
        )

    rejected = Counter(item.reason for item in discovery.rejected)
    pending = summary.unverified + summary.unregistered
    ready = sum(item.ready for item in result.prepared)

    print("\n===== RESUMEN DEL PROCESAMIENTO =====")

    if args.dry_run:
        print(f"Se procesarían:             {summary.planned}")
    else:
        print(f"Procesadas:                 {summary.processed}")

    print(f"Verificadas previamente:    {summary.skipped}")

    if summary.recovered:
        print(f"Registros recuperados:      {summary.recovered}")

    print(f"Rechazadas:                 {len(discovery.rejected)}")

    for reason, count in sorted(rejected.items()):
        print(f"  - {reason} ({count})")

    print(f"Fallos de segmentación:     {summary.segmentation_failed}")

    other_failures = summary.failed - summary.segmentation_failed
    if other_failures:
        print(f"Otros fallos:               {other_failures}")

    print(f"Resultados pendientes:      {pending}")
    print(f"PNG generados:              {summary.processed}")
    print(f"Listos para subir (GD-03B): {ready} de {len(result.prepared)}")

    if discovery.other_folder:
        print(f"De otra carpeta (excluidas): {len(discovery.other_folder)}")

    if discovery.superseded:
        print(f"Revisiones anteriores:      {len(discovery.superseded)}")

    if summary.temp_removed:
        print(f"Temporales antiguos eliminados: {summary.temp_removed}")

    if summary.errors:
        print("\nErrores encontrados:")
        for error in summary.errors:
            print(f"  - {error}", file=sys.stderr)

    if summary.warnings:
        print("\nAdvertencias:")
        for warning in summary.warnings:
            print(f"  - {warning}", file=sys.stderr)

    if not result.complete:
        print(
            "\nLote parcial: hay imágenes sin procesar o resultados sin "
            "verificar. Revise los mensajes anteriores."
        )
        return 6

    return 0


_UPLOAD_LABELS = {
    uploads.UPLOADED: "[SUBIDA]",
    uploads.VERIFIED: "[VERIFICADA]",
    uploads.RECOVERED: "[CONFIRMADA]",
    uploads.REJECTED: "[RECHAZADA]",
    uploads.FAILED: "[ERROR]",
    uploads.UNCERTAIN: "[INCIERTA]",
    uploads.PLANNED: "[SE SUBIRÍA]",
}


def _command_upload(args) -> int:
    options = OutputOptions(
        width=args.width, height=args.height, background=args.background
    )

    if args.limit < 1:
        raise ValueError("--limit debe ser al menos 1.")

    queries.validate_file_id(args.folder_id)
    target_id = args.target_folder_id or args.folder_id
    queries.validate_file_id(target_id)

    secrets = auth.secret_paths(args.secrets_dir).directory

    if _is_inside(args.input, secrets):
        raise ValueError(
            f"--input no puede estar dentro de la carpeta de secretos ({secrets})."
        )

    if not args.input.is_dir():
        raise ValueError(f"No existe la carpeta de resultados: {args.input}")

    # La vista previa no modifica los tokens (ni al renovarlos).
    persist = not args.dry_run
    _, read_service = _connect(args.secrets_dir, persist=persist)

    try:
        _, write_service = _connect(
            args.secrets_dir, profile=auth.WRITE, persist=persist
        )
    except DriveAuthError as exc:
        if not args.dry_run:
            raise
        write_service = None
        print(
            f"[AVISO] {exc} La vista previa continúa sin comprobar el "
            "acceso de escritura."
        )

    print("\nSubida de resultados a Google Drive")
    print(f"Carpeta de los originales: {args.folder_id}")
    print(f"Resultados locales: {args.input}")
    print(
        f"Resultado: {options.width} x {options.height} px, fondo "
        f"{options.background}, modelo {args.model}"
    )

    if args.dry_run:
        print("VISTA PREVIA: no se escribe nada en Google Drive ni en disco.")

    print()

    def show(item):
        line = f"{_UPLOAD_LABELS[item.status]} {item.output_name}"

        if item.remote_name:
            line += f" -> {item.remote_name}"

        if item.remote_id:
            line += f" ({item.remote_id})"

        if item.reason:
            line += f": {item.reason}"

        failed = item.status in (uploads.FAILED, uploads.UNCERTAIN)
        print(line, file=sys.stderr if failed else sys.stdout)

    try:
        summary = uploads.upload_results(
            read_service,
            write_service,
            args.input,
            args.folder_id,
            target_folder_id=target_id,
            model_name=args.model,
            options=options,
            limit=args.limit,
            dry_run=args.dry_run,
            on_item=show,
        )
    except ManifestError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        print("No se subió ninguna imagen.", file=sys.stderr)
        return 8

    target = summary.target
    verified = summary.count(uploads.VERIFIED) + summary.count(uploads.RECOVERED)

    print(f"\nDestino: {target.name} ({target.id})")
    print("\n===== RESUMEN DE LA SUBIDA =====")

    if args.dry_run:
        print(f"Se subirían:                {summary.count(uploads.PLANNED)}")
    else:
        print(f"Subidas:                    {summary.count(uploads.UPLOADED)}")

    print(f"Verificadas previamente:    {verified}")
    print(f"Rechazadas:                 {summary.count(uploads.REJECTED)}")
    print(f"Fallidas:                   {summary.count(uploads.FAILED)}")
    print(f"Pendientes:                 {summary.count(uploads.UNCERTAIN)}")
    print(f"Bytes subidos:              {_format_size(summary.bytes_uploaded)}")

    if args.dry_run:
        access = {True: "verificado", False: "sin acceso", None: "no verificado"}
        print(f"Permiso de escritura (drive.file): {access[summary.write_access]}")
        print(f"\n[AVISO] {summary.write_access_note}")
        print(
            "La vista previa no garantiza que Google acepte la subida: los "
            "permisos, la cuota o la carpeta pueden cambiar."
        )

    if summary.count(uploads.UNCERTAIN):
        print(
            "\nHay subidas con estado incierto: vuelva a ejecutar el comando; "
            "se comprobarán con su ID reservado, sin crear duplicados."
        )

    if summary.aborted is not None:
        raise summary.aborted

    if not summary.complete:
        return 6

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
        "auth-write",
        help=(
            "Autoriza la creación de archivos propios (drive.file), sin "
            "modificar la autorización de solo lectura"
        ),
    ).set_defaults(handler=_command_auth_write)

    logout = commands.add_parser(
        "logout", help="Elimina la autorización guardada localmente"
    )
    logout.add_argument(
        "--write", action="store_true",
        help="Elimina la autorización de escritura en lugar de la de lectura",
    )
    logout.set_defaults(handler=_command_logout)

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

    download = commands.add_parser(
        "download",
        help="Descarga y verifica las imágenes de una carpeta (sin subcarpetas)",
    )
    download.add_argument("--folder-id", required=True, help="ID de la carpeta")
    download.add_argument(
        "--output", type=Path, required=True, help="Carpeta local de destino"
    )
    download.add_argument(
        "--limit", type=int, default=None,
        help="Máximo de imágenes a descargar o verificar (para pruebas)",
    )
    download.add_argument(
        "--max-file-mb", type=float,
        default=downloads.DEFAULT_MAX_FILE_BYTES / downloads.MIB,
        help="Tamaño máximo por imagen en MiB (por defecto, 25)",
    )
    download.add_argument(
        "--originals-only", action="store_true",
        help="Excluye los resultados TRANSFORMADA_",
    )
    download.add_argument(
        "--show-skipped", action="store_true",
        help="Muestra cada archivo rechazado y el motivo",
    )
    download.set_defaults(handler=_command_download)

    process = commands.add_parser(
        "process-local",
        help=(
            "Procesa las imágenes ya descargadas de una carpeta "
            "(sin conexión a Drive)"
        ),
    )
    process.add_argument(
        "--folder-id", required=True,
        help="ID de la carpeta de Drive de origen de las descargas",
    )
    process.add_argument(
        "--input", type=Path, required=True,
        help="Carpeta de descargas de GD-02",
    )
    process.add_argument(
        "--output", type=Path, required=True,
        help="Carpeta de resultados (puede ser la misma)",
    )
    process.add_argument(
        "--model", choices=SUPPORTED_MODELS,
        default=local_processing.DEFAULT_MODEL,
        help=f"Modelo de eliminación de fondo (por defecto, {local_processing.DEFAULT_MODEL})",
    )
    process.add_argument(
        "--background", choices=BACKGROUNDS, default=DEFAULT_BACKGROUND,
        help=f"Fondo del resultado (por defecto, {DEFAULT_BACKGROUND})",
    )
    process.add_argument(
        "--width", type=int, default=DEFAULT_WIDTH,
        help=f"Ancho en píxeles, entre {MIN_SIDE} y {MAX_SIDE} (por defecto, {DEFAULT_WIDTH})",
    )
    process.add_argument(
        "--height", type=int, default=DEFAULT_HEIGHT,
        help=f"Alto en píxeles, entre {MIN_SIDE} y {MAX_SIDE} (por defecto, {DEFAULT_HEIGHT})",
    )
    process.add_argument(
        "--limit", type=int, default=None,
        help="Máximo de imágenes a procesar o verificar (para pruebas)",
    )
    process.add_argument(
        "--dry-run", action="store_true",
        help="Muestra qué se haría sin inicializar el modelo ni escribir nada",
    )
    process.set_defaults(handler=_command_process_local)

    upload = commands.add_parser(
        "upload",
        help="Sube a Drive los PNG verificados por process-local",
    )
    upload.add_argument(
        "--folder-id", required=True,
        help="ID de la carpeta de Drive de los originales",
    )
    upload.add_argument(
        "--input", type=Path, required=True,
        help="Carpeta de resultados de process-local",
    )
    upload.add_argument(
        "--target-folder-id", default=None,
        help=(
            "Carpeta de Drive de destino (por defecto, la de los "
            "originales). Debe ser accesible para drive.file"
        ),
    )
    upload.add_argument(
        "--limit", type=int, required=True,
        help="Máximo de PNG a subir o verificar (obligatorio)",
    )
    upload.add_argument(
        "--model", choices=SUPPORTED_MODELS,
        default=uploads.DEFAULT_MODEL,
        help=f"Modelo con el que se generaron (por defecto, {uploads.DEFAULT_MODEL})",
    )
    upload.add_argument(
        "--background", choices=BACKGROUNDS, default=DEFAULT_BACKGROUND,
        help=f"Fondo de los resultados (por defecto, {DEFAULT_BACKGROUND})",
    )
    upload.add_argument(
        "--width", type=int, default=DEFAULT_WIDTH,
        help=f"Ancho de los resultados (por defecto, {DEFAULT_WIDTH})",
    )
    upload.add_argument(
        "--height", type=int, default=DEFAULT_HEIGHT,
        help=f"Alto de los resultados (por defecto, {DEFAULT_HEIGHT})",
    )
    upload.add_argument(
        "--dry-run", action="store_true",
        help="Muestra qué se subiría sin escribir nada en Drive",
    )
    upload.set_defaults(handler=_command_upload)

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
