import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

VALID_STRATEGIES = {"cross-sectional-momentum", "mean-reversion", "pairs-trading"}
VALID_UNIVERSES = {"sp500", "sp100", "nasdaq100", "russell1000", "russell2000", "dow30", "custom"}
VALID_ORDER_TYPES = {"market", "limit"}
VALID_REBALANCE_FREQUENCIES = {"daily", "weekly", "monthly"}
VALID_RECALCULATE_FREQUENCIES = {"daily", "weekly", "monthly"}
VALID_LOG_LEVELS = {"critical", "error", "warning", "info", "debug"}
VALID_INDICATORS = {"rsi", "bollinger-bands", "zscore"}
VALID_EXIT_BANDS = {"middle", "upper"}


def _err(msg: str) -> None:
    print(f"[pacabot] Config error: {msg}", file=sys.stderr)
    sys.exit(1)


def _require(section: dict, key: str, expected_type: type, section_name: str):
    if key not in section:
        _err(f"[{section_name}] missing required field '{key}'")
    val = section[key]
    if not isinstance(val, expected_type):
        _err(
            f"[{section_name}] '{key}' must be {expected_type.__name__}, "
            f"got {type(val).__name__}"
        )
    return val


def _require_positive_int(section: dict, key: str, section_name: str) -> int:
    val = _require(section, key, int, section_name)
    if val <= 0:
        _err(f"[{section_name}] '{key}' must be a positive integer, got {val}")
    return val


def _require_positive_float(section: dict, key: str, section_name: str) -> float:
    if key not in section:
        _err(f"[{section_name}] missing required field '{key}'")
    val = section[key]
    if not isinstance(val, (int, float)):
        _err(f"[{section_name}] '{key}' must be a number, got {type(val).__name__}")
    val = float(val)
    if val <= 0:
        _err(f"[{section_name}] '{key}' must be positive, got {val}")
    return val


def _require_float_in_range(
    section: dict, key: str, lo: float, hi: float, section_name: str
) -> float:
    if key not in section:
        _err(f"[{section_name}] missing required field '{key}'")
    val = section[key]
    if not isinstance(val, (int, float)):
        _err(f"[{section_name}] '{key}' must be a number, got {type(val).__name__}")
    val = float(val)
    if not (lo <= val <= hi):
        _err(f"[{section_name}] '{key}' must be between {lo} and {hi}, got {val}")
    return val


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class AccountConfig:
    name: str
    paper: bool


@dataclass
class ExecutionConfig:
    trading_start: str | None  # "HH:MM" Eastern, or None if disabled
    trading_end: str | None


@dataclass
class RiskConfig:
    margin_utilization_cap: float
    margin_warning_threshold: float
    margin_critical_threshold: float
    stop_loss: float | None        # None = disabled
    max_position_size: float
    max_position_size_usd: float
    max_positions: int             # 0 = uncapped
    daily_loss_limit: float | None # None = disabled


@dataclass
class LoggingConfig:
    log_level: str
    log_file: str


@dataclass
class MomentumParameters:
    lookback_period: int
    top_n: int
    rebalance_frequency: str


@dataclass
class RSIParameters:
    period: int
    entry_threshold: float
    exit_threshold: float


@dataclass
class BollingerBandsParameters:
    period: int
    std_dev: float
    exit_band: str


@dataclass
class ZScoreParameters:
    period: int
    entry_threshold: float
    exit_threshold: float


@dataclass
class MeanReversionParameters:
    indicator: str
    max_holding_days: int | None
    rsi: RSIParameters | None
    bollinger_bands: BollingerBandsParameters | None
    zscore: ZScoreParameters | None


@dataclass
class PairsParameters:
    pairs: list[list[str]]
    lookback_period: int
    entry_zscore: float
    exit_zscore: float
    stop_loss_zscore: float
    recalculate_frequency: str


@dataclass
class StrategyConfig:
    name: str
    universe: str
    long_only: bool | None       # None for pairs-trading
    order_type: str
    parameters: MomentumParameters | MeanReversionParameters | PairsParameters
    custom_tickers: list[str]    # non-empty only when universe == "custom" and not pairs


@dataclass
class Config:
    account: AccountConfig
    strategy: StrategyConfig
    execution: ExecutionConfig
    risk: RiskConfig
    logging: LoggingConfig


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------

def _parse_account(raw: dict) -> AccountConfig:
    sec = raw.get("account")
    if not sec:
        _err("missing [account] section")
    name = _require(sec, "name", str, "account")
    if not name:
        _err("[account] 'name' must not be empty")
    paper = _require(sec, "paper", bool, "account")
    return AccountConfig(name=name, paper=paper)


