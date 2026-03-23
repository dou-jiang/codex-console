# Scheduled Run Center Design

**Date:** 2026-03-23

## Goal

增强现有定时任务页面，把“运行记录”从计划附属弹窗提升为同页独立的任务记录中心，并补齐以下能力：

1. 独立任务记录列表，支持按任务类型、状态、时间范围筛选。
2. 每条运行记录支持查看详细日志。
3. 运行中的定时任务支持实时查看日志。
4. 当前运行中的定时任务支持手动停止。
5. 清理任务与补量任务增加阶段性进度日志，避免运行中只有开始/结束两条日志。

本次设计遵循用户已确认的交互约束：

- 任务记录列表放在现有 `/scheduled-tasks` 页面中，与计划列表同页展示。
- 实时日志使用前端轮询方案，不引入定时任务专用 WebSocket。
- 手动停止采用**软停止**：收到停止请求后，任务在当前安全处理阶段收尾后退出。

## Scope

### In scope

- 扩展 `scheduled_runs` 数据模型，支持停止请求与日志增量元信息。
- 新增独立运行记录查询、详情、日志增量、停止接口。
- 前端新增同页任务记录列表、筛选栏、日志详情弹窗、运行中实时轮询。
- 调度引擎与 runner 增加协作式软停止能力。
- `cpa_cleanup` / `cpa_refill` 增加阶段性进度日志。
- `account_refresh` 复用停止能力，但本次不额外强化细粒度阶段日志。

### Out of scope

- 不引入定时任务专用 WebSocket/SSE 推送。
- 不新增独立 `/scheduled-runs` 页面。
- 不做日志下载、全文检索、复杂审计权限。
- 不做跨实例分布式停止协调。
- 不做线程强杀或硬中断执行流。

## Current Problems

### 1. 运行记录不是独立中心

当前运行记录只能从计划列表中的“记录”按钮打开，而且只查看某一计划的最近若干 runs。用户无法从全局视角按时间范围和任务类型查看历史运行实例。

### 2. 日志查看是一次性读取，不是实时

当前定时任务日志接口仅支持一次性读取整段 `scheduled_runs.logs`，前端打开日志弹窗时只请求一次。运行中日志不会自动刷新，也不能观察任务是否卡住。

### 3. 没有停止入口

当前调度引擎可以防并发和立即执行，但运行中的定时任务没有“停止请求”机制。对于长时间清理/补量任务，用户只能等待自然结束。

### 4. 过程日志过粗

`cpa_cleanup` 与 `cpa_refill` 现在只有少量开始/结束日志，难以判断当前处理进度，也不利于运行中监控和问题定位。

## Proposed Approach

采用**同页增强型运行中心**方案：

1. 保留现有计划列表职责（创建、编辑、启停、立即执行）。
2. 在同页新增“任务记录列表”区域，作为运行实例维度的主视图。
3. 为运行记录新增独立 API：全局列表、详情、增量日志、停止请求。
4. 前端日志详情弹窗在 `status=running` 时每 2 秒轮询一次增量日志；结束后自动停止轮询。
5. 停止能力基于 `scheduled_runs` 的软停止标记实现，由 runner 在安全检查点协作退出并最终把状态落为 `cancelled`。

该方案的核心优点：

- 最大化复用当前定时任务页面与持久化日志模型。
- 改动面明显小于 WebSocket 推送方案。
- 能满足“同页查看、实时观察、手动停止、历史检索”的目标。

## Data Model Design

### 1. Extend `scheduled_runs`

建议在 `scheduled_runs` 表新增以下字段：

- `task_type`：运行时任务类型快照，冗余保存 `cpa_cleanup` / `cpa_refill` / `account_refresh`。
- `stop_requested_at`：用户发起软停止请求的时间。
- `stop_requested_by`：停止来源，首版固定使用 `manual`。
- `stop_reason`：停止原因，首版使用 `user_requested`。
- `last_log_at`：最后一次追加日志的时间，用于列表展示和运行中判断。
- `log_version`：每次追加日志时自增，用于日志增量拉取与快速判断是否有新日志。

### 2. Run status semantics

持久化状态扩展为：

- `running`
- `success`
- `failed`
- `skipped`
- `cancelled`

UI 侧增加一个派生状态：

- 如果 `status == running` 且 `stop_requested_at != null`，前端显示为“停止中”。

这样可以区分：

- **最终状态**：数据库里真实结论。
- **过渡状态**：UI 上用于表达“已经请求停止，等待收尾退出”。

### 3. Why stop flags live on runs, not plans

停止的是“当前执行实例”，而不是整个计划配置。因此停止标记必须挂在 `scheduled_runs` 上，而不是 `scheduled_plans` 上。

计划的启用/禁用语义仍然保留在 `scheduled_plans.enabled`，与“本次 run 是否停止”解耦。

