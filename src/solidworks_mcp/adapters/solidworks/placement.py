"""Component placement: a position and a rotation as SolidWorks' transform.

SolidWorks keeps a component's placement as an ``IMathTransform`` whose
``ArrayData`` holds 16 doubles: a 3x3 rotation whose rows are the images of
the component's x, y and z axes in assembly coordinates, the translation in
metres, a scale, and three unused zeros. ``IAssemblyDoc::AddComponents3``
takes the same 16 numbers per component and puts the part's **origin** at the
translation. Checked live on SolidWorks 2025: a +90 degree turn about z gives
``[0, 1, 0, -1, 0, 0, 0, 0, 1, ...]`` and turns a beam drawn along x into one
along y.

Rotations are degrees about the assembly's fixed axes, applied x first, then
y, then z, so ``rz`` alone turns a part in plan. Nothing here touches COM.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

#: A 3x3 rotation, rows of a column-vector matrix: ``p_assembly = M @ p_part``.
Matrix = tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]

IDENTITY: Matrix = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))

#: Below this a matrix entry is noise from the trigonometry, written as 0.
_EPSILON = 1e-12


def _clean(value: float) -> float:
    """``value`` with float noise (``6e-17``) rounded to a plain 0."""
    return 0.0 if abs(value) < _EPSILON else value


def multiply(a: Matrix, b: Matrix) -> Matrix:
    """The matrix product ``a @ b``.

    Args:
        a: The left matrix.
        b: The right matrix.

    Returns:
        Matrix: ``a @ b``, which applies ``b`` first.
    """
    return tuple(
        tuple(_clean(sum(a[i][k] * b[k][j] for k in range(3))) for j in range(3))
        for i in range(3)
    )  # type: ignore[return-value]


def rotation(rx: float = 0.0, ry: float = 0.0, rz: float = 0.0) -> Matrix:
    """The rotation for turns about the assembly's x, then y, then z axis.

    Args:
        rx: Degrees about x, applied first.
        ry: Degrees about y, applied second.
        rz: Degrees about z, applied last.

    Returns:
        Matrix: ``Rz @ Ry @ Rx``.
    """
    ax, ay, az = (math.radians(v) for v in (rx, ry, rz))
    cx, sx = math.cos(ax), math.sin(ax)
    cy, sy = math.cos(ay), math.sin(ay)
    cz, sz = math.cos(az), math.sin(az)
    turn_x: Matrix = ((1.0, 0.0, 0.0), (0.0, cx, -sx), (0.0, sx, cx))
    turn_y: Matrix = ((cy, 0.0, sy), (0.0, 1.0, 0.0), (-sy, 0.0, cy))
    turn_z: Matrix = ((cz, -sz, 0.0), (sz, cz, 0.0), (0.0, 0.0, 1.0))
    return multiply(turn_z, multiply(turn_y, turn_x))


def angles(matrix: Matrix) -> tuple[float, float, float]:
    """The ``(rx, ry, rz)`` degrees that :func:`rotation` turns into ``matrix``.

    At ``ry = +-90`` (gimbal lock) ``rx`` is reported as 0 and the whole turn
    about the remaining axis goes to ``rz``.

    Args:
        matrix: A rotation.

    Returns:
        tuple[float, float, float]: Degrees, rounded to 6 places.
    """
    sine_y = max(-1.0, min(1.0, -matrix[2][0]))
    ry = math.asin(sine_y)
    if abs(sine_y) < 1.0 - 1e-9:
        rx = math.atan2(matrix[2][1], matrix[2][2])
        rz = math.atan2(matrix[1][0], matrix[0][0])
    else:
        rx = 0.0
        rz = math.atan2(-matrix[0][1], matrix[1][1])
    return tuple(  # type: ignore[return-value]
        _clean(round(math.degrees(v), 6)) + 0.0 for v in (rx, ry, rz)
    )


def merge(
    given: Sequence[float | None], kept: Sequence[float]
) -> tuple[float, float, float]:
    """Three values: each given one, or the kept one where it is ``None``.

    Args:
        given: New values, ``None`` where nothing was given.
        kept: The current values.

    Returns:
        tuple[float, float, float]: The merged values.
    """
    values = [
        float(kept[i]) if given[i] is None else float(given[i] or 0.0) for i in range(3)
    ]
    return values[0], values[1], values[2]


def to_array(point: Sequence[float], matrix: Matrix) -> list[float]:
    """The 16 ``ArrayData`` numbers that put a part's origin at a point.

    Args:
        point: ``[x, y, z]`` of the part's origin, mm.
        matrix: The part's rotation.

    Returns:
        list[float]: Rotation rows (images of the part's axes), the
        translation in metres, scale 1 and three zeros.
    """
    images = [[matrix[row][column] for row in range(3)] for column in range(3)]
    return [
        *images[0],
        *images[1],
        *images[2],
        float(point[0]) / 1000.0,
        float(point[1]) / 1000.0,
        float(point[2]) / 1000.0,
        1.0,
        0.0,
        0.0,
        0.0,
    ]


def from_array(data: Sequence[float]) -> tuple[tuple[float, float, float], Matrix]:
    """The origin (mm) and rotation an ``ArrayData`` holds.

    Args:
        data: At least the first 12 ``ArrayData`` numbers.

    Returns:
        tuple: ``((x, y, z) in mm, rotation)``.

    Raises:
        ValueError: Fewer than 12 numbers.
    """
    values = [float(v) for v in data]
    if len(values) < 12:
        raise ValueError(f"a transform needs 12 numbers, got {len(values)}")
    matrix: Matrix = tuple(
        tuple(values[column * 3 + row] for column in range(3)) for row in range(3)
    )  # type: ignore[assignment]
    origin = (
        round(values[9] * 1000.0, 6),
        round(values[10] * 1000.0, 6),
        round(values[11] * 1000.0, 6),
    )
    return origin, matrix


def placement(data: Sequence[float]) -> dict[str, list[float]]:
    """A transform as the reply fields ``origin_mm`` and ``rotation_deg``.

    Args:
        data: ``ArrayData``.

    Returns:
        dict[str, list[float]]: ``origin_mm`` ``[x, y, z]`` and
        ``rotation_deg`` ``[rx, ry, rz]``.
    """
    origin, matrix = from_array(data)
    return {"origin_mm": list(origin), "rotation_deg": list(angles(matrix))}
