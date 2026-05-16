import logging
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from backend.ashare.client import fetch_industry_boards, fetch_industry_board_ohlc, industry_board_name
from backend.api.routers.stocks import (
    _cached_sector_constituents,
    _fallback_local_sector_companies,
    _insert_industry_board_ohlc,
    _sector_company_candidates,
    _store_sector_constituents,
)
from backend.database import get_conn

logger = logging.getLogger(__name__)
router = APIRouter()

PERIOD_DAYS = {
    "1d": 1,
    "5d": 5,
    "20d": 20,
}

MIN_COMPLETE_SECTOR_COUNT = 80

SECTOR_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("金融地产", ("银行", "证券", "保险", "多元金融", "房地产", "房产", "物业")),
    ("科技TMT", ("软件", "互联网", "游戏", "通信", "半导体", "芯片", "电子", "元件", "消费电子", "计算机", "传媒", "光学")),
    ("新能源车", ("电池", "光伏", "风电", "能源金属", "电机", "汽车", "整车", "零部件", "充电", "储能")),
    ("医药健康", ("医药", "医疗", "生物", "制药", "化学制药", "中药", "医疗器械", "美容护理")),
    ("大消费", ("食品", "饮料", "白酒", "啤酒", "乳品", "家电", "商贸", "零售", "旅游", "酒店", "餐饮", "纺织", "服装", "农牧", "养殖")),
    ("高端制造", ("机械", "设备", "仪器", "自动化", "电网", "电力设备", "专用设备", "通用设备", "工业母机", "机器人")),
    ("周期资源", ("煤炭", "石油", "燃气", "化工", "钢铁", "有色", "贵金属", "小金属", "建材", "水泥", "玻璃", "采掘")),
    ("基建公用", ("电力", "公用", "环保", "水务", "交通", "港口", "航运", "铁路", "机场", "建筑", "工程", "物流")),
    ("军工安全", ("军工", "航天", "航空", "船舶", "兵器", "国防", "安全")),
]


def _today() -> str:
    return datetime.now().date().isoformat()


def _latest_market_date() -> str:
    today = datetime.now().date()
    weekday = today.weekday()
    if weekday == 5:
        return (today - timedelta(days=1)).isoformat()
    if weekday == 6:
        return (today - timedelta(days=2)).isoformat()
    return today.isoformat()


def _float_or_none(value) -> Optional[float]:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _round_pct(value) -> Optional[float]:
    number = _float_or_none(value)
    return round(number, 2) if number is not None else None


def _cached_board_rows(board_name: str, requested_date: str, limit: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT *
           FROM industry_board_ohlc
           WHERE board_name = ? AND date <= ?
           ORDER BY date DESC
           LIMIT ?""",
        (board_name, requested_date, limit),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _fallback_board_snapshots(requested_date: str, limit: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT b.board_name, b.date, b.change_pct, b.amount, b.volume
           FROM industry_board_ohlc b
           JOIN (
               SELECT board_name, MAX(date) AS date
               FROM industry_board_ohlc
               WHERE date <= ?
               GROUP BY board_name
           ) latest
             ON latest.board_name = b.board_name AND latest.date = b.date
           ORDER BY COALESCE(b.amount, 0) DESC, b.board_name
           LIMIT ?""",
        (requested_date, limit),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            """SELECT DISTINCT board_name
               FROM stock_industry_map
               WHERE board_name IS NOT NULL AND board_name <> ''
               ORDER BY board_name
               LIMIT ?""",
            (limit,),
        ).fetchall()
    conn.close()
    return [
        {
            "board_name": row["board_name"],
            "board_code": "",
            "change_pct": row["change_pct"] if "change_pct" in row.keys() else None,
            "amount": row["amount"] if "amount" in row.keys() else None,
            "volume": row["volume"] if "volume" in row.keys() else None,
            "source": "local_cache",
        }
        for row in rows
        if row["board_name"]
    ]


