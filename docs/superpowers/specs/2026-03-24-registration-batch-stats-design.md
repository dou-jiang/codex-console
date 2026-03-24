# 普通批量注册统计与 A/B 对比报表设计

- 日期：2026-03-24
- 状态：Draft / Approved-for-spec
- 目标读者：技术负责人、实施开发者、项目负责人
- 相关仓库：`/root/project/codex-console`

## 1. 背景

当前项目已经具备：

1. 普通批量注册能力（`/registration/batch`）。
2. 双流水线 Step 级执行记录（`pipeline_step_runs`）。
3. 实验批次对比页面（`/registration-experiments`）。

但普通批量注册结束后，系统目前仍然缺少以下能力：

1. 没有将批量执行结果固化为可复盘的统计快照。
2. 没有批量统计记录列表，无法快速查看历史批次整体表现。
3. 没有支持普通批量记录之间的 A/B 对比。
4. 没有面向业务/管理视角的阶段耗时对比报表。

本设计目标是在**不改变普通批量主执行逻辑**的前提下，为普通批量注册补齐统计快照、列表查询、两两对比和可视化差异报表能力。

---

## 2. 已确认范围

本轮需求已确认如下：

1. **仅覆盖普通批量注册**（`/registration/batch`）。
2. 统计记录覆盖结束状态：
   - `completed`
   - `failed`
   - `cancelled`
3. 对比方式：**最多选择 2 条记录做 A/B 对比**。
4. 统计项必须包含：
   - 使用的注册流程（`pipeline_key`）
   - 开始时间
   - 结束时间
   - 目标数量
   - 成功数量
   - 失败数量
   - 总耗时
   - 平均耗时
   - 各 Step 平均耗时
   - 各业务阶段平均耗时
5. UI 使用**独立新页面**，不与实验对比页混用。

---

## 3. 目标与非目标

### 3.1 目标

本轮设计必须满足以下目标：

1. 在普通批量任务结束时自动生成统计快照并落库。
2. 批量统计记录可在列表中浏览、筛选和查看详情。
3. 支持选择两条记录进行 A/B 对比。
4. 对比报表同时展示：
   - 总体指标差异
   - Step 级平均耗时差异
   - 业务阶段级平均耗时差异
5. 与现有 `registration_tasks` / `pipeline_step_runs` 数据结构兼容，不重写批量执行器。

### 3.2 非目标

本轮不包含：

1. 无限注册模式统计。
2. Outlook 批量统计。
3. 超过 2 条记录的多组对比。
4. PDF / Excel / 图片导出。
5. 复杂图表系统或 BI 平台集成。
6. 自动输出“最佳批次结论”或智能推荐。

---

## 4. 总体方案

推荐采用“**独立批量统计中心**”方案，而不是复用现有 `experiment_batches`。

### 4.1 核心思想

新增独立领域对象和独立页面：

1. 普通批量执行结束后，统一生成一份**统计快照**。
2. 快照由主表 + Step 聚合表 + 业务阶段聚合表组成。
3. 列表页直接读取快照，不在前端或接口层对历史任务做大规模实时重算。
4. 对比接口按两条统计记录生成差异结构，前端只负责展示。

### 4.2 为什么不复用 `ExperimentBatch`

`ExperimentBatch` 的语义是“成对实验批次”，其核心目标是：

- `current_pipeline` vs `codexgen_pipeline` 公平实验
- pair 级对照
- 实验批次汇总

普通批量统计的语义是：

- 单条批量运行结果复盘
- 不要求成对
- 不要求实验配置对齐

如果复用 `ExperimentBatch`：

- 会混淆“实验”和“普通批量”的业务边界；
- 后续筛选、列表、报表语义会越来越复杂；
- 实现上会出现大量“如果是实验 / 如果是普通批量”的分支。

因此本轮建议建立独立批量统计模型。

---

## 5. 数据模型设计

### 5.1 主表：`registration_batch_stats`

表示一条普通批量注册的最终统计快照。

建议字段：

- `id`
- `batch_id`：运行时批量 ID，唯一
- `status`：`completed / failed / cancelled`
- `mode`：`pipeline / parallel`
- `pipeline_key`
- `email_service_type`
- `email_service_id`
- `proxy_strategy_snapshot`
- `config_snapshot`
- `target_count`
- `finished_count`
- `success_count`
- `failed_count`
- `total_duration_ms`
- `avg_duration_ms`
- `started_at`
- `completed_at`
- `created_at`

设计原则：

1. 保存当时的配置快照，避免后续配置变化影响历史复盘。
2. 主表优先承载列表页和总览卡片所需字段。
3. `batch_id` 需唯一，确保 finalize 幂等。

### 5.2 子表：`registration_batch_step_stats`

