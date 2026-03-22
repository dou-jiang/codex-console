# 定时任务设计文档

- 日期：2026-03-22
- 项目：codex-console
- 主题：内置定时任务中心（CPA 清理 / CPA 补号 / 老账号刷新）

## 1. 背景与目标

当前项目已经具备以下基础能力：

- Web UI 管理注册任务、账号、代理、邮箱服务、CPA 服务
- 本地账号存储与状态管理
- CPA 上传能力
- 手动/批量注册能力
- Token 刷新与订阅检测能力

参考 `~/project/chatgpt-register4/auto_pool_maintainer`，本次要在当前 Web UI 内增加一个内置的“定时任务中心”，使系统能够自动完成以下维护动作：

1. 定时清理 CPA 失效账户
2. 定时补号，使指定 CPA 的有效账号数补足到目标数量
3. 定时刷新本地老账号 Token，并同步订阅信息与 CPA

目标是把原本依赖单独脚本的池维护逻辑内聚到当前 Web 服务中，通过 UI 统一管理计划、查看运行记录、筛选失效账号，并进行后续清理。

## 2. 已确认的需求边界

### 2.1 运行方式

- 定时任务直接内置到当前 Web UI 服务中运行
- 不额外拆成独立后台进程
- v1 按单实例部署设计，不考虑分布式调度

### 2.2 计划形态

- 支持同一种任务创建多条计划
- 每条计划只绑定一个 CPA 服务
- 一个本地账号只归属一个主 CPA 服务
- Cron 执行时区固定为 `Asia/Shanghai`
- 每条计划触发方式二选一：
  - Cron 表达式
  - 固定频率（每 N 分钟 / 每 N 小时）

### 2.3 并发与调度约束

- 同一计划不允许并发执行
- 如果计划上一次尚未完成，下一次触发应直接跳过并记录 `skipped`
- 建议同一 CPA 服务维度也尽量串行执行，避免清理/补号/刷新同时作用于同一池子

### 2.4 本地账号失效语义

- 本地账号失效时不直接删除，而是标记为 `expired`
- 账号表需要记录失效时间
- 需要支持查询已失效账号
- 需要支持批量永久删除已失效账号

### 2.5 计划内配置快照

- 补号计划和刷新计划使用“计划内独立配置快照”
- 创建计划时默认引用当前全局配置
- 创建后允许用户单独修改，不再与全局设置联动

## 3. 功能范围

本次设计包含三类定时任务。

### 3.1 定时清理 CPA 失效账户

功能目标：

- 调用指定 CPA 服务，探测并清理远端失效账号
- 同步本地 `accounts` 表，把对应账号标记为失效
- 输出完整任务日志

支持参数：

- 计划名称
- 绑定 CPA 服务
- 触发方式（Cron / 固定频率）
- 单次最大清理数量
- 启用状态

本地同步规则：

- 使用 `email` 作为 CPA 远端数据与本地账号的匹配键
- 仅同步命中当前主 CPA 的本地账号
- 远端探测为失效时，本地更新为：
  - `status = expired`
  - `invalidated_at = now`
  - `invalid_reason = cpa_cleanup`
  - `cpa_uploaded = false`

### 3.2 定时补号

功能目标：

- 以指定 CPA 的远端有效账号数为目标口径
- 当远端有效账号数低于目标数量时，自动补号
- 注册成功后必须上传到该计划绑定 CPA，上传成功才算本次补号成功

支持参数：

- 计划名称
- 绑定 CPA 服务
- 触发方式（Cron / 固定频率）
- 目标有效数量
- 单次最大补号数量
- 连续失败阈值
- 注册配置快照：
  - 邮箱服务类型 / 邮箱服务 ID
  - 并发数
  - 执行模式（pipeline / parallel）
  - interval min/max
  - 代理策略
  - 其他注册相关参数
- 启用状态

连续失败规则：

- 失败统计按“单次补号执行内的连续失败次数”计算
- 单次尝试失败：连续失败数 +1
- 单次尝试成功：连续失败数清零
- 当连续失败达到阈值时：
  - 立即停止本次补号 run
  - 自动禁用当前计划
  - 在计划状态和运行日志中记录禁用原因

### 3.3 定时刷新本地老账号 Token

功能目标：

