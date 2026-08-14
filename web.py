from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import app as api_app, engine, Base
from flutterwave import router as flutterwave_router
from picks import router as picks_router
from performance import router as performance_router
from admin import router as admin_router

import sports_models

from automation import start_scheduler, stop_scheduler


api_app.include_router(flutterwave_router)
api_app.include_router(picks_router)
api_app.include_router(performance_router)
api_app.include_router(admin_router)

Base.metadata.create_all(bind=engine)


web = FastAPI(title="CS/HTFT Platform")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

STATIC_DIR.mkdir(exist_ok=True)

web.mount("/api", api_app)
web.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@web.on_event("startup")
def on_startup():
    start_scheduler()


@web.on_event("shutdown")
def on_shutdown():
    stop_scheduler()


@web.get("/")
def root():
    return RedirectResponse(url="/app")


@web.get("/app", response_class=HTMLResponse)
def app_page():
    index_file = STATIC_DIR / "index.html"

    if not index_file.exists():
        return "<h1>Missing frontend file</h1><p>Create backend/static/index.html</p>"

    return index_file.read_text(encoding="utf-8")