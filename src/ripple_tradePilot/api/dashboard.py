from __future__ import annotations

import csv
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ripple_tradePilot.config_loader import get_vote_threshold, load_config
from ripple_tradePilot.signals import (
    REC_BUY,
    REC_CONFLICT,
    REC_HOLD,
    REC_SELL,
    ProfileSpec,
    evaluate_symbol,
)
from ripple_tradePilot.storage.user_store import get_system_strategy


class DashboardDataError(RuntimeError):
    pass


def _flat_parameters(spec: ProfileSpec) -> Dict[str, Any]:
    """ProfileSpec → 前端消费的扁平参数字典（三件套口径，缺省回退默认值）。"""
    summary = spec.params_summary()
    ma = summary.get("ma", {})
    rsi = summary.get("rsi", {})
    bb = summary.get("bollinger", {})
    return {
        "ma_fast": ma.get("fast", 5),
        "ma_slow": ma.get("slow", 20),
        "rsi_period": rsi.get("period", 14),
        "rsi_oversold": rsi.get("oversold", 30),
        "rsi_overbought": rsi.get("overbought", 70),
        "bb_period": bb.get("period", 20),
        "bb_std": bb.get("num_std", 2.0),
        "vote_threshold": spec.vote_threshold,
    }


def _component_detail(decision, kind: str, key: str) -> Optional[float]:
    """从某个决策里取指定类型组件的指标值（图表叠加线与指标面板用）。"""
    for component in decision.components:
        if component.kind == kind:
            return component.detail.get(key)
    return None


def _vote_ratio_pct(decision) -> int:
    """规则票占比（0-100 整数）。取代旧 confidence——这是投票占比，不是概率。"""
    return round(decision.vote_ratio * 100)