- 定期刷新本地老账号 token
- 拉取订阅信息
- 成功后回传指定 CPA，并更新本地记录
- 若刷新失败或订阅获取失败，则标记本地账号失效

支持参数：

- 计划名称
- 绑定 CPA 服务
- 触发方式（Cron / 固定频率）
- 单次最大刷新数量
- 启用状态

刷新时间规则：

- 首次筛选条件：`registered_at <= now - 7天`
- 后续筛选条件：`last_refresh <= now - 7天`
- 刷新周期在 v1 固定为 7 天

执行结果规则：

- 刷新成功 + 订阅获取成功 + 上传 CPA 成功：
  - 更新 token、过期时间、last_refresh、订阅信息、CPA 上传状态
- 刷新失败：
  - 标记本地账号失效
- 订阅获取失败：
  - 标记本地账号失效
- CPA 上传失败：
  - 不标记失效
  - 仅记录本次任务失败明细
  - 等待后续重试

## 4. 总体架构

建议新增一个内置 Scheduler 子系统，挂载在当前 FastAPI 应用生命周期中。

### 4.1 架构分层

1. 计划管理层
   - 负责计划 CRUD、启停、立即执行、下一次触发时间计算
2. 调度协调层
   - 在应用启动时加载启用计划
   - 按 `Asia/Shanghai` 计算触发时机
   - 控制计划并发和执行派发
3. 任务执行层
   - 三类 runner 分开实现：
     - `cpa_cleanup_runner`
     - `cpa_refill_runner`
     - `account_refresh_runner`
4. 运行记录层
   - 每次执行都写入独立 run 记录
   - 保存状态、摘要、错误、完整日志

### 4.2 为什么不复用现有 registration task_manager

现有 `task_manager` 主要围绕前台注册任务与 WebSocket 实时日志展开，特点是：

- 偏临时运行态
- 主要服务于前端手工发起的注册/批量注册
- 数据持久化粒度有限

定时任务中心需要更稳定的持久化能力：

- 计划定义持久化
- 下一次执行时间持久化
- 运行历史持久化
- 自动禁用与跳过记录持久化

因此建议新建 scheduler 体系，而不是把计划任务硬塞进现有注册 task manager。

## 5. 数据模型设计

### 5.1 accounts 表增强

建议新增字段：

- `primary_cpa_service_id`：账号归属的主 CPA 服务 ID
- `invalidated_at`：账号被标记失效的时间
- `invalid_reason`：失效原因

保留并复用现有字段：

- `registered_at`
- `last_refresh`
- `subscription_type`
- `subscription_at`
- `cpa_uploaded`
- `cpa_uploaded_at`
- `status`

建议语义：

- `active`：本地认为当前有效
- `expired`：本地已失效
- `invalidated_at != null`：曾被系统判定失效
- `cpa_uploaded = true`：最近一次成功同步到了主 CPA

### 5.2 scheduled_plans 表

建议新增计划表 `scheduled_plans`，字段如下：

- `id`
- `name`
- `task_type`：`cpa_cleanup` / `cpa_refill` / `account_refresh`
- `enabled`
- `cpa_service_id`
- `trigger_type`：`cron` / `interval`
- `cron_expression`
- `interval_value`
- `interval_unit`：`minutes` / `hours`
- `config`：JSON，保存任务参数快照
- `next_run_at`
- `last_run_started_at`
- `last_run_finished_at`
- `last_run_status`
- `last_success_at`
- `auto_disabled_reason`
- `created_at`
- `updated_at`

其中 `config` 的建议内容：

- 清理任务：
  - `max_cleanup_count`
- 补号任务：
  - `target_valid_count`
  - `max_refill_count`
  - `max_consecutive_failures`
  - `registration_profile`
- 刷新任务：
  - `refresh_after_days=7`
  - `max_refresh_count`
  - `upload_on_success=true`

### 5.3 scheduled_runs 表

建议新增运行记录表 `scheduled_runs`，字段如下：

- `id`
- `plan_id`
- `trigger_source`：`scheduled` / `manual`
- `status`：`running` / `success` / `failed` / `skipped`
- `started_at`
- `finished_at`
- `summary`：JSON
- `error_message`
- `logs`：TEXT
- `created_at`

`summary` 示例：

- 清理任务：
  - `remote_invalid_found`
  - `remote_deleted`
  - `remote_delete_failed`
  - `local_marked_expired`