def _cached_heatmap_sector_snapshots(period: str, limit: int, min_count: int = MIN_COMPLETE_SECTOR_COUNT) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT payload_json
           FROM market_heatmap_cache
           WHERE period = ?
           ORDER BY date DESC, generated_at DESC
           LIMIT 20""",
        (period,),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            """SELECT payload_json
               FROM market_heatmap_cache
               ORDER BY date DESC, generated_at DESC
               LIMIT 20"""
        ).fetchall()
    conn.close()
    candidates: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        sectors = payload.get("sectors") or []
        if len(sectors) >= min_count:
            candidates = sectors
            break
    if not candidates:
        return []
    return [
        {
            "board_name": item.get("board_name"),
            "board_code": item.get("board_code") or "",
            "change_pct": item.get("change_pct") if item.get("period") == period else None,
            "amount": item.get("amount"),
            "volume": item.get("volume"),
            "market_cap": 0.0,
            "leader_name": (item.get("leader") or {}).get("name"),
            "leader_change_pct": (item.get("leader") or {}).get("change_pct"),
            "source": f"market_heatmap_cache:{period}",
        }
        for item in candidates[:limit]
        if item.get("board_name")
    ]


def _merge_snapshot_lists(base: list[dict], overlay: list[dict], limit: int) -> list[dict]:
    merged: dict[str, dict] = {}
    order: list[str] = []
    for item in base:
        board = industry_board_name(item.get("board_name") or "")
        if not board:
            continue
        if board not in merged:
            order.append(board)
        merged[board] = {**item, "board_name": board}
    for item in overlay:
        board = industry_board_name(item.get("board_name") or "")
        if not board:
            continue
        if board not in merged:
            order.append(board)
        merged[board] = {**merged.get(board, {}), **item, "board_name": board}
    return [merged[board] for board in order[:limit]]


def _store_live_board_snapshots(snapshots: list[dict], requested_date: str) -> int:
    rows = []
    for snapshot in snapshots:
        source = str(snapshot.get("source") or "")
        if source.startswith("market_heatmap_cache") or source == "local_cache":
            continue
        board = industry_board_name(snapshot.get("board_name") or "")
        if not board:
            continue
        change_pct = _float_or_none(snapshot.get("change_pct"))
        amount = _float_or_none(snapshot.get("amount"))
        volume = _float_or_none(snapshot.get("volume"))
        close = _float_or_none(snapshot.get("latest_price"))
        if change_pct is None and amount is None and close is None:
            continue
        rows.append(
            {
                "board_name": board,
                "date": requested_date,
                "open": None,
                "high": None,
                "low": None,
                "close": close,
                "volume": volume,
                "amount": amount,
                "change_pct": change_pct,
                "amplitude": None,
            }
        )
    _insert_industry_board_ohlc(rows)
    return len(rows)


def _ensure_board_history(
    board_name: str,
    requested_date: str,
    period_days: int,
    *,
    allow_remote: bool = True,
) -> tuple[list[dict], Optional[str]]:
    needed = max(period_days + 1, 3)
    rows = _cached_board_rows(board_name, requested_date, needed)
    if len(rows) >= needed or (period_days == 1 and rows):
        return rows, None
    if not allow_remote:
        return rows, "本地历史样本不足，未联网补拉历史行情"

    try:
        end_date = datetime.fromisoformat(requested_date).date()
    except ValueError:
        end_date = datetime.now().date()
    start = (end_date - timedelta(days=max(45, period_days * 3))).isoformat()
    try:
        fetched = fetch_industry_board_ohlc(board_name, start, requested_date)
        if fetched:
            _insert_industry_board_ohlc(fetched)
            rows = _cached_board_rows(board_name, requested_date, needed)
        return rows, None
    except Exception as exc:
        logger.info("Failed to hydrate industry board %s: %s", board_name, exc)
        return rows, str(exc)


def _period_change(rows: list[dict], period: str, snapshot: dict) -> tuple[Optional[float], Optional[str]]:
    snapshot_change = _round_pct(snapshot.get("change_pct"))
    if not rows:
        return snapshot_change, "暂无本地板块历史，使用实时板块快照涨跌幅" if snapshot_change is not None else "暂无板块历史"
    latest = rows[0]
    if period == "1d":
        return _round_pct(latest.get("change_pct", snapshot_change)), None

    offset = PERIOD_DAYS[period]
    if len(rows) <= offset:
        return snapshot_change or _round_pct(latest.get("change_pct")), "历史样本不足，暂用最新日涨跌幅"

    current = _float_or_none(latest.get("close"))
    previous = _float_or_none(rows[offset].get("close"))
    if current is None or previous in (None, 0):
        return snapshot_change or _round_pct(latest.get("change_pct")), "历史收盘价不足，暂用最新日涨跌幅"
    return round((current / previous - 1) * 100, 2), None


def _sort_constituents(items: list[dict]) -> dict:
    def change_value(item: dict) -> float:
        return _float_or_none(item.get("change_pct")) or 0.0

    def amount_value(item: dict) -> float:
        return _float_or_none(item.get("amount")) or 0.0

    gainers = sorted(items, key=change_value, reverse=True)[:5]
    losers = [item for item in sorted(items, key=change_value) if change_value(item) < 0][:5]
    active = sorted(items, key=amount_value, reverse=True)[:8]
    limit_up = [item for item in sorted(items, key=change_value, reverse=True) if change_value(item) >= 9.8][:8]
    strong = [item for item in sorted(items, key=change_value, reverse=True) if change_value(item) >= 5][:8]
    return {
        "gainers": gainers,
        "losers": losers,
        "active": active,
        "limit_up": limit_up,
        "strong": strong,
    }


def _market_breadth(sectors: list[dict]) -> dict:
    up = sum(1 for item in sectors if (item.get("change_pct") or 0) > 0)
    down = sum(1 for item in sectors if (item.get("change_pct") or 0) < 0)
    flat = max(0, len(sectors) - up - down)
    avg_change = None
    changes = [item["change_pct"] for item in sectors if item.get("change_pct") is not None]
    if changes:
        avg_change = round(sum(changes) / len(changes), 2)
    total_amount = sum((_float_or_none(item.get("amount")) or 0.0) for item in sectors)
    return {
        "up": up,
        "down": down,
        "flat": flat,
        "avg_change_pct": avg_change,
        "total_amount": round(total_amount, 2),
    }


def _sector_group(board_name: str) -> str:
    name = str(board_name or "")
    for group, keywords in SECTOR_GROUPS:
        if any(keyword in name for keyword in keywords):
            return group
    return "其他行业"


def _payload_sector_count(payload: dict) -> int:
    summary = payload.get("summary") or {}
    return int(summary.get("sector_count") or len(payload.get("sectors") or []))


def _latest_cached_heatmap(period: str, requested_date: str, min_count: int = MIN_COMPLETE_SECTOR_COUNT) -> Optional[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT payload_json, generated_at, source, date
           FROM market_heatmap_cache
           WHERE period = ? AND date <= ?
           ORDER BY date DESC, generated_at DESC
           LIMIT 8""",
        (period, requested_date),
    ).fetchall()
    conn.close()
    partial: Optional[tuple[dict, object]] = None
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            continue
        if _payload_sector_count(payload) >= min_count:
            summary = payload.setdefault("summary", {})
            summary["cache"] = {
                "hit": True,
                "date": row["date"],
                "generated_at": row["generated_at"],
                "source": row["source"],
            }
            return payload
        if partial is None:
            partial = (payload, row)
    if partial is None:
        return None
    payload, row = partial
    summary = payload.setdefault("summary", {})
    summary.setdefault("notes", []).append(
        f"缓存快照只有 {_payload_sector_count(payload)} 个板块，正在尝试刷新为完整行业云图。"
    )
    summary["cache"] = {
        "hit": True,
        "date": row["date"],
        "generated_at": row["generated_at"],
        "source": row["source"],
        "partial": True,
    }
    return payload


