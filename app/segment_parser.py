"""路段表 HTML 解析器。

目录页（每个分页均含一张路段表）约定：
  - 表头单元格文字含“分段编号”“起始桩号”“终止桩号”，可选“线路编号”“来源”；
  - 数据行使用 <td>；来源列若存在 <a href>，取该链接，否则取当前页 URL；
  - 下一页：``<a rel="next" href="...">``，无该链接即末页。

一行桩号不合法、起止颠倒或缺少编号时，整页解析失败（由采集层记为页面读取
失败，不使用该页任何数据），避免把残缺页当成可靠覆盖证据。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .stations import StationError, parse_station

TABLE_IDS = ("segment-table",)
TABLE_CLASSES = ("segment-table", "segments")
SEGMENT_KEYS = {"分段编号", "路段编号", "区段编号", "编号"}
START_KEYS = {"起始桩号", "起点桩号", "开始桩号", "起桩号"}
END_KEYS = {"终止桩号", "终点桩号", "结束桩号", "止桩号", "截止桩号"}
SOURCE_KEYS = {"来源", "来源链接", "出处"}
LINE_KEYS = {"线路编号", "线路", "线别"}


class SegmentParseError(Exception):
    """路段表结构不符合约定时抛出。"""


@dataclass(frozen=True, slots=True)
class SegmentRecord:
    segment_code: str
    start_station: str
    end_station: str
    start_meters: float
    end_meters: float
    source_url: str
    page_url: str
    line_code: Optional[str] = None

    def to_source(self):
        from .models import SegmentSource

        return SegmentSource(
            segment_code=self.segment_code,
            start_station=self.start_station,
            end_station=self.end_station,
            source_url=self.source_url,
            page_url=self.page_url,
        )


def _cell_text(cell) -> str:
    return " ".join(cell.get_text(" ", strip=True).split())


def _find_table(soup: BeautifulSoup):
    for table in soup.find_all("table"):
        if table.get("id") in TABLE_IDS:
            return table
        classes = set(table.get("class") or [])
        if classes.intersection(TABLE_CLASSES):
            return table
    return None


def _header_columns(header_cells) -> tuple[int, int, int, Optional[int], Optional[int]]:
    seg = start = end = source = line = None
    for idx, cell in enumerate(header_cells):
        text = _cell_text(cell).replace(" ", "")
        if seg is None and text in SEGMENT_KEYS:
            seg = idx
        elif start is None and text in START_KEYS:
            start = idx
        elif end is None and text in END_KEYS:
            end = idx
        elif source is None and text in SOURCE_KEYS:
            source = idx
        elif line is None and text in LINE_KEYS:
            line = idx
    if seg is None or start is None or end is None:
        raise SegmentParseError(
            "路段表表头需包含“分段编号”“起始桩号”“终止桩号”"
        )
    return seg, start, end, source, line


def parse_segment_page(
    html_text: str, url: str, line_code: Optional[str] = None
) -> tuple[list[SegmentRecord], Optional[str]]:
    """返回 (该线路的路段记录, 下一页绝对 URL 或 None)。"""
    soup = BeautifulSoup(html_text, "html.parser")
    table = _find_table(soup)
    if table is None:
        raise SegmentParseError(f"路段页 {url} 缺少路段表")

    header_cells = None
    data_rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        if header_cells is None:
            try:
                _header_columns(cells)
            except SegmentParseError:
                continue
            header_cells = cells
            continue
        # 与表头列数一致的 tr 视为数据行（容忍末行跨列合计等结构）
        if len(cells) >= 3 and cells[0].name == "td":
            data_rows.append(cells)

    if header_cells is None:
        raise SegmentParseError(f"路段页 {url} 未找到表头")

    seg_i, start_i, end_i, source_i, line_i = _header_columns(header_cells)
    need_max = max(
        seg_i, start_i, end_i, source_i if source_i is not None else 0,
        line_i if line_i is not None else 0,
    )

    records: list[SegmentRecord] = []
    for row_no, cells in enumerate(data_rows, start=1):
        if len(cells) <= need_max:
            raise SegmentParseError(
                f"路段页 {url} 第 {row_no} 行列数不足（需要 {need_max + 1} 列）"
            )
        segment_code = _cell_text(cells[seg_i])
        start_text = _cell_text(cells[start_i])
        end_text = _cell_text(cells[end_i])
        if not segment_code:
            raise SegmentParseError(f"路段页 {url} 第 {row_no} 行缺少分段编号")
        try:
            start_m = parse_station(start_text)
            end_m = parse_station(end_text)
        except StationError as exc:
            raise SegmentParseError(
                f"路段页 {url} 第 {row_no} 行桩号无法识别: {exc}"
            ) from exc
        if start_m >= end_m:
            raise SegmentParseError(
                f"路段页 {url} 第 {row_no} 行起始桩号 {start_text} "
                f"不小于终止桩号 {end_text}"
            )

        row_line: Optional[str] = None
        if line_i is not None:
            row_line = _cell_text(cells[line_i]) or None

        source_url = url
        if source_i is not None:
            anchor = cells[source_i].find("a", href=True)
            if anchor is not None:
                source_url = urljoin(url, anchor["href"])

        records.append(
            SegmentRecord(
                segment_code=segment_code,
                start_station=start_text,
                end_station=end_text,
                start_meters=start_m,
                end_meters=end_m,
                source_url=source_url,
                page_url=url,
                line_code=row_line,
            )
        )

    # 若表内带线路编号列，只保留目标线路；否则认为整张表属于该线路。
    if line_i is not None and line_code is not None:
        wanted = line_code.strip()
        records = [r for r in records if r.line_code == wanted]

    next_url = None
    nxt = soup.find("a", rel=True, href=True)
    if nxt is not None and "next" in (nxt.get("rel") or []):
        next_url = urljoin(url, nxt["href"])
    return records, next_url
