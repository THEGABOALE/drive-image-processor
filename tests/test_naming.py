
from itertools import permutations
from pathlib import Path

import pytest

from image_processor.naming import (
    is_eligible_source,
    output_name,
    plan_outputs,
    with_revision,
)
from image_processor.processing import OutputOptions


OUT = Path("salida")
TRANSPARENT = OutputOptions(800, 600, "transparent")


def names(plan):
    """Nombre planificado o error de cada original."""

    return {
        entry.source.name: (
            entry.output_path.name
            if entry.output_path is not None
            else "ERROR"
        )
        for entry in plan.entries
    }


def plan(sources, options=OutputOptions(), existing=()):
    return plan_outputs(
        [Path(name) for name in sources], OUT, options, list(existing)
    )


def test_single_original_keeps_historic_name():
    assert names(plan(["producto.jpg"])) == {
        "producto.jpg": "TRANSFORMADA_producto.png"
    }
    assert names(plan(["producto.jpg"], TRANSPARENT)) == {
        "producto.jpg": "TRANSFORMADA_producto__800x600_transparent.png"
    }


def test_same_stem_gets_source_suffix():
    result = plan(["producto.jpg", "producto.png", "producto.webp"])

    assert names(result) == {
        "producto.jpg": "TRANSFORMADA_producto__src-jpg.png",
        "producto.png": "TRANSFORMADA_producto__src-png.png",
        "producto.webp": "TRANSFORMADA_producto__src-webp.png",
    }
    assert result.warnings == []


def test_jpg_and_jpeg_are_different_sources():
    assert names(plan(["foto.jpg", "foto.jpeg"])) == {
        "foto.jpeg": "TRANSFORMADA_foto__src-jpeg.png",
        "foto.jpg": "TRANSFORMADA_foto__src-jpg.png",
    }


def test_source_suffix_combines_with_variant():
    assert names(plan(["producto.jpg", "producto.png"], TRANSPARENT)) == {
        "producto.jpg": (
            "TRANSFORMADA_producto__src-jpg__800x600_transparent.png"
        ),
        "producto.png": (
            "TRANSFORMADA_producto__src-png__800x600_transparent.png"
        ),
    }


def test_variants_never_share_names():
    variants = [
        OutputOptions(),
        OutputOptions(background="transparent"),
        OutputOptions(800, 600, "white"),
        OutputOptions(800, 600, "transparent"),
        OutputOptions(600, 800, "white"),
    ]
    sources = ["producto.jpg", "producto.png", "unico.webp"]

    all_names = [
        name
        for options in variants
        for name in names(plan(sources, options)).values()
    ]

    assert len({name.casefold() for name in all_names}) == 15


def test_original_with_src_like_suffix_without_collision():
    # producto.jpg no comparte nombre base: no hay sufijos.
    # El nombre con "__" se codifica y no imita un bloque __src-.
    assert names(plan(["producto.jpg", "producto__src-jpg.png"])) == {
        "producto.jpg": "TRANSFORMADA_producto.png",
        "producto__src-jpg.png": "TRANSFORMADA_producto%5F%5Fsrc-jpg__esc.png",
    }


def test_original_with_src_like_suffix_on_later_run():
    # Segunda ejecución con los nombres actuales: nada cambia.
    sources = ["producto.jpg", "producto__src-jpg.png"]
    first = names(plan(sources))

    result = plan(sources, existing=list(first.values()))

    assert names(result) == first
    assert result.warnings == []


def test_legacy_src_like_result_is_reported_as_ambiguous():
    # Resultados de una versión anterior: el de producto__src-jpg.png
    # se guardó como TRANSFORMADA_producto__src-jpg.png.
    result = plan(
        ["producto.jpg", "producto__src-jpg.png"],
        existing=[
            "TRANSFORMADA_producto.png",
            "TRANSFORMADA_producto__src-jpg.png",
        ],
    )

    assert names(result) == {
        "producto.jpg": "ERROR",
        "producto__src-jpg.png": "TRANSFORMADA_producto%5F%5Fsrc-jpg__esc.png",
    }

    error = next(entry.error for entry in result.entries if entry.error)

    assert "TRANSFORMADA_producto__src-jpg.png" in error
    assert "producto__src-jpg.png" in error
    assert len(result.warnings) == 1


