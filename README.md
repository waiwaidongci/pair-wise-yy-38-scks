# 水库防汛调度与操作确认

根据库位、入库流量、下游警戒和施工限制生成复核授权的泄洪指令。现以容量批次把闸门、调度指令、操作记录和审计接成一体：指令提交时绑定时段、闸门组合和下泄量，按剩余过流能力预占，容量不够的待排队并写明缺口。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、容量规则和关闭不变量。
- `src/repository.py`：SQLite建表、事务（BEGIN IMMEDIATE）、版本控制、容量预占、幂等键和审计链。
- `src/service.py`：权限检查、用例编排、并发控制、容量评估与重算、快照和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、失败和容量批次测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8315
```

默认端口为`8315`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 容量批次模型

- **闸门（gates）**：有额定过流能力（capacity）和可用状态（available/maintenance/closed）。状态为维护或关闭时有效过流能力为0。
- **水情观测（water_observations）**：记录库位、入库流量、下游警戒，作为容量评估的依据。
- **容量批次（capacity_batches）**：每次指令提交或重算生成一个批次，绑定水情依据。
- **容量预占（capacity_reservations）**：指令对某闸门在某时段的下泄量预占，记录请求量、预占量和缺口。
- **幂等键（idempotency_keys）**：凭原请求号恢复，重试不重复预占或追加记录。

## 容量规则

- 指令提交时绑定`period_start`、`period_end`、`gate_ids`、`discharge`。
- 按闸门组合中各闸门的剩余过流能力预占：`剩余 = 闸门能力 - 已预占下泄量`。
- 容量足够 → `reserved`（已占足）；不足 → `queued`（排队）并写明缺口`gap = 请求量 - 剩余量`。
- 总工只能授权已占足（`reserved`）的指令；排队中或待补核的指令不能授权。
- 历史指令（无容量依据）升级为`pending_review`（待补核），可通过补核接口补充容量依据。

## 失效重算与快照

- 水情观测变化或闸门可用状态变化后，**未执行指令**按新依据失效重算（释放旧预占、生成新批次）。
- **已执行指令**保留当时快照（水情依据、闸门状态），不参与重算。

## 并发争用

- 容量评估与预占在`BEGIN IMMEDIATE`事务中完成，立即获取写锁。
- 两名值班员同时争用同一闸门时，先到者占用容量，后到者看到剩余能力和冲突缺口。

## 写入失败恢复

- 写入请求携带`request_no`（原请求号）。
- 凭`request_no`恢复：重试返回首次结果，不重复预占、不重复追加操作记录。

## 主要接口

- `GET /health`
- `GET /api/items`、`POST /api/items`、`GET /api/items/{id}`
- `POST /api/items/{id}/records`、`GET /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/supplement`（补核历史指令）
- `GET /api/items/{id}/capacity`（容量评估详情）
- `GET /api/gates`、`POST /api/gates`、`GET /api/gates/{id}`、`PATCH /api/gates/{id}`
- `GET /api/gates/{id}/capacity?period_start=&period_end=`（闸门剩余过流能力）
- `GET /api/observations`、`POST /api/observations`、`GET /api/observations/{id}`
- `GET /api/audit`

允许角色：duty_officer, chief_engineer, dispatcher, viewer。库位超过汛限或入库流量上升时提升紧迫度；授权前必须有复核记录，执行后仍要闭环现场反馈。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
