"""Tests for the component placement maths (no COM)."""

from __future__ import annotations

import pytest

from solidworks_mcp.adapters.solidworks import placement


def _flat(matrix: placement.Matrix) -> list[float]:
    """A matrix's nine entries, row by row (``pytest.approx`` wants flat)."""
    return [value for row in matrix for value in row]


def test_a_quarter_turn_about_z_sends_x_to_y() -> None:
    """The layout SolidWorks 2025 returned live for a +90 degree turn about z."""
    data = placement.to_array((2000.0, 0.0, 0.0), placement.rotation(rz=90))

    assert data[:9] == pytest.approx([0, 1, 0, -1, 0, 0, 0, 0, 1])
    assert data[9:] == pytest.approx([2.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0])


@pytest.mark.parametrize(
    "turn",
    [(0, 0, 0), (90, 0, 0), (0, -30, 0), (10, 20, 30), (-45, 60, 170), (0, 0, -90)],
)
def test_angles_undo_rotation(turn: tuple[float, float, float]) -> None:
    assert placement.angles(placement.rotation(*turn)) == pytest.approx(turn)


def test_angles_at_gimbal_lock_put_the_turn_on_z() -> None:
    """At ry=90 the x and z turns share an axis; the reply keeps one of them."""
    matrix = placement.rotation(0, 90, 25)

    rx, ry, rz = placement.angles(matrix)

    assert (rx, ry) == pytest.approx((0, 90))
    assert _flat(placement.rotation(rx, ry, rz)) == pytest.approx(_flat(matrix))


def test_from_array_reads_back_what_to_array_wrote() -> None:
    matrix = placement.rotation(10, 20, 30)

    origin, read = placement.from_array(
        placement.to_array((1.5, -250.0, 9000.0), matrix)
    )

    assert origin == pytest.approx((1.5, -250.0, 9000.0))
    assert _flat(read) == pytest.approx(_flat(matrix))


def test_from_array_needs_twelve_numbers() -> None:
    with pytest.raises(ValueError, match="needs 12 numbers, got 9"):
        placement.from_array([0.0] * 9)


def test_placement_is_the_reply_fields() -> None:
    data = placement.to_array((100.0, 200.0, 300.0), placement.rotation(rz=45))

    assert placement.placement(data) == {
        "origin_mm": pytest.approx([100.0, 200.0, 300.0]),
        "rotation_deg": pytest.approx([0.0, 0.0, 45.0]),
    }


def test_multiply_applies_the_right_matrix_first() -> None:
    turned = placement.multiply(placement.rotation(rz=90), placement.rotation(rx=90))

    assert _flat(turned) == pytest.approx(_flat(placement.rotation(90, 0, 90)))
    assert placement.multiply(placement.IDENTITY, turned) == turned


def test_merge_keeps_what_is_not_given() -> None:
    assert placement.merge((None, 5, 0), (1.0, 2.0, 3.0)) == (1.0, 5.0, 0.0)