表示一条批量统计中某个 Step 的聚合结果。

建议字段：

- `id`
- `batch_stat_id`
- `step_key`
- `step_order`
- `sample_count`
- `success_count`
- `avg_duration_ms`
- `p50_duration_ms`
- `p90_duration_ms`

说明：

- 数据来源为该批次所有任务对应的 `pipeline_step_runs`。
- 用于详情页和 A/B 对比页的 Step 明细展示。

### 5.3 子表：`registration_batch_stage_stats`

表示一条批量统计中某个业务阶段的聚合结果。

建议字段：

- `id`
- `batch_stat_id`
- `stage_key`
- `sample_count`
- `avg_duration_ms`
- `p50_duration_ms`
- `p90_duration_ms`

该表用于：

- 老板/管理层口径的阶段耗时展示；
- 避免仅有 step 级明细，不利于高层阅读。

### 5.4 与现有模型关系

#### 复用的原始明细

- `registration_tasks`
- `pipeline_step_runs`

#### 新模型职责

- `registration_batch_stats`：最终快照
- `registration_batch_step_stats`：step 聚合
- `registration_batch_stage_stats`：阶段聚合

也就是说：

> 原始执行明细继续由现有表承载；新表只负责可复盘、可查询、可对比的统计快照。

---

## 6. 业务阶段定义

本轮采用固定 `step_key -> stage_key` 映射。

### 6.1 阶段列表

建议固定为：

- `signup_prepare`
- `signup_otp`
- `create_account`
- `login_prepare`
- `login_otp`
- `token_exchange`

### 6.2 Step 映射建议

示例映射：

- `get_proxy_ip`
- `create_email`
- `init_signup_session`
  -> `signup_prepare`

- `send_signup_otp`
- `wait_signup_otp`
- `validate_signup_otp`
  -> `signup_otp`

- `create_account`
  -> `create_account`

- `init_login_session`
- `submit_login_email`
- `submit_login_password`
  -> `login_prepare`

- `wait_login_otp`
- `validate_login_otp`
  -> `login_otp`

- `select_workspace`
- `exchange_oauth_token`
  -> `token_exchange`

### 6.3 映射策略

建议将映射集中放在统计模块内部常量中，例如：

- `STEP_STAGE_MAP`

这样后续新增或调整 Step 时，只需修改统计映射，不必改动已有执行逻辑。

---

## 7. 批量结束时的数据流

### 7.1 批量开始阶段

普通批量开始时，仍沿用当前 `batch_tasks[batch_id]` 内存态，但需确保其中包含足够的统计上下文：

- `batch_id`
- `pipeline_key`
- `mode`
- `count`
- `email_service_type`
- `email_service_id`
- `proxy`
- `interval_min`
- `interval_max`
- `concurrency`
- `started_at`
- `task_uuids`

### 7.2 批量结束阶段

无论批量最终是：

- `completed`
- `failed`
- `cancelled`

都在统一收尾点调用：

- `finalize_batch_statistics(batch_id, status)`

### 7.3 finalize 的职责

#### 1）读取批量上下文

从 `batch_tasks[batch_id]` 读取：

- 目标数量
- 任务列表
- 计数
- 模式
- 流程
- 起止时间

#### 2）查询原始明细

基于 `task_uuids` 查询：

- `registration_tasks`
- `pipeline_step_runs`

#### 3）生成统计快照

生成：

- 主表记录
- Step 聚合记录
- Stage 聚合记录

#### 4）写库

统一写入新建统计表。

### 7.4 统计口径

#### 主表口径

- `target_count`：计划任务数
- `finished_count`：实际进入终态的任务数
- `success_count`
- `failed_count`

#### Step / Stage 口径

只统计**实际存在执行明细**的任务和步骤，不人为补齐未执行任务。

这意味着：

- `cancelled` 批次只统计取消前已经跑过的部分；
- `failed` 批次只统计失败前已产生的执行数据；
- 不强行要求样本数等于 `target_count`。

---

## 8. 幂等性与一致性

### 8.1 幂等性要求

同一个 `batch_id` 只能生成一条主统计记录。

推荐做法：

1. `registration_batch_stats.batch_id` 唯一；
2. finalize 时如发现已有记录，则直接返回已有记录，不重复生成。

### 8.2 统计失败的处理

如果批量本身已经完成，但统计生成失败：

1. **不能反向影响批量任务结果**；
2. 批量状态仍保持其真实结束状态；
3. 仅记录日志和错误告警；
4. 后续可按需补做“手动重建统计”能力（非本轮范围）。

---

## 9. API 设计

建议新增路由文件：

- `src/web/routes/registration_batch_stats.py`

### 9.1 列表接口

`GET /api/registration/batch-stats`

支持参数：