- 补号任务：
  - `remote_valid_before`
  - `target_valid_count`
  - `gap`
  - `attempted_register`
  - `registered_success`
  - `uploaded_success`
  - `uploaded_failed`
  - `auto_disabled`
- 刷新任务：
  - `checked_accounts`
  - `refreshed_success`
  - `subscription_success`
  - `uploaded_success`
  - `marked_expired`

## 6. 三类任务的执行流程

### 6.1 CPA 清理任务执行流程

1. 创建 run 记录，状态为 `running`
2. 加载绑定的 CPA 配置
3. 调用 CPA 远端探测失效账号
4. 按 `max_cleanup_count` 截断处理数量
5. 对远端失效账号执行删除
6. 用 `email + primary_cpa_service_id` 匹配本地账号
7. 更新本地命中账号为 `expired`
8. 写入摘要和日志，结束 run

本地失效判定建议：

- 只要远端探测为失效，本地即可标记失效
- 不必强依赖远端删除成功后才标记本地失效

原因：

- 远端删除失败可能只是管理接口异常，不代表账号重新有效

### 6.2 补号任务执行流程

1. 查询指定 CPA 当前有效账号数
2. 计算缺口：`target_valid_count - remote_valid_count`
3. 若缺口 <= 0，则直接成功结束
4. 实际补号目标：`min(缺口, max_refill_count)`
5. 使用计划中的注册配置快照发起注册
6. 每次尝试以“注册成功 + 上传指定 CPA 成功”作为成功标准
7. 成功后更新本地账号归属与 CPA 状态
8. 若连续失败达到阈值，则停止 run 并自动禁用计划

成功后本地账号建议更新为：

- `status = active`
- `primary_cpa_service_id = 当前计划绑定 CPA`
- `cpa_uploaded = true`
- `cpa_uploaded_at = now`
- 清空 `invalidated_at`
- 清空 `invalid_reason`

如果注册成功但上传 CPA 失败：

- 保留本地账号
- 不计入补号成功数
- `cpa_uploaded = false`
- 记录失败日志

### 6.3 刷新任务执行流程

筛选范围：

- `status = active`
- `primary_cpa_service_id = 当前计划绑定 CPA`
- 满足：
  - `last_refresh is null and registered_at <= now - 7天`
  - 或 `last_refresh <= now - 7天`

逐个处理：

1. 刷新 token
2. 若刷新失败：标记失效
3. 若刷新成功：获取订阅信息
4. 若订阅获取失败：标记失效
5. 若订阅获取成功：更新本地 token 与订阅信息
6. 上传到绑定 CPA
7. 若上传成功：更新 `cpa_uploaded`
8. 若上传失败：不标记失效，只记录失败

刷新失败时本地更新建议：

- `status = expired`
- `invalidated_at = now`
- `invalid_reason = refresh_failed`
- `cpa_uploaded = false`

订阅获取失败时本地更新建议：

- `status = expired`
- `invalidated_at = now`
- `invalid_reason = subscription_check_failed`
- `cpa_uploaded = false`

## 7. 页面与交互设计

### 7.1 新增页面：定时任务

建议在顶部导航增加新入口：`定时任务`。

该页面分为三块：

1. 计划列表
2. 计划编辑弹窗
3. 运行记录查看弹窗 / 抽屉

### 7.2 计划列表字段

建议展示：

- 计划名称
- 类型（清理 / 补号 / 刷新）
- 绑定 CPA
- 触发方式
- 下一次执行时间
- 最近一次执行状态
- 是否启用
- 自动禁用原因
- 操作按钮：
  - 编辑
  - 启用/禁用
  - 立即执行
  - 查看日志
  - 删除

### 7.3 计划编辑弹窗

通用字段：

- 计划名称
- 任务类型
- 绑定 CPA 服务
- 触发方式（Cron / 固定频率）
- 启用状态
- 时区提示：固定 `Asia/Shanghai`

任务参数区：

- 清理任务：
  - 单次最大清理数量
- 补号任务：
  - 目标有效数量
  - 单次最大补号数量
  - 连续失败阈值
  - 注册配置快照
- 刷新任务：
  - 单次最大刷新数量
  - 7 天刷新说明

补号计划建议提供：

