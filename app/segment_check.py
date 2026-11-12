"""桩号核查核心逻辑：去重、冲突判定、覆盖区间计算。

规则：
1. 同分段编号且起止桩号相同 -> 重复记录，只计一次（其余记为 duplicates）。
2. 同分段编号但区间不同 -> 冲突；冲突记录全部列出，且**不参与**覆盖计算，
   不用冲突记录证明覆盖。
3. 相邻路段共用端点（如 K1+000 结束、K1+000 开始）不算重叠：区间按
   [start, end) 半开处理。
4. 目标范围内按可靠记录求并集，补集为缺段；交集（正长度）为重叠段，
   并附上造成重叠的来源。
5. 完整采集且无编号冲突时，无缺段即完整覆盖；页面读取失败或存在编号
   冲突时，剩余空档与完整覆盖都不作为确定结论
   （verdict=indeterminate, fully_covered=null）；但由可靠记录证实的
   重叠仍然照常报告。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .models import (
    GapRange,
    OverlapRange,
    SegmentConflict,
    SegmentSource,
)
from .segment_parser import SegmentRecord
from .stations import format_station

_EPS = 1e-9


def _key(record: SegmentRecord) -> tuple[str, float, float]:
    return (record.segment_code, record.start_meters, record.end_meters)


@dataclass
class DedupeResult:
    reliable: list[SegmentRecord]
    duplicates: list[SegmentRecord]
    conflicts: list[SegmentConflict]
    conflict_sources: list[SegmentRecord]


def dedupe_and_detect_conflicts(
    records: Iterable[SegmentRecord],
) -> DedupeResult:
    by_code: dict[str, list[SegmentRecord]] = {}
    for rec in records:
        by_code.setdefault(rec.segment_code, []).append(rec)

    reliable: list[SegmentRecord] = []
    duplicates: list[SegmentRecord] = []
    conflicts: list[SegmentConflict] = []
    conflict_sources: list[SegmentRecord] = []

    for code in sorted(by_code):
        group = by_code[code]
        distinct: dict[tuple[str, float, float], list[SegmentRecord]] = {}
        for rec in group:
            distinct.setdefault(_key(rec), []).append(rec)

        if len(distinct) == 1:
            first_key = next(iter(distinct))
            reliable.append(distinct[first_key][0])
            duplicates.extend(distinct[first_key][1:])
            continue

        # 同编号多区间 -> 冲突，整组不参与覆盖证明
        sources: list[SegmentSource] = []
        for items in distinct.values():
            conflict_sources.extend(items)
            for item in items:
                src = item.to_source()
                if not any(
                    s.start_station == src.start_station
                    and s.end_station == src.end_station
                    and s.source_url == src.source_url
                    for s in sources
                ):
                    sources.append(src)
        conflicts.append(
            SegmentConflict(
                segment_code=code,
                records=sources,
                message=(
                    f"分段编号 {code} 存在 {len(distinct)} 种不同区间，"
                    "视为冲突，其记录不用于覆盖计算"
                ),
            )
        )

    return DedupeResult(
        reliable=reliable,
        duplicates=duplicates,
        conflicts=conflicts,
        conflict_sources=conflict_sources,
    )


def _merge_intervals(
    intervals: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """合并有正长度交叠的区间；端点相接不合并成“重叠”，但可合并为连续覆盖。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        last = merged[-1]
        if start <= last[1] + _EPS:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def _intersections(
    intervals: list[tuple[float, float]],
) -> list[tuple[float, float, int, int]]:
    """返回任意两条区间正长度交叠的结果；(start, end, i, j)。"""
    hits: list[tuple[float, float, int, int]] = []
    for i in range(len(intervals)):
        a_start, a_end = intervals[i]
        for j in range(i + 1, len(intervals)):
            b_start, b_end = intervals[j]
            overlap_start = max(a_start, b_start)
            overlap_end = min(a_end, b_end)
            if overlap_end > overlap_start + _EPS:
                hits.append((overlap_start, overlap_end, i, j))
    return hits


