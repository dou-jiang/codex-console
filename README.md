# codex-console

这是一个直接基于 [dou-jiang/codex-console](https://github.com/dou-jiang/codex-console) 继续重构的版本。

它不只是原版的修修补补，而是在保留旧 Web UI 可继续使用的前提下，把注册流程逐步拆成更清晰的 API、Worker 和独立边界层，方便后续维护、测试和继续演进。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

- GitHub Repo: [https://github.com/dou-jiang/codex-console](https://github.com/dou-jiang/codex-console)

## 当前版本

这是当前这条重构线的 **v1.0.0** 版本。

重构来源说明：

- 直接重构自 [dou-jiang/codex-console](https://github.com/dou-jiang/codex-console)
- 上述仓库本身基于 [cnlimiter/codex-manager](https://github.com/cnlimiter/codex-manager) 演化而来

如果后续创建新的私有仓库并单独发布，请保留这段来源说明，避免误解代码来源。

## 这个仓库现在是什么

可以把它理解成：

- `dou-jiang/codex-console` 的继续重构版
- 保留旧 Web UI 的兼容运行版
- 同时引入新 API 和新 Worker 的过渡版本
- 为后续彻底替换旧直连执行逻辑提前铺好的底座版本

如果你是第一次接触这个仓库，最重要的一点是：

**原版仍然主要围绕 Web 路由直接驱动流程，这个版本已经开始把核心注册执行链路拆成任务式架构。**

## 和原版有什么区别

| 对比项 | 原版 `dou-jiang/codex-console` | 当前重构版 |
| --- | --- | --- |
| 主体结构 | 主要围绕 `src/web` 和原有流程直接执行 | 在保留旧 Web UI 的同时，新增 `apps/api`、`apps/worker` 和 `packages/*` |
| 执行方式 | Web 路由直接串起任务创建、执行和结果返回 | 新增任务式执行通路，先落库，再由 API / Worker 驱动执行 |
| 启动入口 | 主要是 `webui.py` | 除 `webui.py` 外，还可单独启动 `scripts/run_api.py` 和 `scripts/run_worker.py` |
| 边界拆分 | 邮箱、注册、任务存储等能力更多混在旧流程里 | 额外拆出 `registration_core`、`email_providers`、`account_store` 三个清晰边界 |
| 可观察性 | 主要依赖现有页面和日志查看 | 新增任务列表、任务详情、任务日志、结构化执行结果 |
| 演进方式 | 改逻辑时更容易牵动旧页面和旧路由 | 可以先在新通路里迭代，再逐步把旧入口收敛成兼容壳 |
| 测试覆盖 | 以原有流程验证为主 | 补上 API、Worker、新边界和旧桥接层的回归测试 |

如果你想快速判断“这版值不值得继续接手”，看这张表就够了：它已经不再只是原版的修复维护分支，而是一个开始解耦旧结构的重构分支。

## 新架构概览

当前仓库大致可以按下面理解：

```text
webui.py                      旧 Web UI 入口，继续保留
apps/api/main.py              新 API 入口
apps/worker/main.py           新 Worker 入口
scripts/run_api.py            API 启动脚本
scripts/run_worker.py         Worker 启动脚本
packages/registration_core/   注册执行边界
packages/email_providers/     邮箱服务边界
packages/account_store/       账号、任务、日志存储边界
src/web/routes/               旧页面和兼容路由
docs/migration/               迁移状态和新通路说明
```

这套结构的核心思路不是“一次性推翻旧代码”，而是：

1. 先把最难维护的执行链路拆出边界。
2. 保留旧 Web UI，避免一次重写所有页面。
3. 用新 API / Worker 跑通任务化流程。
4. 后续再决定哪些旧路由只保留兼容壳，哪些可以彻底替换。

## 这次重构新增了什么

相对原版，这条重构线已经额外具备下面这些新能力：

- 新的任务式 API，可创建任务、列出任务、查询单个任务、读取任务日志
- Worker 执行通路，可执行指定任务，也可轮询下一条待处理任务
- Worker 单实例锁和空闲退出控制，便于先做轻量后台运行
- 更完整的任务结果结构，能保留请求参数、执行状态、错误信息和身份结果
- 独立的邮箱服务工厂层，便于继续扩展不同接码源
- 独立的注册执行边界，减少核心流程继续散落在旧 Web 路由里
- 独立的账号 / 任务 / 日志存储层，降低以后改数据存储时的牵连面
- 独立脚本入口和模块入口，便于本地调试、脚本化运行和后续部署
- 覆盖新架构和旧兼容桥接层的回归测试，降低迁移过程中把旧行为改坏的概率

## 文档入口

如果你准备继续接这个仓库，建议按这个顺序看：

- `README.md`：先搞清楚项目来源、架构差异和启动方式
- `docs/internal-deploy.md`：看内部服务最小上线方式和安全默认值
- `docs/migration/2026-03-26-phase1-status.md`：看已经拆出来的边界和当前冻结区
- `docs/migration/2026-03-26-phase2-quickstart.md`：直接试新 API / Worker 通路
- `docs/superpowers/plans/*.md`：看这次重构是怎么分阶段推进的

## QQ群

- 交流群: [291638849（点击加群）](https://qm.qq.com/q/4TETC3mWco)
- Telegram 频道: [codex_console](https://t.me/codex_console)

## 致谢

首先感谢上游项目作者 [cnlimiter](https://github.com/cnlimiter) 提供的优秀基础工程。

本仓库是在原项目思路和结构之上继续做兼容性修复、流程重构和边界拆分，适合作为一个“既能继续用、又能继续拆”的过渡版本。

## 版本更新

### v1.0

1. 新增 Sentinel POW 求解逻辑  
   OpenAI 现在会强制校验 Sentinel POW，原先直接传空值已经不行了，这里补上了实际求解流程。

2. 注册和登录拆成两段  
   现在注册完成后通常不会直接返回可用 token，而是跳转到绑定手机或后续页面。  
   本分支改成“先注册成功，再单独走一次登录流程拿 token”，避免卡死在旧逻辑里。

3. 去掉重复发送验证码  
   登录流程里服务端本身会自动发送验证码邮件，旧逻辑再手动发一次，容易让新旧验证码打架。  
   现在改成直接等待系统自动发来的那封验证码邮件。

4. 修复重新登录流程的页面判断问题  
   针对重新登录时页面流转变化，调整了登录入口和密码提交逻辑，减少卡在错误页面的情况。

5. 优化终端和 Web UI 提示文案  
   保留可读性的前提下，把一些提示改得更友好一点，出错时至少不至于像在挨骂。

### v1.1

1. 修复注册流程中的问题，解决 Outlook 和临时邮箱收不到邮件导致注册卡住、无法完成注册的问题。

2. 修复无法检查订阅状态的问题，提升订阅识别和状态检查的可用性。

3. 新增绑卡半自动模式，支持自动随机地址；3DS 无法跳过，需按实际流程完成验证。

4. 新增已订阅账号管理功能，支持查看和管理账号额度。

5. 新增后台日志功能，并补充数据导出与导入能力，方便排查问题和迁移数据。

6. 优化部分 UI 细节与交互体验，减少页面操作时的割裂感。

7. 补充细节稳定性处理，尽量减少注册、订阅检测和账号管理过程中出现卡住或误判的情况。

### v1.1.1

1. 新增 `CloudMail` 邮箱服务实现，并完成服务注册、配置接入、邮件轮询、验证码提取和基础收件处理能力。

2. 新增上传目标 `newApi` 支持，可根据配置选择不同导入目标类型。

3. 新增 `Codex` 账号导出格式，支持后续登录、迁移和导入使用。

4. 新增 `CPA` 认证文件 `proxy_url` 支持，现可在 CPA 服务配置中保存和使用代理地址。

5. 优化 OAuth token 刷新兼容逻辑，完善异常返回与一次性令牌场景处理，降低刷新报错概率。

6. 优化批量验证流程，改为受控并发执行，减少长时间阻塞和卡死问题。

7. 修复模板渲染兼容问题，提升不同 Starlette 版本下页面渲染稳定性。

8. 修复六位数字误判为 OTP 的问题，避免邮箱域名或无关文本中的六位数字被错误识别为验证码。

9. 新增 Outlook 账户“注册状态”识别与展示功能，可直接看到“已注册/未注册”，并支持显示关联账号编号（如“已注册 #1”）。

10. 修复 Outlook 邮箱匹配大小写问题，避免 Outlook.com 因大小写差异被误判为未注册。

11. 修复 Outlook 列表列错位、乱码和占位文案问题，恢复中文显示并优化列表信息布局。

12. 优化 WebUI 端口冲突处理，默认端口占用时自动切换可用端口。

13. 增加启动时轻量字段迁移逻辑，自动补齐新增字段，提升旧数据升级兼容性。

14. 批量注册上限由 `100` 提升至 `1000`（前后端同步）。

## 核心能力

- 保留原有 Web UI，可继续管理注册任务和账号数据
- 新增任务式 API / Worker 通路，适合逐步替换旧直连执行逻辑
- 支持批量注册、日志实时查看、基础任务管理
- 支持任务日志查询和结构化执行结果回读
- 支持多种邮箱服务接码
- 支持 SQLite 和远程 PostgreSQL
- 支持打包为 Windows/Linux/macOS 可执行文件
- 更适配当前 OpenAI 注册与登录链路

## 新任务通路

仓库里现在还保留旧 Web UI，但已经补出一条新的任务式执行通路，适合逐步替换旧逻辑：

- 新 API：`apps/api/main.py`
- 新 Worker：`apps/worker/main.py`
- 启动脚本：
  - `scripts/run_api.py`
  - `scripts/run_worker.py`

这条新通路已经具备：

- 创建任务
- 列出任务
- 查询单个任务
- 查看任务日志
- 执行单个任务
- 执行下一条待处理任务
- Worker 轮询、单实例锁、空闲退出
- 兼容旧注册链路的桥接执行

如果你想直接试新通路，优先参考：

- `docs/migration/2026-03-26-phase2-quickstart.md`

说明：

- 新任务 API 现在默认也受访问控制保护
- 浏览器侧可复用 Web UI 登录态
- 脚本或接口调试时，可通过 `X-Access-Password: <APP_ACCESS_PASSWORD>` 访问内部任务接口

## 环境要求

- Python 3.10+
- `uv`（推荐）或 `pip`

## 安装依赖

```bash
# 使用 uv（推荐）
uv sync

# 或使用 pip
pip install -r requirements.txt
```

## 环境变量配置

可选。复制 `.env.example` 为 `.env` 后按需修改:

```bash
cp .env.example .env
```

常用变量如下:

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `APP_HOST` | 监听主机 | `127.0.0.1` |
| `APP_PORT` | 监听端口 | `8000` |
| `APP_ACCESS_PASSWORD` | Web UI 访问密钥 | 必须显式设置强密码 |
| `APP_DATABASE_URL` | 数据库连接字符串 | `data/database.db` |
| `APP_DATA_DIR` | 数据目录 | `<repo>/data` |
| `APP_LOGS_DIR` | 日志目录 | `<repo>/logs` |

优先级:

`命令行参数 > 环境变量(.env) > 数据库设置 > 默认值`

## 启动 Web UI

```bash
# 默认启动（127.0.0.1:8000）
python webui.py

# 指定地址和端口
python webui.py --host 127.0.0.1 --port 8080

# 调试模式（热重载）
python webui.py --debug

# 设置 Web UI 访问密钥
python webui.py --access-password mypassword

# 组合参数
python webui.py --host 127.0.0.1 --port 8080 --access-password StrongPass123!
```

说明:

- `--access-password` 的优先级高于数据库中的密钥设置
- 该参数只对本次启动生效
- 非调试模式下，如果访问密码为空、过弱、或仍是 `admin123`，启动会直接失败
- 打包后的 exe 也支持这个参数

例如:

```bash
codex-console.exe --access-password mypassword
```

启动后访问:

[http://127.0.0.1:8000](http://127.0.0.1:8000)

健康检查:

- `GET /healthz` 返回 `{"ok": true}`

内部 API 访问:

- 旧页面的 `/api/*` 路由与任务 WebSocket 不再默认裸露
- 如果你是浏览器内使用，登录后的 cookie 会自动带上
- 如果你是脚本调用，可显式传 `X-Access-Password`

## 启动新 API / Worker

```bash
# API
set PYTHONPATH=%CD%
python scripts/run_api.py --host 127.0.0.1 --port 8000 --database-url sqlite:///./data/api.db

# Worker
set PYTHONPATH=%CD%
python scripts/run_worker.py --database-url sqlite:///./data/api.db --max-iterations 1 --poll-interval-seconds 1
```

调用任务 API 示例：

```bash
curl -X POST http://127.0.0.1:8000/tasks/register \
  -H "Content-Type: application/json" \
  -H "X-Access-Password: StrongPass123!" \
  -d "{\"email_service_type\":\"duck_mail\"}"
```

## Docker 部署

### 使用 docker-compose

```bash
export APP_ACCESS_PASSWORD='StrongPass123!'
docker-compose up -d
```

默认配置只把 Web UI 绑定到宿主机回环地址，适合通过 SSH 隧道、跳板机或内网反向代理访问。  
默认 **不会** 启动 noVNC / VNC。只有显式开启并设置 `VNC_PASSWORD` 时，才会启动桌面相关能力。

### 使用 docker run

```bash
docker run -d \
  -p 127.0.0.1:1455:1455 \
  -e APP_HOST=0.0.0.0 \
  -e APP_PORT=1455 \
  -e APP_ACCESS_PASSWORD=StrongPass123! \
  -e APP_DATABASE_URL=sqlite:////app/data/database.db \
  -e APP_DATA_DIR=/app/data \
  -e APP_LOGS_DIR=/app/logs \
  -e DISPLAY=:99 \
  -e ENABLE_VNC=0 \
  -e VNC_PORT=5900 \
  -e NOVNC_PORT=6080 \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/logs:/app/logs \
  --name codex-console \
  ghcr.io/<yourname>/codex-console:latest
```

说明:

- `APP_HOST`: 在 Docker 容器内建议保持 `0.0.0.0`，宿主机侧再通过 `127.0.0.1:1455:1455` 限制外部访问
- `APP_PORT`: 监听端口，默认 `1455`
- `APP_ACCESS_PASSWORD`: Web UI 访问密码，必须显式设置强密码
- `ENABLE_VNC`: 默认 `0`，未显式开启时不启动 noVNC / VNC
- `VNC_PASSWORD`: 当 `ENABLE_VNC=1` 时必须提供
- `DEBUG`: 设为 `1` 或 `true` 可开启调试模式
- `LOG_LEVEL`: 日志级别，例如 `info`、`debug`

注意:

`-v $(pwd)/data:/app/data` 和 `-v $(pwd)/logs:/app/logs` 很重要，这会把数据库、账号数据和日志持久化到宿主机。否则容器一重启，数据和日志都可能一起消失。

## 使用远程 PostgreSQL

```bash
export APP_DATABASE_URL="postgresql://user:password@host:5432/dbname"
python webui.py
```

也支持 `DATABASE_URL`，但优先级低于 `APP_DATABASE_URL`。

## 打包为可执行文件

```bash
# Windows
build.bat

# Linux/macOS
bash build.sh
```

Windows 打包完成后，默认会在 `dist/` 目录生成类似下面的文件:

```text
dist/codex-console-windows-X64.exe
```

如果打包失败，优先检查:

- Python 是否已加入 PATH
- 依赖是否安装完整
- 杀毒软件是否拦截了 PyInstaller 产物
- 终端里是否有更具体的报错日志

## 项目定位

这个仓库更适合作为:

- `dou-jiang/codex-console` 的继续重构版
- 当前注册链路的兼容维护版
- 旧 Web UI 与新任务架构并行过渡版
- 自己继续二次开发的基础版本

如果你准备公开发布，建议在仓库描述里明确写上:

`Refactored from dou-jiang/codex-console (original lineage: cnlimiter/codex-manager)`

这样既方便别人理解来源，也对上游作者更尊重。

## 仓库命名

当前仓库名:

`codex-console`

## 免责声明

本项目仅供学习、研究和技术交流使用，请遵守相关平台和服务条款，不要用于违规、滥用或非法用途。

因使用本项目产生的任何风险和后果，由使用者自行承担。


