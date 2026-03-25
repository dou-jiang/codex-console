"""
FastAPI 应用主文件
轻量级 Web UI，支持注册、账号管理、设置
"""

import logging
import sys
import secrets
import hmac
import hashlib
from typing import Optional
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config.settings import get_settings
from .routes import api_router
from .routes.websocket import router as ws_router
from .task_manager import task_manager

logger = logging.getLogger(__name__)

# 获取项目根目录
# PyInstaller 打包后静态资源在 sys._MEIPASS，开发时在源码根目录
if getattr(sys, 'frozen', False):
    _RESOURCE_ROOT = Path(sys._MEIPASS)
else:
    _RESOURCE_ROOT = Path(__file__).parent.parent.parent

# 静态文件和模板目录
STATIC_DIR = _RESOURCE_ROOT / "static"
TEMPLATES_DIR = _RESOURCE_ROOT / "templates"


def _build_static_asset_version(static_dir: Path) -> str:
    """基于静态文件最后修改时间生成版本号，避免部署后浏览器继续使用旧缓存。"""
    latest_mtime = 0
    if static_dir.exists():
        for path in static_dir.rglob("*"):
            if path.is_file():
                latest_mtime = max(latest_mtime, int(path.stat().st_mtime))
    return str(latest_mtime or 1)


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例"""
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="OpenAI/Codex CLI 自动注册系统 Web UI",
        docs_url="/api/docs" if settings.debug else None,
        redoc_url="/api/redoc" if settings.debug else None,
    )

    # CORS 中间件
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 挂载静态文件
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
        logger.info(f"静态文件目录: {STATIC_DIR}")
    else:
        # 创建静态目录
        STATIC_DIR.mkdir(parents=True, exist_ok=True)
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
        logger.info(f"创建静态文件目录: {STATIC_DIR}")

    # 创建模板目录
    if not TEMPLATES_DIR.exists():
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(f"创建模板目录: {TEMPLATES_DIR}")

    # 注册 API 路由
    app.include_router(api_router, prefix="/api")

    # 注册 WebSocket 路由
    app.include_router(ws_router, prefix="/api")

    # 模板引擎
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["static_version"] = _build_static_asset_version(STATIC_DIR)

    def _auth_token(password: str) -> str:
        secret = get_settings().webui_secret_key.get_secret_value().encode("utf-8")
        return hmac.new(secret, password.encode("utf-8"), hashlib.sha256).hexdigest()

    def _is_authenticated(request: Request) -> bool:
        cookie = request.cookies.get("webui_auth")
        expected = _auth_token(get_settings().webui_access_password.get_secret_value())
        return bool(cookie) and secrets.compare_digest(cookie, expected)

    def _redirect_to_login(request: Request) -> RedirectResponse:
        return RedirectResponse(url=f"/login?next={request.url.path}", status_code=302)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: Optional[str] = "/"):
        """登录页面"""
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "error": "", "next": next or "/"}
        )

    @app.post("/login")
    async def login_submit(request: Request, password: str = Form(...), next: Optional[str] = "/"):
        """处理登录提交"""
        expected = get_settings().webui_access_password.get_secret_value()
        if not secrets.compare_digest(password, expected):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"request": request, "error": "密码错误", "next": next or "/"},
                status_code=401
            )

        response = RedirectResponse(url=next or "/", status_code=302)
        response.set_cookie("webui_auth", _auth_token(expected), httponly=True, samesite="lax")
        return response

    @app.get("/logout")
    async def logout(request: Request, next: Optional[str] = "/login"):
        """退出登录"""
        response = RedirectResponse(url=next or "/login", status_code=302)
        response.delete_cookie("webui_auth")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        """首页 - 注册页面"""
        if not _is_authenticated(request):
            return _redirect_to_login(request)
        return templates.TemplateResponse(request, "index.html", {"request": request})

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_page(request: Request):
        """账号管理页面"""
        if not _is_authenticated(request):
            return _redirect_to_login(request)
        return templates.TemplateResponse(request, "accounts.html", {"request": request})

    @app.get("/email-services", response_class=HTMLResponse)
    async def email_services_page(request: Request):
        """邮箱服务管理页面"""
        if not _is_authenticated(request):
            return _redirect_to_login(request)
        return templates.TemplateResponse(request, "email_services.html", {"request": request})

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        """设置页面"""
        if not _is_authenticated(request):
            return _redirect_to_login(request)
        return templates.TemplateResponse(request, "settings.html", {"request": request})

    @app.get("/payment", response_class=HTMLResponse)
    async def payment_page(request: Request):
        """支付页面"""
        return templates.TemplateResponse(request, "payment.html", {"request": request})

    @app.on_event("startup")
    async def startup_event():
        """应用启动事件"""
        import asyncio
        from ..database.init_db import initialize_database

        # 确保数据库已初始化（reload 模式下子进程也需要初始化）
        try:
            initialize_database()
        except Exception as e:
            logger.warning(f"数据库初始化: {e}")

        # 设置 TaskManager 的事件循环
        loop = asyncio.get_event_loop()
        task_manager.set_loop(loop)

        logger.info("=" * 50)
        logger.info(f"{settings.app_name} v{settings.app_version} 启动中，程序正在伸懒腰...")
        logger.info(f"调试模式: {settings.debug}")
        logger.info(f"数据库连接已接好线: {settings.database_url}")
        logger.info("=" * 50)

    @app.on_event("shutdown")
    async def shutdown_event():
        """应用关闭事件"""
        logger.info("应用关闭，今天先收摊啦")

    return app


# 创建全局应用实例
app = create_app()