def test_src_like_original_is_disambiguated_from_shared_stem():
    # Antes era una colisión sin solución; ahora cada original
    # tiene su propio destino.
    result = plan(
        ["producto.jpg", "producto.png", "producto__src-jpg.png"]
    )

    assert names(result) == {
        "producto.jpg": "TRANSFORMADA_producto__src-jpg.png",
        "producto.png": "TRANSFORMADA_producto__src-png.png",
        "producto__src-jpg.png": "TRANSFORMADA_producto%5F%5Fsrc-jpg__esc.png",
    }
    assert all(entry.error is None for entry in result.entries)


def test_unresolvable_collision_is_an_explicit_error():
    # Posible incluso en Windows: NTFS no normaliza Unicode.
    composed = "café.jpg"
    decomposed = "café.jpg"

    result = plan([composed, decomposed, "otro.png"])

    errors = {
        entry.source.name: entry.error
        for entry in result.entries
        if entry.error
    }

    assert sorted(errors) == sorted([composed, decomposed])
    assert decomposed in errors[composed]
    assert composed in errors[decomposed]
    assert "Renombre" in errors[composed]


def test_case_differences_in_stem_are_the_same_base_name():
    assert names(plan(["Producto.jpg", "producto.png"])) == {
        "Producto.jpg": "TRANSFORMADA_Producto__src-jpg.png",
        "producto.png": "TRANSFORMADA_producto__src-png.png",
    }


def test_case_only_differences_collide_explicitly():
    # Posible en Linux; en Windows serían el mismo archivo.
    result = plan(["producto.jpg", "producto.JPG", "otro.png"])

    assert names(result) == {
        "producto.JPG": "ERROR",
        "producto.jpg": "ERROR",
        "otro.png": "TRANSFORMADA_otro.png",
    }


def test_unicode_normalization_is_compared():
    composed = "café.jpg"
    decomposed = "café.png"

    assert names(plan([composed, decomposed])) == {
        composed: "TRANSFORMADA_café__src-jpg.png",
        decomposed: "TRANSFORMADA_café__src-png.png",
    }


def test_plan_does_not_depend_on_discovery_order():
    sources = [
        "producto.jpg",
        "producto.png",
        "Banner.webp",
        "producto__src-jpg.png",
        "zeta.jpeg",
    ]
    existing = ["TRANSFORMADA_zeta.png"]

    reference = plan(sources, existing=existing)
    expected = [
        (entry.source.name, entry.output_path, entry.error)
        for entry in reference.entries
    ]

    for order in permutations(sources):
        result = plan(order, existing=existing)

        assert [
            (entry.source.name, entry.output_path, entry.error)
            for entry in result.entries
        ] == expected
        assert result.warnings == reference.warnings

    assert [name for name, _, _ in expected] == [
        "Banner.webp",
        "producto.jpg",
        "producto.png",
        "producto__src-jpg.png",
        "zeta.jpeg",
    ]


def test_historic_result_becomes_ambiguous_with_new_original():
    result = plan(
        ["producto.jpg", "producto.png"],
        existing=["TRANSFORMADA_producto.png"],
    )

    assert names(result) == {
        "producto.jpg": "TRANSFORMADA_producto__src-jpg.png",
        "producto.png": "TRANSFORMADA_producto__src-png.png",
    }
    assert len(result.warnings) == 1
    assert "TRANSFORMADA_producto.png" in result.warnings[0]


def test_previous_source_suffix_keeps_base_name_ambiguous():
    # producto.png ya no está, pero dejó un resultado __src-:
    # el histórico no se atribuye a producto.jpg.
    result = plan(
        ["producto.jpg"],
        existing=[
            "TRANSFORMADA_producto.png",
            "TRANSFORMADA_producto__src-jpg.png",
            "TRANSFORMADA_producto__src-png.png",
        ],
    )

    assert names(result) == {
        "producto.jpg": "TRANSFORMADA_producto__src-jpg.png"
    }
    assert len(result.warnings) == 1


def test_previous_suffix_of_other_variant_is_evidence():
    # La ambigüedad de un nombre base vale para todas las variantes.
    result = plan(
        ["producto.jpg"],
        options=TRANSPARENT,
        existing=["TRANSFORMADA_producto__src-png.png"],
    )

    assert names(result) == {
        "producto.jpg": (
            "TRANSFORMADA_producto__src-jpg__800x600_transparent.png"
        )
    }


def test_unrelated_names_are_not_evidence():
    result = plan(
        ["producto.jpg"],
        existing=[
            "TRANSFORMADA_producto__src-gif.png",
            "TRANSFORMADA_producto__src-jpg__10x10_azul.png",
            "TRANSFORMADA_producto%5F%5Fsrc-jpg__esc.png",
            "TRANSFORMADA_productos__src-jpg.png",
        ],
    )

    assert names(result) == {"producto.jpg": "TRANSFORMADA_producto.png"}


