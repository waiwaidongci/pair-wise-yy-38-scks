# 汛期闸门容量批次调度

多条泄洪指令争用同一组闸门时，按**时段 × 闸门组合 × 下泄量**组成容量批次：提交即按剩余过流能力预占，占不足进入待排队并写明缺口；水情或闸门状态变化后，未执行指令整体失效并按 FIFO 重算，已执行指令永久保留当时容量快照。

## 容量批次规则

- 指令提交必须绑定：时段（`window.start_at/end_at`）、闸门组合（`gate_codes`，可用 `allocations` 指定每闸下泄量，否则均分）、总下泄量（`discharge`）。
- 有效过流能力 = min(闸门设计能力, 最新水情观测上限)；闸门 `unavailable` 时为 0。
- 同一时段按指令提交顺序先到先占：
  - `reserved`：每个闸门都有足够余量，写入逐闸预占（占用前/后余量）。
  - `queued`：任一闸门不足，返回逐闸缺口 `gap`、当前余量、先到冲突指令与队列位置，不占容量。
- 水情观测或闸门可用状态变化 → 时段 `basis_version+1`，全部未执行指令失效后按 FIFO 重算；`executed` 指令不参与重算，操作记录中的快照原样保留。
- 总工（`chief_engineer`）只能授权 `reserved` 指令；授权前若依据已变更会自动重算。历史指令没有容量依据（`basis_version` 为空）时授权被拦截并升级为 `recheck_pending`（待补核）。
- 执行（`dispatcher`）只允许从 `authorized` 转换，写入一条操作记录及当时的时段、依据版本、逐闸能力快照。
- **请求号幂等**：提交用 `request_id`、执行用操作 `request_id`；写入失败后凭同一请求号重试，返回首次结果（`replayed: true`），不会重复预占、重复排队或追加操作记录。

## 模块结构

- `app.py`：参数解析、依赖组装与 HTTP 服务启动。
- `src/domain.py`：数据结构、错误、状态与基础校验。
- `src/rules.py`：有效能力、均分/显式分配、批次评估、FIFO 规划、状态转换与角色矩阵（纯函数）。
- `src/repository.py`：SQLite 建表、`BEGIN IMMEDIATE` 串行写事务、幂等表、操作记录与 SHA-256 审计链。
- `src/service.py`：闸门/水情、容量视图、提交预占、授权、执行快照、依据变化重算。
- `src/http_api.py`：JSON 路由与统一错误响应。
- `src/audit.py`：UTC 时间与 SHA-256 审计事件。
- `tests/`：规则、完整批次流程、失败/并发/幂等测试。

指令状态：`queued`（待排队）、`reserved`（已占足容量）、`authorized`（总工已授权）、`executed`（已执行，终态快照）、`recheck_pending`（历史指令待补核）。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8315
```

首次启动自动建库。使用 `X-Actor`、`X-Role` 请求头传递身份。

## 接口

- `GET /health`
- 闸门：`POST /api/gates`（值班员登记）、`GET /api/gates`
  - `POST /api/gates/observations`：提交某闸水情观测上限（值班员/调度员），触发相关时段重算
  - `POST /api/gates/status`：变更闸门 available/unavailable（值班员），触发相关时段重算
- 容量：`POST /api/capacity`，body 为 `{start_at,end_at}`，返回每闸设计/观测/有效能力、已占、余量、排队需求
- 指令：
  - `POST /api/orders`：值班员提交批次（必带 `request_id`），`reserved`/`queued` 均返回 201 及评估明细
  - `GET /api/orders`、`GET /api/orders/{id}`
  - `POST /api/orders/{id}/authorize`：总工授权（可带 `expected_version` 乐观锁）
  - `POST /api/orders/{id}/execute`：调度员执行，body 带操作 `request_id`
  - `GET /api/orders/{id}/records`：操作记录与执行快照
- `GET /api/audit`（总工/viewer）

错误码：422 校验错误、403 越权、404 不存在、409 冲突（状态不符、排队指令授权、版本过期、请求号冲突）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
