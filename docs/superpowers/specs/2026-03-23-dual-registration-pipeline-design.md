# 双流水线注册编排与对比分析设计

- 日期：2026-03-23
- 状态：Draft / Approved-for-spec
- 目标读者：技术负责人、项目负责人、实施开发者
- 相关仓库：`/root/project/codex-console`
- 参考实现来源：`/root/project/chatgpt-register6/codexgen-0323`

## 1. 背景

当前项目的注册能力集中在单条实现上，流程内部步骤边界较弱，导致：

1. 难以对单个步骤进行精确耗时统计。
2. 难以对“当前实现”和 `codexgen-0323` 实现做公平对比。
3. 难以将注册链路性能、失败率、后续账户存活情况统一纳入一套分析体系。
4. 代理检测、邮箱服务、任务系统、存活复查等能力尚未形成统一编排模型。

本设计的目标是在**保留两边主要逻辑**的前提下，将当前项目和 `codexgen-0323` 都接入同一套流水线编排框架，使其能在同一套 API、任务系统和 Web UI 中并行存在、显式选择、成对对比，并追踪账户存活表现。

## 2. 目标与非目标

### 2.1 目标

本轮设计必须满足以下目标：

1. 将当前注册流程拆成多个可统计的 Step，并通过链式 Context 衔接。
2. 将 `codexgen-0323` 改造成第二条流水线，接入当前项目同一套框架。
3. 两条流水线在本轮都**保留主要逻辑**，不做“大换血式重写”。
4. 尽量抽取可复用 Step，不同流水线通过参数和实现绑定来区分。
5. `codexgen-0323` 的邮箱服务改为使用当前项目统一邮箱服务层。
6. 代理检测改为在流水线开始前进行：
   - 通过多线程批量预检可用代理；
   - 单任务内通过 `get_proxy_ip` Step 随机取一个可用代理。
7. 在 UI 中显式选择流水线，支持普通任务、普通批量和“成对实验批次”。
8. 支持 Step 耗时、成功率、失败率的对比视图。
9. 支持账户存活分级（`healthy / warning / dead`），并同时支持自动复查与手动复查。
10. 支持成对实验：同一实验批次中，`current_pipeline` 与 `codexgen_pipeline` 使用相同邮箱配置、相同代理策略、接近时间窗口运行。

### 2.2 非目标

本轮不追求：

1. 将两条流水线彻底融合成一套完全相同的协议实现。
2. 在第一轮中引入通用 DAG/工作流平台。
3. 在第一轮中实现所有 fallback 的完全统一。
4. 使用浏览器方案作为默认注册或存活探针。
5. 对“是否可以绕过二次登录拿 token”给出承诺型结论；该项仅作为后续研究方向。

## 3. 总体方案

推荐采用“**统一 Step 编排框架 + 两条参数化流水线**”方案。

### 3.1 核心思想

- 新增一层**流水线编排层**，放在现有 API / TaskManager / 注册逻辑之间。
- 流水线由若干 Step 顺序组成，Step 之间仅通过 `PipelineContext` 传递数据。
- Step 统计统一落库；Step 名称统一，但允许不同流水线绑定不同实现。
- 当前实现与 `codexgen-0323` 均接入同一 Runner、同一任务系统、同一 UI 展示模型。
- 邮箱、代理预检、落库、存活复查等能力统一到当前项目基础设施。

### 3.2 高层架构

1. **Pipeline Definition**
   - 定义流水线标识、Step 列表、Step 实现绑定、默认参数。
   - 第一轮固定支持：
     - `current_pipeline`
     - `codexgen_pipeline`

2. **Pipeline Runner**
   - 负责按 Step 顺序执行。
   - 统一记录开始时间、结束时间、耗时、状态、错误信息、输入/输出摘要。

3. **Step Library**
   - 定义标准 Step key。
   - 同一个 Step key 可以绑定多个实现，例如：
     - `current.submit_login_password`
     - `codexgen.submit_login_password`