def _store_heatmap_cache(period: str, payload: dict, source: str = "generated") -> None:
    summary = payload.get("summary") or {}
    date_text = summary.get("date") or _latest_market_date()
    sector_count = _payload_sector_count(payload)
    conn = get_conn()
    existing = conn.execute(
        """SELECT payload_json
           FROM market_heatmap_cache
           WHERE period = ? AND date = ?
           LIMIT 1""",
        (period, date_text),
    ).fetchone()
    if existing:
        try:
            existing_payload = json.loads(existing["payload_json"])
            existing_count = int(existing_payload.get("summary", {}).get("sector_count") or len(existing_payload.get("sectors") or []))
        except Exception:
            existing_count = 0
        if existing_count >= MIN_COMPLETE_SECTOR_COUNT and sector_count < MIN_COMPLETE_SECTOR_COUNT:
            conn.close()
            logger.warning(
                "Skip storing partial market heatmap cache for %s/%s: new=%s existing=%s",
                period,
                date_text,
                sector_count,
                existing_count,
            )
            return
    conn.execute(
        """INSERT OR REPLACE INTO market_heatmap_cache
           (period, date, payload_json, generated_at, source)
           VALUES (?, ?, ?, ?, ?)""",
        (
            period,
            date_text,
            json.dumps(payload, ensure_ascii=False),
            datetime.now().isoformat(),
            source,
        ),
    )
    conn.commit()
    conn.close()


