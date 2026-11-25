"""Profile facing: SolidWorks' rule, measured live, and the turn that points a member's height up.

The expected frames are SolidWorks 2025 SP4's own, read off UPN 200 and RHS
150x100 members (flange side and height side from each body's faces and
centre of mass) on lines and chains in these directions.
"""

import math

import pytest

from solidworks_mcp.adapters.solidworks import weldment_orientation as o


def close(a, b, tol=0.002):
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


S17 = math.sqrt(17.0)
S5 = math.sqrt(5.0)


@pytest.mark.parametrize(
    ("a", "x", "y"),
    [
        ((1, 0, 0), (0, 0, -1), (0, 1, 0)),
        ((-1, 0, 0), (0, 0, 1), (0, 1, 0)),
        ((0, 0, 1), (1, 0, 0), (0, 1, 0)),
        ((0, 0, -1), (1, 0, 0), (0, -1, 0)),
        ((0, 1, 0), (1, 0, 0), (0, 0, -1)),
        ((0, -1, 0), (1, 0, 0), (0, 0, 1)),
        # a rafter sloping 17.7 degrees in the xy plane: rolled by its slope
        ((2, -0.64, 0), (0.093, 0.290, -0.952), (0.290, 0.906, 0.305)),
        ((1, 1, 1), (0.789, -0.211, -0.577), (-0.211, 0.789, -0.577)),
    ],
)
def test_a_lone_member_faces_as_solidworks_turns_it(a, x, y):
    fx, fy = o.lone_frame(o.unit(a))
    assert close(fx, x) and close(fy, y)


@pytest.mark.parametrize(
    ("points", "frames"),
    [
        (  # a portal frame in xy: one axis stays square to the plane
            [(0, 0, 0), (0, 3, 0), (4, 2, 0), (4, 0, 0)],
            [
                ((1, 0, 0), (0, 0, -1)),
                ((-1 / S17, -4 / S17, 0), (0, 0, -1)),
                ((-1, 0, 0), (0, 0, -1)),
            ],
        ),
        (
            [(0, 0, 0), (0, 3, 0), (0, 2, 4), (0, 0, 4)],
            [
                ((1, 0, 0), (0, 0, -1)),
                ((1, 0, 0), (0, 4 / S17, 1 / S17)),
                ((1, 0, 0), (0, 0, 1)),
            ],
        ),
        (
            [(0, 0, 0), (0, 2, 0), (2, 2, 1), (2, 3, 3)],
            [
                ((1, 0, 0), (0, 0, -1)),
                ((0.2, -0.894, -0.4), (0.4, 0.447, -0.8)),
                ((0.684, -0.652, 0.326), (0.730, 0.611, -0.305)),
            ],
        ),
    ],
)
def test_a_chain_carries_the_frame_round_its_bends(points, frames):
    directions = [o.direction(p, q) for p, q in zip(points, points[1:], strict=False)]
    for got, (x, y) in zip(o.chain_frames(directions), frames, strict=True):
        assert close(got[0], x) and close(got[1], y)


def test_angle_turns_by_the_right_hand_about_the_member():
    x, _ = o.lone_frame((0.0, 0.0, 1.0))
    assert close(o.rotate(x, (0.0, 0.0, 1.0), math.radians(30)), (0.866, 0.5, 0.0))


def test_a_sloped_rafter_is_turned_back_upright():
    turn, spread = o.group_turn(
        [o.direction((220, 4310, 0), (5880, 2500, 0))], o.MODEL_UP
    )
    # found by hand on the pavilion before the rule was known: -17.73
    assert math.degrees(turn) == pytest.approx(-17.75, abs=0.05)
    assert spread == 0.0


def test_a_planar_frame_chain_takes_one_turn_from_its_sloped_member():
    points = [(0, 0, 0), (0, 3, 0), (4, 2, 0), (4, 0, 0)]
    directions = [o.direction(p, q) for p, q in zip(points, points[1:], strict=False)]
    turn, spread = o.group_turn(directions, o.MODEL_UP)
    # the posts run along up and don't vote; the rafter puts the height in plane
    assert math.degrees(turn) == pytest.approx(90.0, abs=0.01)
    assert spread == pytest.approx(0.0, abs=1e-6)


