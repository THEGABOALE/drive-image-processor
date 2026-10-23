"""
Política de nombres de los resultados.

1. Un original cuyo nombre base no comparte con otro conserva el
   nombre histórico: TRANSFORMADA_producto.png.
2. Si varios originales comparten nombre base, cada uno agrega
   la extensión de origen: TRANSFORMADA_producto__src-jpg.png.
   El nombre base también se considera compartido si el destino
   ya contiene resultados __src- de ese nombre que ningún original
   actual reclama, para no atribuir un resultado antiguo a otro
   original.
3. Las variantes no predeterminadas agregan al final sus
   dimensiones y su fondo:
   TRANSFORMADA_producto__src-jpg__800x600_transparent.png.
4. Si aun así varios originales reclaman el mismo nombre, ninguno
   de ellos se procesa y cada uno se informa como error.

Los nombres se comparan sin distinguir mayúsculas y en forma
Unicode NFC, de modo conservador respecto de Windows.
"""

import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .processing import OutputOptions


OUTPUT_PREFIX = "TRANSFORMADA_"

SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp"
}

SOURCE_TAGS = sorted(ext[1:] for ext in SUPPORTED_EXTENSIONS)


@dataclass(frozen=True)
class PlannedOutput:
    source: Path
    output_path: Path | None
    error: str | None = None


@dataclass
class OutputPlan:
    entries: list[PlannedOutput] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def name_key(name: str) -> str:
    """Clave para comparar nombres equivalentes en Windows."""

    return unicodedata.normalize("NFC", name).casefold()


def is_eligible_source(path: Path) -> bool:
    """Original compatible que no es un resultado previo."""

    return (
        path.suffix.lower() in SUPPORTED_EXTENSIONS
        and not path.name.upper().startswith(OUTPUT_PREFIX)
    )


def source_tag(path: Path) -> str:
    return path.suffix[1:].lower()


def output_name(
    stem: str,
    options: OutputOptions,
    source_tag: str | None = None,
) -> str:
    """
    Nombre del resultado de una imagen.

        TRANSFORMADA_producto.png
        TRANSFORMADA_producto__src-jpg.png
        TRANSFORMADA_producto__800x600_transparent.png
        TRANSFORMADA_producto__src-jpg__800x600_transparent.png
    """
    name = f"{OUTPUT_PREFIX}{stem}"

    if source_tag is not None:
        name += f"__src-{source_tag}"

    if options != OutputOptions():
        name += (
            f"__{options.width}x{options.height}"
            f"_{options.background}"
        )

    return f"{name}.png"


def plan_outputs(
    sources: list[Path],
    output_dir: Path,
    options: OutputOptions,
    existing_names: list[str] = (),
) -> OutputPlan:
    """
    Asigna un destino independiente a cada original.

    El resultado no depende del orden de sources. Solo usa los
    nombres recibidos: no lee ni escribe en el disco.
    """
    output_dir = Path(output_dir)
    existing = {name_key(name) for name in existing_names}

    ordered = sorted(
        sources, key=lambda path: (name_key(path.name), path.name)
    )

    groups: dict[str, list[Path]] = {}

    for source in ordered:
        groups.setdefault(name_key(source.stem), []).append(source)

    # Primera pasada: solo el tamaño de cada grupo.
    shared = {
        key for key, group in groups.items() if len(group) > 1
    }

    claimed = {
        name_key(
            output_name(
                source.stem,
                options,
                source_tag(source)
                if name_key(source.stem) in shared
                else None,
            )
        )
        for source in ordered
    }

    # Segunda pasada: resultados __src- previos que ningún
    # original actual reclama indican un nombre base ambiguo.
    for key, group in groups.items():
        stem = group[0].stem

        evidence = [
            name_key(output_name(stem, options, tag))
            for tag in SOURCE_TAGS
        ]

        if any(
            item in existing and item not in claimed
            for item in evidence
        ):
            shared.add(key)

    plan = OutputPlan()
    candidates = []

    for key, group in groups.items():
        if key not in shared:
            candidates.append(
                (group[0], output_name(group[0].stem, options))
            )
            continue

        historic = output_name(group[0].stem, options)

        if name_key(historic) in existing:
            plan.warnings.append(
                f"{historic}: resultado ambiguo, porque hay o hubo "
                f"varios originales con el nombre base "
                f"'{group[0].stem}'. No se atribuye a ninguno."
            )

        for source in group:
            candidates.append(
                (
                    source,
                    output_name(
                        source.stem, options, source_tag(source)
                    ),
                )
            )

    candidates.sort(
        key=lambda item: (name_key(item[0].name), item[0].name)
    )

    # Se compara por posición: en Windows, Path("a.jpg") y
    # Path("a.JPG") son iguales aunque sean dos originales.
    claims: dict[str, list[int]] = {}

    for index, (_, name) in enumerate(candidates):
        claims.setdefault(name_key(name), []).append(index)

    for index, (source, name) in enumerate(candidates):
        rivals = [
            candidates[other][0].name
            for other in claims[name_key(name)]
            if other != index
        ]

        if rivals:
            plan.entries.append(
                PlannedOutput(
                    source=source,
                    output_path=None,
                    error=(
                        f"el nombre de salida {name} también "
                        f"corresponde a {', '.join(rivals)}. "
                        "Renombre uno de los originales."
                    ),
                )
            )
        else:
            plan.entries.append(
                PlannedOutput(
                    source=source,
                    output_path=output_dir / name,
                )
            )

    return plan