- “从当前全局注册配置填充”按钮
- 创建时自动填充一次
- 编辑后与全局设置解耦

### 7.4 运行记录展示

点击“查看日志”时，展示最近运行记录：

- 开始时间
- 结束时间
- 运行状态
- 摘要统计
- 错误信息
- 完整日志

建议默认查看最近 20 次。

### 7.5 账号管理页增强

建议账号管理页增加：

- 新列：
  - 主 CPA
  - 失效时间
  - 失效原因
- 新筛选：
  - `status = expired`
  - 按主 CPA 筛选
  - “只看已失效账号”快捷筛选
- 批量操作：
  - 继续复用现有批量删除能力
  - 支持批量永久删除失效账号

## 8. API 设计

### 8.1 计划管理 API

建议新增：

- `GET /api/scheduled-plans`
- `POST /api/scheduled-plans`
- `GET /api/scheduled-plans/{id}`
- `PUT /api/scheduled-plans/{id}`
- `DELETE /api/scheduled-plans/{id}`
- `POST /api/scheduled-plans/{id}/enable`
- `POST /api/scheduled-plans/{id}/disable`
- `POST /api/scheduled-plans/{id}/run`

### 8.2 运行记录 API

建议新增：

- `GET /api/scheduled-plans/{id}/runs`
- `GET /api/scheduled-runs/{run_id}`
- `GET /api/scheduled-runs/{run_id}/logs`

### 8.3 账号列表接口增强

建议扩展现有 `/api/accounts`：

- 支持：
  - `status=expired`
  - `primary_cpa_service_id`
  - `invalid_only=true`
- 返回字段增加：
  - `primary_cpa_service_id`
  - `invalidated_at`
  - `invalid_reason`

## 9. 实现分层建议

建议新增以下模块边界：

1. `scheduler/models` / `scheduler/schemas`
   - 计划与 run 的数据结构
2. `scheduler/service`
   - 计划 CRUD、启停、下一次执行时间计算、自动禁用
3. `scheduler/engine`
   - 启动轮询、到点派发、并发控制、CPA 维度锁
4. `scheduler/runners`
   - 三类任务的具体执行逻辑

## 10. 可复用现有能力

可以直接复用：

- CPA 服务配置表与 CRUD
- 现有注册流程核心能力
- `refresh_account_token`
- `check_subscription_status`
- `upload_to_cpa`
- 现有 `accounts` CRUD 基础能力

需要新增封装：

- CPA 远端有效账号统计
- CPA 远端失效探测与删除
- 补号 runner 的连续失败自动禁用逻辑
- 刷新任务账号筛选器
- 计划运行日志聚合器

## 11. 错误处理与日志策略

### 11.1 通用要求

- 所有计划执行必须生成 run 记录
- 即使触发被跳过，也要写 `skipped` 运行记录
- 所有运行记录都要保留可读日志

### 11.2 清理任务

- CPA 接口异常：run 标记失败
- 本地同步异常：继续处理其他账号，并在 run 汇总中记录局部失败

### 11.3 补号任务

- 单次注册失败：累计连续失败数
- 注册成功但 CPA 上传失败：记失败但不算成功
- 达到连续失败阈值：停止 run、禁用计划

### 11.4 刷新任务

- 刷新失败：本地标记失效
- 订阅获取失败：本地标记失效
- CPA 上传失败：仅记失败，不标记失效

## 12. v1 明确不做

为控制范围，v1 不包含以下能力：

- 多实例分布式调度
- 每条计划独立时区
- 一个账号归属多个 CPA
- 一个计划绑定多个 CPA
- 定时任务实时 WebSocket 日志流
- 刷新周期自定义（v1 固定为 7 天）

## 13. 推荐实施方案

最终推荐方案：

- 在当前 Web UI 内置单实例调度中心
- 新增：
  - `scheduled_plans`
  - `scheduled_runs`
- 增强：
  - `accounts.primary_cpa_service_id`
  - `accounts.invalidated_at`
  - `accounts.invalid_reason`
- 新增独立“定时任务”页面
- 账号管理页增强失效筛选与批量永久删除
- 补号计划支持连续失败自动禁用

该方案兼顾了：

- 与现有项目结构的适配性
- 对参考项目自动池维护逻辑的吸收
- 后续继续扩展更多计划类型的可维护性
