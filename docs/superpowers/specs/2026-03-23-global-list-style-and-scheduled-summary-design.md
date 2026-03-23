# Global List Style and Scheduled Summary Design

**Date:** 2026-03-23

## Goal

统一全站列表类界面的视觉风格，提升为更有层次感的卡片化后台风；同时把定时任务运行摘要改为业务可读、只保留关键信息的精简摘要。

## Confirmed Decisions

- 风格方向：**B. 卡片化后台风**
- 改造范围：**全站列表样式统一优化**
- 定时任务摘要规则：
  - `cpa_refill`：只显示补号数量
  - `cpa_cleanup`：显示检测数量和清理数量
  - `account_refresh`：显示处理数量、刷新成功、上传成功
- 失败/取消时：显示关键计数，并追加一句简短失败原因

## In Scope

- 统一 `accounts`、`email_services`、`settings`、`scheduled_tasks`、首页最近账号列表的表格视觉风格。
- 优化列表外层容器、表头、行 hover、状态徽章、操作按钮组、空状态、分页区。
- 优化定时任务页的计划列表、运行中心、运行详情/日志区样式。
- 重写定时任务运行摘要展示逻辑，使其不再直接暴露原始 JSON。

## Out of Scope

- 不改后端定时任务执行逻辑和 summary 数据结构。
- 不把展示型表格全部重构成全卡片流式布局。
- 不修改支付页等非列表型页面的主视觉结构。
- 不改接口字段命名，不新增接口。

## Current Problems

### 1. 列表风格偏“基础表格”，专业感不足

当前多个页面共享基础 `.data-table` 样式，但层次较平，表格更像默认管理台表格，而不是成熟后台产品的列表系统。

### 2. 列表之间缺少统一的视觉语言

虽然都使用表格，但不同页面在按钮区、空状态、边距、局部内联样式上的处理不一致，整体观感不够统一。

### 3. 定时任务摘要可读性弱

运行中心摘要字段会直接展示完整错误文本或 `summary` JSON，用户需要自己理解字段语义，无法快速扫读任务结果。

## Proposed Approach

采用“**统一列表基座 + 定时任务专项增强**”方案：

1. 在全局 CSS 中强化列表容器、表头、数据行、按钮组、分页和空状态的视觉规范。
2. 区分“展示型列表”和“编辑型表格”：
   - 展示型列表采用卡片化后台风。
   - 编辑型表格保持录入效率，只做细节美化。
3. 在 `scheduled_tasks.js` 中新增摘要格式化逻辑，基于 `task_type + summary + error_message` 生成业务可读摘要。
4. 对定时任务详情区增加摘要标签、信息块和日志容器的视觉分层，提升运行记录的专业感。

## Visual Design Rules

### 1. 展示型列表

- 列表外层容器增加更明确的边框、圆角、阴影与内边距节奏。
- 表头采用更轻的背景、字距和大写风格，增强“数据区”识别。
- 表格行用更细腻的 hover、间距和过渡，营造“卡片行”感。
- 操作列按钮统一为紧凑按钮组，间距、尺寸一致。
- 列表支持在小屏下自然横向滚动，避免强行压缩。

### 2. 状态表达

- `status-badge` 统一升级，增强颜色层次、边框和视觉稳定性。
- 失败、运行中、禁用、警告等状态保持现有语义色，但更贴近后台产品样式。

### 3. 附属区块

- 分页区统一顶部描边与背景。
- 空状态统一居中、弱化图标、优化骨架屏承载。
- 运行详情区使用摘要标签与信息块来替代简单文本堆叠。

## Scheduled Summary Mapping

### `cpa_refill`

优先读取：

- `uploaded_success`

输出格式：

- 成功：`补号 {uploaded_success}`
- 失败/取消：`补号 {uploaded_success} · 原因：{short_reason}`

### `cpa_cleanup`

优先读取：

- `invalid_items_found`
- `remote_deleted`

输出格式：

- 成功：`检测 {invalid_items_found} · 清理 {remote_deleted}`
- 失败/取消：`检测 {invalid_items_found} · 清理 {remote_deleted} · 原因：{short_reason}`

备注：清理数量优先体现远端删除数量，因为这是最直观的“实际清理结果”。

### `account_refresh`

优先读取：

- `processed`
- `refreshed_success`
- `uploaded_success`

输出格式：

- 成功：`处理 {processed} · 刷新 {refreshed_success} · 上传 {uploaded_success}`
- 失败/取消：`处理 {processed} · 刷新 {refreshed_success} · 上传 {uploaded_success} · 原因：{short_reason}`

## Error Message Policy

- 失败/取消时优先使用 `error_message`。
- 如果失败/取消但 `error_message` 为空，则根据状态给出简短兜底：
  - `failed` → `执行失败`
  - `cancelled` → `用户停止`
  - `stopping` → `停止中`
- 长错误文本需要截断，避免撑爆列表布局。

## File-Level Design

- `static/css/style.css`
  - 承担全局列表系统升级。
  - 新增通用列表类：如操作按钮组、摘要标签、日志详情块等。
- `static/js/scheduled_tasks.js`
  - 承担摘要格式化逻辑。
  - 调整运行中心与详情弹窗的渲染结构。
- `templates/scheduled_tasks.html`
  - 尽量少改结构，仅在需要时增加语义 class，承接新的样式系统。

## Testing Strategy

- 前端资源测试：
  - 为摘要格式化逻辑补充断言，覆盖三类任务与失败附带原因的输出。
  - 为运行详情渲染新增断言，确认摘要和信息区结构存在。
- 页面样式测试：
  - 维持现有模板/资源加载测试。
  - 对关键 class 名的存在进行轻量断言，避免纯视觉回归没有保障。

## Risks and Mitigations

### 风险 1：全局表格样式影响编辑型表格

通过新增更具体的 class 或在选择器中保留默认路径，避免配置编辑表被过度卡片化。

### 风险 2：摘要字段缺失

格式化逻辑要为所有数值字段设置 `0` 默认值，并提供 `-` / 兜底文案，保证任何历史运行记录都能正常展示。

### 风险 3：错误信息过长破坏布局

通过截断和摘要容器换行策略控制宽度，同时把完整错误留在详情/日志中查看。

## Implementation Notes

- 优先做 CSS 基座升级，保证全站列表立即获得统一收益。
- 再对定时任务摘要和详情区做专项增强。
- 不做后端改动，避免扩大验证面。