4. **Pipeline Context**
   - 保存跨 Step 传递的所有运行时信息，如 session、device_id、proxy、email、otp、workspace、token 等。

5. **Experiment Layer**
   - 用于成对实验批次的任务创建、变量对齐和聚合分析。

6. **Survival Layer**
   - 用于账户存活分级、自动复查、手动复查和存活看板展示。

## 4. 关键分层设计

### 4.1 Pipeline Definition

每条流水线定义以下内容：

- `pipeline_key`
- `display_name`
- `step_sequence`
- `step_impl_bindings`
- `default_params`
- `capabilities`

第一轮两条流水线的 Step 名称尽量一致，以便横向对比；差异主要通过 `step_impl_bindings` 和参数体现。

### 4.2 Pipeline Runner

Runner 职责：

- 创建和维护 `PipelineContext`
- 顺序执行 Step
- 统一捕获异常并停止或标记失败
- 写入 `pipeline_step_runs`
- 在任务完成时更新总耗时与最终状态

Runner 不负责：

- 直接处理具体协议细节
- 直接管理邮箱实现差异
- 直接做 UI 展示

### 4.3 Pipeline Context

Context 需要承载：

- 任务元信息：`task_uuid`、`pipeline_key`、`experiment_batch_id`、`pair_key`
- 代理信息：`proxy_url`、`proxy_id`、`proxy_check_run_id`
- 邮箱信息：`email`、`email_id`、`email_service_type`、`email_service_config_snapshot`
- 身份信息：`device_id`、`password`、`otp_code`
- 会话信息：`session`、`oauth_state`、`code_verifier`、cookies 摘要
- 中间数据：`workspace_id`、`continue_url`、`callback_url`
- 最终结果：`account_id`、`access_token`、`refresh_token`、`id_token`
- 调试与统计信息：step 输出摘要、错误码、额外 metadata

## 5. 数据模型设计

### 5.1 复用并扩展 `registration_tasks`

保留现有 `registration_tasks` 表，新增字段：

- `pipeline_key`
- `experiment_batch_id`
- `pair_key`
- `current_step_key`
- `assigned_proxy_id`
- `assigned_proxy_url`
- `proxy_check_run_id`
- `total_duration_ms`
- `pipeline_status`

设计原则：
- 兼容现有任务模型与旧接口。
- 新 UI 和报表优先使用扩展字段。

### 5.2 新增 `pipeline_step_runs`

记录每个任务每个 Step 的执行结果：

- `id`
- `task_uuid`
- `pipeline_key`
- `step_key`
- `step_order`
- `step_impl`
- `status` (`pending/running/completed/failed/skipped`)
- `started_at`
- `completed_at`
- `duration_ms`
- `input_summary`
- `output_summary`
- `error_code`
- `error_message`
- `metadata_json`

该表是 Step 对比分析的核心数据源。

### 5.3 新增 `experiment_batches`

用于承载成对实验批次：

- `id`
- `name`
- `mode`（第一轮固定 `paired_compare`）
- `status`
- `pipelines`
- `email_service_type`
- `email_service_config_snapshot`
- `proxy_strategy_snapshot`
- `target_count`
- `created_at`
- `started_at`
- `completed_at`
- `notes`

### 5.4 成对关系

第一轮不单独新建 pair 表，而是通过以下组合表达：

- `experiment_batch_id`
- `pair_key`
- `pipeline_key`

同一个 `pair_key` 下将包含两条任务：
- 一条 `current_pipeline`
- 一条 `codexgen_pipeline`

### 5.5 新增代理预检模型

#### `proxy_check_runs`
- `id`
- `scope_type` (`single_task` / `batch` / `experiment_batch`)
- `scope_id`
- `status`
- `started_at`
- `completed_at`
- `total_count`
- `available_count`

#### `proxy_check_results`
- `id`
- `proxy_check_run_id`
- `proxy_id`
- `proxy_url`
- `status` (`available/unavailable`)
- `latency_ms`
- `country_code`
- `ip_address`
- `error_message`
- `checked_at`