def build_market_heatmap_payload(
    period: str,
    requested_date: str,
    max_sectors: int = 90,
    constituents_limit: int = 8,
    hydrate_limit: int = 24,
    history_hydrate_limit: int = 24,
    allow_remote_history: bool = True,
) -> dict:
    notes: list[str] = []
    using_cached_snapshots = False
    snapshots = fetch_industry_boards()
    if len(snapshots) < MIN_COMPLETE_SECTOR_COUNT:
        cached_snapshots = _cached_heatmap_sector_snapshots(period, max_sectors)
        if cached_snapshots:
            using_cached_snapshots = True
            local_snapshots = _fallback_board_snapshots(requested_date, max_sectors)
            if snapshots:
                notes.append(f"实时行业列表只有 {len(snapshots)} 个板块，已使用历史完整快照补足。")
            else:
                notes.append("实时行业板块列表不可用，已使用历史完整快照。")
            snapshots = _merge_snapshot_lists(
                cached_snapshots,
                _merge_snapshot_lists(local_snapshots, snapshots, max_sectors),
                max_sectors,
            )
    if not snapshots:
        notes.append("实时行业板块列表不可用，已尝试使用本地缓存。")
        snapshots = _fallback_board_snapshots(requested_date, max_sectors)
    stored_live_count = _store_live_board_snapshots(snapshots, requested_date)
    if stored_live_count:
        notes.append(f"已缓存 {stored_live_count} 个行业板块快照。")

    seen: set[str] = set()
    sectors: list[dict] = []
    sorted_snapshots = sorted(
        snapshots,
        key=lambda item: _float_or_none(item.get("amount")) or _float_or_none(item.get("market_cap")) or 0,
        reverse=True,
    )[:max_sectors]

    for index, snapshot in enumerate(sorted_snapshots):
        board = industry_board_name(snapshot.get("board_name") or "")
        if not board or board in seen:
            continue
        seen.add(board)
        has_snapshot_change = _float_or_none(snapshot.get("change_pct")) is not None
        if has_snapshot_change and (period == "1d" or using_cached_snapshots):
            rows, history_error = [], None
        elif not using_cached_snapshots and index < history_hydrate_limit:
            rows, history_error = _ensure_board_history(
                board,
                requested_date,
                PERIOD_DAYS[period],
                allow_remote=allow_remote_history,
            )
        else:
            rows, history_error = [], "未补拉历史行情，使用行业摘要涨跌幅"
        latest = rows[0] if rows else {}
        change_pct, change_note = _period_change(rows, period, snapshot)
        amount = _float_or_none(latest.get("amount")) or _float_or_none(snapshot.get("amount"))
        volume = _float_or_none(latest.get("volume")) or _float_or_none(snapshot.get("volume"))
        date_text = latest.get("date") or requested_date

        constituents_payload = {"items": [], "quality": "low", "note": "未拉取成分股预览"}
        if constituents_limit > 0 and not using_cached_snapshots and index < hydrate_limit:
            try:
                constituents_payload = _sector_company_candidates(board, date_text, limit=constituents_limit)
            except Exception as exc:
                logger.info("Failed to fetch sector constituents for %s: %s", board, exc)
                constituents_payload = {
                    "items": [],
                    "quality": "low",
                    "note": f"成分股预览不可用：{exc}",
                }

        constituents = constituents_payload.get("items") or []
        top_groups = _sort_constituents(constituents)
        weight = amount or _float_or_none(snapshot.get("market_cap")) or max(1, len(constituents))
        note_parts = [part for part in [change_note, constituents_payload.get("note"), history_error] if part]
        quality = constituents_payload.get("quality") or ("medium" if rows else "low")

        sectors.append(
            {
                "board_name": board,
                "sector_group": _sector_group(board),
                "board_code": snapshot.get("board_code") or "",
                "date": date_text,
                "period": period,
                "change_pct": change_pct,
                "amount": amount,
                "volume": volume,
                "weight": max(float(weight or 1), 1.0),
                "constituent_count": constituents_payload.get("count") or len(constituents),
                "constituents": constituents,
                "top_gainers": top_groups["gainers"],
                "top_losers": top_groups["losers"],
                "top_active": top_groups["active"],
                "limit_up": top_groups["limit_up"],
                "strong_stocks": top_groups["strong"],
                "limit_up_count": len(top_groups["limit_up"]),
                "leader": {
                    "name": snapshot.get("leader_name"),
                    "change_pct": _round_pct(snapshot.get("leader_change_pct")),
                },
                "quality": quality,
                "note": "；".join(note_parts) if note_parts else "",
                "source": {
                    "board": snapshot.get("source") or "industry_board",
                    "history": "industry_board_ohlc" if rows else "snapshot",
                    "constituents": "sector_constituents_cache_or_akshare" if constituents else "unavailable",
                },
            }
        )

    sectors.sort(key=lambda item: item.get("weight") or 1, reverse=True)
    latest_dates = [item["date"] for item in sectors if item.get("date")]
    summary = {
        "period": period,
        "date": max(latest_dates) if latest_dates else requested_date,
        "sector_count": len(sectors),
        "breadth": _market_breadth(sectors),
        "notes": notes,
        "cache": {"hit": False},
    }
    return {
        "summary": summary,
        "sectors": sectors,
    }


