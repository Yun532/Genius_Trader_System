import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import settings
from backend.database import init_db
from backend.api.routers import stocks, news, analysis, predict, northbound

logger = logging.getLogger(__name__)
app = FastAPI(title="PokieTicker A-share", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:7777", "http://127.0.0.1:7777"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

app.include_router(stocks.router, prefix="/api/stocks", tags=["stocks"])
app.include_router(news.router, prefix="/api/news", tags=["news"])
app.include_router(analysis.router, prefix="/api/analysis", tags=["analysis"])
app.include_router(predict.router, prefix="/api/predict", tags=["predict"])
app.include_router(northbound.router, prefix="/api/northbound", tags=["northbound"])


def _start_watchlist_scheduler() -> None:
    if not settings.auto_sync_watchlist:
        return

    def run_loop() -> None:
        tz = ZoneInfo("Asia/Shanghai")
        last_run_date = None
        while True:
            now = datetime.now(tz)
            should_run = (
                now.hour > settings.auto_sync_hour
                or (now.hour == settings.auto_sync_hour and now.minute >= settings.auto_sync_minute)
            )
            if should_run and last_run_date != now.date():
                try:
                    logger.info("Starting scheduled A-share watchlist sync")
                    stocks.sync_watchlist_job(stale_only=True, max_age_days=settings.auto_sync_stale_days)
                    last_run_date = now.date()
                except Exception as exc:
                    logger.exception("Scheduled watchlist sync failed: %s", exc)
            time.sleep(300)

    threading.Thread(target=run_loop, name="watchlist-sync-scheduler", daemon=True).start()


@app.on_event("startup")
def startup():
    init_db()
    _start_watchlist_scheduler()


@app.get("/api/health")
def health():
    return {"status": "ok"}