class DashboardService:
    def __init__(
        self,
        data_dir: Optional[Path] = None,
        config_path: Optional[Path] = None,
        backtest_db: Optional[Path] = None,
        extra_symbols: Optional[Sequence[Dict[str, Any]]] = None,
        excluded_symbols: Optional[Sequence[str]] = None,
    ):
        self.data_dir = data_dir or Path(os.getenv("TRADEPILOT_DATA_DIR", Path.cwd() / "data"))
        self.config_path = config_path or Path(os.getenv("TRADEPILOT_CONFIG", Path.cwd() / "config.yaml"))
        self.backtest_db = backtest_db or self._resolve_backtest_db()
        self.extra_symbols = list(extra_symbols or [])
        self.excluded_symbols = {
            str(symbol).upper() for symbol in (excluded_symbols or [])
        }

    def _resolve_backtest_db(self) -> Path:
        configured = os.getenv("TRADEPILOT_BACKTEST_DB")
        if configured:
            return Path(configured)

        candidates = [
            self.data_dir / "backtest" / "backtest_results.db",
            Path.cwd() / "src" / "data" / "backtest" / "backtest_results.db",
        ]
        return next((path for path in candidates if path.exists()), candidates[0])

    def _config(self) -> Dict[str, Any]:
        return load_config(str(self.config_path))

    def _symbols(self) -> List[Dict[str, Any]]:
        config = self._config()
        assets = []
        for symbol in config.get("symbols", []):
            assets.append({**symbol, "asset_class": symbol.get("asset_class", "stock")})
        for future in config.get("futures", []):
            assets.append({**future, "asset_class": "future"})
        assets.extend(self.extra_symbols)
        unique: Dict[str, Dict[str, Any]] = {}
        for asset in assets:
            code = str(asset.get("code", "")).upper()
            if code and code not in self.excluded_symbols:
                unique[code] = {**unique.get(code, {}), **asset, "code": code}
        return list(unique.values())

    def configured_assets(self) -> List[Dict[str, Any]]:
        config = self._config()
        return [
            {
                **symbol,
                "code": str(symbol.get("code", "")).upper(),
                "asset_class": symbol.get("asset_class", "stock"),
            }
            for symbol in config.get("symbols", [])
            if symbol.get("code")
        ]

    def _profile(self, symbol: Dict[str, Any]) -> Dict[str, Any]:
        config = self._config()
        profiles = {
            **config.get("strategy_profiles", {}),
            **config.get("futures_strategy_profiles", {}),
        }
        return profiles.get(symbol.get("strategy_profile", ""), {})

    def _system_strategy(self, symbol: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.backtest_db.exists():
            return None
        return get_system_strategy(
            symbol["code"], symbol.get("asset_class", "stock"), self.backtest_db
        )

    def _read_bars(self, symbol: str) -> List[Dict[str, Any]]:
        database_bars = self._read_database_bars(symbol)
        if database_bars:
            return database_bars

        path = self.data_dir / f"{symbol}.csv"
        if not path.exists():
            raise DashboardDataError(f"行情文件不存在: {path.name}")

        bars: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                raw_date = str(row.get("trade_date") or row.get("timestamp") or "")
                try:
                    timestamp = datetime.strptime(raw_date[:8], "%Y%m%d")
                except ValueError:
                    try:
                        timestamp = datetime.fromisoformat(raw_date)
                    except ValueError:
                        continue

                try:
                    bars.append(
                        {
                            "timestamp": timestamp,
                            "open": float(row["open"]),
                            "high": float(row["high"]),
                            "low": float(row["low"]),
                            "close": float(row["close"]),
                            "volume": float(row.get("vol") or row.get("volume") or 0),
                        }
                    )
                except (KeyError, TypeError, ValueError):
                    continue

        bars.sort(key=lambda item: item["timestamp"])
        if not bars:
            raise DashboardDataError(f"行情文件无有效数据: {path.name}")
        return bars

    def _read_database_bars(self, symbol: str) -> List[Dict[str, Any]]:
        if not self.backtest_db.exists():
            return []
        try:
            with sqlite3.connect(self.backtest_db) as connection:
                rows = connection.execute(
                    """
                    SELECT trade_date, open, high, low, close, volume
                    FROM daily_bars
                    WHERE symbol = ?
                    ORDER BY trade_date
                    """,
                    (symbol,),
                ).fetchall()
        except sqlite3.Error:
            return []
        bars = []
        for trade_date, open_price, high, low, close, volume in rows:
            try:
                timestamp = datetime.strptime(str(trade_date)[:10], "%Y-%m-%d")
            except ValueError:
                try:
                    timestamp = datetime.strptime(str(trade_date)[:8], "%Y%m%d")
                except ValueError:
                    continue
            bars.append(
                {
                    "timestamp": timestamp,
                    "open": float(open_price),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                    "volume": float(volume or 0),
                }
            )
        return bars

    def market_detail(
        self,
        symbol_code: str,
        limit: int = 160,
        profile_override: Optional[Dict[str, Any]] = None,
        strategy_profile: Optional[str] = None,
        use_default_strategy: bool = True,
    ) -> Dict[str, Any]:
        symbol = next((item for item in self._symbols() if item["code"] == symbol_code), None)
        if symbol is None:
            raise DashboardDataError(f"未配置标的: {symbol_code}")

        bars = self._read_bars(symbol_code)
        system_strategy = self._system_strategy(symbol)
        default_parameters = (
            symbol.get("default_strategy_parameters")
            if use_default_strategy
            else None
        )
        effective_override = (
            profile_override
            if profile_override is not None
            else default_parameters
            if default_parameters is not None
            else system_strategy["parameters"]
            if system_strategy is not None
            else None
        )
        profile = (
            effective_override
            if effective_override is not None
            else self._profile(symbol)
        )
        effective_strategy_profile = (
            strategy_profile
            or (
                symbol.get("default_strategy_name")
                if use_default_strategy
                else None
            )
            or (system_strategy["profile"] if system_strategy is not None else None)
            or symbol.get("strategy_profile", "未配置")
        )
        # 统一投票口径：dashboard / monitor / 回测共用 signals.evaluate_symbol（A1 单一真源）。
        if profile_override is not None:
            profile_source = "explicit"
        elif default_parameters is not None:
            profile_source = "default_strategy"
        elif system_strategy is not None:
            profile_source = "system"
        else:
            profile_source = "config"
        evaluation = evaluate_symbol(
            bars,
            profile,
            default_threshold=get_vote_threshold(self._config()),
            source=profile_source,
        )
        spec = evaluation.spec
        parameters = _flat_parameters(spec)
        decisions = evaluation.decisions

        # 信号列表 = 决策转移点（进入 BUY/SELL 的边沿事件），与回测/监控同源
        signals: List[Dict[str, Any]] = [
            {
                "date": bars[event.index]["timestamp"].date().isoformat(),
                "side": event.side.value,
                "price": bars[event.index]["close"],
                "reason": event.decision.reason,
            }
            for event in evaluation.events
        ]

        latest = bars[-1]
        previous = bars[-2] if len(bars) > 1 else latest
        latest_decision = decisions[-1]
        lag_days = max((datetime.now().date() - latest["timestamp"].date()).days, 0)
        start = max(len(bars) - max(40, min(limit, 260)), 0)

        chart_bars = []
        for index in range(start, len(bars)):
            bar = bars[index]
            decision = decisions[index]
            chart_bars.append(
                {
                    "date": bar["timestamp"].date().isoformat(),
                    "open": bar["open"],
                    "high": bar["high"],
                    "low": bar["low"],
                    "close": bar["close"],
                    "volume": bar["volume"],
                    "ma_fast": _component_detail(decision, "ma", "fast_ma"),
                    "ma_slow": _component_detail(decision, "ma", "slow_ma"),
                    "bb_upper": _component_detail(decision, "bollinger", "upper"),
                    "bb_middle": _component_detail(decision, "bollinger", "middle"),
                    "bb_lower": _component_detail(decision, "bollinger", "lower"),
                }
            )

        return {
            "symbol": symbol_code,
            "name": symbol.get("name", symbol_code),
            "asset_class": symbol.get("asset_class", "stock"),
            "exchange": symbol.get("exchange", symbol_code.rsplit(".", 1)[-1] if "." in symbol_code else ""),
            "strategy_profile": effective_strategy_profile,
            "system_strategy_profile": (
                system_strategy["profile"]
                if system_strategy is not None
                else symbol.get("strategy_profile", "未配置")
            ),
            "default_strategy_id": symbol.get("default_strategy_id"),
            "profile_kind": spec.kind if effective_override is not None or profile else "unknown",
            "profile_source": profile_source,
            "parameters": parameters,
            "price": latest["close"],
            "change": latest["close"] - previous["close"],
            "change_pct": (latest["close"] / previous["close"] - 1) * 100 if previous["close"] else 0,
            "latest_date": latest["timestamp"].date().isoformat(),
            "freshness": "fresh" if lag_days <= 4 else "stale",
            "lag_days": lag_days,
            "recommendation": latest_decision.recommendation,
            # vote_ratio：规则票占比（0-100），取代旧 confidence 伪概率——它不是概率
            "vote_ratio": _vote_ratio_pct(latest_decision),
            "is_conflict": latest_decision.is_conflict,
            "buy_count": latest_decision.buy_count,
            "sell_count": latest_decision.sell_count,
            "vote_threshold": latest_decision.vote_threshold,
            "votes": latest_decision.votes,
            "reason": latest_decision.reason,
            "indicators": {
                "ma_fast": _component_detail(latest_decision, "ma", "fast_ma"),
                "ma_slow": _component_detail(latest_decision, "ma", "slow_ma"),
                "rsi": _component_detail(latest_decision, "rsi", "rsi"),
                "bb_upper": _component_detail(latest_decision, "bollinger", "upper"),
                "bb_middle": _component_detail(latest_decision, "bollinger", "middle"),
                "bb_lower": _component_detail(latest_decision, "bollinger", "lower"),
            },
            "bars": chart_bars,
            "signals": signals[-8:][::-1],
            "total_rows": len(bars),
            "user_added": bool(symbol.get("user_added")),
        }

    def strategy_catalog(self) -> List[Dict[str, Any]]:
        configured_owner = str(
            self._config().get("strategy_owner") or "TradePilot"
        ).strip()
        strategies = []
        for symbol in self._symbols():
            try:
                item = self.market_detail(symbol["code"])
            except DashboardDataError:
                continue
            strategies.append(
                {
                    "id": f"system:{item['symbol']}",
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "asset_class": item["asset_class"],
                    "profile": item["strategy_profile"],
                    "kind": item["profile_kind"],
                    "parameters": item["parameters"],
                    "recommendation": item["recommendation"],
                    "vote_ratio": item["vote_ratio"],
                    "is_conflict": item["is_conflict"],
                    "visibility": "public",
                    "owner": str(
                        symbol.get("strategy_owner") or configured_owner
                    ).strip(),
                    "is_owner": False,
                    "is_system": True,
                    "system_key": f"{item['asset_class']}:{item['symbol']}",
                }
            )
        return strategies

    def dashboard(self) -> Dict[str, Any]:
        details = []
        errors = []
        for symbol in self._symbols():
            try:
                details.append(self.market_detail(symbol["code"]))
            except DashboardDataError as error:
                errors.append(
                    {
                        "symbol": symbol["code"],
                        "asset_class": symbol.get("asset_class", "stock"),
                        "error": str(error),
                    }
                )

        # CONFLICT 是统一口径新增的第四态（组件多空分歧/票数不足），单列计数，
        # 不再被静默并入 HOLD——buy+sell+hold+conflict == symbols 恒成立。
        recommendation_counts = {
            side: sum(item["recommendation"] == side for item in details)
            for side in (REC_BUY, REC_SELL, REC_HOLD, REC_CONFLICT)
        }
        latest_date = max((item["latest_date"] for item in details), default=None)

        def _asset_count(asset_class: str, recommendation: str) -> int:
            return sum(
                item["asset_class"] == asset_class
                and item["recommendation"] == recommendation
                for item in details
            )

        asset_counts = {
            asset_class: {
                "configured": sum(item.get("asset_class") == asset_class for item in self._symbols()),
                "available": sum(item["asset_class"] == asset_class for item in details),
                "buy": _asset_count(asset_class, REC_BUY),
                "sell": _asset_count(asset_class, REC_SELL),
                "hold": _asset_count(asset_class, REC_HOLD),
                "conflict": _asset_count(asset_class, REC_CONFLICT),
            }
            for asset_class in ("stock", "future")
        }
        return {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "summary": {
                "symbols": len(details),
                "buy": recommendation_counts[REC_BUY],
                "sell": recommendation_counts[REC_SELL],
                "hold": recommendation_counts[REC_HOLD],
                "conflict": recommendation_counts[REC_CONFLICT],
                "stale": sum(item["freshness"] == "stale" for item in details),
                "latest_date": latest_date,
                "by_asset": asset_counts,
            },
            "markets": details,
            "system": {
                "api": "online",
                "data_source": "SQLite daily bars + CSV legacy fallback",
                "database": "SQLite unified storage",
                "config_path": str(self.config_path),
                "data_dir": str(self.data_dir),
                "errors": errors,
            },
        }
