"""FastAPI app entrypoint."""

import logging
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config.project_notice import PROJECT_NOTICE
from ..config.settings import get_settings, update_settings
from .auth import (
    build_auth_token,
    build_login_redirect,
    build_setup_password_redirect,
    is_default_security_config_active,
    is_request_authenticated,
    require_api_auth,
)
from .routes import api_router
from .routes.websocket import router as ws_router
from .scheduler import scheduled_registration_service
from .task_manager import task_manager

logger = logging.getLogger(__name__)
auto_registration_coordinator = None

if getattr(sys, "frozen", False):
    _RESOURCE_ROOT = Path(sys._MEIPASS)
else:
    _RESOURCE_ROOT = Path(__file__).parent.parent.parent

STATIC_DIR = _RESOURCE_ROOT / "static"
TEMPLATES_DIR = _RESOURCE_ROOT / "templates"


def _build_static_asset_version(static_dir: Path) -> str:
    latest_mtime = 0
    if static_dir.exists():
        for path in static_dir.rglob("*"):
            if path.is_file():
                latest_mtime = max(latest_mtime, int(path.stat().st_mtime))
    return str(latest_mtime or 1)


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio

        from ..core.auto_registration import (
            AutoRegistrationCoordinator,
            register_auto_registration_coordinator,
        )
        from ..core.db_logs import cleanup_database_logs
        from ..database.init_db import initialize_database
        from .auto_quick_refresh_scheduler import auto_quick_refresh_scheduler
        from .routes.registration import run_auto_registration_batch
        from .selfcheck_scheduler import selfcheck_scheduler

        try:
            initialize_database()
        except Exception as exc:
            logger.warning("Database init failed: %s", exc)

        loop = asyncio.get_running_loop()
        task_manager.set_loop(loop)

        global auto_registration_coordinator
        auto_registration_coordinator = AutoRegistrationCoordinator(
            trigger_callback=run_auto_registration_batch,
        )
        register_auto_registration_coordinator(auto_registration_coordinator)
        auto_registration_coordinator.start()

        async def run_log_cleanup_once() -> None:
            try:
                result = await asyncio.to_thread(cleanup_database_logs)
                logger.info(
                    "Log cleanup done: deleted=%s remaining=%s",
                    result.get("deleted_total", 0),
                    result.get("remaining", 0),
                )
            except Exception as exc:
                logger.warning("Log cleanup failed: %s", exc)

        async def periodic_log_cleanup() -> None:
            while True:
                try:
                    await asyncio.sleep(3600)
                    await run_log_cleanup_once()
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.warning("Periodic log cleanup failed: %s", exc)

        await run_log_cleanup_once()
        app.state.log_cleanup_task = asyncio.create_task(periodic_log_cleanup())
        await scheduled_registration_service.start()
        app.state.scheduled_registration_service = scheduled_registration_service
        app.state.auto_quick_refresh_task = asyncio.create_task(auto_quick_refresh_scheduler.run_loop())
        app.state.selfcheck_scheduler_task = asyncio.create_task(selfcheck_scheduler.run_loop())

        logger.info("=" * 50)
        logger.info("%s v%s starting", settings.app_name, settings.app_version)
        logger.info("Debug mode: %s", settings.debug)
        logger.info("Database URL: %s", settings.database_url)
        if is_default_security_config_active():
            logger.warning("Default security config detected, visit /setup-password")
        logger.info("=" * 50)

        try:
            yield
        finally:
            async def _cancel_and_wait(task):
                if not task:
                    return
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.warning("Background task shutdown failed: %s", exc)

            cleanup_task = getattr(app.state, "log_cleanup_task", None)
            await _cancel_and_wait(cleanup_task)

            scheduler_service = getattr(app.state, "scheduled_registration_service", None)
            if scheduler_service:
                await scheduler_service.stop()

            auto_quick_refresh_task = getattr(app.state, "auto_quick_refresh_task", None)
            await _cancel_and_wait(auto_quick_refresh_task)

            selfcheck_scheduler_task = getattr(app.state, "selfcheck_scheduler_task", None)
            await _cancel_and_wait(selfcheck_scheduler_task)

            if auto_registration_coordinator is not None:
                await auto_registration_coordinator.stop()
                register_auto_registration_coordinator(None)
                auto_registration_coordinator = None

            logger.info("Application stopped")

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="OpenAI/Codex CLI Web UI",
        docs_url="/api/docs" if settings.debug else None,
        redoc_url="/api/redoc" if settings.debug else None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    else:
        STATIC_DIR.mkdir(parents=True, exist_ok=True)
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    if not TEMPLATES_DIR.exists():
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    app.include_router(api_router, prefix="/api", dependencies=[Depends(require_api_auth)])
    app.include_router(ws_router, prefix="/api")

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["static_version"] = _build_static_asset_version(STATIC_DIR)
    templates.env.globals["project_notice"] = PROJECT_NOTICE

    def _render_template(
        request: Request,
        name: str,
        context: Optional[Dict[str, Any]] = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        template_context: Dict[str, Any] = {"request": request}
        if context:
            template_context.update(context)

        try:
            return templates.TemplateResponse(
                request=request,
                name=name,
                context=template_context,
                status_code=status_code,
            )
        except TypeError:
            return templates.TemplateResponse(name, template_context, status_code=status_code)

    def _guard_page_request(request: Request) -> Optional[RedirectResponse]:
        if is_default_security_config_active():
            return build_setup_password_redirect()
        if not is_request_authenticated(request):
            return build_login_redirect(request)
        return None

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, next: Optional[str] = "/", notice: Optional[str] = ""):
        if is_default_security_config_active():
            return build_setup_password_redirect()
        return _render_template(
            request,
            "login.html",
            {"error": "", "next": next or "/", "notice": notice or ""},
        )

    @app.post("/login")
    async def login_submit(request: Request, password: str = Form(...), next: Optional[str] = "/"):
        if is_default_security_config_active():
            return build_setup_password_redirect()

        expected = get_settings().webui_access_password.get_secret_value()
        if not secrets.compare_digest(password, expected):
            return _render_template(
                request,
                "login.html",
                {"error": "Invalid password", "next": next or "/", "notice": ""},
                status_code=401,
            )

        response = RedirectResponse(url=next or "/", status_code=302)
        auth_cookie = build_auth_token(
            expected,
            get_settings().webui_secret_key.get_secret_value(),
        )
        response.set_cookie("webui_auth", auth_cookie, httponly=True, samesite="lax")
        return response

    @app.get("/setup-password", response_class=HTMLResponse)
    async def setup_password_page(request: Request):
        if not is_default_security_config_active():
            return RedirectResponse(url="/login", status_code=302)
        return _render_template(
            request,
            "setup_password.html",
            {"error": "", "message": ""},
        )

    @app.post("/setup-password", response_class=HTMLResponse)
    async def setup_password_submit(
        request: Request,
        old_password: str = Form(...),
        new_password: str = Form(...),
        confirm_password: str = Form(...),
    ):
        if not is_default_security_config_active():
            return RedirectResponse(url="/login", status_code=302)

        expected = get_settings().webui_access_password.get_secret_value()
        if not secrets.compare_digest(str(old_password or ""), str(expected or "")):
            return _render_template(
                request,
                "setup_password.html",
                {"error": "Current password is incorrect", "message": ""},
                status_code=400,
            )

        new_value = str(new_password or "").strip()
        confirm_value = str(confirm_password or "").strip()
        if len(new_value) < 8:
            return _render_template(
                request,
                "setup_password.html",
                {"error": "Password must be at least 8 characters", "message": ""},
                status_code=400,
            )
        if new_value != confirm_value:
            return _render_template(
                request,
                "setup_password.html",
                {"error": "Passwords do not match", "message": ""},
                status_code=400,
            )
        if new_value == "admin123":
            return _render_template(
                request,
                "setup_password.html",
                {"error": "Default password is not allowed", "message": ""},
                status_code=400,
            )

        update_settings(
            webui_access_password=new_value,
            webui_secret_key=secrets.token_urlsafe(48),
        )
        response = RedirectResponse(
            url="/login?notice=Password updated, please sign in again",
            status_code=302,
        )
        response.delete_cookie("webui_auth")
        return response

    @app.get("/logout")
    async def logout(request: Request, next: Optional[str] = "/login"):
        response = RedirectResponse(url=next or "/login", status_code=302)
        response.delete_cookie("webui_auth")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        redirect_response = _guard_page_request(request)
        if redirect_response:
            return redirect_response
        return _render_template(request, "index.html")

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_page(request: Request):
        redirect_response = _guard_page_request(request)
        if redirect_response:
            return redirect_response
        return _render_template(request, "accounts.html")

    @app.get("/accounts-overview", response_class=HTMLResponse)
    async def accounts_overview_page(request: Request):
        redirect_response = _guard_page_request(request)
        if redirect_response:
            return redirect_response
        return _render_template(request, "accounts_overview.html")

    @app.get("/email-services", response_class=HTMLResponse)
    async def email_services_page(request: Request):
        redirect_response = _guard_page_request(request)
        if redirect_response:
            return redirect_response
        return _render_template(request, "email_services.html")

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        redirect_response = _guard_page_request(request)
        if redirect_response:
            return redirect_response
        return _render_template(request, "settings.html")

    @app.get("/payment", response_class=HTMLResponse)
    async def payment_page(request: Request):
        redirect_response = _guard_page_request(request)
        if redirect_response:
            return redirect_response
        return _render_template(request, "payment.html")

    @app.get("/card-pool", response_class=HTMLResponse)
    async def card_pool_page(request: Request):
        redirect_response = _guard_page_request(request)
        if redirect_response:
            return redirect_response
        return _render_template(request, "card_pool.html")

    @app.get("/auto-team", response_class=HTMLResponse)
    async def auto_team_page(request: Request):
        redirect_response = _guard_page_request(request)
        if redirect_response:
            return redirect_response
        return _render_template(request, "auto_team.html")

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request):
        redirect_response = _guard_page_request(request)
        if redirect_response:
            return redirect_response
        return _render_template(request, "logs.html")

    @app.get("/selfcheck", response_class=HTMLResponse)
    async def selfcheck_page(request: Request):
        redirect_response = _guard_page_request(request)
        if redirect_response:
            return redirect_response
        return _render_template(request, "selfcheck.html")

    return app


app = create_app()
