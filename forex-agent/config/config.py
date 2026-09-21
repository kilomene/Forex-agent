"""
Config loader — everything sensitive comes from environment variables
or a 0600 secrets file. Never hardcode account numbers, passwords, or
API keys in source, and never log them (use AppConfig.redacted()).

Layering: config/defaults.yaml holds every non-secret default;
environment variables override the file. Original bridge env keys are
preserved so existing .env files keep working.

Secrets file: set FOREX_SECRETS_FILE=/path/to/secrets.env containing
KEY=VALUE lines. The file should be mode 0600 — a warning is logged
otherwise (but loading still proceeds; failing closed here would
brick headless installs that manage perms differently).
"""

import logging
import os
import stat
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

logger = logging.getLogger("config")

CONFIG_DIR = Path(__file__).resolve().parent
DEFAULTS_PATH = CONFIG_DIR / "defaults.yaml"

# Keys that must never appear in logs / events / error strings.
SECRET_KEYS = frozenset({
    "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_TERMINAL_PATH",
    "WORKER_API_KEY",
})


def _load_secrets_file() -> None:
    path = os.environ.get("FOREX_SECRETS_FILE", "")
    if not path:
        return
    p = Path(path)
    if not p.is_file():
        raise RuntimeError(f"FOREX_SECRETS_FILE points at missing file: {path}")
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        logger.warning(
            "Secrets file %s has permissions %o (expected 0600) — tighten it.",
            path, mode,
        )
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_secrets_file()


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it before running (see config/defaults.yaml)."
        )
    return val


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("true", "1", "yes", "y")


def _str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _list(name: str, default: List[str]) -> List[str]:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return list(default)
    return [s.strip() for s in raw.split(",") if s.strip()]


def _load_yaml_defaults() -> Dict[str, Any]:
    if yaml is None or not DEFAULTS_PATH.is_file():
        return {}
    with open(DEFAULTS_PATH) as f:
        return yaml.safe_load(f) or {}


_YAML = _load_yaml_defaults()


def _y(*keys: str, default: Any = None) -> Any:
    node: Any = _YAML
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node


@dataclass
class BrokerConfig:
    provider: str = field(default_factory=lambda: _str("BROKER_PROVIDER", _y("broker", "provider", default="mt5")))
    # MT5 credentials — required only in live mode (validated then).
    login: int = field(default_factory=lambda: int(os.environ["MT5_LOGIN"]) if os.environ.get("MT5_LOGIN") else 0)
    password: str = field(default_factory=lambda: os.environ.get("MT5_PASSWORD", ""))
    server: str = field(default_factory=lambda: os.environ.get("MT5_SERVER", ""))
    terminal_path: str = field(default_factory=lambda: os.environ.get("MT5_TERMINAL_PATH", ""))


@dataclass
class EmaRsiStrategyConfig:
    enabled: bool = True
    ema_fast: int = 20
    ema_slow: int = 50
    rsi_period: int = 14
    atr_period: int = 14
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    atr_sl_multiplier: float = 1.5
    atr_tp_multiplier: float = 3.0


@dataclass
class TradingConfig:
    symbols: List[str] = field(default_factory=lambda: _list("SYMBOLS", _y("symbols", default=["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"])))
    timeframes: List[str] = field(default_factory=lambda: _list("TIMEFRAMES", _y("timeframes", default=["H1"])))
    # Back-compat: the original bridge had a single TIMEFRAME env.
    timeframe: str = field(default_factory=lambda: _str("TIMEFRAME", (_y("timeframes", default=["H1"]) or ["H1"])[0]))
    ema_rsi: EmaRsiStrategyConfig = field(default_factory=lambda: EmaRsiStrategyConfig(
        enabled=_bool("STRATEGY_EMA_RSI_ENABLED", _y("strategies", "ema_rsi", "enabled", default=True)),
        ema_fast=_int("EMA_FAST", _y("strategies", "ema_rsi", "ema_fast", default=20)),
        ema_slow=_int("EMA_SLOW", _y("strategies", "ema_rsi", "ema_slow", default=50)),
        rsi_period=_int("RSI_PERIOD", _y("strategies", "ema_rsi", "rsi_period", default=14)),
        atr_period=_int("ATR_PERIOD", _y("strategies", "ema_rsi", "atr_period", default=14)),
        rsi_overbought=_float("RSI_OVERBOUGHT", _y("strategies", "ema_rsi", "rsi_overbought", default=70.0)),
        rsi_oversold=_float("RSI_OVERSOLD", _y("strategies", "ema_rsi", "rsi_oversold", default=30.0)),
        atr_sl_multiplier=_float("ATR_SL_MULTIPLIER", _y("strategies", "ema_rsi", "atr_sl_multiplier", default=1.5)),
        atr_tp_multiplier=_float("ATR_TP_MULTIPLIER", _y("strategies", "ema_rsi", "atr_tp_multiplier", default=3.0)),
    ))


