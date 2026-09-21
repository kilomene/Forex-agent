"""Configuration package: defaults.yaml + env→dataclass loader.

Public surface:
    from config import load_config, AppConfig
"""

from .config import (
    AppConfig,
    AutonomyConfig,
    BrokerConfig,
    ChartConfig,
    EmaRsiStrategyConfig,
    EventsConfig,
    ExitManagerConfig,
    KillSwitchConfig,
    LoggingConfig,
    NotificationsConfig,
    PerformanceReviewConfig,
    RiskConfig,
    TradingConfig,
    WorkerConfig,
    load_config,
)

__all__ = [
    "AppConfig",
    "AutonomyConfig",
    "BrokerConfig",
    "ChartConfig",
    "EmaRsiStrategyConfig",
    "EventsConfig",
    "ExitManagerConfig",
    "KillSwitchConfig",
    "LoggingConfig",
    "NotificationsConfig",
    "PerformanceReviewConfig",
    "RiskConfig",
    "TradingConfig",
    "WorkerConfig",
    "load_config",
]