- `status`
- `pipeline_key`
- `limit`
- `offset`

返回：

- 总数
- 统计记录列表

每条记录至少包含：

- `id`
- `batch_id`
- `status`
- `mode`
- `pipeline_key`
- `started_at`
- `completed_at`
- `target_count`
- `success_count`
- `failed_count`
- `total_duration_ms`
- `avg_duration_ms`

### 9.2 详情接口

`GET /api/registration/batch-stats/{id}`

返回：

- 主记录
- Step 聚合列表
- Stage 聚合列表

### 9.3 对比接口

`POST /api/registration/batch-stats/compare`

请求体：

- `left_id`
- `right_id`

返回结构建议：

- `left`
- `right`
- `summary_diff`
- `step_diffs`
- `stage_diffs`

#### `summary_diff`

至少包括：

- `target_count`
- `success_count`
- `failed_count`
- `success_rate`
- `total_duration_ms`
- `avg_duration_ms`

#### `step_diffs`

按 `step_key` 对齐，返回：

- `step_key`
- `left.avg_duration_ms`
- `right.avg_duration_ms`
- `delta_duration_ms`
- `delta_rate`

#### `stage_diffs`

按 `stage_key` 对齐，返回：

- `stage_key`
- `left.avg_duration_ms`
- `right.avg_duration_ms`
- `delta_duration_ms`
- `delta_rate`

### 9.4 缺失数据处理

如果某个 step / stage 一边存在、一边不存在：

- 缺失侧显示 `null`
- 不强行生成 delta_rate

这样可以兼容不同流程、不同阶段覆盖度和异常批次。

---

## 10. 页面设计

新页面路径建议：

- `/registration-batch-stats`

### 10.1 页面结构

页面分为三块：

#### 1）统计列表区

展示历史批量统计记录，支持：

- 时间倒序
- 状态筛选
- 流程筛选
- 勾选记录

#### 2）单条详情区

当只选择一条记录时，展示：

- 基础信息卡片
- Step 聚合表
- Stage 聚合表

#### 3）A/B 对比报表区

当选择两条记录时，展示：

- 总览指标卡
- Step 对比表
- Stage 对比表

### 10.2 交互规则

- 选择 0 条：只显示列表
- 选择 1 条：显示详情
- 选择 2 条：显示对比报表
- 选择 >2 条：前端阻止并提示

### 10.3 可视化策略

第一版优先采用：

- 指标卡片
- 表格对比
- 差值颜色高亮

本轮不强制接入重型图表库。

---

## 11. 实现边界

### 11.1 本轮包含

1. 普通批量结束后自动生成统计快照。
2. 列表、详情、两两对比接口。
3. 独立页面展示列表、详情和 A/B 报表。

### 11.2 本轮不包含

1. 无限注册统计。
2. Outlook 批量统计。
3. 多记录对比。
4. 导出能力。
5. 自动结论推荐。

---

## 12. 测试策略

建议分四层测试。

### 12.1 模型 / CRUD 测试

验证：

- 主表可持久化；
- Step 子表可持久化；
- Stage 子表可持久化；
- 关联关系正确。

### 12.2 统计聚合测试

验证：

- 能从 `registration_tasks + pipeline_step_runs` 计算主统计；
- 能生成 step 平均耗时；
- 能生成 stage 平均耗时；
- `completed / failed / cancelled` 三种状态均可生成记录。

### 12.3 路由测试

验证：

- 列表接口返回筛选和分页结果；
- 详情接口返回完整统计结构；
- compare 接口正确生成 summary / step / stage diff。

### 12.4 前端资产 / 交互测试

验证：

- 新页面模板中存在列表、详情、对比容器；
- 前端脚本会请求列表、详情、compare；
- 勾选 2 条后触发 compare 渲染；
- 超过 2 条时前端限制生效。

---

## 13. 推荐实施顺序

建议按以下顺序实施：

1. 新增 ORM 模型与数据库迁移；
2. 新增统计聚合与 finalize 模块；
3. 在普通批量收尾点接入统计落库；
4. 新增列表 / 详情 / compare API；
5. 新增独立页面与前端脚本；
6. 补齐测试。

这样可以先打通后端统计链路，再接 UI，避免前端页面先做出来却拿不到稳定数据。

---

## 14. 结论

本设计推荐为普通批量注册新增独立“批量统计中心”，其核心价值是：

1. 将普通批量的最终表现固化为稳定可复盘的统计快照；
2. 支持历史记录列表化管理；
3. 支持两条记录的 A/B 对比；
4. 同时面向技术视角（step）和管理视角（stage）展示差异。

该方案与现有实验体系边界清晰、与现有批量执行逻辑耦合较低、便于分阶段实施，适合作为本轮正式实现方案。