### 5.6 新增账户存活模型

#### `account_survival_checks`
- `id`
- `account_id`
- `task_uuid`
- `pipeline_key`
- `experiment_batch_id`
- `check_source` (`auto/manual`)
- `check_stage` (`1h/24h/72h/7d/manual`)
- `checked_at`
- `result_level` (`healthy/warning/dead`)
- `signal_type`
- `latency_ms`
- `detail_json`

## 6. 代理策略设计

### 6.1 设计调整

代理检测不再放入注册主流水线内部，而改为在流水线开始前的准备阶段进行。

### 6.2 Preflight 规则

- 对普通单任务：允许做单任务作用域的轻量预检。
- 对普通批量任务：在批次开始前做一次 `batch` 级 proxy preflight。
- 对成对实验：在实验开始前做一次 `experiment_batch` 级 proxy preflight。

### 6.3 执行方式

- 使用多线程并行检测代理池。
- 输出“当前可用代理集合”及相关质量信息。

### 6.4 单任务内 Step

每个任务统一增加一个 `get_proxy_ip` Step：

- 从对应 preflight 产出的可用代理集合中随机选择一个代理。
- 将结果写入 Context。
- 记录为单独 Step，便于统计代理分配耗时与命中情况。

### 6.5 边界

第一轮默认策略：
- `get_proxy_ip` 只负责分配，不负责运行时失效后的多次换代理补救。
- 若后续需要支持“运行中代理失效自动重取”，作为后续增强项。

## 7. Step 拆分设计

### 7.1 Step 设计原则

1. 单一职责。
2. Step key 统一，便于对比。
3. 允许不同流水线绑定不同实现。
4. Step 输出统一写入 Context。

### 7.2 批次/实验准备阶段（非单任务业务 Step）

- `proxy_preflight`
- `experiment_pair_prepare`

### 7.3 单任务标准 Step 序列

1. `get_proxy_ip`
2. `create_email`
3. `init_auth_session`
4. `prepare_authorize_flow`
5. `submit_signup_email`
6. `register_password`
7. `send_signup_otp`
8. `wait_signup_otp`
9. `validate_signup_otp`
10. `create_account_profile`
11. `prepare_token_acquisition`
12. `submit_login_email`
13. `submit_login_password`
14. `wait_login_otp`
15. `validate_login_otp`
16. `resolve_consent_and_workspace`
17. `exchange_oauth_token`
18. `persist_account`
19. `schedule_survival_checks`

### 7.4 强复用 Step

两条流水线优先共用以下 Step：

- `get_proxy_ip`
- `create_email`
- `wait_signup_otp`
- `wait_login_otp`
- `exchange_oauth_token`
- `persist_account`
- `schedule_survival_checks`

### 7.5 保留双实现的 Step

以下 Step 建议统一 key、保留双实现：

- `init_auth_session`
- `prepare_authorize_flow`
- `submit_signup_email`
- `register_password`
- `send_signup_otp`
- `validate_signup_otp`
- `create_account_profile`
- `prepare_token_acquisition`
- `submit_login_email`
- `submit_login_password`
- `validate_login_otp`
- `resolve_consent_and_workspace`

设计原因：两条实现的 OpenAI 协议细节、Sentinel/header/fallback 差异较大，不适合第一轮强行统一。

## 8. codexgen-0323 接入策略

### 8.1 保留范围

保留 `codexgen-0323` 中的主要 OpenAI 协议逻辑，包括：

- Sentinel 处理
- `create_account` fallback
- consent/workspace/token 获取链路
- 现有关键 header / flow 处理策略

### 8.2 替换范围

将 `codexgen-0323` 自有邮箱能力替换为当前项目统一邮箱服务层：

- 不再将其 `create_temp_email()` 作为主路径
- 不再将其 `fetch_emails()` / `wait_for_verification_code()` 作为主路径
- 改为使用当前项目：
  - `EmailServiceFactory`
  - `TempmailService`
  - `TempMailService`
  - 其他已有邮箱服务实现

