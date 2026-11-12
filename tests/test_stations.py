"""桩号核查单元测试：桩号换算、去重/冲突、缺段与重叠、部分采集结论。"""
from __future__ import annotations

import pytest

from app.segment_check import analyze, dedupe_and_detect_conflicts
from app.segment_parser import SegmentParseError, SegmentRecord, parse_segment_page
from app.stations import StationError, format_station, parse_station


def rec(
    code: str,
    start: str,
    end: str,
    source: str = "http://testserver/sample/segment_sources/X.html",
    page: str = "http://testserver/sample/segments/page_1.html",
) -> SegmentRecord:
    return SegmentRecord(
        segment_code=code,
        start_station=start,
        end_station=end,
        start_meters=parse_station(start),
        end_meters=parse_station(end),
        source_url=source,
        page_url=page,
    )


# ---- 桩号换算 ----

@pytest.mark.parametrize(
    "text,meters",
    [
        ("K12+345", 12345.0),
        ("k12+345", 12345.0),
        ("12+345", 12345.0),
        ("K0+000", 0.0),
        ("K0+999", 999.0),
        (" K12 + 345 ", 12345.0),
        ("K12+345.5", 12345.5),
        ("K100+000", 100000.0),
    ],
)
def test_parse_station_converts_to_meters(text, meters):
    assert parse_station(text) == meters


@pytest.mark.parametrize(
    "bad",
    ["K12+", "K+345", "K12+1000", "K12-345", "12", "K12+34a", "", "K-1+000"],
)
def test_parse_station_rejects_bad_formats(bad):
    with pytest.raises(StationError):
        parse_station(bad)


@pytest.mark.parametrize(
    "text", ["K0+000", "K12+345", "K1+007", "K0+050.5", "K3+999"]
)
def test_station_roundtrip(text):
    assert format_station(parse_station(text)) == text


# ---- 去重与冲突 ----

def test_same_code_same_range_counted_once():
    records = [
        rec("S1", "K0+000", "K1+000", "http://a/1"),
        rec("S1", "K0+000", "K1+000", "http://a/2"),
    ]
    result = dedupe_and_detect_conflicts(records)
    assert [r.segment_code for r in result.reliable] == ["S1"]
    assert len(result.duplicates) == 1
    assert result.conflicts == []


def test_same_code_different_range_is_conflict_and_excluded():
    records = [
        rec("S1", "K0+000", "K1+000", "http://a/1"),
        rec("S1", "K0+500", "K1+200", "http://a/2"),
        rec("S2", "K1+200", "K2+000", "http://a/3"),
    ]
    result = dedupe_and_detect_conflicts(records)
    assert [r.segment_code for r in result.reliable] == ["S2"]
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.segment_code == "S1"
    assert {c.source_url for c in conflict.records} == {"http://a/1", "http://a/2"}


def test_conflict_records_do_not_prove_coverage():
    """S1 冲突记录（两变体合起来能盖住 K0+000~K1+500）不得被用来补空。"""
    records = [
        rec("S1", "K0+000", "K1+000", "http://a/1"),
        rec("S1", "K0+500", "K1+500", "http://a/2"),
        rec("S2", "K1+500", "K2+000", "http://a/3"),
    ]
    out = analyze(
        records,
        target_start=parse_station("K0+000"),
        target_end=parse_station("K2+000"),
        complete=True,
    )
    assert out["verdict"] == "indeterminate"
    assert out["fully_covered"] is None
    # 结论不确定：不报“确定缺段”，仅给出参考未覆盖区间
    assert out["gaps"] == []
    assert [(g.start_station, g.end_station) for g in out["uncovered_ranges"]] == [
        ("K0+000", "K1+500")
    ]


# ---- 缺段与重叠 ----

def test_adjacent_shared_endpoint_is_not_overlap_but_continuous():
    records = [
        rec("S1", "K0+000", "K1+000"),
        rec("S2", "K1+000", "K2+000"),
    ]
    out = analyze(
        records,
        target_start=parse_station("K0+000"),
        target_end=parse_station("K2+000"),
        complete=True,
    )
    assert out["overlaps"] == []
    assert out["gaps"] == []
    assert out["verdict"] == "complete"
    assert out["fully_covered"] is True


def test_overlapping_segment_is_reported_with_sources():
    records = [
        rec("S1", "K0+000", "K1+000", "http://a/1"),
        rec("S2", "K0+500", "K1+500", "http://a/2"),
    ]
    out = analyze(
        records,
        target_start=parse_station("K0+000"),
        target_end=parse_station("K1+500"),
        complete=True,
    )
    assert len(out["overlaps"]) == 1
    overlap = out["overlaps"][0]
    assert (overlap.start_station, overlap.end_station) == ("K0+500", "K1+000")
    assert {s.segment_code for s in overlap.sources} == {"S1", "S2"}
    assert overlap.sources[0].source_url.startswith("http://a/")
    assert out["fully_covered"] is True


