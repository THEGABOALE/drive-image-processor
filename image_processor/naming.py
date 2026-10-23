"""
Política de nombres de los resultados.

Un nombre se compone de bloques, siempre en este orden:

    TRANSFORMADA_<nombre base>[__esc][__src-<ext>][__<ancho>x<alto>_<fondo>][__rev-<12 hex>].png

El bloque __rev- lo agrega el manifiesto (ver manifest.py y
batch.py) cuando el nombre base ya pertenece a otro resultado.

1. "__" es un separador reservado. Un nombre base que no lo
   contiene se usa literal, así que los nombres ordinarios
   conservan su resultado histórico: TRANSFORMADA_producto.png.
2. Un nombre base que contiene "__" se codifica ("%" -> "%25",
   "_" -> "%5F") y se marca con __esc. Así ningún nombre de
   fotografía puede imitar los bloques del sistema y cada
   combinación de original y variante tiene un nombre propio.
3. Si varios originales comparten nombre base, cada uno agrega
   la extensión de origen: TRANSFORMADA_producto__src-jpg.png.
   El nombre base también se considera compartido si el destino
   contiene resultados __src- de ese nombre en cualquier variante,
   para no atribuir un resultado antiguo a otro original.
4. Las variantes no predeterminadas agregan al final sus
   dimensiones y su fondo.
5. Si varios originales reclaman el mismo nombre, ninguno se
   procesa y cada uno se informa como error.
6. Un resultado existente que una versión anterior pudo generar
   para otro original actual (antes de codificar "__") no se
   atribuye a nadie: se informa como error.

Los nombres se comparan sin distinguir mayúsculas y en forma
Unicode NFC, de modo conservador respecto de Windows.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .processing import BACKGROUNDS, OutputOptions


OUTPUT_PREFIX = "TRANSFORMADA_"

SUPPORTED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp"
}

SOURCE_TAGS = sorted(ext[1:] for ext in SUPPORTED_EXTENSIONS)

SEPARATOR = "__"
ESCAPE_MARKER = "__esc"

_TAGS = "|".join(SOURCE_TAGS)
_BACKGROUNDS = "|".join(BACKGROUNDS)

_REVISION = r"__rev-[0-9a-f]{12}"

# Bloques que siguen al nombre base, en minúsculas (name_key).
_SOURCE_AND_VARIANT = re.compile(
    rf"(?:{_TAGS})(?:__\d+x\d+_(?:{_BACKGROUNDS}))?(?:{_REVISION})?\.png"
)

# Lo que una versión anterior agregaba al nombre base literal.
_LEGACY_VARIANT = re.compile(
    rf"(?:__(?P<width>\d+)x(?P<height>\d+)_(?P<background>{_BACKGROUNDS}))?\.png"
)


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


def is_ordinary_stem(stem: str) -> bool:
    """Un nombre base sin el separador reservado se usa literal."""

    return SEPARATOR not in stem


def encode_stem(stem: str) -> str:
    """
    Nombre base tal como aparece en el resultado.

    Los nombres ordinarios no cambian. Los que contienen "__"
    se codifican sin "_" y se marcan con __esc.
    """
    if is_ordinary_stem(stem):
        return stem

    encoded = stem.replace("%", "%25").replace("_", "%5F")

    return f"{encoded}{ESCAPE_MARKER}"


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
        TRANSFORMADA_producto%5F%5Fsrc-jpg__esc.png
    """
    name = f"{OUTPUT_PREFIX}{encode_stem(stem)}"

    if source_tag is not None:
        name += f"__src-{source_tag}"

    if options != OutputOptions():
        name += (
            f"__{options.width}x{options.height}"
            f"_{options.background}"
        )

    return f"{name}.png"


def with_revision(name: str, revision: str) -> str:
    """Agrega el bloque __rev-<revisión> a un nombre de resultado."""

    if not re.fullmatch(r"[0-9a-f]{12}", revision):
        raise ValueError(f"Revisión inválida: {revision}")

    return f"{name[:-len('.png')]}__rev-{revision}.png"


