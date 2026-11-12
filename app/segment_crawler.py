"""路段明细采集器：沿同站分页读取路段表。

与公告采集器不同，这里允许“部分成功”：单个页面读取或解析失败时记录该
失败并停止沿下一页继续（失败页上的分页链接不可得），已成功读取的页面
仍返回给分析层，用于给出可靠记录证实的重叠。存在失败页时，分析层不得把
剩余空档或完整覆盖当作确定结论。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from . import config
from .segment_parser import SegmentParseError, SegmentRecord, parse_segment_page


class SegmentCrawlError(Exception):
    """单页抓取失败；该页不可用且无法再取得其分页链接。"""


@dataclass
class SegmentPageError:
    page_url: str
    error: str


@dataclass
class SegmentCrawlResult:
    records: list[SegmentRecord] = field(default_factory=list)
    total_pages: int = 0
    fetched_pages: int = 0
    failed_pages: list[SegmentPageError] = field(default_factory=list)


def _same_origin(base: str, candidate: str) -> bool:
    a, b = urlsplit(base), urlsplit(candidate)
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)


class SegmentCrawler:
    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(config.MAX_CONCURRENCY)

    async def _get(self, client: httpx.AsyncClient, url: str) -> str:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SegmentCrawlError(f"抓取失败 {url}: {exc}") from exc
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            raise SegmentCrawlError(f"响应不是 HTML {url} (content-type={ctype})")
        return resp.text

    async def collect(
        self, catalog_url: str, line_code: str
    ) -> SegmentCrawlResult:
        result = SegmentCrawlResult()
        async with httpx.AsyncClient(
            timeout=config.REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": config.USER_AGENT},
        ) as client:
            seen: set[str] = set()
            page_url: str | None = catalog_url
            while page_url is not None:
                if page_url in seen:
                    result.failed_pages.append(
                        SegmentPageError(
                            page_url=page_url, error="分页出现循环，已停止"
                        )
                    )
                    break
                seen.add(page_url)
                result.total_pages += 1
                if result.total_pages > config.MAX_PAGES:
                    result.failed_pages.append(
                        SegmentPageError(
                            page_url=page_url,
                            error=f"分页数超过上限 {config.MAX_PAGES}，已停止",
                        )
                    )
                    break

                if not _same_origin(catalog_url, page_url):
                    result.failed_pages.append(
                        SegmentPageError(
                            page_url=page_url,
                            error="分页链接跨站，已停止（仅允许同站分页）",
                        )
                    )
                    break

                try:
                    html_text = await self._get(client, page_url)
                except SegmentCrawlError as exc:
                    result.failed_pages.append(
                        SegmentPageError(page_url=page_url, error=str(exc))
                    )
                    # 失败页上无法取得下一页链接；停止但保留已读页数据
                    break

                try:
                    records, next_url = parse_segment_page(
                        html_text, page_url, line_code
                    )
                except SegmentParseError as exc:
                    result.failed_pages.append(
                        SegmentPageError(page_url=page_url, error=str(exc))
                    )
                    break

                result.records.extend(records)
                result.fetched_pages += 1
                page_url = next_url

        return result

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
