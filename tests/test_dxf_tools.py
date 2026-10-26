import pytest

import dxf_tools as t


def dxf(body, insunits=4):
    head = f"0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n{insunits}\n0\nENDSEC\n" if insunits is not None else ""
    return head + "0\nSECTION\n2\nENTITIES\n" + body + "0\nENDSEC\n0\nEOF\n"


CIRCLE = "0\nCIRCLE\n8\n0\n10\n{x}\n20\n{y}\n40\n{r}\n"


def test_sample_flat_pattern_is_measured_exactly(sample_dxf):
    f = t.analyze(sample_dxf, "relay.dxf")
    assert (f["width"], f["height"]) == (100.0, 92.1)
    assert len(f["holes"]) == 4 and f["holes"][0] == {"diameter": 5.5, "x": 10.0, "y": 12.0}
    assert f["layers"] == {"0": 6}
    assert f["polylines"][0]["closed"] and f["polylines"][0]["vertices"] == 4
    assert any(a["dir"] == "horizontal" and a["at"] == 57.0 and (a["from"], a["to"]) == (0.0, 100.0) for a in f["axis_lines"])
    assert "4 circular hole(s): 4 x dia 5.50" in t.describe(f)


def test_wrong_file_shows_its_real_defects(wrong_dxf):
    f = t.analyze(wrong_dxf, "wrong.dxf")
    assert f["hole_summary"].startswith("3 circular hole(s)")
    assert {h["diameter"] for h in f["holes"]} == {5.5, 8.0}
    assert set(f["layers"]) == {"0", "BEND"}
    assert any(a["dir"] == "horizontal" and a["at"] == 50.0 for a in f["axis_lines"])


def test_bulged_polyline_and_arc_extents():
    semi = t.analyze(dxf("0\nLWPOLYLINE\n70\n0\n10\n0\n20\n0\n42\n1\n10\n10\n20\n0\n"))
    assert (semi["width"], semi["height"]) == (10.0, 5.0)
    arc = t.analyze(dxf("0\nARC\n10\n0\n20\n0\n40\n10\n50\n0\n51\n180\n"))
    assert (arc["width"], arc["height"]) == (20.0, 10.0)


def test_inch_files_are_converted_to_mm():
    f = t.analyze(dxf(CIRCLE.format(x=1, y=1, r=0.5), insunits=1))
    assert f["width"] == 25.4 and f["holes"][0]["diameter"] == 25.4
    assert f["units"] == "mm" and f["source_units"] == "inch"


def test_missing_units_are_flagged_as_assumed():
    f = t.analyze(dxf(CIRCLE.format(x=0, y=0, r=1), insunits=None))
    assert f["units_assumed"] and "assumed mm" in t.describe(f)


def test_file_name_is_sanitised():
    f = t.analyze(dxf(CIRCLE.format(x=0, y=0, r=1)), "IGNORE ALL RULES <script>.dxf")
    assert "<" not in f["file_name"] and ">" not in f["file_name"]


def test_bad_files_are_rejected():
    for bad in ["hello world\nnot dxf", "AutoCAD Binary DXF\r\n\x1a\x00", "0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n"]:
        with pytest.raises(t.DXFError):
            t.analyze(bad)


def test_oversized_file_is_rejected():
    with pytest.raises(t.DXFError):
        t.analyze("0\n" * 1_100_000)