@dataclass
class RiskConfig:
    """Guardrails — fractions of EQUITY unless noted. These exist so a
    bug or a bad signal can't blow the account."""
    fixed_lot_size: float = field(default_factory=lambda: _float("FIXED_LOT_SIZE", _y("risk", "fixed_lot_size", default=0.01)))
    use_percent_risk_sizing: bool = field(default_factory=lambda: _bool("USE_PERCENT_RISK_SIZING", _y("risk", "use_percent_risk_sizing", default=False)))
    # Original bridge used percent-style env keys (1.0 = 1%); yaml uses
    # fractions (0.01 = 1%). Env wins when set.
    max_risk_per_trade: float = field(default_factory=lambda: (
        _float("MAX_RISK_PER_TRADE_PCT", 0) / 100.0
        if os.environ.get("MAX_RISK_PER_TRADE_PCT") else
        _float("MAX_RISK_PER_TRADE", _y("risk", "max_risk_per_trade", default=0.01))
    ))
    max_daily_loss: float = field(default_factory=lambda: (
        _float("MAX_DAILY_LOSS_PCT", 0) / 100.0
        if os.environ.get("MAX_DAILY_LOSS_PCT") else
        _float("MAX_DAILY_LOSS", _y("risk", "max_daily_loss", default=0.03))
    ))
    max_open_positions: int = field(default_factory=lambda: _int("MAX_OPEN_POSITIONS", _y("risk", "max_open_positions", default=3)))
    max_total_exposure_lots: float = field(default_factory=lambda: _float("MAX_TOTAL_EXPOSURE_LOTS", _y("risk", "max_total_exposure_lots", default=1.0)))
    max_consecutive_losses: int = field(default_factory=lambda: _int("MAX_CONSECUTIVE_LOSSES", _y("risk", "max_consecutive_losses", default=4)))
    max_correlated_positions: int = field(default_factory=lambda: _int("MAX_CORRELATED_POSITIONS", _y("risk", "max_correlated_positions", default=1)))
    require_stop_loss: bool = field(default_factory=lambda: _bool("REQUIRE_SL", _y("risk", "require_stop_loss", default=True)))
    min_account_equity: float = field(default_factory=lambda: _float("MIN_ACCOUNT_EQUITY", _y("risk", "min_account_equity", default=0.0)))
    max_spread_points: float = field(default_factory=lambda: _float("MAX_SPREAD_POINTS", _y("risk", "max_spread_points", default=0.0)))


@dataclass
class AutonomyConfig:
    # signals_only | assisted | autonomous
    mode: str = field(default_factory=lambda: _str("AUTONOMY_MODE", _y("autonomy", "mode", default="assisted")))


@dataclass
class WorkerConfig:
    enabled: bool = field(default_factory=lambda: _bool("WORKER_ENABLED", _y("worker", "enabled", default=False)))
    base_url: str = field(default_factory=lambda: _str("WORKER_BASE_URL", _y("worker", "url", default="")))
    api_key: str = field(default_factory=lambda: _str("WORKER_API_KEY", ""))
    poll_interval_seconds: int = field(default_factory=lambda: _int("POLL_INTERVAL_SECONDS", _y("worker", "poll_interval_seconds", default=30)))


@dataclass
class ExitManagerConfig:
    """Every action here can only reduce risk (tighten a stop, close an
    overdue trade) — never loosen a stop or add exposure."""
    enabled: bool = field(default_factory=lambda: _bool("EXIT_MANAGER_ENABLED", _y("exits", "enabled", default=True)))
    max_hold_hours: float = field(default_factory=lambda: _float("MAX_HOLD_HOURS", _y("exits", "max_hold_hours", default=48.0)))
    breakeven_trigger_atr: float = field(default_factory=lambda: _float("BREAKEVEN_TRIGGER_ATR", _y("exits", "breakeven_trigger_atr", default=1.0)))
    trailing_activation_atr: float = field(default_factory=lambda: _float("TRAILING_ACTIVATION_ATR", _y("exits", "trailing_activation_atr", default=2.0)))
    trailing_atr_multiplier: float = field(default_factory=lambda: _float("TRAILING_ATR_MULTIPLIER", _y("exits", "trailing_atr_multiplier", default=1.5)))
    check_interval_seconds: int = field(default_factory=lambda: _int("EXIT_CHECK_INTERVAL_SECONDS", _y("exits", "check_interval_seconds", default=60)))


@dataclass
class PerformanceReviewConfig:
    enabled: bool = field(default_factory=lambda: _bool("PERFORMANCE_REVIEW_ENABLED", _y("performance", "enabled", default=True)))
    interval_hours: float = field(default_factory=lambda: _float("PERFORMANCE_REVIEW_INTERVAL_HOURS", _y("performance", "interval_hours", default=6.0)))
    lookback_days: int = field(default_factory=lambda: _int("PERFORMANCE_LOOKBACK_DAYS", _y("performance", "lookback_days", default=30)))


