# 市政工程投标公告采集与桩号核查 API

面向投标专员核对各标段**实际投标截止时间**的采集服务：接收公告目录 URL，沿分页抓取与内置样例结构一致的详情页，按招标编号归集**原公告 / 改期公告 / 撤回公告**，沿有效公告链计算当前截止时间；并行改期时间不一致时如实返回冲突，不猜测结果。

另提供**桩号核查**接口：输入线路编号、核查起止桩号与路段明细目录 URL，沿同站分页读取施工路段表，换算桩号、去重、判定冲突，并报告目标范围内的缺段、重叠段及来源。

## 业务规则

1. **归集**：以招标编号分组。原公告为链根；每条改期公告通过“依据公告”注明其依据的公告（原公告或上一条改期）；撤回公告通过“撤回公告”字段指定被撤回的改期。
2. **撤回级联失效**：撤回指定改期后，该改期及其全部后续改期（沿依据链传递依赖）标记为失效并注明原因；失效公告不参与截止时间计算。
3. **有效链与截止时间**：从原公告出发，沿有效改期逐级下行，链末端公告的投标截止时间即当前有效截止时间，返回结果同时给出该公告的来源链接。
4. **并行改期冲突**：同一有效依据下存在多条有效并行改期时：
   - 截止时间一致 → 取该时间并在 `notes` 说明；
   - 截止时间不一致 → `status=conflict`，`current_deadline=null`，`conflict` 列出全部冲突公告及其时间与来源链接，**不猜结果**。
5. **重新采集**：再次采集成功时用完整新结果**整体替换**旧结果（临时文件 + 原子 rename）；抓取或解析任一页面失败时返回 502 且**不触碰存储**，旧结果完整保留。

## 桩号核查规则

接口 `POST /api/station-check`，请求字段同时接受 camelCase 与 snake_case：

```json
{
  "lineCode": "LX-2026-A",
  "startStation": "K0+000",
  "endStation": "K3+500",
  "catalogUrl": "http://localhost:8000/sample/segments/page_1.html"
}
```

1. **桩号换算**：`K12+345` → `12345` 米（`K` 可省略，`+` 后米数 0~999，可带小数）。
2. **同站分页**：沿 `<a rel="next">` 读取全部路段页；跨站分页链接会被拒绝。
3. **去重**：同一分段编号且起止桩号相同 → 只计一次，其余列入 `duplicates`。
4. **冲突**：同一分段编号但区间不同 → 列入 `conflicts`，列出每个变体及其来源；**冲突记录不参与覆盖计算**，不用它们证明覆盖。
5. **缺段**：目标范围内未被可靠记录覆盖的部分；**相邻路段共用端点不算重叠**（区间按 `[start, end)` 处理）。
6. **重叠**：可靠记录之间的正长度交叠，列出区间及双方来源。
7. **结论**：只有完整采集且无编号冲突时才给出 `verdict=complete|gaps` 与 `fully_covered`，`gaps` 才是确定缺段；页面读取失败或存在编号冲突时 `verdict=indeterminate`、`fully_covered=null`、`gaps=[]`，按已读可靠记录算出的空档只放在 `uncovered_ranges` 供参考，**不把剩余空档或完整覆盖报为确定结论**，但由可靠记录证实的重叠仍照常列出。

## 目录结构

```
app/                 FastAPI 应用
  main.py            路由：采集、查询、桩号核查、离线样例静态挂载
  crawler.py         分页抓取（httpx 异步 + 并发限制 + 上限/循环保护）
  parser.py          目录页/详情页 HTML 解析
  chain.py           归集、撤回级联失效、并行冲突判定
  segment_parser.py  路段表 HTML 解析（线路过滤、来源链接）
  segment_crawler.py 路段表同站分页采集（允许部分成功）
  segment_check.py   去重、编号冲突、缺段/重叠计算与结论判定
  stations.py        桩号 <-> 米换算
  storage.py         JSON 文件存储（异步锁、原子替换）
  models.py          Pydantic 模型
sample_site/         可离线访问的分页 HTML 公告与路段样例（相对链接、无外部资源）
  index.html
  catalog/page_1..3.html
  detail/A001..A018.html
  segments/page_1..3.html        主场景（重复/冲突/重叠/缺段）
  segments/clean/page_1..2.html  完整覆盖
  segments/broken/page_1.html    第 2 页缺失（读取失败）
  segment_sources/               路段来源页
scripts/generate_samples.py  样例站点生成脚本（结构即解析契约）
tests/               单元 + 集成测试
Dockerfile / docker-compose.yml
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
# API:      http://localhost:8000
# 交互文档:  http://localhost:8000/docs
# 离线样例:  http://localhost:8000/sample  （也可直接用浏览器打开 sample_site/index.html）
```

采集结果持久化在 `tender-data` 卷内（`/data/store.json`）。

## 本地无 Docker 运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/generate_samples.py          # 已随仓库提供，修改样例后重新生成
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口

### `POST /api/collect` 触发采集

```json
// 请求
{ "catalog_url": "http://localhost:8000/sample/catalog/page_1.html" }
// 成功响应 200
{
  "status": "collected",
  "catalog_url": "...",
  "total_pages": 3,
  "total_announcements": 18,
  "collected_at": "2026-11-11T10:46:32.894225+00:00",
  "replaced_previous": true
}
// 失败响应 502（旧结果保留）
{ "detail": { "error": "抓取失败 ...", "previous_result_retained": true,
              "message": "采集失败，已保留上一次成功采集的结果" } }
```

### `GET /api/tenders` 各标段截止时间汇总