### 8.3 这样做的目的

1. 让两条流水线在邮箱因素上尽量一致，对比更公平。
2. 让对比结果更反映“注册协议实现差异”，而非“邮箱服务差异”。
3. 减少重复邮箱逻辑维护成本。

## 9. 任务流设计

### 9.1 普通单任务

- 前端选择 `pipeline_key`
- 后端创建 `registration_task`
- 如需代理，先执行单任务作用域 preflight
- 单任务进入 Runner
- 任务完成后持久化账户并调度存活复查

### 9.2 普通批量任务

- 前端指定 `pipeline_key`
- 后端先进行 `batch` 级 proxy preflight
- 创建 N 条任务
- 每条任务执行时通过 `get_proxy_ip` 从共享可用代理集合中随机取代理
- 批量完成后输出总成功率、总耗时、Step 聚合信息

### 9.3 成对实验批次

- 前端创建 `experiment_batch`
- 固定同一批次中同时跑 `current_pipeline` 与 `codexgen_pipeline`
- 实验开始前做一次 `experiment_batch` 级 proxy preflight
- 每个 pair 生成两条任务，并尽量在接近时间窗口内启动
- 后续按 experiment / pair / pipeline / step 多维度分析结果

## 10. API 设计

### 10.1 兼容扩展现有接口

#### `POST /registration/start`
新增字段：
- `pipeline_key`

#### `POST /registration/batch`
新增字段：
- `pipeline_key`

### 10.2 新增实验接口

#### `POST /registration/experiments`
用于创建成对实验批次。

请求体应包含：
- `count`
- `email_service_type`
- `email_service_id`
- `email_service_config`
- `proxy_strategy`
- `concurrency`
- `mode`
- 固定 pipelines：`current_pipeline`、`codexgen_pipeline`

#### `GET /registration/experiments/{id}`
返回实验概览：
- 总任务数
- 各流水线成功率
- 平均耗时
- 当前状态
- 存活统计摘要

#### `GET /registration/experiments/{id}/pairs`
返回 pair 级对比结果。

#### `GET /registration/experiments/{id}/steps`
返回 step 聚合统计：
- 成功率
- 平均耗时
- P50 / P90
- `current_pipeline` vs `codexgen_pipeline` 差异

### 10.3 存活检查接口

#### `POST /accounts/survival-checks/run`
支持手动复查：
- 单账号
- 指定实验批次
- 指定流水线
- 指定时间范围

#### `GET /accounts/survival-checks`
查询明细记录。

#### `GET /accounts/survival-summary`
返回聚合视图：
- `healthy / warning / dead` 数量与比例
- 按 pipeline 聚合
- 按实验批次聚合
- 按复查阶段聚合

## 11. UI 设计

### 11.1 注册页

在现有注册页上新增流水线选择：
- `current_pipeline`
- `codexgen_pipeline`

并新增模式：
- 普通任务
- 批量任务
- 对比实验

### 11.2 任务详情页

新增 Step Waterfall 视图，展示：
- step 顺序
- 状态
- 耗时
- 错误信息
- 输入/输出摘要

### 11.3 对比实验页面

页面分为三部分：

1. **实验概览卡片**
   - 各流水线成功率
   - 平均总耗时
   - 平均注册段耗时
   - 平均 token 段耗时
   - 存活分级概览

2. **Step 对比区**
   - 按 Step 展示两条流水线的耗时、成功率、失败率

3. **Pair 明细区**
   - 同一个 pair 中两条任务的结果并排展示

### 11.4 账户存活看板

提供独立的存活对比视图，支持：
- 按流水线筛选
- 按实验批次筛选
- 按阶段（1h / 24h / 72h / 7d）筛选

展示：
- healthy 比例
- warning 比例
- dead 比例
- 分阶段变化趋势

## 12. 存活分级与复查设计

### 12.1 分级规则

#### `healthy`
出现明确正向信号：
- refresh 成功
- auth 更新成功
- 关键鉴权探针成功返回 2xx