def test_gap_is_reported_when_complete_but_uncovered():
    records = [
        rec("S1", "K0+000", "K0+500"),
        rec("S2", "K1+000", "K1+500"),
    ]
    out = analyze(
        records,
        target_start=parse_station("K0+000"),
        target_end=parse_station("K1+500"),
        complete=True,
    )
    assert [(g.start_station, g.end_station) for g in out["gaps"]] == [
        ("K0+500", "K1+000")
    ]
    assert out["verdict"] == "gaps"
    assert out["fully_covered"] is False


def test_overlap_sources_stay_aligned_when_intervals_are_clipped():
    """先被裁掉的区间不能导致重叠来源错位。"""
    records = [
        rec("OUT", "K0+000", "K0+200"),   # 完全在目标范围之前，将被裁掉
        rec("S1", "K1+000", "K2+000"),
        rec("S2", "K1+500", "K2+500"),
    ]
    out = analyze(
        records,
        target_start=parse_station("K1+000"),
        target_end=parse_station("K2+000"),
        complete=True,
    )
    assert len(out["overlaps"]) == 1
    overlap = out["overlaps"][0]
    assert (overlap.start_station, overlap.end_station) == ("K1+500", "K2+000")
    assert {s.segment_code for s in overlap.sources} == {"S1", "S2"}


def test_target_range_is_clipped():
    """超出核查范围的部分不参与缺段/重叠计算。"""
    records = [
        rec("S1", "K0+000", "K1+000"),
        rec("S2", "K3+000", "K4+000"),
    ]
    out = analyze(
        records,
        target_start=parse_station("K0+000"),
        target_end=parse_station("K2+000"),
        complete=True,
    )
    assert [(g.start_station, g.end_station) for g in out["gaps"]] == [
        ("K1+000", "K2+000")
    ]
    assert out["overlaps"] == []


def test_incomplete_crawl_reports_overlap_but_not_conclusions():
    records = [
        rec("S1", "K0+000", "K1+000"),
        rec("S2", "K0+400", "K0+900"),
    ]
    out = analyze(
        records,
        target_start=parse_station("K0+000"),
        target_end=parse_station("K2+000"),
        complete=False,
    )
    assert len(out["overlaps"]) == 1
    assert out["fully_covered"] is None
    assert out["verdict"] == "indeterminate"
    # 不报确定缺段；未覆盖区间仅作参考
    assert out["gaps"] == []
    assert [(g.start_station, g.end_station) for g in out["uncovered_ranges"]] == [
        ("K1+000", "K2+000")
    ]


# ---- 路段页解析 ----

SEGMENT_HTML = """<!DOCTYPE html>
<html><body>
<table id="segment-table">
<tr><th>分段编号</th><th>线路编号</th><th>起始桩号</th><th>终止桩号</th><th>来源</th></tr>
<tr><td>S1</td><td>LX-1</td><td>K0+000</td><td>K0+500</td>
    <td><a href="sources/1.html">来源 1</a></td></tr>
<tr><td>S2</td><td>LX-2</td><td>K0+500</td><td>K1+000</td>
    <td><a href="sources/2.html">来源 2</a></td></tr>
</table>
<a rel="next" href="page_2.html">下一页</a>
</body></html>
"""


def test_parse_segment_page_filters_line_and_uses_source_links():
    records, next_url = parse_segment_page(
        SEGMENT_HTML, "http://testserver/sample/segments/page_1.html", "LX-1"
    )
    assert len(records) == 1
    record = records[0]
    assert record.start_meters == 0 and record.end_meters == 500
    assert record.source_url == "http://testserver/sample/segments/sources/1.html"
    assert record.page_url == "http://testserver/sample/segments/page_1.html"
    assert next_url == "http://testserver/sample/segments/page_2.html"


def test_parse_segment_page_last_page_has_no_next():
    records, next_url = parse_segment_page(
        SEGMENT_HTML.replace('<a rel="next" href="page_2.html">下一页</a>', ""),
        "http://testserver/sample/segments/page_1.html",
        "LX-1",
    )
    assert len(records) == 1
    assert next_url is None


def test_parse_segment_page_rejects_reversed_station():
    bad = SEGMENT_HTML.replace("K0+000</td><td>K0+500", "K0+500</td><td>K0+000")
    with pytest.raises(SegmentParseError, match="不小于"):
        parse_segment_page(bad, "http://x/page_1.html", "LX-1")