def refresh_market_heatmap_cache(
    period: str = "1d",
    date: Optional[str] = None,
    *,
    max_sectors: int = 90,
    constituents_limit: int = 8,
    hydrate_limit: int = 24,
    history_hydrate_limit: int = 24,
    allow_remote_history: bool = True,
) -> dict:
    if period not in PERIOD_DAYS:
        raise ValueError("period must be one of 1d, 5d, 20d")
    payload = build_market_heatmap_payload(
        period,
        date or _latest_market_date(),
        max_sectors=max_sectors,
        constituents_limit=constituents_limit,
        hydrate_limit=hydrate_limit,
        history_hydrate_limit=history_hydrate_limit,
        allow_remote_history=allow_remote_history,
    )
    _store_heatmap_cache(period, payload, source="refresh")
    payload.setdefault("summary", {})["cache"] = {"hit": False, "stored": True}
    return payload


def warm_market_constituents_cache(
    date: Optional[str] = None,
    *,
    period: str = "1d",
    max_sectors: int = 90,
    limit: int = 30,
    max_seconds: int = 420,
) -> dict:
    requested_date = date or _latest_market_date()
    payload = _latest_cached_heatmap(period, requested_date)
    if not payload:
        return {"date": requested_date, "attempted": 0, "stored": 0, "skipped": 0, "errors": 0}

    started = datetime.now()
    attempted = 0
    stored = 0
    skipped = 0
    errors = 0
    for sector in (payload.get("sectors") or [])[:max_sectors]:
        if (datetime.now() - started).total_seconds() >= max_seconds:
            break
        board = industry_board_name(sector.get("board_name") or "")
        if not board:
            continue
        cached = _cached_sector_constituents(board, requested_date)
        if cached:
            skipped += 1
            continue
        attempted += 1
        try:
            detail = _sector_company_candidates(board, requested_date, limit=limit)
            items = detail.get("items") or []
            if items:
                _store_sector_constituents(board, requested_date, items)
                stored += 1
        except Exception as exc:
            logger.info("Failed to warm sector constituents for %s: %s", board, exc)
            errors += 1
    return {
        "date": requested_date,
        "attempted": attempted,
        "stored": stored,
        "skipped": skipped,
        "errors": errors,
    }


