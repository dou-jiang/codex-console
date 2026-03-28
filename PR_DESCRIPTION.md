# PR 说明（codex-console2 -> codex-console）

## 结论摘要
本次 PR 基于 `K:\github\codex-console2` 对比原仓库 `K:\github\codex-console`，当前实际代码差异为 **1 项**：
- 删除 GitHub Actions 工作流文件：`.github/workflows/docker-publish.yml`

除上述文件外，其余同名文件内容一致（按全量哈希比对）。

## 修改方案
### 目标
- 清理不需要的镜像发布流水线配置，保持当前仓库 CI 行为可控。

### 实施内容
- 移除：`.github/workflows/docker-publish.yml`

## 涉及文件
- 删除文件：`.github/workflows/docker-publish.yml`

## 影响范围
### 直接影响
- 仓库将不再触发该文件定义的 Docker 发布工作流。

### 间接影响
- 如果团队仍依赖该 workflow 进行镜像发布，发布链路会中断；需改由其它 workflow 或手动流程执行。

## 验证结果
- 已完成目录级全量比对（`K:\github\codex-console2` vs `K:\github\codex-console`）：
  - 同名文件：108
  - 同名文件内容差异：0
  - 新增文件：0
  - 删除文件：1（即上述 workflow 文件）

## 回滚方案
如需回滚本次变更：
1. 从原仓库 `K:\github\codex-console` 恢复 `.github/workflows/docker-publish.yml`。
2. 提交回滚 commit 并重新触发 CI 验证。

## 风险评估
- 风险等级：低（仅 CI 配置变更）
- 关注点：确认团队当前是否仍需要该 Docker 发布流水线。

## 建议的 PR 标题
- `chore(ci): remove docker-publish workflow`

## 建议的 Commit Message
- `chore(ci): remove .github/workflows/docker-publish.yml`