```json
[
  { "tender_code": "ZG-2026-001", "project_name": "市政道路改造工程",
    "status": "ok", "current_deadline": "2026-12-04 09:30",
    "conflict": null, "notes": ["当前截止时间依据有效公告链末端公告 A018（改期公告）"] },
  { "tender_code": "ZG-2026-003", "status": "conflict", "current_deadline": null,
    "conflict": [ {"announcement_id": "A012", "deadline": "2026-12-05 09:30",
                   "published_at": "2026-11-12 09:00", "source_url": "..."},
                  {"announcement_id": "A013", "deadline": "2026-12-08 09:30", ...} ] }
]
```

### `GET /api/tenders/{招标编号}` 单标段完整公告链

每条公告带 `valid` / `invalid_reason` / `source_url`，可直接核对来源。例：

```
A011 改期公告 valid=False 被撤回公告直接撤回
A017 改期公告 valid=False 所依据的改期公告 A011 已失效（撤回级联）
```

其他：`GET /api/raw`（最近一次采集原始数据）、`GET /health`。

### `POST /api/station-check` 桩号核查

请求与规则见上文“桩号核查规则”。以主场景（`/sample/segments/page_1.html`，
目标 `K0+000`~`K3+500`）为例的响应要点：

```json
{
  "line_code": "LX-2026-A",
  "complete": true, "fetched_pages": 3, "total_pages": 3, "failed_pages": [],
  "verdict": "indeterminate", "fully_covered": null,
  "duplicates": [ { "segment_code": "S003", "source_url": ".../segment_sources/SRC-03B.html" } ],
  "conflicts": [ { "segment_code": "S006",
    "records": [ { "start_station": "K1+800", "end_station": "K2+000", "source_url": "..." },
                 { "start_station": "K2+000", "end_station": "K2+300", "source_url": "..." } ],
    "message": "分段编号 S006 存在 2 种不同区间，视为冲突，其记录不用于覆盖计算" } ],
  "gaps": [],
  "uncovered_ranges": [ { "start_station": "K1+800", "end_station": "K2+100", ... } ],
  "overlaps": [ { "start_station": "K1+400", "end_station": "K1+500",
    "sources": [ { "segment_code": "S003", ... }, { "segment_code": "S005", ... } ] } ]
}
```

`verdict`：`complete`（完整覆盖）/ `gaps`（采集完整但有确定缺段）/ `indeterminate`（页面读取失败或存在编号冲突，不下确定结论；此时 `gaps` 为空，空档仅见 `uncovered_ranges`）。

## 内置样例覆盖的场景

| 招标编号 | 场景 | 预期结果 |
|---|---|---|
| ZG-2026-001 | 原公告 + 4 次串行改期 | 截止时间取末次改期 `2026-12-04 09:30` |
| ZG-2026-002 | 第二次改期被撤回，第三次改期依赖它 | 撤回 + 级联失效，回退到第一次改期 `2026-11-26 09:30` |
| ZG-2026-003 | 同一依据下两条并行改期、时间不同 | `conflict`，列出 A012/A013 两个时间与来源 |
| ZG-2026-004 | 第一次改期被撤回、第二次级联失效 | 回退到原公告 `2026-11-21 09:30` |

| 路段样例 | 目标范围 | 场景与预期 |
|---|---|---|
| `segments/page_1..3` | `K0+000`~`K3+500` | 重复 S003（只计一次）、冲突 S006（两种区间，不证覆盖）、重叠 K1+400~K1+500、未覆盖参考 K1+800~K2+100；`verdict=indeterminate` |
| `segments/clean/page_1..2` | `K0+000`~`K2+000` | 相邻路段仅共用端点，无重叠无缺段；`verdict=complete` |
| `segments/broken/page_1` | `K0+000`~`K1+500` | 第 2 页 404：保留第 1 页可靠重叠 K0+300~K0+800，`fully_covered=null` |

## 页面结构约定（采集目标需与样例一致）

- 目录页：`<ul class="catalog">` 内每条一个详情 `<a href>`；分页用 `<a rel="next" href="...">`，无该链接即末页。
- 详情页：`<table id="announcement-meta">` 内 `th/td` 键值对，字段为
  公告编号、招标编号、公告类型（招标公告/改期公告/撤回公告）、项目名称、发布时间、
  投标截止时间、依据公告（改期必填）、撤回公告（撤回必填）。
- 路段页：`<table id="segment-table">`（或 `class=segment-table`），表头含
  分段编号、起始桩号、终止桩号，可选线路编号、来源；来源列若有 `<a href>` 取该链接，
  否则取当前页 URL；分页同样用 `<a rel="next">`。桩号非法、起止颠倒或缺编号的
  页面按“读取失败”处理，不使用该页数据。

## 测试

```bash
pip install -r requirements.txt
pytest -q
# 53 passed：
#   链路规则 7 项（含撤回级联、并行冲突、部分撤回），公告 API 全流程 5 项
#   （含采集失败保留旧结果、重新采集整体替换、离线样例可访问）
#   桩号规则 33 项（换算、去重、冲突不证覆盖、共用端点不算重叠、裁剪错位、部分采集结论）
#   桩号核查 API 8 项（主场景、完整覆盖、分页失败、跨站拒绝、字段别名、422、首页失败、离线可达）
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATA_DIR` | `./data` | 采集结果存储目录 |
| `REQUEST_TIMEOUT` | `10` | 单次 HTTP 超时秒数 |
| `MAX_PAGES` | `100` | 目录分页上限（防循环/异常） |
| `MAX_CONCURRENCY` | `8` | 详情页抓取并发数 |
