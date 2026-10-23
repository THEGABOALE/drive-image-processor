
from itertools import permutations
from pathlib import Path

import pytest

from image_processor.naming import (
    is_eligible_source,
    output_name,
    plan_outputs,
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
    assert names(plan(["producto.jpg", "producto__src-jpg.png"])) == {
        "producto.jpg": "TRANSFORMADA_producto.png",
        "producto__src-jpg.png": "TRANSFORMADA_producto__src-jpg.png",
    }


def test_original_with_src_like_suffix_on_later_run():
    # El resultado de producto__src-jpg.png ya existe y le
    # pertenece: no indica un nombre base ambiguo.
    result = plan(
        ["producto.jpg", "producto__src-jpg.png"],
        existing=[
            "TRANSFORMADA_producto.png",
            "TRANSFORMADA_producto__src-jpg.png",
        ],
    )

    assert names(result) == {
        "producto.jpg": "TRANSFORMADA_producto.png",
        "producto__src-jpg.png": "TRANSFORMADA_producto__src-jpg.png",
    }
    assert result.warnings == []


def test_unresolvable_collision_is_an_explicit_error():
    result = plan(
        ["producto.jpg", "producto.png", "producto__src-jpg.png"]
    )

    assert names(result) == {
        "producto.jpg": "ERROR",
        "producto.png": "TRANSFORMADA_producto__src-png.png",
        "producto__src-jpg.png": "ERROR",
    }

    errors = {
        entry.source.name: entry.error
        for entry in result.entries
        if entry.error
    }

    assert "producto__src-jpg.png" in errors["producto.jpg"]
    assert "producto.jpg" in errors["producto__src-jpg.png"]
    assert "TRANSFORMADA_producto__src-jpg.png" in errors["producto.jpg"]


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


def test_previous_suffix_of_other_variant_is_not_evidence():
    result = plan(
        ["producto.jpg"],
        options=TRANSPARENT,
        existing=["TRANSFORMADA_producto__src-png.png"],
    )

    assert names(result) == {
        "producto.jpg": "TRANSFORMADA_producto__800x600_transparent.png"
    }


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