@pytest.mark.parametrize(
    "name, eligible",
    [
        ("producto.jpg", True),
        ("producto.JPEG", True),
        ("producto.webp", True),
        ("producto.gif", False),
        ("TRANSFORMADA_producto.png", False),
        ("transformada_producto.jpg", False),
    ],
)
def test_eligible_sources(name, eligible):
    assert is_eligible_source(Path(name)) is eligible


def test_output_name_with_source_tag():
    assert (
        output_name("producto", OutputOptions(), "png")
        == "TRANSFORMADA_producto__src-png.png"
    )


# --- Revisión correctiva de H-04 -----------------------------------

TRICKY_STEMS = [
    "producto",
    "producto_",
    "_producto",
    "producto__src-jpg",
    "producto__src-png",
    "producto__800x600_transparent",
    "producto__400x400_transparent",
    "producto__src-jpg__800x600_transparent",
    "producto__esc",
    "producto%5F%5Fsrc-jpg__esc",
    "producto%5F%5Fsrc-jpg",
    "a%5Fb",
    "a__b",
    "a_%5Fb",
    "a%__b",
    "a___b",
]

NAME_VARIANTS = [
    OutputOptions(),
    OutputOptions(background="transparent"),
    OutputOptions(800, 600, "transparent"),
    OutputOptions(800, 600, "white"),
]


def test_output_names_are_injective():
    combos = [
        (stem, tag, options)
        for stem in TRICKY_STEMS
        for tag in [None, "jpg", "jpeg", "png", "webp"]
        for options in NAME_VARIANTS
    ]

    generated = {}

    for stem, tag, options in combos:
        key = output_name(stem, options, tag).casefold()
        assert key not in generated, (
            f"{(stem, tag, options)} y {generated[key]} comparten {key}"
        )
        generated[key] = (stem, tag, options)


def test_ordinary_stems_keep_their_names():
    assert (
        output_name("producto_", OutputOptions())
        == "TRANSFORMADA_producto_.png"
    )
    assert (
        output_name("a%5Fb", OutputOptions())
        == "TRANSFORMADA_a%5Fb.png"
    )
    assert (
        output_name("mi_foto-1", TRANSPARENT, "jpg")
        == "TRANSFORMADA_mi_foto-1__src-jpg__800x600_transparent.png"
    )


def test_reserved_separator_in_stem_is_encoded():
    assert (
        output_name("producto__800x600_transparent", OutputOptions())
        == "TRANSFORMADA_producto%5F%5F800x600%5Ftransparent__esc.png"
    )
    assert (
        output_name("a__b", TRANSPARENT, "jpg")
        == "TRANSFORMADA_a%5F%5Fb__esc__src-jpg__800x600_transparent.png"
    )
    assert (
        output_name("100%__x", OutputOptions())
        == "TRANSFORMADA_100%25%5F%5Fx__esc.png"
    )


def test_reserved_suffix_original_never_takes_variant_name():
    sources = ["producto.jpg", "producto__800x600_transparent.jpg"]

    default = names(plan(sources))
    transparent = names(plan(sources, TRANSPARENT))

    assert transparent["producto.jpg"] == (
        "TRANSFORMADA_producto__800x600_transparent.png"
    )
    assert default["producto__800x600_transparent.jpg"] != (
        transparent["producto.jpg"]
    )
    assert len(set(default.values()) | set(transparent.values())) == 4


def test_new_src_like_original_does_not_reuse_old_result():
    result = plan(
        ["producto__src-jpg.png"],
        existing=[
            "TRANSFORMADA_producto__src-jpg.png",
            "TRANSFORMADA_producto__src-png.png",
        ],
    )

    planned = names(result)["producto__src-jpg.png"]

    assert planned not in (
        "TRANSFORMADA_producto__src-jpg.png",
        "TRANSFORMADA_producto__src-png.png",
    )


def test_source_suffix_evidence_from_other_variants():
    result = plan(
        ["producto.jpg"],
        TRANSPARENT,
        existing=[
            "TRANSFORMADA_producto.png",
            "TRANSFORMADA_producto__800x600_transparent.png",
            "TRANSFORMADA_producto__src-jpg.png",
            "TRANSFORMADA_producto__src-png.png",
        ],
    )

    assert names(result) == {
        "producto.jpg": (
            "TRANSFORMADA_producto__src-jpg__800x600_transparent.png"
        )
    }
    assert any(
        "TRANSFORMADA_producto__800x600_transparent.png" in warning
        for warning in result.warnings
    )