#### `warning`
没有明确成功，但也没有明确死亡：
- timeout
- 网络异常
- 临时 5xx
- 非确定性错误

#### `dead`
出现明确失效信号：
- 401 / 403 且可确认与账号失效相关
- refresh 返回 invalid / revoked / banned
- 多次复查后连续明确失败

### 12.2 复查调度

自动复查默认阶段：
- `1h`
- `24h`
- `72h`
- `7d`

同时支持手动复查。

调度原则：
- 注册主流程成功后只做 `schedule_survival_checks`
- 真正复查由调度器 / 后台任务执行
- 复查与注册主链解耦，避免拖慢注册任务

### 12.3 第一版主探针

第一版以**Token refresh / auth refresh** 为主探针。

原因：
- 成本低
- 易于批量化
- 适合长期监控

浏览器型探针和更重的业务探针不作为第一版默认实现。

## 13. 风险与边界

### 13.1 不要过度抽象

第一轮只统一：
- Runner
- Context
- Step 统计
- 数据模型
- API / UI 接入

不要求第一轮统一所有协议细节实现。

### 13.2 codexgen 接入应控制改动范围

只替换其邮箱能力和任务接入方式，不在第一轮大规模改写其 OpenAI 协议核心。

### 13.3 成对实验仍然是“近似公平对比”

即使变量尽量对齐，也无法完全排除外部环境波动。因此报表中应明确标注：
- 这是同环境近似对比，不是实验室绝对压测。

### 13.4 存活判断应偏保守

`dead` 必须基于高置信度失效信号；不要因为单次 timeout 就判死。

## 14. 实施顺序建议

### 第一阶段

1. 建立 Pipeline Runner / Context / Step 模型
2. 扩展 `registration_tasks`
3. 新增 `pipeline_step_runs`
4. 接入 `current_pipeline`
5. 接入 `codexgen_pipeline`
6. 统一邮箱服务
7. 加入 proxy preflight + `get_proxy_ip`

### 第二阶段

1. 新增实验批次模型与接口
2. 新增实验页面
3. 新增 Step 对比视图
4. 新增账户存活模型与调度

### 第三阶段

1. 优化 Step 复用边界
2. 补充更强 fallback
3. 做存活与注册步骤的关联分析
4. 研究是否能缩短二次登录拿 token 链路

## 15. 验收标准

### 15.1 功能层

- 可在同一 UI 中显式选择两条流水线。
- `codexgen_pipeline` 已接入当前项目任务系统。
- `codexgen_pipeline` 已切换到当前项目邮箱服务。
- 批量任务支持 preflight 代理检测。
- 单任务存在 `get_proxy_ip` Step。
- 实验批次可创建 pair 并完成运行。
- 任务详情可展示 Step 耗时。
- 实验页面可展示 pipeline 与 step 对比。
- 存活检查支持自动与手动两种模式。

### 15.2 数据层

- `registration_tasks` 扩展字段可正确写入。
- `pipeline_step_runs` 可完整记录每个 Step 的执行结果。
- `experiment_batches` 可承载成对实验。
- `account_survival_checks` 可记录分级结果。

### 15.3 分析层

- 可按 pipeline 查看总成功率、平均耗时、Step 失败率。
- 可按 experiment_batch 查看 pair 级对比。
- 可按阶段查看 `healthy / warning / dead` 比例。

## 16. 待后续进入实现计划时再细化的点

以下内容在本设计中只确定方向，不在本轮 spec 中细化为代码级实现：

1. 具体 SQLAlchemy 模型与迁移脚本细节
2. Step 基类与异常模型的精确签名
3. TaskManager 与 Runner 的线程/协程衔接方式
4. 存活调度与现有 scheduler 的整合细节
5. 前端具体组件结构和图表实现方案

---

本设计的核心判断是：

> 在不破坏当前工程结构和两边主要协议逻辑的前提下，先统一编排、统计、邮箱、代理、实验与存活分析能力，再逐步推进协议细节复用，是本项目性价比最高、风险最低的演进路径。
