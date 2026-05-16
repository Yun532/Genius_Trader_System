from pydantic_settings import BaseSettings
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    polygon_api_key: str = ""          # Kept for US stock fallback
    anthropic_api_key: str = ""
    deepseek_api_key: str = ""
    openai_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    openai_base_url: str = "https://api.openai.com/v1"
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-v4-flash"
    llm_fallback_provider: str = "openai"
    llm_fallback_model: str = "gpt-5.4-mini"
    llm_analysis_mode: str = "primary_with_fallback"  # "primary_with_fallback" | "parallel_review"
    llm_cache_enabled: bool = True
    llm_recent_auto_days: int = 14
    llm_daily_reason_cache_ttl_hours: int = 24 * 30
    llm_range_analysis_cache_ttl_hours: int = 24 * 14
    llm_signal_reference_cache_ttl_hours: int = 24
    llm_stock_report_cache_ttl_hours: int = 24 * 7
    macro_chain_cache_ttl_hours: int = 24 * 7
    news_web_search_enabled: bool = False
    news_web_search_provider: str = "openai"          # "openai" | "tavily"; DeepSeek support is provider-dependent
    news_web_search_max_results: int = 8
    news_web_search_lookback_days: int = 14
    news_web_search_cache_ttl_hours: int = 24
    openai_web_search_model: str = "gpt-5.4-mini"
    tavily_api_key: str = ""
    auto_sync_watchlist: bool = False
    auto_sync_hour: int = 16
    auto_sync_minute: int = 45
    auto_sync_stale_days: int = 1
    tushare_token: str = ""            # Tushare Pro token (optional backup)
    data_source: str = "akshare"       # "akshare" | "polygon"
    database_path: str = str(PROJECT_ROOT / "pokieticker.db")
    kronos_enabled: bool = True
    kronos_repo_path: str = ""          # Optional local clone path for https://github.com/shiyu-coder/Kronos
    kronos_tokenizer_name: str = "NeoQuasar/Kronos-Tokenizer-base"
    kronos_model_name: str = "NeoQuasar/Kronos-small"
    kronos_device: str = "cpu"
    kronos_max_context: int = 512

    model_config = {"env_file": str(PROJECT_ROOT / ".env"), "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