def test_legacy_result_of_reserved_suffix_original_is_ambiguous():
    # Un resultado creado antes de codificar los sufijos reservados
    # por producto__800x600_transparent.jpg (variante predeterminada).
    result = plan(
        ["producto.jpg", "producto__800x600_transparent.jpg"],
        TRANSPARENT,
        existing=["TRANSFORMADA_producto__800x600_transparent.png"],
    )

    entries = {entry.source.name: entry for entry in result.entries}

    assert entries["producto.jpg"].output_path is None
    assert "producto__800x600_transparent.jpg" in (
        entries["producto.jpg"].error
    )
    assert entries["producto__800x600_transparent.jpg"].output_path.name == (
        "TRANSFORMADA_producto%5F%5F800x600%5Ftransparent"
        "__esc__800x600_transparent.png"
    )


def test_normalization_only_collision_is_explicit():
    composed = "café.png"
    decomposed = "café.png"

    assert names(plan([composed, decomposed, "otro.png"])) == {
        composed: "ERROR",
        decomposed: "ERROR",
        "otro.png": "TRANSFORMADA_otro.png",
    }


def test_legacy_check_uses_format_of_existing_result():
    sources = [
        Path("producto.jpg"),
        Path("producto__800x600_transparent.jpg"),
    ]
    existing = ["TRANSFORMADA_producto__800x600_transparent.png"]

    def describe_as(size, mode):
        return lambda name: (size, mode)

    # 800 x 600 RGBA: es el formato que pide producto.jpg, no el
    # predeterminado que una versión anterior habría generado.
    own = plan_outputs(
        sources, OUT, TRANSPARENT, existing,
        describe_existing=describe_as((800, 600), "RGBA"),
    )
    assert all(entry.error is None for entry in own.entries)

    # 400 x 400 RGB: solo pudo generarlo el otro original.
    legacy = plan_outputs(
        sources, OUT, TRANSPARENT, existing,
        describe_existing=describe_as((400, 400), "RGB"),
    )
    assert names(legacy)["producto.jpg"] == "ERROR"

    # Ilegible: no se atribuye.
    unreadable = plan_outputs(
        sources, OUT, TRANSPARENT, existing,
        describe_existing=lambda name: None,
    )
    assert names(unreadable)["producto.jpg"] == "ERROR"


def test_repeated_plan_with_src_like_sibling_has_no_errors():
    sources = ["producto.jpg", "producto.png", "producto__src-jpg.png"]

    first = names(plan(sources))
    again = plan(sources, existing=list(first.values()))

    assert names(again) == first
    assert all(entry.error is None for entry in again.entries)
    assert again.warnings == []


# --- H-05: bloque de revisión ------------------------------------

def test_with_revision():
    assert (
        with_revision("TRANSFORMADA_producto__src-jpg.png", "0123456789ab")
        == "TRANSFORMADA_producto__src-jpg__rev-0123456789ab.png"
    )

    for invalid in ("0123", "0123456789AB", "../x", "0123456789abc"):
        with pytest.raises(ValueError):
            with_revision("TRANSFORMADA_producto.png", invalid)


def test_revision_names_remain_unique():
    revisions = [None, "000000000000", "aaaaaaaaaaaa"]
    generated = set()
    total = 0

    for stem in TRICKY_STEMS + ["producto__rev-000000000000"]:
        for tag in [None, "jpg", "png"]:
            for options in NAME_VARIANTS:
                for revision in revisions:
                    name = output_name(stem, options, tag)

                    if revision is not None:
                        name = with_revision(name, revision)

                    generated.add(name.casefold())
                    total += 1

    assert len(generated) == total


def test_revision_source_results_are_evidence():
    result = plan(
        ["producto.jpg"],
        existing=["TRANSFORMADA_producto__src-png__rev-aaaaaaaaaaaa.png"],
    )

    assert names(result) == {
        "producto.jpg": "TRANSFORMADA_producto__src-jpg.png"
    }


def test_registered_names_skip_legacy_check():
    sources = [
        Path("producto.jpg"),
        Path("producto__800x600_transparent.jpg"),
    ]
    legacy = "TRANSFORMADA_producto__800x600_transparent.png"

    unknown = plan_outputs(sources, OUT, TRANSPARENT, [legacy])
    known = plan_outputs(
        sources, OUT, TRANSPARENT, [legacy],
        known_names={legacy.casefold()},
    )

    assert names(unknown)["producto.jpg"] == "ERROR"
    assert names(known)["producto.jpg"] == legacy