## Backend API Design

### 1. Global run list

`GET /api/scheduled-runs`

支持筛选参数：

- `task_type`
- `status`
- `plan_id`
- `started_from`
- `started_to`
- `page`
- `page_size`

返回每项字段：

- `id`
- `plan_id`
- `plan_name`
- `task_type`
- `trigger_source`
- `status`
- `started_at`
- `finished_at`
- `duration_seconds`
- `stop_requested_at`
- `last_log_at`
- `summary`
- `error_message`
- `can_stop`

### 2. Run detail

`GET /api/scheduled-runs/{run_id}`

返回：

- 运行基础信息
- 关联计划简要信息（`plan_name`, `task_type`, `plan_enabled`）
- `summary`
- `error_message`
- `stop_requested_at`
- `stop_reason`
- `log_version`
- `last_log_at`
- `is_running`
- `can_stop`

### 3. Incremental logs

`GET /api/scheduled-runs/{run_id}/logs?offset=0`

返回：

- `run_id`
- `status`
- `offset`
- `next_offset`
- `chunk`
- `has_more`
- `is_running`
- `stop_requested_at`
- `log_version`
- `last_log_at`

设计要点：

- 首次打开传 `offset=0`，后续按 `next_offset` 增量拉取。
- 不再要求前端每次全量获取整段日志。
- 运行结束后 `is_running=false`，前端停止轮询。

### 4. Stop request

`POST /api/scheduled-runs/{run_id}/stop`

行为：

- 仅允许 `status=running` 的 run 调用。
- 如果已停止/已结束，返回 409 或幂等成功响应。
- 成功时写入：
  - `stop_requested_at = now`
  - `stop_requested_by = manual`
  - `stop_reason = user_requested`
- 返回：
  - `success`
  - `run_id`
  - `status = stopping`（接口返回语义）

### 5. Compatibility

现有接口继续保留：

- `GET /api/scheduled-plans/{plan_id}/runs`
- `GET /api/scheduled-plans/runs/{run_id}/logs`

现有“计划级记录弹窗”可逐步迁移为复用新的全局运行记录中心数据源。

## Scheduler / Runner Design

### 1. Scheduler engine responsibilities

调度引擎需要增加以下能力：

- 在创建 `ScheduledRun` 时同步写入 `task_type` 初始值。
- 暴露运行状态查询与停止请求入口给路由层调用。
- 当 run 最终因用户停止而结束时，确保：
  - `status = cancelled`
  - `error_message = user requested stop`
  - `last_run_status` 同步到计划表

### 2. Soft stop model

新增统一停止检查函数，例如：

- `is_run_stop_requested(run_id)`
- `raise_if_run_stop_requested(run_id, stage=...)`

行为：

1. 检查 `scheduled_runs.stop_requested_at` 是否已设置。
2. 若已设置，则追加一条日志说明当前阶段将收尾退出。
3. 抛出专用异常（如 `ScheduledRunCancelledError`）。
4. runner 捕获后写入最终取消状态并结束。

### 3. Cleanup runner progress logs

`cpa_cleanup` 增加两类阶段性日志：

#### Probe stage

- 利用现有 `progress_callback`，保留开始/完成日志。
- 当探测账户数较多时，每处理 **100 个候选账户** 输出一条进度日志。
- 若总量不足 100，也至少输出第一条和最终条。

示例：

- `cleanup probe progress (scanned=100, invalid=12)`
- `cleanup probe progress (scanned=200, invalid=19)`

#### Expire/delete stage

- 标记本地失效时，每处理 **100 个账户** 输出一条日志。
- 删除远端失效账号时，每处理 **100 个删除动作** 输出一条日志。

示例：

- `cleanup expire progress (processed=100, local_expired=88)`
- `cleanup remote delete progress (processed=100, deleted=95, failed=5)`

### 4. Refill runner progress logs

`cpa_refill` 增加以下阶段性日志：

#### Target resolution

始终输出：

- `refill target resolved (current=120, target=200, refill_target=80)`

#### Register/upload progress

满足任一条件时输出一次：

- `registered_success + registered_failed` 达到 20 的倍数。
- `uploaded_success` 达到 20 的倍数。
- 连续失败数达到阈值预警点。

示例：

- `refill progress (attempted=20, registered_success=14, registered_failed=6, uploaded_success=12, uploaded_failed=2)`

#### Auto-disable preamble

当连续失败达到阈值前，先输出明确日志，再自动禁用计划：

- `refill consecutive failures reached threshold (current=10, threshold=10)`
- `refill plan auto-disabled: consecutive_failures_reached`

### 5. Stop checkpoints

#### Cleanup runner

建议检查点：

- probe 进度回调后
- 每条本地失效标记前
- 每次远端 delete 前或每批 delete 后