def analyze(
    records: list[SegmentRecord],
    target_start: float,
    target_end: float,
    complete: bool,
) -> dict:
    """计算去重、冲突、缺段、重叠，并给出结论。"""
    dedupe = dedupe_and_detect_conflicts(records)

    # 把可靠记录裁剪到核查范围；保留记录引用，避免裁剪后下标错位
    clipped: list[tuple[float, float, SegmentRecord]] = []
    for record in dedupe.reliable:
        start = max(record.start_meters, target_start)
        end = min(record.end_meters, target_end)
        if end > start:
            clipped.append((start, end, record))

    merged = _merge_intervals([(start, end) for start, end, _ in clipped])

    # 缺段：目标范围内未被可靠区间覆盖的部分
    gaps: list[GapRange] = []
    cursor = target_start
    for start, end in merged:
        if start > cursor + _EPS:
            gaps.append(
                GapRange(
                    start_station=format_station(cursor),
                    end_station=format_station(start),
                    start_meters=cursor,
                    end_meters=start,
                )
            )
        cursor = max(cursor, end)
    if cursor < target_end - _EPS:
        gaps.append(
            GapRange(
                start_station=format_station(cursor),
                end_station=format_station(target_end),
                start_meters=cursor,
                end_meters=target_end,
            )
        )

    # 重叠：同一可靠区间之间的正长度交叠（相邻共用端点不算）
    raw_hits = _intersections([(start, end) for start, end, _ in clipped])
    overlaps: list[OverlapRange] = []
    for start, end, i, j in raw_hits:
        sources = [clipped[i][2].to_source(), clipped[j][2].to_source()]
        overlaps.append(
            OverlapRange(
                start_station=format_station(start),
                end_station=format_station(end),
                start_meters=start,
                end_meters=end,
                sources=sources,
            )
        )
    # 同一重叠区间可能由多对记录造成：按区间聚合并补齐全部来源
    grouped: dict[tuple[float, float], OverlapRange] = {}
    for ov in overlaps:
        k = (round(ov.start_meters, 6), round(ov.end_meters, 6))
        if k not in grouped:
            grouped[k] = ov
        else:
            existing = grouped[k]
            for src in ov.sources:
                if not any(
                    s.segment_code == src.segment_code
                    and s.start_station == src.start_station
                    and s.end_station == src.end_station
                    and s.source_url == src.source_url
                    for s in existing.sources
                ):
                    existing.sources.append(src)
    overlaps = list(grouped.values())

    full_coverage = not gaps
    # 页面读取失败或存在编号冲突时，均不能把剩余空档/完整覆盖当作确定结论；
    # 冲突虽不参与覆盖计算，其存在本身也说明明细不完整。
    determinate = complete and not dedupe.conflicts
    if not determinate:
        verdict = "indeterminate"
        fully_covered: Optional[bool] = None
        uncovered_ranges = gaps      # 仅作参考，不作为确定缺段
        gaps = []
    else:
        verdict = "complete" if full_coverage else "gaps"
        fully_covered = full_coverage
        uncovered_ranges = gaps

    notes: list[str] = []
    if dedupe.duplicates:
        notes.append(
            f"{len(dedupe.duplicates)} 条同编号同区间重复记录已去重，只计一次"
        )
    if dedupe.conflicts:
        notes.append(
            "存在 "
            f"{len(dedupe.conflicts)} 个分段编号的区间冲突，"
            "冲突记录未用于覆盖计算"
        )
    if not complete:
        notes.append(
            "存在页面读取失败，剩余空档与完整覆盖均不能作为确定结论；"
            "已报告的重叠均由可靠记录证实"
        )
    elif dedupe.conflicts:
        notes.append(
            "存在编号冲突，剩余空档与完整覆盖均不能作为确定结论；"
            "已报告的重叠均由可靠记录证实"
        )
    elif full_coverage:
        notes.append("目标范围已被可靠路段记录完整覆盖")
    else:
        notes.append("采集完整，但目标范围内存在未被可靠记录覆盖的缺段")

    return {
        "reliable": dedupe.reliable,
        "duplicates": [r.to_source() for r in dedupe.duplicates],
        "conflicts": dedupe.conflicts,
        "gaps": gaps,
        "uncovered_ranges": uncovered_ranges,
        "overlaps": overlaps,
        "verdict": verdict,
        "fully_covered": fully_covered,
        "notes": notes,
    }