def _parse_execution(raw: dict) -> ExecutionConfig:
    sec = raw.get("execution")
    if not sec:
        _err("missing [execution] section")

    def _parse_time(key: str) -> str | None:
        if key not in sec:
            _err(f"[execution] missing required field '{key}'")
        val = sec[key]
        if val is False:
            return None
        if not isinstance(val, str):
            _err(f"[execution] '{key}' must be a time string (HH:MM) or false")
        parts = val.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            _err(f"[execution] '{key}' must be in HH:MM format, got '{val}'")
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            _err(f"[execution] '{key}' has invalid time value '{val}'")
        return val

    start = _parse_time("trading-start")
    end = _parse_time("trading-end")
    if start is not None and end is not None and start >= end:
        _err("[execution] 'trading-start' must be before 'trading-end'")
    return ExecutionConfig(trading_start=start, trading_end=end)


def _parse_risk(raw: dict) -> RiskConfig:
    sec = raw.get("risk")
    if not sec:
        _err("missing [risk] section")

    margin_cap = _require_float_in_range(sec, "margin-utilization-cap", 0.01, 0.99, "risk")
    margin_warn = _require_float_in_range(sec, "margin-warning-threshold", 0.01, 0.99, "risk")
    margin_crit = _require_float_in_range(sec, "margin-critical-threshold", 0.01, 1.0, "risk")

    if not (margin_cap < margin_warn < margin_crit):
        _err(
            "[risk] thresholds must satisfy: "
            "margin-utilization-cap < margin-warning-threshold < margin-critical-threshold"
        )

    # stop-loss: float or false
    if "stop-loss" not in sec:
        _err("[risk] missing required field 'stop-loss'")
    sl_raw = sec["stop-loss"]
    if sl_raw is False:
        stop_loss = None
    elif isinstance(sl_raw, (int, float)) and float(sl_raw) > 0:
        stop_loss = float(sl_raw)
    else:
        _err("[risk] 'stop-loss' must be a positive number or false")
        stop_loss = None  # unreachable, satisfies type checker

    max_pos_size = _require_float_in_range(sec, "max-position-size", 0.001, 1.0, "risk")
    max_pos_usd = _require_positive_float(sec, "max-position-size-usd", "risk")

    if "max-positions" not in sec:
        _err("[risk] missing required field 'max-positions'")
    mp = sec["max-positions"]
    if not isinstance(mp, int) or mp < 0:
        _err("[risk] 'max-positions' must be a non-negative integer (0 = uncapped)")
    max_positions = mp

    if "daily-loss-limit" not in sec:
        _err("[risk] missing required field 'daily-loss-limit'")
    dll_raw = sec["daily-loss-limit"]
    if dll_raw is False:
        daily_loss_limit = None
    elif isinstance(dll_raw, (int, float)) and float(dll_raw) > 0:
        daily_loss_limit = float(dll_raw)
    else:
        _err("[risk] 'daily-loss-limit' must be a positive number or false")
        daily_loss_limit = None

    return RiskConfig(
        margin_utilization_cap=margin_cap,
        margin_warning_threshold=margin_warn,
        margin_critical_threshold=margin_crit,
        stop_loss=stop_loss,
        max_position_size=max_pos_size,
        max_position_size_usd=max_pos_usd,
        max_positions=max_positions,
        daily_loss_limit=daily_loss_limit,
    )


def _parse_logging(raw: dict) -> LoggingConfig:
    sec = raw.get("logging")
    if not sec:
        _err("missing [logging] section")
    level = _require(sec, "log-level", str, "logging").lower()
    if level not in VALID_LOG_LEVELS:
        _err(f"[logging] 'log-level' must be one of {sorted(VALID_LOG_LEVELS)}, got '{level}'")
    log_file = _require(sec, "log-file", str, "logging")
    if not log_file:
        _err("[logging] 'log-file' must not be empty")
    return LoggingConfig(log_level=level, log_file=log_file)


def _parse_momentum_parameters(params: dict) -> MomentumParameters:
    lp = _require_positive_int(params, "lookback-period", "strategy.parameters")
    top_n = _require_positive_int(params, "top-n", "strategy.parameters")
    freq = _require(params, "rebalance-frequency", str, "strategy.parameters")
    if freq not in VALID_REBALANCE_FREQUENCIES:
        _err(
            f"[strategy.parameters] 'rebalance-frequency' must be one of "
            f"{sorted(VALID_REBALANCE_FREQUENCIES)}, got '{freq}'"
        )
    return MomentumParameters(lookback_period=lp, top_n=top_n, rebalance_frequency=freq)


