import sqlite3
from backend.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickers (
    symbol        TEXT PRIMARY KEY,
    name          TEXT,
    sector        TEXT,
    last_ohlc_fetch   TEXT,
    last_news_fetch   TEXT
);

CREATE TABLE IF NOT EXISTS ohlc (
    symbol        TEXT NOT NULL,
    date          TEXT NOT NULL,
    open          REAL,
    high          REAL,
    low           REAL,
    close         REAL,
    volume        REAL,
    vwap          REAL,
    transactions  INTEGER,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS news_raw (
    id            TEXT PRIMARY KEY,
    title         TEXT,
    description   TEXT,
    publisher     TEXT,
    author        TEXT,
    published_utc TEXT,
    article_url   TEXT,
    image_url     TEXT,
    amp_url       TEXT,
    tickers_json  TEXT,
    insights_json TEXT,
    news_type     TEXT
);

CREATE TABLE IF NOT EXISTS news_ticker (
    news_id       TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    PRIMARY KEY (news_id, symbol),
    FOREIGN KEY (news_id) REFERENCES news_raw(id)
);

CREATE TABLE IF NOT EXISTS layer0_results (
    news_id       TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    passed        INTEGER NOT NULL,
    reason        TEXT,
    PRIMARY KEY (news_id, symbol)
);

CREATE TABLE IF NOT EXISTS layer1_results (
    news_id       TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    relevance     TEXT,
    key_discussion      TEXT,
    chinese_summary     TEXT,
    sentiment           TEXT,
    discussion          TEXT,
    reason_growth       TEXT,
    reason_decrease     TEXT,
    PRIMARY KEY (news_id, symbol)
);

CREATE TABLE IF NOT EXISTS layer2_results (
    news_id       TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    discussion    TEXT,
    growth_reasons  TEXT,
    decrease_reasons TEXT,
    created_at    TEXT,
    PRIMARY KEY (news_id, symbol)
);

CREATE TABLE IF NOT EXISTS news_aligned (
    news_id       TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    published_utc TEXT,
    ret_t0        REAL,
    ret_t1        REAL,
    ret_t3        REAL,
    ret_t5        REAL,
    ret_t10       REAL,
    PRIMARY KEY (news_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_news_aligned_symbol_date ON news_aligned(symbol, trade_date);

CREATE TABLE IF NOT EXISTS market_events (
    id            TEXT PRIMARY KEY,
    symbol        TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    event_date    TEXT NOT NULL,
    published_at  TEXT,
    title         TEXT NOT NULL,
    summary       TEXT,
    source        TEXT,
    url           TEXT,
    sentiment     TEXT,
    impact        TEXT,
    metrics_json  TEXT,
    raw_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_events_symbol_date ON market_events(symbol, event_date);
CREATE INDEX IF NOT EXISTS idx_market_events_type ON market_events(event_type);

CREATE TABLE IF NOT EXISTS financial_reports (
    id                      TEXT PRIMARY KEY,
    symbol                  TEXT NOT NULL,
    announcement_date       TEXT NOT NULL,
    report_period           TEXT,
    revenue                 REAL,
    net_profit              REAL,
    non_gaap_net_profit     REAL,
    operating_cash_flow     REAL,
    roe                     REAL,
    yoy_revenue             REAL,
    yoy_net_profit          REAL,
    metrics_json            TEXT
);
CREATE INDEX IF NOT EXISTS idx_financial_reports_symbol_date
ON financial_reports(symbol, announcement_date);

CREATE TABLE IF NOT EXISTS batch_jobs (
    batch_id      TEXT PRIMARY KEY,
    symbol        TEXT,
    status        TEXT,
    total         INTEGER,
    completed     INTEGER DEFAULT 0,
    created_at    TEXT,
    finished_at   TEXT
);

CREATE TABLE IF NOT EXISTS batch_request_map (
    batch_id      TEXT NOT NULL,
    custom_id     TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    article_ids   TEXT NOT NULL,
    PRIMARY KEY (batch_id, custom_id)
);

-- ============================================================
-- A-share specific tables (Phase 1)
-- ============================================================

CREATE TABLE IF NOT EXISTS northbound_flow (
    date         TEXT PRIMARY KEY,
    sh_net_flow  REAL,
    sz_net_flow  REAL,
    total_flow   REAL
);

CREATE TABLE IF NOT EXISTS market_index_ohlc (
    index_symbol TEXT NOT NULL,
    index_name   TEXT,
    date         TEXT NOT NULL,
    open         REAL,
    high         REAL,
    low          REAL,
    close        REAL,
    volume       REAL,
    amount       REAL,
    change_pct   REAL,
    amplitude    REAL,
    PRIMARY KEY (index_symbol, date)
);

CREATE TABLE IF NOT EXISTS stock_industry_map (
    symbol        TEXT PRIMARY KEY,
    industry_name TEXT,
    board_name    TEXT,
    source        TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS industry_board_ohlc (
    board_name TEXT NOT NULL,
    date       TEXT NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    volume     REAL,
    amount     REAL,
    change_pct REAL,
    amplitude  REAL,
    PRIMARY KEY (board_name, date)
);

CREATE TABLE IF NOT EXISTS daily_reason_cache (
    symbol       TEXT NOT NULL,
    date         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    llm_used     INTEGER DEFAULT 0,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS analysis_cache (
    cache_key    TEXT PRIMARY KEY,
    cache_type   TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    llm_used     INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL,
    expires_at   TEXT,
    meta_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_analysis_cache_type ON analysis_cache(cache_type, expires_at);

CREATE TABLE IF NOT EXISTS northbound_holding (
    symbol       TEXT NOT NULL,
    date         TEXT NOT NULL,
    hold_shares  INTEGER,
    hold_ratio   REAL,
    change_shares INTEGER,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS lhb (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    date         TEXT NOT NULL,
    reason       TEXT,
    buy_amount   REAL,
    sell_amount  REAL,
    net_amount   REAL,
    department   TEXT
);
CREATE INDEX IF NOT EXISTS idx_lhb_symbol_date ON lhb(symbol, date);

CREATE TABLE IF NOT EXISTS margin_data (
    symbol       TEXT NOT NULL,
    date         TEXT NOT NULL,
    rzye         REAL,
    rqye         REAL,
    rzjme        REAL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS share_unlock (
    symbol       TEXT NOT NULL,
    unlock_date  TEXT NOT NULL,
    unlock_shares INTEGER,
    unlock_ratio REAL,
    PRIMARY KEY (symbol, unlock_date)
);

CREATE TABLE IF NOT EXISTS stock_concept (
    symbol       TEXT NOT NULL,
    concept_name TEXT NOT NULL,
    PRIMARY KEY (symbol, concept_name)
);

CREATE TABLE IF NOT EXISTS analyst_ratings (
    id                 TEXT PRIMARY KEY,
    symbol             TEXT NOT NULL,
    stock_name         TEXT,
    report_date        TEXT NOT NULL,
    institution        TEXT,
    analyst            TEXT,
    rating             TEXT,
    is_first_rating    TEXT,
    rating_change      TEXT,
    previous_rating    TEXT,
    target_price_low   REAL,
    target_price_high  REAL,
    source             TEXT,
    raw_json           TEXT,
    updated_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_analyst_ratings_symbol_date ON analyst_ratings(symbol, report_date);

CREATE TABLE IF NOT EXISTS analyst_rating_sync (
    symbol          TEXT PRIMARY KEY,
    last_checked_at TEXT,
    start_date      TEXT,
    end_date        TEXT,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS macro_chain_context (
    symbol        TEXT NOT NULL,
    date          TEXT NOT NULL,
    context_type  TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    sources_count INTEGER DEFAULT 0,
    llm_used      INTEGER DEFAULT 0,
    generated_at  TEXT NOT NULL,
    expires_at    TEXT,
    PRIMARY KEY (symbol, date, context_type)
);
CREATE INDEX IF NOT EXISTS idx_macro_chain_symbol_date ON macro_chain_context(symbol, date);

CREATE TABLE IF NOT EXISTS sector_relation_map (
    base_board_name    TEXT NOT NULL,
    related_board_name TEXT NOT NULL,
    relation_type      TEXT NOT NULL,
    reason             TEXT,
    source             TEXT,
    updated_at         TEXT,
    PRIMARY KEY (base_board_name, related_board_name, relation_type)
);

CREATE TABLE IF NOT EXISTS sector_leaders_cache (
    board_name    TEXT NOT NULL,
    date          TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    generated_at  TEXT NOT NULL,
    PRIMARY KEY (board_name, date)
);

CREATE TABLE IF NOT EXISTS sector_constituents_cache (
    board_name    TEXT NOT NULL,
    date          TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    generated_at  TEXT NOT NULL,
    expires_at    TEXT,
    PRIMARY KEY (board_name, date)
);

CREATE TABLE IF NOT EXISTS market_heatmap_cache (
    period        TEXT NOT NULL,
    date          TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    generated_at  TEXT NOT NULL,
    source        TEXT,
    PRIMARY KEY (period, date)
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate_ashare_columns(conn)
    conn.close()
    print(f"Database initialized at {settings.database_path}")


def _migrate_ashare_columns(conn: sqlite3.Connection):
    """Add A-share specific columns to existing tables (safe to run repeatedly)."""
    _add_column(conn, "tickers", "market", "TEXT")        # 主板/创业板/科创板/北交所
    _add_column(conn, "tickers", "industry", "TEXT")      # 行业
    _add_column(conn, "tickers", "list_date", "TEXT")     # 上市日期
    _add_column(conn, "tickers", "is_st", "INTEGER DEFAULT 0")
    _add_column(conn, "tickers", "limit_pct", "REAL DEFAULT 0.10")

    _add_column(conn, "ohlc", "amount", "REAL")           # 成交额
    _add_column(conn, "ohlc", "turnover_rate", "REAL")    # 换手率
    _add_column(conn, "ohlc", "change_pct", "REAL")       # 涨跌幅
    _add_column(conn, "ohlc", "amplitude", "REAL")        # 振幅
    _add_column(conn, "ohlc", "is_limit_up", "INTEGER DEFAULT 0")
    _add_column(conn, "ohlc", "is_limit_down", "INTEGER DEFAULT 0")
    _add_column(conn, "ohlc", "is_suspended", "INTEGER DEFAULT 0")

    _add_column(conn, "news_raw", "image_url", "TEXT")
    _add_column(conn, "news_raw", "news_type", "TEXT")

    conn.execute(
        """CREATE TABLE IF NOT EXISTS market_index_ohlc (
            index_symbol TEXT NOT NULL,
            index_name   TEXT,
            date         TEXT NOT NULL,
            open         REAL,
            high         REAL,
            low          REAL,
            close        REAL,
            volume       REAL,
            amount       REAL,
            change_pct   REAL,
            amplitude    REAL,
            PRIMARY KEY (index_symbol, date)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS stock_industry_map (
            symbol        TEXT PRIMARY KEY,
            industry_name TEXT,
            board_name    TEXT,
            source        TEXT,
            updated_at    TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS industry_board_ohlc (
            board_name TEXT NOT NULL,
            date       TEXT NOT NULL,
            open       REAL,
            high       REAL,
            low        REAL,
            close      REAL,
            volume     REAL,
            amount     REAL,
            change_pct REAL,
            amplitude  REAL,
            PRIMARY KEY (board_name, date)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS daily_reason_cache (
            symbol       TEXT NOT NULL,
            date         TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            llm_used     INTEGER DEFAULT 0,
            generated_at TEXT NOT NULL,
            PRIMARY KEY (symbol, date)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS analysis_cache (
            cache_key    TEXT PRIMARY KEY,
            cache_type   TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            llm_used     INTEGER DEFAULT 0,
            created_at   TEXT NOT NULL,
            expires_at   TEXT,
            meta_json    TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analysis_cache_type ON analysis_cache(cache_type, expires_at)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS analyst_ratings (
            id                 TEXT PRIMARY KEY,
            symbol             TEXT NOT NULL,
            stock_name         TEXT,
            report_date        TEXT NOT NULL,
            institution        TEXT,
            analyst            TEXT,
            rating             TEXT,
            is_first_rating    TEXT,
            rating_change      TEXT,
            previous_rating    TEXT,
            target_price_low   REAL,
            target_price_high  REAL,
            source             TEXT,
            raw_json           TEXT,
            updated_at         TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analyst_ratings_symbol_date ON analyst_ratings(symbol, report_date)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS analyst_rating_sync (
            symbol          TEXT PRIMARY KEY,
            last_checked_at TEXT,
            start_date      TEXT,
            end_date        TEXT,
            error           TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS macro_chain_context (
            symbol        TEXT NOT NULL,
            date          TEXT NOT NULL,
            context_type  TEXT NOT NULL,
            payload_json  TEXT NOT NULL,
            sources_count INTEGER DEFAULT 0,
            llm_used      INTEGER DEFAULT 0,
            generated_at  TEXT NOT NULL,
            expires_at    TEXT,
            PRIMARY KEY (symbol, date, context_type)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_macro_chain_symbol_date ON macro_chain_context(symbol, date)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sector_relation_map (
            base_board_name    TEXT NOT NULL,
            related_board_name TEXT NOT NULL,
            relation_type      TEXT NOT NULL,
            reason             TEXT,
            source             TEXT,
            updated_at         TEXT,
            PRIMARY KEY (base_board_name, related_board_name, relation_type)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sector_leaders_cache (
            board_name    TEXT NOT NULL,
            date          TEXT NOT NULL,
            payload_json  TEXT NOT NULL,
            generated_at  TEXT NOT NULL,
            PRIMARY KEY (board_name, date)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sector_constituents_cache (
            board_name    TEXT NOT NULL,
            date          TEXT NOT NULL,
            payload_json  TEXT NOT NULL,
            generated_at  TEXT NOT NULL,
            expires_at    TEXT,
            PRIMARY KEY (board_name, date)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS market_heatmap_cache (
            period        TEXT NOT NULL,
            date          TEXT NOT NULL,
            payload_json  TEXT NOT NULL,
            generated_at  TEXT NOT NULL,
            source        TEXT,
            PRIMARY KEY (period, date)
        )"""
    )

    conn.commit()


def _add_column(conn: sqlite3.Connection, table: str, column: str, col_type: str):
    """Safely add a column if it doesn't exist."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except sqlite3.OperationalError:
        pass  # Column already exists


if __name__ == "__main__":
    init_db()
