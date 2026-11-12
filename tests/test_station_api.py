"""桩号核查 API 集成测试：真实走分页抓取 -> 解析 -> 去重/冲突/覆盖分析。

请求通过 httpx.MockTransport 路由到本地样例 HTML，与离线样例一致。
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, storage
from app.main import app

SITE = config.SAMPLE_DIR
MAIN = "http://testserver/sample/segments/page_1.html"
CLEAN = "http://testserver/sample/segments/clean/page_1.html"
BROKEN = "http://testserver/sample/segments/broken/page_1.html"


def _sample_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if not path.startswith("/sample/"):
        return httpx.Response(404, text="not found")
    f = (SITE / path[len("/sample/"):]).resolve()
    if not str(f).startswith(str(SITE.resolve())) or not f.is_file():
        return httpx.Response(404, text="not found")
    return httpx.Response(
        200,
        content=f.read_bytes(),
        headers={"content-type": "text/html; charset=utf-8"},
    )


_HANDLER = {"fn": _sample_handler}


def _dispatch(request: httpx.Request) -> httpx.Response:
    return _HANDLER["fn"](request)


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(config, "STORE_PATH", tmp_path / "store.json")
    from app import main
    monkeypatch.setattr(main, "store", storage.Store(config.STORE_PATH))

    _HANDLER["fn"] = _sample_handler
    transport = httpx.MockTransport(_dispatch)
    real_async_client = httpx.AsyncClient

    class PatchedAsyncClient(real_async_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", PatchedAsyncClient)
    yield TestClient(app)
    _HANDLER["fn"] = _sample_handler


def _check(client, **body):
    return client.post("/api/station-check", json=body)


def test_main_scenario_reports_gap_overlap_and_conflict(client):
    resp = _check(
        client,
        lineCode="LX-2026-A",
        startStation="K0+000",
        endStation="K3+500",
        catalogUrl=MAIN,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["complete"] is True
    assert body["total_pages"] == 3 and body["fetched_pages"] == 3
    assert body["failed_pages"] == []
    # 存在编号冲突 -> 不把完整覆盖/剩余空档报为确定结论
    assert body["verdict"] == "indeterminate"
    assert body["fully_covered"] is None

    # 同编号同区间只计一次（S003 两条只保留一条参与计算）
    codes = [s["segment_code"] for s in body["segments"]]
    assert codes.count("S003") == 1
    assert "S001" in codes and "S099" not in codes    # 其他线路被过滤
    assert [d["segment_code"] for d in body["duplicates"]] == ["S003"]
    assert len(body["duplicates"]) == 1

    # 同编号不同区间 -> 冲突，记录全部列出且带来源
    assert len(body["conflicts"]) == 1
    conflict = body["conflicts"][0]
    assert conflict["segment_code"] == "S006"
    assert {(r["start_station"], r["end_station"]) for r in conflict["records"]} == {
        ("K1+800", "K2+000"),
        ("K2+000", "K2+300"),
    }
    assert all(r["source_url"].startswith("http://testserver/sample/")
               for r in conflict["records"])

    # 冲突排除后存在未覆盖区间参考值；不确定结论下调为 uncovered_ranges
    assert body["gaps"] == []
    assert [(g["start_station"], g["end_station"]) for g in body["uncovered_ranges"]] == [
        ("K1+800", "K2+100")
    ]

    # 可靠记录证实的重叠 + 双方来源
    assert len(body["overlaps"]) == 1
    overlap = body["overlaps"][0]
    assert (overlap["start_station"], overlap["end_station"]) == ("K1+400", "K1+500")
    assert {s["segment_code"] for s in overlap["sources"]} == {"S003", "S005"}
    assert all(s["source_url"].startswith("http://testserver/sample/segment_sources/")
               for s in overlap["sources"])


def test_clean_scenario_is_complete_without_shared_endpoint_overlap(client):
    resp = _check(
        client,
        line_code="LX-2026-B",
        start_station="K0+000",
        end_station="K2+000",
        catalog_url=CLEAN,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] == "complete"
    assert body["fully_covered"] is True
    assert body["gaps"] == [] and body["overlaps"] == []
    assert body["conflicts"] == []
    assert body["complete"] is True


def test_page_fetch_failure_keeps_overlaps_but_indeterminate(client):
    resp = _check(
        client,
        lineCode="LX-2026-D",
        startStation="K0+000",
        endStation="K1+500",
        catalogUrl=BROKEN,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["complete"] is False
    assert body["fetched_pages"] == 1 and body["total_pages"] == 2
    assert len(body["failed_pages"]) == 1
    assert body["failed_pages"][0]["page_url"].endswith("/broken/page_2.html")

    # 不把剩余空档或完整覆盖报为确定结论
    assert body["fully_covered"] is None
    assert body["verdict"] == "indeterminate"
    # 已读页中由可靠记录证实的重叠仍列出（精确到区间与双方来源）
    got = {
        (o["start_station"], o["end_station"]): {s["segment_code"] for s in o["sources"]}
        for o in body["overlaps"]
    }
    assert got == {
        ("K0+300", "K0+500"): {"R001", "R003"},
        ("K0+500", "K0+800"): {"R002", "R003"},
    }
    # 确定缺段为空；未覆盖区间仅作参考
    assert body["gaps"] == []
    assert [(g["start_station"], g["end_station"]) for g in body["uncovered_ranges"]] == [
        ("K1+000", "K1+500")
    ]


def test_catalog_url_accepts_camel_case_and_snake_case(client):
    for payload in (
        {
            "lineCode": "LX-2026-B",
            "startStation": "K0+000",
            "endStation": "K2+000",
            "catalogUrl": CLEAN,
        },
        {
            "line_code": "LX-2026-B",
            "start_station": "K0+000",
            "end_station": "K2+000",
            "catalog_url": CLEAN,
        },
    ):
        resp = client.post("/api/station-check", json=payload)
        assert resp.status_code == 200, resp.text
        assert resp.json()["fully_covered"] is True


def test_invalid_station_returns_422(client):
    reversed_range = _check(
        client,
        lineCode="LX-2026-B",
        startStation="K1+000",
        endStation="K0+000",
        catalogUrl=CLEAN,
    )
    assert reversed_range.status_code == 422
    assert "小于" in reversed_range.json()["detail"]

    invalid = _check(
        client,
        lineCode="LX-2026-B",
        startStation="abc",
        endStation="K2+000",
        catalogUrl=CLEAN,
    )
    assert invalid.status_code == 422
    assert "无法识别" in invalid.json()["detail"]

    # 省略 K 前缀的 0+000 仍属合法桩号
    ok = _check(
        client,
        lineCode="LX-2026-B",
        startStation="0+000",
        endStation="K2+000",
        catalogUrl=CLEAN,
    )
    assert ok.status_code == 200


def test_broken_catalog_first_page_is_indeterminate(client):
    """首页即失败：没有任何可靠记录，也不得给出完整覆盖结论。"""
    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated outage")

    _HANDLER["fn"] = failing
    try:
        resp = _check(
            client,
            lineCode="LX-2026-B",
            startStation="K0+000",
            endStation="K2+000",
            catalogUrl=CLEAN,
        )
    finally:
        _HANDLER["fn"] = _sample_handler

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fetched_pages"] == 0
    assert body["complete"] is False
    assert body["fully_covered"] is None
    assert body["verdict"] == "indeterminate"
    assert body["segments"] == [] and body["overlaps"] == []


def test_cross_site_next_link_is_refused(client):
    """分页链接指向其他站点时停止抓取，并按不完整采集处理。"""
    def cross_site(request: httpx.Request) -> httpx.Response:
        resp = _sample_handler(request)
        if request.url.path.endswith("/segments/page_1.html"):
            text = resp.text.replace(
                'href="page_2.html">下一页',
                'href="http://evil.example/page_2.html">下一页',
            )
            return httpx.Response(200, content=text,
                                  headers={"content-type": "text/html"})
        return resp

    _HANDLER["fn"] = cross_site
    try:
        resp = _check(
            client,
            lineCode="LX-2026-A",
            startStation="K0+000",
            endStation="K3+500",
            catalogUrl=MAIN,
        )
    finally:
        _HANDLER["fn"] = _sample_handler

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["complete"] is False
    assert body["fetched_pages"] == 1
    assert any("跨站" in e["error"] for e in body["failed_pages"])
    assert body["fully_covered"] is None


def test_offline_segment_sample_is_reachable(client):
    """样例本身可离线访问（静态挂载），无需先调用采集。"""
    idx = client.get("/sample/segments/index.html")
    assert idx.status_code == 200
    assert "施工路段桩号表" in idx.text
    page = client.get("/sample/segments/page_2.html")
    assert page.status_code == 200
    assert 'id="segment-table"' in page.text
    src = client.get("/sample/segment_sources/SRC-05.html")
    assert src.status_code == 200