def _parse_rsi(sub: dict) -> RSIParameters:
    period = _require_positive_int(sub, "period", "strategy.parameters.rsi")
    entry = _require_float_in_range(sub, "entry-threshold", 1, 49, "strategy.parameters.rsi")
    exit_ = _require_float_in_range(sub, "exit-threshold", 2, 99, "strategy.parameters.rsi")
    if exit_ <= entry:
        _err("[strategy.parameters.rsi] 'exit-threshold' must be greater than 'entry-threshold'")
    return RSIParameters(period=period, entry_threshold=entry, exit_threshold=exit_)


def _parse_bollinger_bands(sub: dict) -> BollingerBandsParameters:
    period = _require_positive_int(sub, "period", "strategy.parameters.bollinger-bands")
    std_dev = _require_positive_float(sub, "std-dev", "strategy.parameters.bollinger-bands")
    exit_band = _require(sub, "exit-band", str, "strategy.parameters.bollinger-bands")
    if exit_band not in VALID_EXIT_BANDS:
        _err(
            f"[strategy.parameters.bollinger-bands] 'exit-band' must be one of "
            f"{sorted(VALID_EXIT_BANDS)}, got '{exit_band}'"
        )
    return BollingerBandsParameters(period=period, std_dev=std_dev, exit_band=exit_band)


def _parse_zscore(sub: dict) -> ZScoreParameters:
    period = _require_positive_int(sub, "period", "strategy.parameters.zscore")
    if "entry-threshold" not in sub:
        _err("[strategy.parameters.zscore] missing required field 'entry-threshold'")
    entry = sub["entry-threshold"]
    if not isinstance(entry, (int, float)):
        _err("[strategy.parameters.zscore] 'entry-threshold' must be a number")
    entry = float(entry)
    if entry >= 0:
        _err("[strategy.parameters.zscore] 'entry-threshold' must be negative")
    if "exit-threshold" not in sub:
        _err("[strategy.parameters.zscore] missing required field 'exit-threshold'")
    exit_ = sub["exit-threshold"]
    if not isinstance(exit_, (int, float)):
        _err("[strategy.parameters.zscore] 'exit-threshold' must be a number")
    exit_ = float(exit_)
    if exit_ <= entry:
        _err("[strategy.parameters.zscore] 'exit-threshold' must be greater than 'entry-threshold'")
    return ZScoreParameters(period=period, entry_threshold=entry, exit_threshold=exit_)


def _parse_mean_reversion_parameters(params: dict, raw: dict) -> MeanReversionParameters:
    indicator = _require(params, "indicator", str, "strategy.parameters")
    if indicator not in VALID_INDICATORS:
        _err(
            f"[strategy.parameters] 'indicator' must be one of "
            f"{sorted(VALID_INDICATORS)}, got '{indicator}'"
        )

    if "max-holding-days" not in params:
        _err("[strategy.parameters] missing required field 'max-holding-days'")
    mhd_raw = params["max-holding-days"]
    if mhd_raw is False:
        max_holding_days = None
    elif isinstance(mhd_raw, int) and mhd_raw > 0:
        max_holding_days = mhd_raw
    else:
        _err("[strategy.parameters] 'max-holding-days' must be a positive integer or false")
        max_holding_days = None

    nested = raw.get("strategy", {}).get("parameters", {})

    rsi_params = None
    bb_params = None
    zs_params = None

    if indicator == "rsi":
        sub = nested.get("rsi")
        if not sub:
            _err("[strategy.parameters.rsi] section is required when indicator = 'rsi'")
        rsi_params = _parse_rsi(sub)
    elif indicator == "bollinger-bands":
        sub = nested.get("bollinger-bands")
        if not sub:
            _err(
                "[strategy.parameters.bollinger-bands] section is required "
                "when indicator = 'bollinger-bands'"
            )
        bb_params = _parse_bollinger_bands(sub)
    elif indicator == "zscore":
        sub = nested.get("zscore")
        if not sub:
            _err("[strategy.parameters.zscore] section is required when indicator = 'zscore'")
        zs_params = _parse_zscore(sub)

    return MeanReversionParameters(
        indicator=indicator,
        max_holding_days=max_holding_days,
        rsi=rsi_params,
        bollinger_bands=bb_params,
        zscore=zs_params,
    )


