# Scheduled Tasks UI Hardening Design

**Date:** 2026-03-22

## Goal

对定时任务页面做一轮小范围前端加固，解决三个明确问题：

1. CPA 服务列表加载失败时没有用户可见提示。
2. 页面上的操作按钮缺少防重复点击保护，可能导致重复请求。
3. 页面资产测试只校验 hook 存在，未校验计划列表行内按钮的实际渲染标记。

本次改动只做前端与前端资产测试增强，不改动后端 API 语义，不扩展功能范围。

## Scope

### In scope

- 为 CPA 服务列表加载失败增加 toast 错误提示。
- 为定时任务页面上的请求型按钮增加前端“请求中锁/防重入”能力。
- 为计划列表行内按钮增加稳定的可测试标记，便于资产测试验证渲染结果。
- 补强 `tests/test_scheduled_tasks_page_assets.py`，覆盖页面模板与计划行按钮渲染相关断言。

### Out of scope

- 不新增后端接口。
- 不修改调度引擎行为。
- 不修改计划数据结构。
- 不引入新的前端框架或状态管理。
- 不做跨页面通用按钮防抖框架抽象；仅处理定时任务页面当前需要的场景。

## Current Problems

### 1. CPA 服务加载失败静默降级

当前 `loadCpaServices()` 捕获异常后仅把 `cpaServicesCache` 置空，没有 toast。虽然用户仍可手动输入 CPA 服务 ID，但不知道下拉为何为空，容易误判为页面异常。

### 2. 操作按钮可重复触发

当前页面上的刷新、创建、编辑、启停、立即执行、记录查看等操作，多数没有统一的前端防重入机制。用户连续点击时可能触发重复请求，带来重复 toast、重复 API 调用、状态闪烁等问题。

### 3. 资产测试缺少渲染层断言

现有测试主要验证：模板里是否存在某些元素 ID、脚本里是否存在函数名和接口路径。它们可以证明“代码被写了”，但不能证明计划列表实际渲染出的行内按钮具有预期文案或稳定标记。

## Proposed Approach

### A. 使用“请求中锁”作为按钮防重入主方案

不采用单纯时间防抖，而采用更贴合请求生命周期的前端保护：

- 用户点击按钮后，立即将对应按钮设为 `disabled`。
- 请求未完成前，同一按钮后续点击直接忽略。
- 请求结束后恢复按钮状态。

这样比纯时间防抖更稳，因为它能覆盖慢请求场景，真正防止重复提交。

### B. 为渲染出的计划行按钮增加稳定测试标记

在 `renderPlans()` 生成的行按钮上增加 `data-action`、`data-plan-id` 等稳定属性，例如：

- `data-action="detail"`
- `data-action="logs"`
- `data-action="edit"`
- `data-action="toggle"`
- `data-action="run-now"`

这些标记既不影响现有交互，也能让资产测试验证“实际渲染模板字符串中包含预期按钮结构”。

### C. CPA 服务加载失败时显式 toast

`loadCpaServices()` 请求失败时：

- 仍保持手动输入 CPA ID 的兜底能力；
- 同时弹出 `toast.error(...)`，说明“CPA 服务列表加载失败，请手动输入服务 ID”；
- 避免在同一打开流程中重复多次弹 toast。

## Design Details

## 1. Frontend behavior changes

### 1.1 CPA 服务列表加载

`loadCpaServices()` 增加一个可选参数，用于控制是否弹 toast，例如 `showErrorToast = false`。

- 页面初始化时的静默预加载可不弹 toast，避免首屏反复打扰。
- 在打开新建/编辑弹窗时，如果加载失败，则弹 toast，因为这时用户正在进行与 CPA 服务相关的操作。

### 1.2 请求型按钮保护

增加一个小型助手函数，用于包装按钮点击后的异步操作，职责包括：

- 接收按钮元素和异步函数；
- 如果按钮已处于 busy 状态则直接返回；
- 设置 `disabled` 与 `data-busy="true"`；
- 异步结束后恢复；
- 不吞掉原有错误处理。

应用范围：

- 顶部刷新按钮。
- 新建计划按钮。
- 表单提交按钮。
- 行内详情/记录/编辑/启停/立即执行按钮。
- 查看日志与“返回记录”等会触发请求或快速重复点击的按钮。

说明：

- 对“打开本地模态框但不发请求”的按钮，也允许复用同一保护策略，以保持交互一致性。
- 本次保护只覆盖定时任务页面，不抽象到全站公共库。

### 1.3 行内按钮渲染标记

`renderPlans()` 中每个按钮增加稳定属性，示例：

```html
<button data-action="edit" data-plan-id="12">编辑</button>
```

这样测试不必依赖完整内联 `onclick` 字符串，也不用只靠函数名存在性来间接推断功能。

## 2. Testing changes

补强 `tests/test_scheduled_tasks_page_assets.py`：

### 2.1 脚本层断言

新增断言验证：

- `loadCpaServices` 中存在 toast 错误提示文案或调用。
- 存在用于按钮忙碌态/防重入的脚本逻辑标记，如 `disabled`、`data-busy` 或统一助手函数。

### 2.2 渲染层断言

新增断言验证 `renderPlans()` 输出模板字符串中包含：

- 编辑按钮文案与 `data-action="edit"`
- 启用/禁用按钮的 `data-action="toggle"`
- 立即执行按钮的 `data-action="run-now"`
- 记录按钮的 `data-action="logs"`
- 详情按钮的 `data-action="detail"`
- 对应的 `data-plan-id`

目标是确保测试覆盖“行内按钮真实渲染结构”，而不只是函数名。

## Error Handling

- CPA 服务加载失败：toast 提示 + 保留手动输入兜底。
- 按钮忙碌态期间重复点击：忽略，不重复发请求。
- 请求失败后：沿用现有 toast 错误提示，并确保按钮恢复可点击状态。

## Compatibility

- 不改变现有接口路径和请求体。
- 不改变现有页面布局结构，只为按钮补充属性与交互保护。
- 与现有 `toast`、`api`、`format`、`closeModal` 等工具保持兼容。

## Acceptance Criteria

1. 打开新建/编辑计划弹窗时，若 `/api/cpa-services` 请求失败，页面会出现明确 toast 提示。
2. 用户连续点击同一操作按钮时，不会在请求未完成前重复发起同类请求。
3. 计划列表行内按钮具备稳定的 `data-action` / `data-plan-id` 标记。
4. 资产测试可验证计划行按钮的实际渲染结构，而不是仅验证 hook 是否存在。
5. 现有 create/edit/enable/disable/run/details/logs 流程不回归。
