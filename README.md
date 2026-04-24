# codex-console

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-v1.1.2-2563eb.svg)](https://github.com/dou-jiang/codex-console/releases)

基于 [cnlimiter/codex-manager](https://github.com/cnlimiter/codex-manager) 持续修复、整合并增强的维护版本，重点补齐当前 OpenAI / Codex CLI 注册链路、任务调度、邮箱服务、支付绑定、系统自检与 Web 管理能力。

项目地址：

- GitHub: [https://github.com/dou-jiang/codex-console](https://github.com/dou-jiang/codex-console)
- Blog: [https://blog.cysq8.cn/](https://blog.cysq8.cn/)

## v1.1.2 更新内容

`v1.1.2` 主要整合并落地了多组 PR 与后续修复，当前版本重点包括：

- 统一鉴权与安全基线：新增 `auth.py`、首次改密页、API 与 WebSocket 鉴权统一收口。
- 自动注册链路增强：补齐自动补货、库存监控、批次取消感知、注册重试与 PR60 anyauto V2 回退链路。
- 系统自检与修复中心：新增自检路由、页面、调度器和运行记录。
- 统一任务中心：将注册、支付、自检、Auto Team 等任务状态统一管理。
- 周期任务调度：支持计划任务创建、启停、立即执行与前端管理。
- New-API 上传：支持独立服务配置、测试、单账号和批量上传，以及注册成功后自动上传。
- Auto Team 模块：新增后端接口与前端管理页面。
- 卡池与绑卡能力：增加卡池管理、上游对接与自动绑卡支持。
- 邮箱服务扩展：补齐 CloudMail、LuckMail、YYDS Mail 等邮箱服务，并保留原有邮件链路。
- 数据库迁移体系：引入 Alembic，补齐迁移目录与说明。
- 测试与 CI：补充多组接口、任务、注册与上传相关测试，并加入测试工作流。

## 核心能力

- Web UI 管理注册、支付、账号、自检、日志、卡池、Auto Team 等功能。
- 支持单任务、批量任务、自动补货、定时任务、任务暂停/继续/取消/重试。
- 支持多种邮箱服务：Tempmail、YYDS Mail、Outlook、CloudMail、LuckMail、DuckMail、Freemail、IMAP、Temp-Mail 自部署等。
- 支持支付链接、绑卡任务、卡池管理、账户快照保留、审计日志。
- 支持 CPA、Sub2API、Team Manager、New-API 等上传链路。
- 支持 SQLite 与 PostgreSQL。
- 支持 Windows / Linux / macOS 打包。

## Blog 说明

维护者会在 Blog 持续更新以下内容：

- 部署教程与环境配置说明
- 版本更新日志与 Release 说明
- 常见问题、报错排查和修复记录
- 邮箱服务、上传服务、任务调度等功能的使用说明
- 与上游变化相关的兼容性调整说明

访问地址：

- [https://blog.cysq8.cn/](https://blog.cysq8.cn/)

## 赞助支持

如果这个项目对你有帮助，欢迎赞助支持项目继续维护与更新。

<table>
  <tr>
    <td align="center">
      <strong>微信赞助</strong><br />
      <img src="docs/assets/wechat-pay.png" alt="微信赞助二维码" width="260" />
    </td>
    <td align="center">
      <strong>支付宝赞助</strong><br />
      <img src="docs/assets/alipay-pay.png" alt="支付宝赞助二维码" width="260" />
    </td>
  </tr>
</table>

## 环境要求

- Python 3.10+
- 推荐使用 `uv`

## 安装依赖

```bash
# 推荐
uv sync

# 或使用 pip
pip install -r requirements.txt
```

## 快速开始

```bash
# 激活环境
conda activate codex-console-commission

# 默认启动
python webui.py

# 指定监听地址和端口
python webui.py --host 0.0.0.0 --port 8083 --debug

# 调试模式
python webui.py --debug

# 设置 Web UI 访问密码
python webui.py --access-password your_password
```

启动后访问：

- [http://127.0.0.1:8000](http://127.0.0.1:8000)

## 启动管理脚本

项目内置了统一启动脚本，支持：

- `start` / `restart` / `stop` / `status` / `logs`
- 指定端口启动
- 端口占用检测与推荐新端口
- **默认端口冲突时自动切换到下一个可用端口并继续启动**
- debug 模式启动
- 启动前自动 `git pull/fetch+merge` 更新代码
- 启动前自动构建/依赖同步
- 启动前自动校验运行依赖，避免启动后才因缺包崩溃
- 自动打开浏览器
- 后台守护 + 健康检查失败自动重启
- 可选择使用 **conda 环境** 启动；若系统未安装 conda，会提示后直接停止

### 本次调整整理（2026-04-21）

本次围绕 `scripts/manage_webui.py`、`scripts/manage-webui.ps1`、`scripts/manage-webui.bat` 做了以下增强：

1. **修复 Windows `.bat` 参数透传问题**
   - 之前 `manage-webui.bat` 直接把参数透传给 `.ps1`，会导致 `--use-conda` 这类 GNU 风格参数被 PowerShell 误解析。
   - 现在 `.bat` 入口已改为**直接调用 Python 驱动**，因此以下写法可以正常使用：
     ```bat
     .\scripts\manage-webui.bat start --use-conda --conda-env codex-console-commission --open-browser
     ```

2. **新增可选 conda 启动模式**
   - 支持通过 `--use-conda --conda-env <env>` 指定使用 conda 环境启动。
   - 若系统未安装 conda，会直接提示安装 Miniconda/Anaconda 后退出。
   - 若指定环境不存在，也会直接报错退出。
   - 当前实现会**直接使用目标 conda 环境内的 Python 可执行文件**启动，而不是依赖 `conda run ... python` 的间接方式，进程状态更稳定，PID 跟踪更准确。

3. **补充运行依赖校验**
   - 即使使用了 `--skip-build`，脚本也会先校验目标运行环境中是否存在核心依赖（如 `sqlalchemy`、`fastapi`、`uvicorn`、`jinja2`、`aiosqlite`、`curl_cffi` 等）。
   - 如果依赖缺失，会在启动前直接报错，而不是等 WebUI 起到一半才在日志里崩溃。

4. **新增自动换端口启动**
   - 若指定端口已被占用，脚本会自动在后续端口中寻找空闲端口并继续启动。
   - 例如占用 `8000` 时，脚本会提示：
     ```text
     [WARN] Port 8000 is occupied by PID xxxx; automatically switching to available port 8001.
     ```

5. **新增 `logs` 子命令**
   - 可直接输出某个端口对应的最近 `stdout/stderr` 日志，快速排错。
   - 示例：
     ```bat
     .\scripts\manage-webui.bat logs --port 8000 --lines 120
     ```
   - 同时修复了 Windows 控制台下某些日志字符导致的 `UnicodeEncodeError`。

6. **清理并兼容历史残留状态**
   - 启动前会自动清理对应端口的陈旧 PID/状态文件。
   - 遇到历史启动残留时，能更平滑地恢复，而不是直接卡在旧 PID 状态上。

### Windows

PowerShell：

```powershell
# 默认启动
.\scripts\manage-webui.ps1 start

# 指定端口 + debug
.\scripts\manage-webui.ps1 start -Port 8083 -DebugMode

# 重启
.\scripts\manage-webui.ps1 restart -Port 8083

# 停止
.\scripts\manage-webui.ps1 stop -Port 8083

# 查看状态
.\scripts\manage-webui.ps1 status -Port 8083

# 查看最近日志
.\scripts\manage-webui.ps1 logs -Port 8083 -Lines 120

# 启动后自动打开浏览器
.\scripts\manage-webui.ps1 start -Port 8083 -OpenBrowser

# 开启守护模式（健康检查失败自动重启）
.\scripts\manage-webui.ps1 start -Port 8083 -Guard -OpenBrowser

# 指定以 conda 环境启动
.\scripts\manage-webui.ps1 start -Port 8083 -UseConda -CondaEnv codex-console-commission
```

批处理入口：

```bat
.\scripts\manage-webui.bat start --port 8083 --open-browser --guard --use-conda --conda-env codex-console-commission
```

### Linux

先赋权：

```bash
chmod +x ./scripts/manage-webui.sh
```

示例：

```bash
# 默认启动
./scripts/manage-webui.sh start

# 指定端口 + debug
./scripts/manage-webui.sh start --port 8083 --debug

# 重启
./scripts/manage-webui.sh restart --port 8083

# 停止
./scripts/manage-webui.sh stop --port 8083

# 查看状态
./scripts/manage-webui.sh status --port 8083

# 查看最近日志
./scripts/manage-webui.sh logs --port 8083 --lines 120

# 启动后自动打开浏览器
./scripts/manage-webui.sh start --port 8083 --open-browser

# 开启守护模式（健康检查失败自动重启）
./scripts/manage-webui.sh start --port 8083 --guard --open-browser

# 指定以 conda 环境启动
./scripts/manage-webui.sh start --port 8083 --use-conda --conda-env codex-console-commission
```

### 常用参数

```text
--port <端口>                   指定监听端口，默认 8000
--host <地址>                   指定监听地址，默认 0.0.0.0
--debug                         debug 模式启动
--skip-update                   跳过 git 更新
--skip-build                    跳过构建/依赖同步
--open-browser                  启动成功后自动打开浏览器
--guard                         启用后台守护与健康检查失败自动重启
--use-conda                     使用 conda 环境启动
--conda-env <环境名>            指定 conda 环境名；默认使用项目目录名
--health-url <URL模板>          自定义健康检查地址，默认 http://{host}:{port}/login
--health-interval <秒>          健康检查间隔，默认 15
--health-timeout <秒>           健康检查超时，默认 5
--health-fail-threshold <次数>  连续失败多少次后自动重启，默认 3
--remote <远程名> --branch <分支>
                               指定从哪个远程/分支更新代码
--python <解释器路径>           指定 Python 可执行文件
--lines <行数>                  logs 子命令读取的日志行数，默认 80
```

### Conda 启动说明

如果使用 `--use-conda`：

- 脚本会先检查本机是否已安装 conda
- 若 **未安装 conda**，会直接提示安装 Miniconda/Anaconda 后退出
- 若 conda 已安装，但指定环境不存在，也会提示后退出
- 构建和运行都会切到对应 conda 环境中执行

例如：

```bash
./scripts/manage-webui.sh start --use-conda --conda-env codex-console-commission
```

```powershell
.\scripts\manage-webui.ps1 start -UseConda -CondaEnv codex-console-commission
```

### 运行状态文件

脚本会把 PID、状态和日志写到：

```text
logs/runtime/webui-<port>.pid
logs/runtime/webui-<port>.json
logs/runtime/webui-<port>.stdout.log
logs/runtime/webui-<port>.stderr.log
```

### 守护模式说明

开启 `--guard` 后：

- 管理脚本会后台拉起一个 watchdog 进程
- watchdog 周期性请求健康检查 URL
- 若服务进程退出，或连续多次健康检查失败，会自动重启 WebUI
- `status` 可查看当前 watchdog / webui PID、最近健康状态、重启次数
- `logs` 可直接查看当前端口最近 stdout/stderr 输出，便于快速排错

默认健康检查地址为：

```text
http://127.0.0.1:<port>/login
```

也可自定义，例如：

```bash
./scripts/manage-webui.sh start --port 8083 --guard --health-url "http://{host}:{port}/api/settings"
```

## 常用环境变量

复制 `.env.example` 为 `.env` 后按需修改：

```bash
cp .env.example .env
```

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `APP_HOST` | 监听地址 | `0.0.0.0` |
| `APP_PORT` | 监听端口 | `8000` |
| `APP_ACCESS_PASSWORD` | Web UI 访问密码 | `admin123` |
| `APP_DATABASE_URL` | 数据库连接 | `data/database.db` |

## Docker 部署

### docker-compose

```bash
docker-compose up -d
```

如果需要查看可视化浏览器：

- noVNC: `http://127.0.0.1:6080`

### docker run

```bash
docker run -d \
  -p 1455:1455 \
  -p 6080:6080 \
  -e DISPLAY=:99 \
  -e ENABLE_VNC=1 \
  -e VNC_PORT=5900 \
  -e NOVNC_PORT=6080 \
  -e WEBUI_HOST=0.0.0.0 \
  -e WEBUI_PORT=1455 \
  -e WEBUI_ACCESS_PASSWORD=your_secure_password \
  -v $(pwd)/data:/app/data \
  --name codex-console \
  ghcr.io/<yourname>/codex-console:latest
```

## 数据库迁移

```bash
alembic revision --autogenerate -m "your_change"
alembic upgrade head
```

更多说明见：

- [alembic/README.md](alembic/README.md)

## 致谢

感谢上游项目作者 [cnlimiter](https://github.com/cnlimiter) 提供的基础工程与思路。

## 免责声明

本项目仅供学习、研究和技术交流使用，请遵守相关平台和服务条款，不要用于违规、滥用或非法用途。
因使用本项目产生的任何风险与后果，由使用者自行承担。