def _parse_pairs_parameters(params: dict) -> PairsParameters:
    if "pairs" not in params:
        _err("[strategy.parameters] missing required field 'pairs'")
    pairs_raw = params["pairs"]
    if not isinstance(pairs_raw, list) or len(pairs_raw) == 0:
        _err("[strategy.parameters] 'pairs' must be a non-empty list")
    pairs: list[list[str]] = []
    for i, p in enumerate(pairs_raw):
        if not isinstance(p, list) or len(p) != 2:
            _err(f"[strategy.parameters] 'pairs[{i}]' must be a list of exactly 2 ticker strings")
        if not all(isinstance(t, str) and t for t in p):
            _err(f"[strategy.parameters] 'pairs[{i}]' tickers must be non-empty strings")
        pairs.append([p[0].upper(), p[1].upper()])

    lp = _require_positive_int(params, "lookback-period", "strategy.parameters")
    entry = _require_positive_float(params, "entry-zscore", "strategy.parameters")
    exit_ = _require_positive_float(params, "exit-zscore", "strategy.parameters")
    stop = _require_positive_float(params, "stop-loss-zscore", "strategy.parameters")

    if exit_ >= entry:
        _err("[strategy.parameters] 'exit-zscore' must be less than 'entry-zscore'")
    if stop <= entry:
        _err("[strategy.parameters] 'stop-loss-zscore' must be greater than 'entry-zscore'")

    freq = _require(params, "recalculate-frequency", str, "strategy.parameters")
    if freq not in VALID_RECALCULATE_FREQUENCIES:
        _err(
            f"[strategy.parameters] 'recalculate-frequency' must be one of "
            f"{sorted(VALID_RECALCULATE_FREQUENCIES)}, got '{freq}'"
        )

    return PairsParameters(
        pairs=pairs,
        lookback_period=lp,
        entry_zscore=entry,
        exit_zscore=exit_,
        stop_loss_zscore=stop,
        recalculate_frequency=freq,
    )


def _parse_strategy(raw: dict) -> StrategyConfig:
    sec = raw.get("strategy")
    if not sec:
        _err("missing [strategy] section")

    name = _require(sec, "name", str, "strategy")
    if name not in VALID_STRATEGIES:
        _err(f"[strategy] 'name' must be one of {sorted(VALID_STRATEGIES)}, got '{name}'")

    universe = _require(sec, "universe", str, "strategy")
    if universe not in VALID_UNIVERSES:
        _err(f"[strategy] 'universe' must be one of {sorted(VALID_UNIVERSES)}, got '{universe}'")

    order_type = _require(sec, "order-type", str, "strategy")
    if order_type not in VALID_ORDER_TYPES:
        _err(
            f"[strategy] 'order-type' must be one of {sorted(VALID_ORDER_TYPES)}, "
            f"got '{order_type}'"
        )

    # long-only: required for momentum and mean-reversion, must be absent for pairs-trading
    long_only: bool | None = None
    if name == "pairs-trading":
        if "long-only" in sec:
            _err("[strategy] 'long-only' must not be set for pairs-trading")
    else:
        long_only = _require(sec, "long-only", bool, "strategy")

    # custom tickers
    custom_tickers: list[str] = []
    if universe == "custom" and name != "pairs-trading":
        if "tickers" not in sec:
            _err("[strategy] 'tickers' is required when universe = 'custom'")
        tickers_raw = sec["tickers"]
        if not isinstance(tickers_raw, list) or len(tickers_raw) == 0:
            _err("[strategy] 'tickers' must be a non-empty list of ticker strings")
        custom_tickers = [t.upper() for t in tickers_raw]

    params_raw = raw.get("strategy", {}).get("parameters")
    if params_raw is None:
        _err("missing [strategy.parameters] section")

    if name == "cross-sectional-momentum":
        parameters = _parse_momentum_parameters(params_raw)
    elif name == "mean-reversion":
        parameters = _parse_mean_reversion_parameters(params_raw, raw)
    else:
        parameters = _parse_pairs_parameters(params_raw)

    return StrategyConfig(
        name=name,
        universe=universe,
        long_only=long_only,
        order_type=order_type,
        parameters=parameters,
        custom_tickers=custom_tickers,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def load_config(path: str) -> Config:
    config_path = Path(path)
    if not config_path.exists():
        _err(f"config file not found: {path}")
    if not config_path.suffix == ".toml":
        _err(f"config file must have a .toml extension: {path}")

    with open(config_path, "rb") as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            _err(f"failed to parse TOML config: {e}")

    account = _parse_account(raw)
    strategy = _parse_strategy(raw)
    execution = _parse_execution(raw)
    risk = _parse_risk(raw)
    logging_cfg = _parse_logging(raw)

    return Config(
        account=account,
        strategy=strategy,
        execution=execution,
        risk=risk,
        logging=logging_cfg,
    )