def test_up_along_every_member_gives_no_turn():
    assert o.group_turn([(0.0, 1.0, 0.0)], o.MODEL_UP) == (None, 0.0)


def test_a_chain_out_of_plane_reports_its_spread():
    points = [(0, 0, 0), (2, 0, 0), (2, 2, 2)]
    directions = [o.direction(p, q) for p, q in zip(points, points[1:], strict=False)]
    _turn, spread = o.group_turn(directions, o.MODEL_UP)
    assert spread == pytest.approx(45.0, abs=0.01)


def test_purlins_turn_square_to_the_roof():
    roof = o.unit((0.3045, 0.9525, 0.0))
    turn, _ = o.group_turn([(0.0, 0.0, 1.0)], roof)
    _x, y = o.lone_frame((0.0, 0.0, 1.0))
    assert close(o.rotate(y, (0.0, 0.0, 1.0), turn), roof)


def test_head_to_tail_turns_reversed_segments():
    ends = [((5, 0, 0), (0, 0, 0)), ((5, 0, 0), (5, 3, 0)), ((9, 3, 0), (5, 3, 0))]
    assert o.head_to_tail(ends, [0, 1, 2]) == [
        ((0, 0, 0), (5, 0, 0)),
        ((5, 0, 0), (5, 3, 0)),
        ((5, 3, 0), (9, 3, 0)),
    ]
    assert o.head_to_tail(ends, [2]) == [ends[2]]


def test_chained_draws_each_chain_in_order():
    segments = [[5, 3, 0, 9, 3, 0], [0, 0, 0, 0, 3, 0], [5, 0, 0, 5, 3, 0]]
    ordered, plan = o.chained(segments, [[2, 0], [1]])
    assert ordered == [[5, 0, 0, 5, 3, 0], [5, 3, 0, 9, 3, 0], [0, 0, 0, 0, 3, 0]]
    assert plan == [[0, 1], [2]]


def test_roll_error_reads_the_flat_sides():
    axis = (1.0, 0.0, 0.0)
    upright = [(0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1), (1, 0, 0)]
    assert o.roll_error((0.0, 1.0, 0.0), axis, upright) == pytest.approx(0.0)
    r = math.radians(17.7)
    rolled = [(0, math.cos(r), math.sin(r)), (0, -math.sin(r), math.cos(r))]
    assert o.roll_error((0.0, 1.0, 0.0), axis, rolled) == pytest.approx(17.7)
    assert o.roll_error((0.0, 1.0, 0.0), axis, [(1, 0, 0)]) is None


def test_zero_vectors_are_refused():
    with pytest.raises(ValueError, match="zero length"):
        o.unit((0.0, 0.0, 0.0))


def test_opposite_directions_turn_about_x_or_y():
    assert close(
        o.carry((0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, -1.0)), (0, -1, 0)
    )
    assert close(
        o.carry((0.0, 0.0, 1.0), (1.0, 0.0, 0.0), (-1.0, 0.0, 0.0)), (0, 0, -1)
    )
    assert o.is_vertical((0.0, -1.0, 0.0)) and not o.is_vertical((1.0, 0.0, 0.0))


def test_a_closed_loop_of_tubes_is_no_miss_but_of_angles_is():
    loop = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 0)]
    directions = [o.direction(p, q) for p, q in zip(loop, loop[1:], strict=False)]
    assert o.group_turn(directions, o.MODEL_UP, half_turn_same=True)[
        1
    ] == pytest.approx(0.0)
    assert o.group_turn(directions, o.MODEL_UP)[1] == pytest.approx(180.0)
    assert o.symmetric("rhs", "RHS50x30x3.2") and o.symmetric("ipe", "IPE200")
    assert not o.symmetric("angle iron", "L50x50x5") and not o.symmetric(
        "upn", "UPN200"
    )
