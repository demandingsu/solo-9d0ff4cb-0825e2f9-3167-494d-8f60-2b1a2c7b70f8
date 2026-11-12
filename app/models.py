"""Pydantic 数据模型。"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, HttpUrl


class AnnouncementType(str, Enum):
    ORIGINAL = "招标公告"
    RESCHEDULE = "改期公告"
    WITHDRAWAL = "撤回公告"


class CollectRequest(BaseModel):
    catalog_url: HttpUrl = Field(..., description="公告目录第一页 URL")


class Announcement(BaseModel):
    """采集并解析后的单条公告（原始链路口径，不做失效判断）。"""

    announcement_id: str
    tender_code: str
    project_name: str
    type: AnnouncementType
    title: str
    published_at: str
    deadline: Optional[str] = None
    based_on_id: Optional[str] = None     # 改期公告所依据的公告 ID
    withdraws_id: Optional[str] = None    # 撤回公告所针对的改期公告 ID
    source_url: str


class CollectResult(BaseModel):
    catalog_url: str
    total_pages: int
    total_announcements: int
    collected_at: str
    announcements: list[Announcement]


# ---- 查询侧视图模型 ----

class AnnouncementView(BaseModel):
    announcement_id: str
    type: AnnouncementType
    title: str
    published_at: str
    deadline: Optional[str] = None
    based_on_id: Optional[str] = None
    withdraws_id: Optional[str] = None
    valid: bool = True
    invalid_reason: Optional[str] = None
    source_url: str


class ConflictItem(BaseModel):
    announcement_id: str
    deadline: str
    published_at: str
    source_url: str


class TenderSummary(BaseModel):
    tender_code: str
    project_name: str
    status: str  # ok | conflict | original_missing
    current_deadline: Optional[str] = None
    conflict: Optional[list[ConflictItem]] = None
    notes: list[str] = Field(default_factory=list)


class TenderDetail(TenderSummary):
    announcements: list[AnnouncementView]


class CollectResponse(BaseModel):
    status: str
    catalog_url: str
    total_pages: int
    total_announcements: int
    collected_at: str
    replaced_previous: bool


# ---- 桩号核查（路段表）----

class SegmentCheckRequest(BaseModel):
    """桩号核查请求。

    同时接受 camelCase 与 snake_case 字段名，方便资料员从表格复制粘贴。
    """

    model_config = ConfigDict(populate_by_name=True)

    line_code: str = Field(
        ...,
        min_length=1,
        validation_alias="lineCode",
        description="线路编号（如 LX-2026-A）",
    )
    start_station: str = Field(
        ...,
        validation_alias="startStation",
        description="核查起始桩号（如 K0+000）",
    )
    end_station: str = Field(
        ...,
        validation_alias="endStation",
        description="核查终止桩号（如 K3+500）",
    )
    catalog_url: HttpUrl = Field(
        ...,
        validation_alias="catalogUrl",
        description="路段明细目录第一页 URL（同站分页）",
    )


class SegmentSource(BaseModel):
    """一条可靠路段记录及其来源。"""

    segment_code: str
    start_station: str
    end_station: str
    source_url: str
    page_url: str


class SegmentConflict(BaseModel):
    """同一分段编号下的冲突记录。

    当同编号记录区间不同时，不选边，全部列为冲突；冲突记录不参与覆盖证明。
    """

    segment_code: str
    records: list[SegmentSource]
    message: str


class StationRange(BaseModel):
    """以桩号表示的区间。"""

    start_station: str
    end_station: str
    start_meters: float
    end_meters: float


class OverlapRange(StationRange):
    """重叠区间（含来源）。"""

    sources: list[SegmentSource] = Field(default_factory=list)


class GapRange(StationRange):
    """缺段区间。"""


class PageFetchError(BaseModel):
    page_url: str
    error: str


class SegmentCheckResponse(BaseModel):
    line_code: str
    start_station: str
    end_station: str
    catalog_url: str
    collected_at: str

    total_pages: int
    fetched_pages: int
    failed_pages: list[PageFetchError] = Field(default_factory=list)
    complete: bool = Field(
        ...,
        description="是否完整采集；存在页面读取失败时为 false",
    )
    verdict: str = Field(
        ...,
        description="结论：complete（完整覆盖）| gaps（采集完整但有缺段）| "
        "indeterminate（存在页面读取失败或编号冲突，不下确定结论）",
    )
    fully_covered: Optional[bool] = Field(
        None,
        description="目标范围是否被可靠记录完整覆盖；结论不确定时为 null",
    )

    segments: list[SegmentSource] = Field(
        default_factory=list,
        description="统计后参与覆盖计算的路段（同编号同区间只计一次）",
    )
    duplicates: list[SegmentSource] = Field(
        default_factory=list,
        description="被去重（同编号且同区间）的重复记录",
    )
    conflicts: list[SegmentConflict] = Field(default_factory=list)
    gaps: list[GapRange] = Field(
        default_factory=list,
        description="确定缺段；仅当结论确定（verdict=complete|gaps）时填充",
    )
    uncovered_ranges: list[GapRange] = Field(
        default_factory=list,
        description="按已读可靠记录计算的未覆盖区间，供参考；"
        "结论不确定时不代表确定缺段",
    )
    overlaps: list[OverlapRange] = Field(default_factory=list)

    notes: list[str] = Field(default_factory=list)
