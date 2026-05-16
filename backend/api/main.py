import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import settings
from backend.database import init_db
from backend.api.routers import stocks, news, analysis, predict, northbound, market

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
app.include_router(market.router, prefix="/api/market", tags=["market"])


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


def _start_market_heatmap_scheduler() -> None:
    def run_loop() -> None:
        tz = ZoneInfo("Asia/Shanghai")
        last_run_date = None
        while True:
            now = datetime.now(tz)
            is_trading_weekday = now.weekday() < 5
            should_run = is_trading_weekday and (now.hour > 15 or (now.hour == 15 and now.minute >= 20))
            if should_run and last_run_date != now.date():
                try:
                    logger.info("Refreshing scheduled market heatmap cache")
                    for period in ("1d", "5d", "20d"):
                        market.refresh_market_heatmap_cache(
                            period=period,
                            constituents_limit=0,
                            hydrate_limit=0,
                            history_hydrate_limit=0 if period == "1d" else 90,
                            allow_remote_history=False,
                        )
                    warm_result = market.warm_market_constituents_cache(max_sectors=90, limit=30)
                    logger.info("Scheduled market constituent warmup finished: %s", warm_result)
                    last_run_date = now.date()
                except Exception as exc:
                    logger.exception("Scheduled market heatmap refresh failed: %s", exc)
            time.sleep(600)

    threading.Thread(target=run_loop, name="market-heatmap-scheduler", daemon=True).start()


@app.on_event("startup")
def startup():
    init_db()
    _start_watchlist_scheduler()
    _start_market_heatmap_scheduler()


@app.get("/api/health")
def health():
    return {"status": "ok"}