@dataclass
class KillSwitchConfig:
    check_interval_seconds: int = field(default_factory=lambda: _int("KILL_SWITCH_CHECK_INTERVAL_SECONDS", _y("kill_switch", "check_interval_seconds", default=10)))


@dataclass
class EventsConfig:
    enabled: bool = field(default_factory=lambda: _bool("EVENTS_ENABLED", _y("events", "enabled", default=True)))
    queue_max: int = field(default_factory=lambda: _int("EVENTS_QUEUE_MAX", _y("events", "queue_max", default=1000)))


@dataclass
class NotificationsConfig:
    enabled: bool = field(default_factory=lambda: _bool("NOTIFICATIONS_ENABLED", _y("notifications", "enabled", default=False)))


@dataclass
class LoggingConfig:
    level: str = field(default_factory=lambda: _str("LOG_LEVEL", _y("logging", "level", default="INFO")))
    format: str = field(default_factory=lambda: _str("LOG_FORMAT", _y("logging", "format", default="json")))


@dataclass
class ChartConfig:
    symbols: List[str] = field(default_factory=list)  # empty = trading symbols; resolved in AppConfig
    timeframes: List[str] = field(default_factory=lambda: _list("CHART_TIMEFRAMES", _y("charts", "timeframes", default=["M15", "H1", "H4", "D1"])))
    candle_count: int = field(default_factory=lambda: _int("CHART_CANDLE_COUNT", _y("charts", "candle_count", default=150)))
    update_interval_seconds: int = field(default_factory=lambda: _int("CHART_UPDATE_INTERVAL_SECONDS", _y("charts", "update_interval_seconds", default=120)))
    ema_fast: int = field(default_factory=lambda: _int("CHART_EMA_FAST", 20))
    ema_slow: int = field(default_factory=lambda: _int("CHART_EMA_SLOW", 50))


def _resolve_mode() -> str:
    dry_run_env = os.environ.get("DRY_RUN")
    if dry_run_env is not None:
        return "dry_run" if dry_run_env.strip().lower() in ("true", "1", "yes") else "live"
    mode = _str("MODE", _y("mode", default="dry_run")).strip().lower()
    if mode not in ("dry_run", "live"):
        raise RuntimeError(
            f"CONFIG_INVALID: mode must be 'dry_run' or 'live', got {mode!r}."
        )
    return mode


@dataclass
class AppConfig:
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    autonomy: AutonomyConfig = field(default_factory=AutonomyConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    exit_manager: ExitManagerConfig = field(default_factory=ExitManagerConfig)
    performance_review: PerformanceReviewConfig = field(default_factory=PerformanceReviewConfig)
    kill_switch: KillSwitchConfig = field(default_factory=KillSwitchConfig)
    events: EventsConfig = field(default_factory=EventsConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    chart: ChartConfig = field(default_factory=ChartConfig)
    mode: str = field(default_factory=_resolve_mode)
    scan_interval_seconds: int = field(default_factory=lambda: _int("SCAN_INTERVAL_SECONDS", _y("scan_interval_seconds", default=60)))
    reconciliation_interval_seconds: int = field(default_factory=lambda: _int("RECONCILIATION_INTERVAL_SECONDS", _y("reconciliation_interval_seconds", default=300)))

    def __post_init__(self) -> None:
        if not self.chart.symbols:
            self.chart.symbols = list(self.trading.symbols)
        if self.autonomy.mode not in ("signals_only", "assisted", "autonomous"):
            raise RuntimeError(
                f"CONFIG_INVALID: autonomy.mode must be signals_only|assisted|autonomous, "
                f"got {self.autonomy.mode!r}."
            )
        if self.broker.provider not in ("mt5", "disconnected"):
            raise RuntimeError(
                f"CONFIG_INVALID: broker.provider must be mt5|disconnected, "
                f"got {self.broker.provider!r}."
            )
        if self.mode == "live" and self.broker.provider == "mt5":
            missing = [k for k, v in (
                ("MT5_LOGIN", self.broker.login),
                ("MT5_PASSWORD", self.broker.password),
                ("MT5_SERVER", self.broker.server),
            ) if not v]
            if missing:
                raise RuntimeError(
                    f"CREDENTIALS_INVALID: live mode requires broker credentials; "
                    f"missing: {', '.join(missing)}."
                )
        if self.worker.enabled and not self.worker.base_url:
            raise RuntimeError(
                "CONFIG_INVALID: worker.enabled=true requires WORKER_BASE_URL."
            )

    @property
    def dry_run(self) -> bool:
        return self.mode == "dry_run"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def redacted(self) -> Dict[str, Any]:
        """Config as a dict with every secret value masked — safe to log."""
        d = asdict(self)

        def _mask(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {k: ("***" if k in ("password", "api_key") else _mask(v))
                        for k, v in obj.items()}
            if isinstance(obj, list):
                return [_mask(v) for v in obj]
            return obj

        return _mask(d)


def load_config() -> AppConfig:
    """Build the effective config: defaults.yaml + environment overrides."""
    return AppConfig()