#### Refill runner

建议检查点：

- 每轮 `while` 开始前
- 每次注册结果返回后
- 每次上传 CPA 结束后

#### Refresh runner

建议检查点：

- 每轮待处理账号开始前
- token refresh 返回后
- subscription 检查后
- CPA 上传结束后

首版 refresh 任务只要求支持停止，不要求额外细粒度阶段日志。

## Frontend Design

### 1. Same-page layout

在 `/scheduled-tasks` 页面保留原有“计划列表”卡片，并在其下新增一个独立卡片：

- 标题：`任务记录`

两个区域职责分离：

1. **计划列表**：计划级管理
2. **任务记录列表**：运行实例级检索与排障

### 2. Run list filter bar

筛选栏建议包含：

- 任务类型下拉
- 状态下拉
- 开始时间从
- 开始时间到
- 查询按钮
- 重置按钮

“计划列表”的“记录”按钮可以联动设置 `plan_id` 过滤并滚动到下方运行记录区。

### 3. Run list columns

建议列：

- Run ID
- 计划名称
- 任务类型
- 触发源
- 状态
- 开始时间
- 结束时间
- 耗时
- 最后日志时间
- 操作

操作包含：

- 查看详情
- 查看日志
- 停止任务（仅运行中可用）

### 4. Log detail modal

继续使用弹窗而不是新侧栏，以复用现有结构并降低改造成本。

弹窗内容分两块：

#### Basic info

- Run ID
- 计划名称
- 任务类型
- 状态
- 触发源
- 开始/结束时间
- 摘要
- 错误信息

#### Log panel

- 当前状态与实时刷新提示
- 手动刷新按钮
- 停止任务按钮（若可停止）
- 自动滚动开关
- `<pre>` 日志区

### 5. Live log polling behavior

- 打开日志弹窗时先请求详情和首段日志。
- 若 `status=running`：前端每 2 秒请求一次增量日志。
- 若状态转为 `success / failed / cancelled / skipped`：自动停止轮询。
- 若已请求停止但尚未退出：UI 显示“停止中”。

## Error Handling

### 1. Stop request conflicts

- 停止已完成 run：返回 409 或幂等响应，前端提示“任务已结束”。
- 重复停止：返回成功但保持“停止中”，避免误判为错误。

### 2. Incremental log polling

- 单次轮询失败时只弹轻量 warning 或静默重试，不立即关闭弹窗。
- 连续失败达到阈值时停止自动轮询，并保留“手动刷新”入口。

### 3. Runner cancellation

- 用户停止不记为 `failed`，统一记为 `cancelled`。
- 日志中必须明确写出：
  - 收到停止请求
  - 任务已按请求停止

## Testing Strategy

### 1. Backend tests

新增/补强以下测试：

- `scheduled_runs` 新字段迁移与默认值。
- 全局运行记录列表筛选（任务类型、状态、时间范围）。
- 运行详情接口返回 `can_stop / is_running / log_version`。
- 增量日志接口按 `offset` 正确返回 `chunk / next_offset`。
- stop 接口只允许停止 `running` run。
- 停止请求后 runner 能在安全点退出并最终落库为 `cancelled`。

### 2. Runner tests

- cleanup runner 每 100 个处理输出阶段日志。
- refill runner 每 20 个尝试输出阶段日志。
- stop flag 生效时 cleanup/refill/refresh 都会提前退出。
- 被取消的 run 最终状态、summary、error_message 正确。

### 3. Frontend / asset tests

- 页面模板包含独立“任务记录”区域和筛选控件。
- 列表脚本存在 run list load/filter/render hooks。
- 日志弹窗存在实时轮询、停止按钮、自动停止轮询逻辑。
- 行内按钮包含稳定 `data-action` 标记。

### 4. Real UI verification

回归验证至少覆盖：

1. 创建计划并立即执行。
2. 在任务记录列表中筛选出对应 run。
3. 打开运行中日志并观察实时刷新。
4. 点击停止任务，看到状态变为“停止中”并最终变为“已停止”。
5. cleanup/refill 阶段性日志在页面中可见。

## Acceptance Criteria

1. `/scheduled-tasks` 页面中有独立的任务记录列表区域。
2. 运行记录支持按任务类型、状态、时间范围筛选。
3. 每条记录都可以查看详细日志。
4. 对于运行中的记录，日志详情弹窗可自动实时刷新。
5. 对于运行中的记录，用户可以手动发起软停止。
6. 停止请求后任务不会被硬中断，而是在安全点收尾后以 `cancelled` 结束。
7. `cpa_cleanup` 与 `cpa_refill` 运行过程中会输出阶段性进度日志。
8. 现有计划管理能力（创建、编辑、启停、立即执行）不回归。