def _sector_detail_payload(board_name: str, date_text: str, limit: int) -> dict:
    candidates = _sector_company_candidates(board_name, date_text, limit=limit)
    items = candidates.get("items") or []
    groups = _sort_constituents(items)
    return {
        "board_name": candidates.get("board_name") or industry_board_name(board_name),
        "date": date_text,
        "constituent_count": candidates.get("count") or len(items),
        "constituents": items,
        "top_gainers": groups["gainers"],
        "top_losers": groups["losers"],
        "top_active": groups["active"],
        "limit_up": groups["limit_up"],
        "strong_stocks": groups["strong"],
        "limit_up_count": len(groups["limit_up"]),
        "quality": candidates.get("quality") or "low",
        "note": candidates.get("note") or "",
        "cached": candidates.get("cached", False),
        "error": candidates.get("error"),
    }


def _sector_detail_cached_payload(board_name: str, date_text: str, limit: int) -> dict:
    board = industry_board_name(board_name)
    items = _cached_sector_constituents(board, date_text) or []
    cached = bool(items)
    if not items:
        items = _fallback_local_sector_companies(board, date_text, limit)
    groups = _sort_constituents(items)
    if cached:
        note = "板块成分股来自本地缓存，近 5/20 日涨幅优先使用本地已缓存日 K。"
    elif items:
        note = "板块成分股来自本地股票-行业映射样本，完整成分股会在盘后后台补齐。"
    else:
        note = "本地暂无该板块成分股缓存；盘后后台联网成功后会补齐。"
    return {
        "board_name": board,
        "date": date_text,
        "constituent_count": len(items),
        "constituents": items[:limit],
        "top_gainers": groups["gainers"],
        "top_losers": groups["losers"],
        "top_active": groups["active"],
        "limit_up": groups["limit_up"],
        "strong_stocks": groups["strong"],
        "limit_up_count": len(groups["limit_up"]),
        "quality": "high" if len(items) >= 8 else "medium" if len(items) >= 3 else "low",
        "note": note,
        "cached": cached,
        "error": None,
    }


@router.get("/heatmap")
def market_heatmap(
    period: str = Query("1d"),
    date: Optional[str] = None,
    max_sectors: int = Query(90, ge=1, le=120),
    constituents_limit: int = Query(8, ge=0, le=30),
    hydrate_limit: int = Query(24, ge=0, le=120),
    history_hydrate_limit: int = Query(24, ge=0, le=120),
    refresh: bool = False,
):
    if period not in PERIOD_DAYS:
        raise HTTPException(status_code=400, detail="period must be one of 1d, 5d, 20d")

    requested_date = date or _latest_market_date()
    if not refresh:
        cached = _latest_cached_heatmap(period, requested_date)
        if cached:
            return cached

    try:
        return refresh_market_heatmap_cache(
            period,
            requested_date,
            max_sectors=max_sectors,
            constituents_limit=constituents_limit,
            hydrate_limit=hydrate_limit,
            history_hydrate_limit=history_hydrate_limit,
        )
    except Exception as exc:
        cached = _latest_cached_heatmap(period, requested_date)
        if cached:
            cached.setdefault("summary", {}).setdefault("notes", []).append(f"刷新失败，已使用缓存：{exc}")
            return cached
        raise


@router.get("/sector/{board_name}/constituents")
def market_sector_constituents(
    board_name: str,
    date: Optional[str] = None,
    limit: int = Query(30, ge=1, le=80),
    refresh: bool = False,
):
    try:
        if not refresh:
            return _sector_detail_cached_payload(board_name, date or _latest_market_date(), limit)
        return _sector_detail_payload(board_name, date or _latest_market_date(), limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
