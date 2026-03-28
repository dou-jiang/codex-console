# codex-console

基于 [cnlimiter/codex-manager](https://github.com/cnlimiter/codex-manager) 持续修复和维护的增强版本。

这个版本的目标很直接: 把近期 OpenAI 注册链路里那些“昨天还能跑，今天突然翻车”的坑补上，让注册、登录、拿 token、打包运行都更稳一点。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

## QQ群

- 交流群: https://qm.qq.com/q/ZTCKxawxeo

## 致谢

首先感谢上游项目作者 [cnlimiter](https://github.com/cnlimiter) 提供的优秀基础工程。

本仓库是在原项目思路和结构之上进行兼容性修复、流程调整和体验优化，适合作为一个“当前可用的修复维护版”继续使用。

## 这个分支修了什么

为适配当前注册链路，这个分支重点补了下面几个问题:

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

## 核心能力

- Web UI 管理注册任务和账号数据
- 支持批量注册、日志实时查看、基础任务管理
- 支持多种邮箱服务接码
- 支持 SQLite 和远程 PostgreSQL
- 支持打包为 Windows/Linux/macOS 可执行文件
- 更适配当前 OpenAI 注册与登录链路

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
| `APP_HOST` | 监听主机 | `0.0.0.0` |
| `APP_PORT` | 监听端口 | `8000` |
| `APP_ACCESS_PASSWORD` | Web UI 访问密钥 | `admin123` |
| `APP_DATABASE_URL` | 数据库连接字符串 | `data/database.db` |

优先级:

`命令行参数 > 环境变量(.env) > 数据库设置 > 默认值`

## 启动 Web UI

```bash
# 默认启动（127.0.0.1:8000）
python webui.py

# 指定地址和端口
python webui.py --host 0.0.0.0 --port 8080

# 调试模式（热重载）
python webui.py --debug

# 设置 Web UI 访问密钥
python webui.py --access-password mypassword

# 组合参数
python webui.py --host 0.0.0.0 --port 8080 --access-password mypassword
```

说明:

- `--access-password` 的优先级高于数据库中的密钥设置
- 该参数只对本次启动生效
- 打包后的 exe 也支持这个参数

例如:

```bash
codex-console.exe --access-password mypassword
```

启动后访问:

[http://127.0.0.1:8000](http://127.0.0.1:8000)

## Docker 部署

### 使用 docker-compose

```bash
docker-compose up -d
```

你可以在 `docker-compose.yml` 中修改环境变量，比如端口和访问密码。  
如果需要看“全自动绑卡”的可视化浏览器，打开：

- noVNC: `http://127.0.0.1:6080`

### 使用 docker run

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

说明:

- `WEBUI_HOST`: 监听主机，默认 `0.0.0.0`
- `WEBUI_PORT`: 监听端口，默认 `1455`
- `WEBUI_ACCESS_PASSWORD`: Web UI 访问密码
- `DEBUG`: 设为 `1` 或 `true` 可开启调试模式
- `LOG_LEVEL`: 日志级别，例如 `info`、`debug`

注意:

`-v $(pwd)/data:/app/data` 很重要，这会把数据库和账号数据持久化到宿主机。否则容器一重启，数据也可能跟着表演消失术。

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

- 原项目的修复增强版
- 当前注册链路的兼容维护版
- 自己二次开发的基础版本

如果你准备公开发布，建议在仓库描述里明确写上:

`Forked and fixed from cnlimiter/codex-manager`

这样既方便别人理解来源，也对上游作者更尊重。

## 仓库命名

当前仓库名:

`codex-console`

## 安全基线说明（新增）

- `/api/*` 与 `/api/ws/*` 已统一接入登录鉴权。
- 首次启动检测到默认口令或默认密钥时，会强制跳转到 `/setup-password` 完成改密。
- 支付相关 API Key 不再使用代码内硬编码默认值，需通过环境变量或配置显式提供。

## 数据库迁移（Alembic）

```bash
alembic revision --autogenerate -m "your_change"
alembic upgrade head
```

初始化与更多说明见：

- `alembic/README.md`

## 免责声明

本项目仅供学习、研究和技术交流使用，请遵守相关平台和服务条款，不要用于违规、滥用或非法用途。

因使用本项目产生的任何风险和后果，由使用者自行承担。