def _has_source_suffix_results(stem: str, existing: set[str]) -> bool:
    """¿Hay resultados __src- de este nombre base, en cualquier variante?"""

    prefix = name_key(f"{OUTPUT_PREFIX}{encode_stem(stem)}__src-")

    return any(
        key.startswith(prefix)
        and _SOURCE_AND_VARIANT.fullmatch(key[len(prefix):])
        for key in existing
    )


def expected_format(options: OutputOptions) -> tuple[tuple[int, int], str]:
    """Tamaño y modo exactos del PNG de una variante."""

    mode = "RGBA" if options.background == "transparent" else "RGB"

    return (options.width, options.height), mode


def _legacy_variant(name: str, stem: str) -> OutputOptions | None:
    """
    Variante con la que una versión anterior, que usaba el nombre
    base literal, habría guardado este nombre para stem.
    """
    key = name_key(name)
    prefix = name_key(f"{OUTPUT_PREFIX}{stem}")

    if not key.startswith(prefix):
        return None

    match = _LEGACY_VARIANT.fullmatch(key[len(prefix):])

    if match is None:
        return None

    if match["width"] is None:
        return OutputOptions()

    try:
        return OutputOptions(
            int(match["width"]),
            int(match["height"]),
            match["background"],
        )
    except ValueError:
        return None


def _legacy_owners(
    name: str,
    source_index: int,
    candidates: list[tuple[Path, str]],
    options: OutputOptions,
    existing: set[str],
    describe_existing,
) -> list[str]:
    """
    Originales actuales a los que una versión anterior, sin
    codificar "__", pudo haber guardado este resultado existente.
    """
    owners = []

    for index, (other, other_name) in enumerate(candidates):
        if index == source_index or is_ordinary_stem(other.stem):
            continue

        legacy = _legacy_variant(name, other.stem)

        if legacy is None:
            continue

        if legacy == options:
            # Misma variante: si el otro original ya tiene su
            # resultado con el nombre actual, este no es suyo.
            if name_key(other_name) in existing:
                continue
        else:
            # Variante distinta: el formato del archivo indica
            # cuál de las dos lo generó.
            found = (
                describe_existing(name)
                if describe_existing is not None
                else None
            )

            if found == expected_format(options):
                continue

        owners.append(other.name)

    return owners


def plan_outputs(
    sources: list[Path],
    output_dir: Path,
    options: OutputOptions,
    existing_names: list[str] = (),
    describe_existing=None,
    known_names: set[str] = frozenset(),
) -> OutputPlan:
    """
    Asigna un destino independiente a cada original.

    El resultado no depende del orden de sources. Solo usa los
    nombres recibidos y, si se indica, describe_existing(nombre),
    que devuelve el (tamaño, modo) de un resultado existente o
    None. No escribe en el disco.

    known_names son las claves (name_key) de los resultados que
    registra el manifiesto: su procedencia ya se conoce, así que
    no se evalúan como posibles resultados de una versión anterior.
    """
    output_dir = Path(output_dir)
    existing = {name_key(name) for name in existing_names}
    known = set(known_names)

    ordered = sorted(
        sources, key=lambda path: (name_key(path.name), path.name)
    )

    groups: dict[str, list[Path]] = {}

    for source in ordered:
        groups.setdefault(name_key(source.stem), []).append(source)

    # Un nombre base es compartido si hay varios originales, o si
    # el destino ya tiene resultados __src- suyos en alguna variante.
    shared = {
        key
        for key, group in groups.items()
        if len(group) > 1
        or _has_source_suffix_results(group[0].stem, existing)
    }

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
            continue

        owners = (
            _legacy_owners(
                name,
                index,
                candidates,
                options,
                existing,
                describe_existing,
            )
            if name_key(name) in existing and name_key(name) not in known
            else []
        )

        if owners:
            plan.entries.append(
                PlannedOutput(
                    source=source,
                    output_path=None,
                    error=(
                        f"el resultado existente {name} pudo haberlo "
                        f"generado una versión anterior para "
                        f"{', '.join(owners)}. No se atribuye ni se "
                        "sobrescribe: muévalo o renómbrelo para "
                        "volver a procesar."
                    ),
                )
            )
            continue

        plan.entries.append(
            PlannedOutput(
                source=source,
                output_path=output_dir / name,
            )
        )

    return plan
