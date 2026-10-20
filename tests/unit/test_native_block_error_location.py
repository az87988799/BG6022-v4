"""Exact native admission error positions; no runtime or scientific execution."""

import hashlib

import pytest

from orca_agent.orca.native import _lines
from orca_agent.orca.native_managed import _read_block


def test_actual_a026_inputs_identify_the_unconsumed_line_without_echoing_it():
    # Exact immutable writer Artifacts from a026's two known-not-started attempts.
    fixtures = (
        ("", "3338a2230f0812495bf86b756439327ed449a951ae31ba904f745e376f8be045"),
        (" CPCM(water)", "617146d44c52084f89968205607afdcf12d357fae835bb64a650a4302cba32c3"),
    )
    for solvent, digest in fixtures:
        text = (f"! RKS B3LYP def2-TZVP{solvent}\n%pal nprocs 4 end\n%maxcore 192\n"
                "%scf MaxIter 256 AutoTRAH false end\nNoAutoStart\n* xyzfile 0 1 coords.xyz\n")
        assert hashlib.sha256(text.encode("ascii")).hexdigest() == digest
        lines = _lines(text)
        original = list(lines)
        with pytest.raises(ValueError) as caught:
            _read_block(lines, 3)
        assert str(caught.value) == (
            "trailing input after %scf block at active input line 5 "
            "(blank/comment-only lines excluded)")
        assert "NoAutoStart" not in str(caught.value)
        assert lines == original
        assert hashlib.sha256(text.encode("ascii")).hexdigest() == digest


def test_trailing_same_line_uses_that_line_and_does_not_echo_input():
    lines = _lines("%scf MaxIter 256 AutoTRAH false end user-private-token\n")
    with pytest.raises(ValueError) as caught:
        _read_block(lines, 0)
    assert str(caught.value) == (
        "trailing input after %scf block at active input line 1 "
        "(blank/comment-only lines excluded)")
    assert "user-private-token" not in str(caught.value)


def test_comment_and_blank_lines_are_explicitly_excluded_from_position():
    text = ("# input comment\n\n%scf MaxIter 256 AutoTRAH false end # block comment\n"
            "# another comment\n\nuser-private-token\n")
    with pytest.raises(ValueError) as caught:
        _read_block(_lines(text), 0)
    assert str(caught.value) == (
        "trailing input after %scf block at active input line 2 "
        "(blank/comment-only lines excluded)")
    assert "user-private-token" not in str(caught.value)


def test_valid_block_retains_values_position_and_input_bytes():
    text = "! RKS B3LYP def2-TZVP NoAutoStart\n%scf\n MaxIter 256\n AutoTRAH false\nend\n"
    lines = _lines(text)
    original = list(lines)
    digest = hashlib.sha256(text.encode("ascii")).hexdigest()
    assert _read_block(lines, 1) == ("scf", {"maxiter": "256", "autotrah": "false"}, 4)
    assert lines == original
    assert hashlib.sha256(text.encode("ascii")).hexdigest() == digest
