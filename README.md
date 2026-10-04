# 离线矢量瓦片编译 API

基于 **Python + FastAPI + SQLite + Shapely + mapbox-vector-tile** 的离线矢量瓦片服务。
上传 **EPSG:4326 GeoJSON** 图层后，服务以**不可变 revision（修订版本）**保存；
请求瓦片时按 **Web Mercator (EPSG:3857)** 投影、在线编译为 **Mapbox Vector Tile (.mvt)**，
并保证**相同输入永远产生字节级一致的瓦片和 ETag**。

---

## 1. 功能特性

| 能力 | 说明 |
| --- | --- |
| 发布图层 | `POST /datasets` 上传 GeoJSON `FeatureCollection`（multipart 或原始 JSON body），创建数据集并发布 **revision 1** |
| 不可变版本 | 每次发布写入一个新的 revision；历史 revision 永不被覆盖，可通过 `?revision=N` 固定取瓦片 |
| 乐观并发控制 | 发布新版本必须携带 `expected_revision`，与当前 head 不一致时返回 **409 Conflict**；head 比较、revision 插入与 head 更新在单个跨进程有效的 `BEGIN IMMEDIATE` 事务内原子完成，多 worker 并发追加也只有一方成功 |
| 瓦片编译 | `GET /datasets/{id}/tiles/{z}/{x}/{y}.mvt`，支持 **z = 0–14** 的 XYZ 瓦片 |
| 离线增量清单 | `GET /datasets/{id}/tile-diff?from_revision&to_revision&z`：按**实际编码字节**列出两个不可变 revision 之间发生变化的瓦片（含两端 ETag），候选范围覆盖新增 / 删除 / 移动 / 属性变化与 **8 像素缓冲**邻瓦片；z=14 的局部更新只枚举触达瓦片，不枚举全球 |
| 投影 | EPSG:4326 经纬度 → 球面 Web Mercator（R = 6378137），瓦片内坐标系为 4096 网格、Y 轴向下 |
| 8 像素缓冲裁剪 | 点 / 线 / 面先裁剪到瓦片外扩 **8 px（128/4096 单位）**的边界，保证跨瓦片连续 |
| 0.5 px 拓扑简化 | 以 **0.5 px（8/4096 单位）**容差做保拓扑 Douglas–Peucker 简化 |
| 4096 量化 | 坐标四舍五入到整数 4096 网格，并清除退化线 / 零面积环 |
| 稳定排序 | 图层按名称、要素按 ID（数值 → 字符串 → 内容哈希）确定性排序后编码 |
| 字节一致 / ETag | 同一 revision + 同一瓦片坐标，重复请求**字节完全相同、ETag 相同**；已编译瓦片持久化到 SQLite |
| 条件请求 | 支持 `If-None-Match`，命中时返回 **304 Not Modified** |
| 错误处理 | 非法 GeoJSON / 非法几何 / 无效瓦片参数返回 **400**，数据集或版本不存在返回 **404** |
| 一键启动 | 提供 `Dockerfile` 与 `docker-compose.yml`，数据持久化在命名卷中；两个 worker 在全新卷上同时建库由**非阻塞**文件锁 + 有界重试串行化，两者都会完整启动（见 [§3.2](#32-全新数据卷初始化竞态复核)）；另有进程持续占锁时启动会**在退避预算内明确失败退出而不是悬挂**，解锁后可正常启动（见 [§3.3](#33-持锁--解锁启动复核docker-compose)） |

---

## 2. 目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由：发布、版本、瓦片、离线增量清单、错误处理
│   ├── tiler.py         # 裁剪 / 简化 / 量化 / 稳定排序 / MVT 编码 / 候选瓦片枚举
│   ├── projection.py    # EPSG:4326 -> Web Mercator -> 瓦片 4096 网格
│   ├── geojson.py       # GeoJSON 校验与规范化
│   ├── db.py            # SQLite 存储（datasets / revisions / tile_cache，非阻塞跨进程建库锁与事务）
│   └── config.py        # 环境变量配置
├── data/sample.geojson  # 示例图层（点 / 线 / 面）
├── scripts/
│   └── hold_init_lock.py # 持锁复核辅助：独占 <db>.init.lock（可定时释放）
├── tests/               # pytest 测试（100 个用例，含 tile-diff 字节裁决、缓冲邻瓦片与 z14 局部更新）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## 3. Docker Compose 一键启动（推荐）

```bash
docker compose up -d --build
```

启动后：

- 服务地址：<http://localhost:8000>
- 交互文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/healthz>
- SQLite 数据库保存在 Docker 命名卷 `tile-data`（容器内 `/data/tiles.db`），容器重建后数据不丢
- 默认以 **2 个 uvicorn worker 进程**启动（`VT_WORKERS=2`），两个进程共享同一个
  SQLite 文件；**首次启动建库由跨进程文件锁串行化、并带有界重试**（见
  [§3.2](#32-全新数据卷初始化竞态复核)），跨进程的追加发布则由 `BEGIN IMMEDIATE`
  事务串行化，可直接按 [§4.2](#42-发布新版本乐观并发控制) 末尾的并发命令复核竞态
- 若**另一个进程持续持有** `/data/tiles.db.init.lock` 排他锁，worker 不会无限等待：
  取锁是**非阻塞尝试**，失败后与 SQLite 忙锁共用同一份重试 / 退避预算，预算耗尽即抛
  `SchemaUnavailable` 明确结束启动；持锁进程释放后重启即可正常建库。可用
  Compose 一键复核，见 [§3.3](#33-持锁--解锁启动复核docker-compose)

常用命令：

```bash
docker compose ps            # 查看状态与健康检查
docker compose logs -f       # 查看日志
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除数据卷
```

> 如需把数据库放到宿主机目录，可把 `docker-compose.yml` 中的
> `tile-data:/data` 改为 `./data:/data`。

### 3.1 本地直接运行（不用 Docker）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export VT_DB_PATH=./data/tiles.db          # SQLite 文件路径
# 追加发布的跨进程竞态可像 Docker 一样用多 worker 复核：追加 --workers 2
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
```

运行测试：

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

### 3.2 全新数据卷初始化竞态复核

两个 worker 在全新卷上同时执行 lifespan 建库时，`PRAGMA busy_timeout` 会在
`PRAGMA journal_mode=WAL` **之前**安装；建库 DDL 全部使用
`CREATE TABLE/INDEX IF NOT EXISTS`，并由容器内 `/data/tiles.db.init.lock` 上的
排他文件锁（`flock`）串行化。取锁使用 **`LOCK_EX | LOCK_NB` 非阻塞尝试**：抢不到锁
（例如另一个进程正在建库、或持锁不放）时立即抛 `InitLockUnavailable`，与 SQLite 的
`database is locked` 忙错误**共用同一份**最多 8 次、指数退避（0.1 s 起，上限 2 s）的
有界预算；抢到锁的进程仍串行建库，且 DDL 异常时在 `finally` 中释放已获取的锁。最后
重新打开连接复核 WAL 模式与全部 schema 对象均可见。因此两个 worker 都会完整启动、建同
一份 schema 且不重复建表；而一个**持续不放锁的外部进程**只会让启动在预算内重试后退
出（见 [§3.3](#33-持锁--解锁启动复核docker-compose)），绝不会卡在阻塞式 `flock` 里
悬挂。

**第 1 步：在全新数据卷上一键启动**（`down -v` 确保卷是空的）：

```bash
docker compose down -v          # 仅复核时需要：删除旧卷，模拟全新数据盘
docker compose up -d --build
```

**第 2 步：确认两个 worker 都完成启动，且没有 `database is locked`**：

```bash
docker compose logs vector-tiles | grep -E "Started server process|startup complete|locked"
```

预期输出（两个进程各一次 `Application startup complete`，且无任何 `locked`）：

```text
INFO:     Started server process [10]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Started server process [11]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
```

**第 3 步：确认健康检查稳定为 200**（容器状态与连续 HTTP 请求两路复核）：

```bash
docker compose ps
# NAME                      ... STATUS
# offline-vector-tile-api   ... Up (healthy)

for i in $(seq 1 10); do
  curl -s -o /dev/null -w "%{http_code} " http://localhost:8000/healthz
done; echo
```

```text
200 200 200 200 200 200 200 200 200 200
```

`/healthz` 的响应体：

```bash
curl -s -i http://localhost:8000/healthz
```

```http
HTTP/1.1 200 OK
content-type: application/json
```

```json
{"status": "ok"}
```

**第 4 步：直接检查卷内数据库——WAL 已启用、四张 schema 对象各只定义一次**：

```bash
docker compose exec vector-tiles python - <<'EOF'
import sqlite3
conn = sqlite3.connect("/data/tiles.db")
print("journal_mode =", conn.execute("PRAGMA journal_mode").fetchone()[0].lower())
rows = conn.execute(
    "SELECT type, name FROM sqlite_master "
    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
).fetchall()
for kind, name in rows:
    print(f"{kind:5s} {name}")
dupes = conn.execute(
    "SELECT name FROM sqlite_master GROUP BY name HAVING COUNT(*) > 1"
).fetchall()
print("duplicate objects:", dupes)
EOF
```

```text
journal_mode = wal
index idx_tile_cache_rev
table datasets
table revisions
table tile_cache
duplicate objects: []
```

**第 5 步（可选）：反复清空卷并启动，多轮复核**：

```bash
for round in 1 2 3; do
  docker compose down -v >/dev/null 2>&1
  docker compose up -d --build >/dev/null 2>&1
  sleep 3
  code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/healthz)
  locked=$(docker compose logs vector-tiles | grep -c 'database is locked' || true)
  echo "round $round: healthz=$code locked_errors=$locked"
done
```

```text
round 1: healthz=200 locked_errors=0
round 2: healthz=200 locked_errors=0
round 3: healthz=200 locked_errors=0
```

初始化相关参数也可以用环境变量调整（见 [§7](#7-配置环境变量)）：
`VT_SQLITE_BUSY_TIMEOUT_MS`（默认 30000）、`VT_INIT_MAX_ATTEMPTS`（默认 8）、
`VT_INIT_RETRY_DELAY_S`（默认 0.1）。

### 3.3 持锁 / 解锁启动复核（Docker Compose）

本节复核修复点：**另一个进程持续持有 `tiles.db.init.lock` 排他锁时，启动不能悬挂**。
worker 的取锁为非阻塞尝试，与 SQLite 忙锁共用 `VT_INIT_MAX_ATTEMPTS` / 退避预算；预算
内锁被释放则两个 worker 串行建库、`/healthz` 返回 200；锁一直不释放则明确抛
`SchemaUnavailable` 并结束启动，容器进入重启退避而不是无限等待。

Compose 提供一个独立的 `init-lock-holder` 服务（放在 `locktest` profile 中，
**普通 `docker compose up` 不会启动它**），它用 `scripts/hold_init_lock.py` 抢同一把
`/data/tiles.db.init.lock` 排他锁：不带秒数时持续持锁直到被 `stop`，带秒数时到时自动
释放（用于复核“预算内释放”）。

**第 1 步：全新卷 + 先启动持锁者，再启动 API（持续占锁）**

```bash
docker compose down -v
docker compose --profile locktest up -d init-lock-holder
docker compose logs init-lock-holder
# vt-init-lock-holder  | holding exclusive lock on /data/tiles.db.init.lock until stopped
docker compose up -d vector-tiles
```

**第 2 步：确认 worker 在退避预算内重试后明确失败，而不是悬挂**

```bash
sleep 10
docker compose logs vector-tiles | grep -E "init_db attempt|SchemaUnavailable|startup failed" | head -20
```

预期日志（每个 worker 先按 0.1 s、0.2 s、0.4 s 退避重试，预算耗尽即抛
`SchemaUnavailable` 并结束启动）：

```text
WARNING:  init_db attempt 1/8 failed (init lock is held by another process: /data/tiles.db.init.lock); retrying in 0.10s
WARNING:  init_db attempt 2/8 failed (init lock is held by another process: /data/tiles.db.init.lock); retrying in 0.20s
WARNING:  init_db attempt 3/8 failed (init lock is held by another process: /data/tiles.db.init.lock); retrying in 0.40s
ERROR:    init_db gave up after 8 attempts: init lock is held by another process: /data/tiles.db.init.lock
app.db.SchemaUnavailable: database initialisation failed after 8 attempts: init lock is held by another process: /data/tiles.db.init.lock
ERROR:    Application startup failed. Exiting.
```

此时端口尚未提供服务，健康检查不通（**不会有任何请求悬挂**）：

```bash
curl -s -o /dev/null -w "%{http_code}\n" --max-time 3 http://localhost:8000/healthz || echo "connection refused (startup not complete)"
```

```text
000
connection refused (startup not complete)
```

容器状态表现为反复重启（`restart: unless-stopped`），但每次启动都是有界的：

```bash
docker compose ps vector-tiles
# NAME                      ... STATUS
# offline-vector-tile-api   ... Restarting (...)
```

**第 3 步：释放锁并重启——两个 worker 串行建好同一份完整 schema，/healthz 返回 200**

```bash
docker compose stop init-lock-holder
docker compose restart vector-tiles
sleep 5
for i in $(seq 1 10); do
  curl -s -o /dev/null -w "%{http_code} " http://localhost:8000/healthz
done; echo
```

```text
200 200 200 200 200 200 200 200 200 200
```

```bash
docker compose logs vector-tiles | grep -E "Started server process|Application startup complete"
```

```text
INFO:     Started server process [10]
INFO:     Application startup complete.
INFO:     Started server process [11]
INFO:     Application startup complete.
```

两个 worker 看到的是同一份完整 schema（WAL + 三张表 + 索引，各只定义一次），
检查命令与输出与 [§3.2 第 4 步](#32-全新数据卷初始化竞态复核) 相同：

```text
journal_mode = wal
index idx_tile_cache_rev
table datasets
table revisions
table tile_cache
duplicate objects: []
```

**第 4 步（可选）：预算内自动释放——worker 重试若干次后在锁释放时直接建库成功**

把持锁者换成“持锁 2 秒后自动释放”（`run` 覆盖 `command` 追加秒数；注意
`--profile` 要放在子命令之前）。默认 8 次重试、退避 0.1→0.2→0.4→0.8→1.6→2.0…
（累计预算约 7 s）足以覆盖这 2 秒，因此**无需重启 API**：正在重试的 worker 在锁释放
后抢到锁即可完成启动。

```bash
docker compose down -v
docker compose --profile locktest run -d --name vt-lock-timed \
  init-lock-holder \
  python scripts/hold_init_lock.py /data/tiles.db.init.lock 2
docker compose up -d vector-tiles
sleep 8
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/healthz
docker compose logs vector-tiles | grep -E "init_db attempt" | wc -l
docker compose logs vector-tiles | grep -c "Application startup complete"
```

```text
200
# 锁释放前两个 worker 各会出现若干条 init_db attempt 重试警告（具体条数取决于调度，通常 6-10）
2
```

重试日志形如：

```text
WARNING:  init_db attempt 1/8 failed (init lock is held by another process: /data/tiles.db.init.lock); retrying in 0.10s
WARNING:  init_db attempt 2/8 failed (init lock is held by another process: /data/tiles.db.init.lock); retrying in 0.20s
WARNING:  init_db attempt 3/8 failed (init lock is held by another process: /data/tiles.db.init.lock); retrying in 0.40s
...
INFO:     Application startup complete.
```

> 复核结束后清理：`docker compose --profile locktest down -v`。

---

## 4. HTTP API 与 curl 示例

以下示例假设服务运行在 `http://localhost:8000`。

### 4.1 发布图层（创建数据集，revision 1）

使用 `multipart/form-data` 上传（字段 `file` 为 GeoJSON，`layer` 为 MVT 图层名）：

```bash
curl -s -X POST http://localhost:8000/datasets \
  -F "file=@data/sample.geojson;type=application/geo+json" \
  -F "layer=demo"
```

返回 `201 Created`：

```json
{
  "id": "32dca29ff4a24c869d9aa4b4686c26fa",
  "layer": "demo",
  "revision": 1,
  "content_hash": "96b7b15130feb9a517c348cf985e007217e29c456f4baa80b728ee7186408",
  "feature_count": 4,
  "published_at": "2026-09-29T14:21:52Z"
}
```

也可以直接发送原始 GeoJSON body（图层名用 query 参数，省略时为 `default`）：

```bash
curl -s -X POST "http://localhost:8000/datasets?layer=demo" \
  -H "Content-Type: application/geo+json" \
  --data-binary @data/sample.geojson
```

把数据集 ID 存到变量，后续示例复用：

```bash
DS=32dca29ff4a24c869d9aa4b4686c26fa
```

### 4.2 发布新版本（乐观并发控制）

新版本必须携带 `expected_revision`（当前 head 版本号）。成功后 revision 前进为 2：

```bash
curl -s -X POST "http://localhost:8000/datasets/$DS/revisions" \
  -F "file=@data/sample.geojson;type=application/geo+json" \
  -F "expected_revision=1"
```

如果 `expected_revision` 已过期（head 已被别人推进），返回 **409 Conflict**：

```bash
curl -i -X POST "http://localhost:8000/datasets/$DS/revisions" \
  -F "file=@data/sample.geojson;type=application/geo+json" \
  -F "expected_revision=1"
```

```http
HTTP/1.1 409 Conflict
```

```json
{
  "error": "revision conflict: expected_revision does not match current head",
  "dataset_id": "32dca29ff4a24c869d9aa4b4686c26fa",
  "expected_revision": 1,
  "current_revision": 2
}
```

客户端应重新拉取 head、合并变更后，再用新的 `expected_revision` 重试。

#### 并发发布（跨进程竞态复核）

乐观锁的比较与写入在单个 `BEGIN IMMEDIATE` 事务内完成（比较 head → 插入新
revision → 更新 head 原子提交，见 `app/db.py` 的 `publish_next_revision`）。
因此即使**两个独立工作进程**以相同的 `expected_revision` 同时提交，也只有一方
得到 `201`，其余方返回结构不变的 `409`（`current_revision` 指向新 head），不会
出现重复版本、半提交或未映射的数据库错误。

下面用 8 个并发请求同时把 `expected_revision=1` 追加到同一数据集（Compose 默认
就是 2 个 worker 进程共享同一库；`xargs -P8` 让请求真正同时发出）：

```bash
seq 8 | xargs -P8 -I{} sh -c '
  curl -s -o /tmp/race-{}.json -w "request %{http_code}\n" \
    -X POST "http://localhost:8000/datasets/$DS/revisions" \
    -F "file=@data/sample.geojson;type=application/geo+json" \
    -F "expected_revision=1"'
```

实测结果——**恰好 1 个 201，7 个 409**（哪一个请求成为 201 取决于调度，每次运行
可能不同）：

```text
request 201
request 409
request 409
request 409
request 409
request 409
request 409
request 409
```

获胜请求的 `201 Created` 响应体：

```json
{
    "id": "25c78460dd9745c5a36c37169b7e3458",
    "layer": "demo",
    "revision": 2,
    "content_hash": "96b7b15130feb9a517c348cf985e007217e29c456f4baa80b728ee7186408",
    "feature_count": 4,
    "published_at": "2026-09-30T06:58:41Z"
}
```

落败请求的 `409 Conflict` 响应（`-i` 同时显示状态行）：

```http
HTTP/1.1 409 Conflict
content-type: application/json
```

```json
{
  "error": "revision conflict: expected_revision does not match current head",
  "dataset_id": "25c78460dd9745c5a36c37169b7e3458",
  "expected_revision": 1,
  "current_revision": 2
}
```

确认库里没有产生额外版本（只有 revision 1、2，head 为 2）：

```bash
curl -s http://localhost:8000/datasets/$DS | python3 -m json.tool
```

```json
{
  "id": "25c78460dd9745c5a36c37169b7e3458",
  "layer": "demo",
  "latest_revision": 2,
  "revisions": [
    { "revision": 1, "...": "..." },
    { "revision": 2, "...": "..." }
  ]
}
```

历史 revision 不可变，仍可按旧版本读取瓦片（响应头 `x-revision: 1`，固定版本为
`immutable` 缓存）：

```bash
curl -s -D - -o /tmp/r1.mvt \
  "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt?revision=1"
```

```http
HTTP/1.1 200 OK
etag: "75dd0d37d05a70e4ed743cb72731f3bb8f3d0a513cfa58a1b473c3e265f4a481"
x-revision: 1
cache-control: public, max-age=31536000, immutable
```

### 4.3 查看数据集与版本列表

```bash
curl -s http://localhost:8000/datasets
curl -s http://localhost:8000/datasets/$DS | python3 -m json.tool
```

### 4.4 获取矢量瓦片

瓦片路径为标准 XYZ 编号（`y=0` 在最北），缩放级别 **0–14**：

```bash
# 0 级世界瓦片
curl -s -o tile-0-0-0.mvt \
  -D - \
  "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt"
```

响应头：

```http
HTTP/1.1 200 OK
content-type: application/vnd.mapbox-vector-tile
etag: "75dd0d37d05a70e4ed743cb72731f3bb8f3d0a513cfa58a1b473c3e265f4a481"
x-revision: 3
vary: If-None-Match
cache-control: public, no-cache
```

取一个高级别瓦片（例如北京附近的 z=14 瓦片）：

```bash
curl -s -o beijing-z14.mvt \
  "http://localhost:8000/datasets/$DS/tiles/14/13489/6208.mvt"
```

**固定版本取瓦片**（适合长缓存，响应为 `Cache-Control: …, immutable`）：

```bash
curl -s "http://localhost:8000/datasets/$DS/tiles/10/807/401.mvt?revision=1" -o r1.mvt
```

### 4.5 条件请求（If-None-Match → 304）

先用 `-c` 把 ETag 存进 cookie jar，再带上 `If-None-Match`：

```bash
curl -s -o tile.mvt -c cookies.txt \
  "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt"

ETAG=$(grep -i etag cookies.txt | awk '{print $NF}')

curl -s -o /dev/null -w "%{http_code}\n" \
  -H "If-None-Match: $ETAG" \
  "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt"
# 304
```

或者手工复制 ETag：

```bash
curl -i -H 'If-None-Match: "75dd0d37d05a70e4ed743cb72731f3bb8f3d0a513cfa58a1b473c3e265f4a481"' \
  "http://localhost:8000/datasets/$DS/tiles/0/0/0.mvt"
# HTTP/1.1 304 Not Modified（无响应体）
```

### 4.6 离线瓦片增量清单（tile-diff）

`GET /datasets/{id}/tile-diff` 读取 SQLite 中两个**不可变 revision** 的 GeoJSON，
经与瓦片接口**完全相同**的投影、8 像素缓冲裁剪、简化、量化与 MVT 编译链路，
按 **x、y 升序**返回在给定缩放级别上**实际编码字节发生变化**的瓦片，以及两端的
ETag。清单里的 ETag 与钉版瓦片接口（`?revision=N`）返回的 ETag 逐字节一致，
客户端可直接拿去做 `If-None-Match` / 增量下载。

查询参数（均必填）：

| 参数 | 说明 |
| --- | --- |
| `from_revision` | 旧修订号，正整数（`>= 1`），必须存在 |
| `to_revision` | 新修订号，正整数（`>= 1`），必须存在；允许反方向比较 |
| `z` | 缩放级别，`0–14` |

**候选范围如何保证不漏报**：要素只可能影响它投影后（含 8 px = 1/32 瓦片外扩）
覆盖到的瓦片。服务在该缩放级别的“分数瓦片坐标”里求每个要素的包围盒并集，
因此：

- **新增 / 删除 / 移动 / 属性变化**都会进入候选（属性变化不动几何，同样报出）；
- 与瓦片边界相距 ≤ 8 px 的要素会因**缓冲**进入邻瓦片，邻瓦片同样在候选中；
- 是否计入候选只取决于几何，**没有 ID 或 ID 重复的要素不会被漏报**；
- 最终一律以**编译后的 MVT 字节**裁决：源 GeoJSON 不同但瓦片字节相同（例如
  0.5 px 以内的抖动、被简化吃掉的顶点、同序重复点）**不报**；
- 候选只是局部瓦片集合（点要素在 z=14 至多触达本体 + 4 个缓冲邻瓦片），
  **z=14 的局部更新绝不会枚举全球 2²⁸ 个瓦片**。

准备两个局部修订（rev 1 只有北京点；rev 2 改属性并新增东京点）：

```bash
R1='{"type":"FeatureCollection","features":[{"type":"Feature","id":1,"properties":{"name":"Beijing","kind":"capital"},"geometry":{"type":"Point","coordinates":[116.4074,39.9042]}}]}'
R2='{"type":"FeatureCollection","features":[{"type":"Feature","id":1,"properties":{"name":"Peking","kind":"capital"},"geometry":{"type":"Point","coordinates":[116.4074,39.9042]}},{"type":"Feature","id":2,"properties":{"name":"Tokyo","kind":"capital"},"geometry":{"type":"Point","coordinates":[139.6917,35.6895]}}]}'
DS=$(curl -s -X POST "http://localhost:8000/datasets?layer=demo" \
  -H 'Content-Type: application/geo+json' -d "$R1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s -o /dev/null -X POST \
  "http://localhost:8000/datasets/$DS/revisions?expected_revision=1" \
  -H 'Content-Type: application/geo+json' -d "$R2"
```

请求清单（z=8，rev 1 → rev 2）：

```bash
curl -s "http://localhost:8000/datasets/$DS/tile-diff?from_revision=1&to_revision=2&z=8"
```

```json
{
  "dataset_id": "84806cfd2e6a46d5a4c2294f5a91bfa9",
  "layer": "demo",
  "z": 8,
  "from_revision": 1,
  "to_revision": 2,
  "latest_revision": 2,
  "changed_tiles": [
    {
      "z": 8,
      "x": 210,
      "y": 96,
      "from_etag": "\"ef987139be510868174f6d6ccbd9c81bf0583227f62a478a0d2d971b9df1c9a7\"",
      "to_etag": "\"0c223d4abbd047faf88799e05bc57943165a88bc311c03bed55071a6a4a182f8\""
    },
    {
      "z": 8,
      "x": 210,
      "y": 97,
      "from_etag": "\"2b411a550016e9525e3a7542e6b2b5e13ae93b69fac3c985ca0f3259174c89e7\"",
      "to_etag": "\"79be2070a9569c602e2b2c1293c96e0b86974395446d546f941b980f51d8b831\""
    },
    {
      "z": 8,
      "x": 227,
      "y": 100,
      "from_etag": "\"2a792afe41c934edd71c07b2b290f8a698c16656b5251f22be4e7a10d7e2fa06\"",
      "to_etag": "\"6ce539ded311fd2a5b36cac0134d93e6bfcc175d5919c16656b5251f22be4e7a10d7e2fa06\""
    }
  ]
}
```

> 结果按 `x` 升序、`y` 升序排列。北京点在 z=8 因 8 px 缓冲同时落入
> `(210,96)` 与 `(210,97)`（边界两侧），东京点新增触达 `(227,100)`，
> 三处均报出。

清单中的 ETag 与钉版瓦片完全一致（可直接比对）：

```bash
curl -s -D - -o /dev/null \
  "http://localhost:8000/datasets/$DS/tiles/8/210/96.mvt?revision=1" | grep -i etag
# etag: "ef987139be510868174f6d6ccbd9c81bf0583227f62a478a0d2d971b9df1c9a7"
```

相同修订（或两份修订编译出的瓦片完全相同）返回空清单，且不枚举任何瓦片：

```bash
curl -s "http://localhost:8000/datasets/$DS/tile-diff?from_revision=2&to_revision=2&z=8"
```

```json
{"dataset_id":"84806cfd2e6a46d5a4c2294f5a91bfa9","layer":"demo","z":8,
 "from_revision":2,"to_revision":2,"latest_revision":2,"changed_tiles":[]}
```

z=14 的局部属性更新只编译触达瓦片（实测毫秒级返回，下例仅 2 个瓦片）：

```bash
curl -s "http://localhost:8000/datasets/$DS/tile-diff?from_revision=1&to_revision=2&z=14"
```

```json
{
  "dataset_id": "84806cfd2e6a46d5a4c2294f5a91bfa9",
  "layer": "demo",
  "z": 14,
  "from_revision": 1,
  "to_revision": 2,
  "latest_revision": 2,
  "changed_tiles": [
    {
      "z": 14,
      "x": 13489,
      "y": 6208,
      "from_etag": "\"453240ce86ef07868e205146b20fefa7a0360bbf1d525316ff8af8d696bbe82a\"",
      "to_etag": "\"95c63b6730d48cb166dc1f92fcfc05d4731f2bd5594774366a37663ad305f69e\""
    },
    {
      "z": 14,
      "x": 14549,
      "y": 6451,
      "from_etag": "\"2a792afe41c934edd71c07b2b290f8a698c16656b5251f22be4e7a10d7e2fa06\"",
      "to_etag": "\"c45ae316e0fe24ae359fde4924034c2c034170fa9a399065cc94d4bda186fb9c\""
    }
  ]
}
```

> `(13489,6208)` 是北京点所在瓦片（rev1→rev2 仅属性变化，ETag 随之变化）；
> `(14549,6451)` 是东京点新增瓦片，其 `from_etag` 是该图层的“空瓦片”字节
> （不含任何要素的图层编码，故各空瓦片 ETag 相同）。注意点要素在 z=14 只
> 触达本体（本例恰好不跨 8 px 边界），全程只枚举了这 2 个瓦片。

错误情形：

```bash
# 数据集不存在 / 修订不存在 -> 404
curl -i "http://localhost:8000/datasets/nope/tile-diff?from_revision=1&to_revision=2&z=0"
# HTTP/1.1 404 Not Found  {"error":"dataset not found", ...}
curl -i "http://localhost:8000/datasets/$DS/tile-diff?from_revision=1&to_revision=9&z=0"
# HTTP/1.1 404 Not Found  {"error":"revision 9 does not exist", ...}

# 非法修订号 / 非法 z / 缺参数 -> 400
curl -i "http://localhost:8000/datasets/$DS/tile-diff?from_revision=x&to_revision=2&z=0"
# HTTP/1.1 400 Bad Request  {"error":"query parameter 'from_revision' must be an integer"}
curl -i "http://localhost:8000/datasets/$DS/tile-diff?from_revision=1&to_revision=2&z=15"
# HTTP/1.1 400 Bad Request  {"error":"zoom z must be between 0 and 14"}
curl -i "http://localhost:8000/datasets/$DS/tile-diff?from_revision=1&to_revision=2"
# HTTP/1.1 400 Bad Request  {"error":"missing required query parameter 'z'"}
```

### 4.7 错误示例

| 场景 | 状态码 |
| --- | --- |
| GeoJSON 不是 FeatureCollection / 空集合 / JSON 解析失败 / 坐标越界 / 自交多边形 / null 几何 | `400` |
| 瓦片 `z` 超出 0–14、`x/y` 为负或超出该级别范围、非整数 | `400` |
| tile-diff 缺少 `from_revision` / `to_revision` / `z`、非整数或修订号非正、`z` 超出 0–14 | `400` |
| 数据集或 `revision` 不存在（含 tile-diff 的任一端修订） | `404` |
| 发布版本时 `expected_revision` 与 head 不一致 | `409` |
| 上传体积超过限制（默认 50 MB） | `413` |

```bash
# 非法缩放级别
curl -i "http://localhost:8000/datasets/$DS/tiles/15/0/0.mvt"
# HTTP/1.1 400 Bad Request
# {"error":"zoom z must be between 0 and 14"}

# x 超出范围（z=2 时合法范围为 0..3）
curl -i "http://localhost:8000/datasets/$DS/tiles/2/9/0.mvt"
# {"error":"tile coordinates out of range for z=2: 0 <= x,y < 4"}

# 非法 GeoJSON
curl -i -X POST http://localhost:8000/datasets \
  -H "Content-Type: application/json" \
  -d '{"type":"FeatureCollection","features":[]}'
# HTTP/1.1 400 Bad Request
# {"error":"FeatureCollection contains no features"}
```

---

## 5. 瓦片规格（编译管线）

对每个瓦片坐标与每个源要素依次执行：

1. **投影**：经度/纬度（EPSG:4326）→ 球面 Web Mercator 米（EPSG:3857，
   纬度在 ±85.05112878° 处截断），再线性映射到当前瓦片的 **4096 × 4096**
   局部整数网格（原点左上、X 向右、Y 向下）。
2. **裁剪**：与外扩 **8 像素**（8/256 × 4096 = **128 单位**）的矩形求交；
   裁剪后维度降级的部分被丢弃（线不允许退化成点，面不允许退化成线）。
3. **简化**：`preserve_topology=True` 的 Douglas–Peucker 简化，容差
   **0.5 像素 = 8 单位**。
4. **量化与清洗**：四舍五入到整数网格并夹在缓冲窗口内；删除重合点、
   零长度线段与零面积环，保证几何有效。
5. **稳定排序编码**：图层名升序，要素按 ID 分类确定性排序
   （非负整数 ID → 字符串 ID → 无 ID 者按内容 SHA-256），属性键排序，
   再交给 `mapbox-vector-tile` 编码为 MVT v2（`extent=4096`）。

**确定性**：管线中不含任何随机量或无序字典遍历；瓦片首次编译后，字节体与
ETag 会持久化在 SQLite `tile_cache` 表中，因此重复请求乃至服务进程重启后，
同一 `(dataset, revision, z, x, y)` 的字节和 ETag 始终一致。

**增量清单复用同一管线**：`tile-diff` 不做任何几何或属性级的“语义比对”，而是
对候选瓦片两端各跑一次上述同一管线、以编码字节（即 SHA-256 ETag）是否相同
裁决，所以清单与钉版瓦片天然一致；候选瓦片则由“要素投影包围盒 ∩ 瓦片 8 px
缓冲窗”在分数瓦片坐标中求出（见 [§4.6](#46-离线瓦片增量清单tile-diff)）。

> 说明：MVT 要素 ID 按协议仅支持非负 uint64 整数；字符串 ID 的要素仍会被
> 确定性排序，但其 ID 不写入 protobuf（属性中若需要可自行保留）。

---

## 6. API 速览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/datasets` | 上传 GeoJSON，创建数据集并发布 revision 1 |
| `POST` | `/datasets/{id}/revisions` | 追加不可变 revision（需 `expected_revision`，冲突返回 409） |
| `GET` | `/datasets` | 列出全部数据集 |
| `GET` | `/datasets/{id}` | 数据集元数据与全部 revision |
| `GET/HEAD` | `/datasets/{id}/tiles/{z}/{x}/{y}.mvt` | 获取瓦片（支持 `?revision=N` 与 `If-None-Match`） |
| `GET` | `/datasets/{id}/tile-diff` | 两个 revision 间实际字节变化的瓦片清单（`from_revision`、`to_revision`、`z=0–14`，按 x、y 升序，ETag 与钉版瓦片一致） |
| `GET` | `/healthz` | 健康检查 |
| `GET` | `/docs` | Swagger UI 交互文档 |

---

## 7. 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VT_DB_PATH` | `/data/tiles.db` | SQLite 数据库路径 |
| `VT_HOST` / `VT_PORT` | `0.0.0.0` / `8000` | 监听地址与端口 |
| `VT_MIN_ZOOM` / `VT_MAX_ZOOM` | `0` / `14` | 允许编译的缩放级别范围 |
| `VT_MAX_UPLOAD_BYTES` | `52428800` | 单次上传大小上限（50 MB） |
| `VT_WORKERS` | `2`（Docker） | uvicorn 工作进程数；多进程共享同一 SQLite 文件，首次建库与追加发布分别由文件锁和跨进程事务串行化 |
| `VT_TILE_CACHE_SIZE` | `4096` | 进程内瓦片 LRU 缓存条目数 |
| `VT_SQLITE_BUSY_TIMEOUT_MS` | `30000` | SQLite 忙等待（busy handler）毫秒数；连接建立后、切换 WAL 之前生效 |
| `VT_INIT_MAX_ATTEMPTS` | `8` | lifespan 建库有界重试次数；SQLite 忙锁与**非阻塞 init 文件锁抢锁失败**共用此预算，耗尽即抛 `SchemaUnavailable` 结束启动 |
| `VT_INIT_RETRY_DELAY_S` | `0.1` | 建库重试的初始退避秒数（指数翻倍，上限 2 秒），同样作用于文件锁抢锁重试 |
