import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Literal, Mapping, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from ripple_tradePilot import __version__
from ripple_tradePilot.api.dashboard import DashboardDataError, DashboardService
from ripple_tradePilot.backtest.engine import run_backtest
from ripple_tradePilot.backtest.report import compute_metrics, compute_trade_stats
from ripple_tradePilot.backtest.rules import MarketRules, price_limit_for_symbol
from ripple_tradePilot.backtest.serialize import serialize_backtest_result
from ripple_tradePilot.models.types import Bar
from ripple_tradePilot.config_loader import get_vote_threshold, load_config
from ripple_tradePilot.signals.backtest_profile import (
    BACKTEST_STRATEGIES,
    PARAM_WHITELIST,
    IllegalParamError,
    UnknownProfileError,
    params_schema,
    resolve_backtest_strategy,
)
from ripple_tradePilot.data.market_service import (
    INDEX_CODES,
    aggregate_breadth,
    load_index_bars,
)
from ripple_tradePilot.data.stock_service import (
    InvalidStockSymbolError,
    StockDataService,
    StockDataUnavailableError,
)
from ripple_tradePilot.storage.database import (
    init_database,
    list_stock_catalog,
    load_daily_bars,
    load_industry_board_bars,
    load_industry_boards,
    load_industry_membership,
    load_market_daily,
    load_stock_quotes,
    stock_catalog_name,
    stock_catalog_names,
)
from ripple_tradePilot.storage.user_store import (
    SESSION_DAYS,
    BacktestNotFoundError,
    StrategyNotFoundError,
    UsernameTakenError,
    WatchlistExistsError,
    WatchlistNotFoundError,
    authenticate_user,
    create_session,
    create_strategy,
    create_user,
    delete_session,
    delete_user_backtest,
    ensure_system_strategies,
    get_user_backtest,
    list_user_backtests,
    list_user_stocks,
    record_backtest_run,
    list_visible_strategies,
    list_user_watchlist,
    set_watchlist_default_strategy,
    upsert_watchlist_item,
    update_strategy,
    update_strategy_visibility,
    user_for_session,
)

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "tradepilot_session"
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_database()
    _sync_configured_system_strategies()
    yield


app = FastAPI(title="TradePilot API", version=__version__, lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)


class StrategyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    asset_class: Literal["stock", "future"]
    symbol: str = Field(min_length=1, max_length=32)
    profile: str = Field(min_length=1, max_length=80)
    parameters: Dict[str, float]
    visibility: Literal["public", "private"] = "private"


class StrategyVisibilityUpdate(BaseModel):
    visibility: Literal["public", "private"]


class StrategyUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    profile: str = Field(min_length=1, max_length=80)
    parameters: Dict[str, float]
    visibility: Literal["public", "private"]


class DefaultStrategyUpdate(BaseModel):
    strategy_id: Optional[int] = Field(default=None, ge=1)


class WatchlistCreate(BaseModel):
    symbol: str = Field(min_length=6, max_length=16)


def _normalize_username(username: str) -> str:
    normalized = username.strip()
    if "@" in normalized:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
            raise HTTPException(status_code=422, detail="邮箱格式不正确")
        return normalized.lower()
    if len(normalized) > 32:
        raise HTTPException(status_code=422, detail="用户名不能超过 32 个字符")
    if not all(character.isalnum() or character in "_.-" for character in normalized):
        raise HTTPException(
            status_code=422,
            detail="用户名只能包含字母、数字、点、下划线和连字符，或使用邮箱",
        )
    return normalized


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=os.getenv("TRADEPILOT_SECURE_COOKIE", "false").lower() == "true",
        samesite="lax",
        path="/",
    )


def optional_user(request: Request) -> Optional[Dict]:
    return user_for_session(request.cookies.get(SESSION_COOKIE, ""))


def required_user(user: Optional[Dict] = Depends(optional_user)) -> Dict:
    if user is None:
        raise HTTPException(status_code=401, detail="请先注册或登录")
    return user


def _dashboard_for_user(user: Optional[Dict]) -> DashboardService:
    base_service = DashboardService()
    configured_items = {
        item["code"]: item for item in base_service.configured_assets()
    }
    configured = set(configured_items)
    catalog_names = stock_catalog_names()
    records = list_user_stocks(user["id"]) if user is not None else []
    visible_strategies = (
        {item["id"]: item for item in list_visible_strategies(user["id"])}
        if user is not None
        else {}
    )
    symbols = []
    for code, item in configured_items.items():
        if code in catalog_names:
            symbols.append({**item, "name": catalog_names[code]})
    for item in records:
        if not item["is_watched"]:
            continue
        is_user_added = item["symbol"] not in configured
        symbol = {
            "code": item["symbol"],
            "name": catalog_names.get(item["symbol"], item["name"]),
            "asset_class": "stock",
            "user_added": is_user_added,
        }
        if is_user_added:
            symbol["strategy_profile"] = "默认组合策略"
        default_strategy = visible_strategies.get(item.get("default_strategy_id"))
        if (
            default_strategy is not None
            and default_strategy["asset_class"] == "stock"
            and default_strategy["symbol"] == item["symbol"]
        ):
            symbol.update(
                {
                    "default_strategy_id": default_strategy["id"],
                    "default_strategy_name": default_strategy["name"],
                    "default_strategy_parameters": default_strategy["parameters"],
                }
            )
        symbols.append(symbol)
    excluded = [item["symbol"] for item in records if not item["is_watched"]]
    service = DashboardService(extra_symbols=symbols, excluded_symbols=excluded)
    # D5：注入 ML 预估打分器。用 service.backtest_db（已按 env/探测解析）查 ml_models，
    # 与看板其余读库口径同一个库文件。
    service.scorer = _ml_scorer(service.backtest_db)
    return service


def _ml_scorer(db_path):
    """取 D5 打分器（进程级缓存，在位 model_id 未变则复用已反序列化的工件）。

    **ML 支线的任何失败都退化为 ``None``**：sklearn/joblib 未安装、无 promoted 模型、工件
    文件损坏、DB 查询异常——看板照常出规则票（``vote_ratio``），只是 ``forecast`` 为 null。
    """
    try:
        from ripple_tradePilot.ml.scoring import get_scorer

        return get_scorer(db_path=db_path)
    except Exception as exc:
        logger.warning("ML 打分器不可用，看板降级为仅规则票：%s", exc)
        return None


def _sync_configured_system_strategies():
    templates = DashboardService().strategy_catalog()
    strategies_by_owner: Dict[str, list] = {}
    for item in templates:
        strategies_by_owner.setdefault(item["owner"], []).append(item)
    bound_keys = set()
    for owner, items in strategies_by_owner.items():
        bound_keys.update(ensure_system_strategies(owner, items))
    return templates, bound_keys


def _configured_stock_map() -> Dict[str, Dict]:
    return {
        item["code"]: item
        for item in DashboardService().configured_assets()
        if item.get("asset_class") == "stock"
    }


def _stock_records(user: Optional[Dict]) -> Dict[str, Dict]:
    if user is None:
        return {}
    return {item["symbol"]: item for item in list_user_stocks(user["id"])}


def _stock_catalog(user: Optional[Dict]) -> Dict:
    configured = _configured_stock_map()
    records = _stock_records(user)
    symbols: Dict[str, Dict] = {
        item["symbol"]: dict(item) for item in list_stock_catalog()
    }
    empty_market = {
        "market": "",
        "exchange": "",
        "board": "",
        "industry": "",
        "area": "",
        "list_status": "L",
        "list_date": "",
        "updated_at": None,
        "latest_date": None,
        "price": None,
        "change": None,
        "change_pct": None,
        "pre_close": None,
        "price_time": None,
        "price_source": None,
        "price_kind": "unavailable",
        "quote_time": None,
        "quote_volume": None,
        "quote_amount": None,
        "turnover_rate": None,
    }
    for code, item in configured.items():
        symbols.setdefault(
            code,
            {
                **empty_market,
                "symbol": code,
                "name": item.get("name", code),
                "source": "config",
            },
        )
    for code, item in records.items():
        symbols.setdefault(
            code,
            {
                **empty_market,
                "symbol": code,
                "name": item["name"],
                "source": "watchlist",
            },
        )

    items = []
    for code in sorted(symbols):
        symbol = symbols[code]
        record = records.get(code)
        watched = record["is_watched"] if record is not None else code in configured
        latest_date = symbol.get("latest_date")
        price_time = symbol.get("price_time") or latest_date
        freshness = "unavailable"
        if price_time:
            try:
                parsed = datetime.fromisoformat(str(price_time).replace("Z", "+00:00"))
            except ValueError:
                parsed = datetime.strptime(str(price_time)[:8], "%Y%m%d")
            freshness = (
                "fresh"
                if max((datetime.now().date() - parsed.date()).days, 0) <= 4
                else "stale"
            )
        item = {
            "symbol": code,
            "name": symbol.get("name", code),
            "market": symbol.get("market", ""),
            "exchange": symbol.get("exchange", ""),
            "board": symbol.get("board") or symbol.get("market", ""),
            "industry": symbol.get("industry", ""),
            "area": symbol.get("area", ""),
            "list_status": symbol.get("list_status", "L"),
            "list_date": symbol.get("list_date", ""),
            "catalog_updated_at": symbol.get("updated_at"),
            "is_watched": watched,
            "is_default": code in configured,
            "last_updated_at": record.get("last_updated_at") if record else None,
            "price": symbol.get("price"),
            "pre_close": symbol.get("pre_close"),
            "change": symbol.get("change"),
            "change_pct": symbol.get("change_pct"),
            "latest_date": latest_date,
            "price_time": price_time,
            "price_source": symbol.get("price_source"),
            "price_kind": symbol.get("price_kind", "unavailable"),
            "quote_time": symbol.get("quote_time"),
            "volume": symbol.get("quote_volume"),
            "amount": symbol.get("quote_amount"),
            "turnover_rate": symbol.get("turnover_rate"),
            "freshness": freshness,
        }
        items.append(item)
    return {"items": items}


def _stock_error(error: RuntimeError) -> HTTPException:
    status_code = 422 if isinstance(error, InvalidStockSymbolError) else 503
    return HTTPException(status_code=status_code, detail=str(error))


@app.get("/", include_in_schema=False)
def dashboard_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
def register(credentials: Credentials, response: Response):
    username = _normalize_username(credentials.username)
    try:
        user = create_user(username, credentials.password)
    except UsernameTakenError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    _sync_configured_system_strategies()
    _set_session_cookie(response, create_session(user["id"]))
    return {"user": user}


@app.post("/api/auth/login")
def login(credentials: Credentials, response: Response):
    username = _normalize_username(credentials.username)
    user = authenticate_user(username, credentials.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    _set_session_cookie(response, create_session(user["id"]))
    return {"user": user}


@app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response):
    delete_session(request.cookies.get(SESSION_COOKIE, ""))
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="lax")


@app.get("/api/auth/me")
def auth_me(user: Optional[Dict] = Depends(optional_user)):
    return {"user": user}


@app.get("/api/dashboard")
def dashboard(user: Optional[Dict] = Depends(optional_user)):
    return _dashboard_for_user(user).dashboard()


@app.get("/api/markets/{symbol}")
def market_detail(
    symbol: str,
    limit: int = Query(default=160, ge=40, le=260),
    strategy_id: Optional[int] = Query(default=None, ge=1),
    system_strategy: bool = Query(default=False),
    user: Optional[Dict] = Depends(optional_user),
):
    try:
        strategy = None
        if strategy_id is not None:
            if user is None:
                raise HTTPException(status_code=401, detail="请先注册或登录")
            strategy = next(
                (
                    item
                    for item in list_visible_strategies(user["id"])
                    if item["id"] == strategy_id and item["symbol"] == symbol
                ),
                None,
            )
            if strategy is None:
                raise HTTPException(status_code=404, detail="策略不存在、不可见或不适用于当前标的")

        detail = _dashboard_for_user(user).market_detail(
            symbol,
            limit=limit,
            profile_override=strategy["parameters"] if strategy else None,
            strategy_profile=strategy["name"] if strategy else None,
            use_default_strategy=not system_strategy,
        )
        if strategy is not None and strategy["asset_class"] != detail["asset_class"]:
            raise HTTPException(status_code=404, detail="策略不存在、不可见或不适用于当前标的")
        return detail
    except DashboardDataError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/strategies")
def strategies(user: Dict = Depends(required_user)):
    system_strategies, bound_keys = _sync_configured_system_strategies()
    user_strategies = list_visible_strategies(user["id"])
    unbound_system_strategies = [
        item for item in system_strategies if item["system_key"] not in bound_keys
    ]
    return {"items": user_strategies + unbound_system_strategies}


@app.post("/api/strategies", status_code=status.HTTP_201_CREATED)
def add_strategy(payload: StrategyCreate, user: Dict = Depends(required_user)):
    strategy = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    strategy["name"] = strategy["name"].strip()
    if strategy["asset_class"] == "stock":
        try:
            strategy["symbol"] = StockDataService.normalize_symbol(strategy["symbol"])
        except InvalidStockSymbolError as error:
            raise _stock_error(error) from error
        if (
            not stock_catalog_name(strategy["symbol"])
            and strategy["symbol"] not in _configured_stock_map()
        ):
            raise HTTPException(status_code=422, detail="股票标的必须来自全部数据池")
    else:
        strategy["symbol"] = strategy["symbol"].strip().upper()
    strategy["profile"] = strategy["profile"].strip()
    return {"item": create_strategy(user["id"], strategy)}


@app.patch("/api/strategies/{strategy_id}/visibility")
def change_strategy_visibility(
    strategy_id: int,
    payload: StrategyVisibilityUpdate,
    user: Dict = Depends(required_user),
):
    try:
        item = update_strategy_visibility(strategy_id, user["id"], payload.visibility)
    except StrategyNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"item": item}


@app.patch("/api/strategies/{strategy_id}")
def change_strategy(
    strategy_id: int,
    payload: StrategyUpdate,
    user: Dict = Depends(required_user),
):
    strategy = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    strategy["name"] = strategy["name"].strip()
    strategy["profile"] = strategy["profile"].strip()
    try:
        item = update_strategy(strategy_id, user["id"], strategy)
    except StrategyNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"item": item}


@app.get("/api/backtests")
def backtests(user: Dict = Depends(required_user)):
    futures_suffixes = (".CFFEX", ".SHFE", ".DCE", ".CZCE", ".INE", ".GFEX")
    items = [
        {
            **result,
            "asset_class": (
                "future"
                if str(result.get("symbol", "")).endswith(futures_suffixes)
                else "stock"
            ),
        }
        for result in list_user_backtests(user["id"])
    ]
    return {"items": items}


@app.delete("/api/backtests/{backtest_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_backtest(backtest_id: int, user: Dict = Depends(required_user)):
    """删除本人的回测记录；记录不存在或属于他人时返回 404。"""
    try:
        if not delete_user_backtest(backtest_id, user["id"]):
            raise BacktestNotFoundError("回测记录不存在或无权删除")
    except BacktestNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/backtests/{backtest_id}")
def backtest_detail(backtest_id: int, user: Dict = Depends(required_user)):
    """取单条回测记录的完整结果，供前端历史回放。

    记录不存在或属于他人 → 404；旧记录未保存明细 → 409（前端提示无法回放）。
    """
    record = get_user_backtest(backtest_id, user["id"])
    if record is None:
        raise HTTPException(status_code=404, detail="回测记录不存在或无权查看")
    raw = record.get("result_json")
    if not raw:
        raise HTTPException(status_code=409, detail="该记录未保存权益曲线与成交明细，无法回放（请重跑）")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=409, detail="回测明细解析失败，请重跑")
    return {"data": data}


@app.get("/api/watchlist")
def watchlist(user: Dict = Depends(required_user)):
    return {"items": list_user_watchlist(user["id"])}


@app.patch("/api/watchlist/{symbol}/default-strategy")
def change_watchlist_default_strategy(
    symbol: str,
    payload: DefaultStrategyUpdate,
    user: Dict = Depends(required_user),
):
    try:
        normalized = StockDataService.normalize_symbol(symbol)
    except InvalidStockSymbolError as error:
        raise _stock_error(error) from error

    configured = _configured_stock_map()
    records = _stock_records(user)
    record = records.get(normalized)
    is_watched = (
        record["is_watched"] if record is not None else normalized in configured
    )
    if not is_watched:
        raise HTTPException(status_code=404, detail="只能设置当前观察池股票的默认策略")

    if payload.strategy_id is not None:
        strategy = next(
            (
                item
                for item in list_visible_strategies(user["id"])
                if item["id"] == payload.strategy_id
                and item["asset_class"] == "stock"
                and item["symbol"] == normalized
            ),
            None,
        )
        if strategy is None:
            raise HTTPException(
                status_code=404,
                detail="策略不存在、不可见或不适用于当前标的",
            )

    if record is None:
        configured_item = configured[normalized]
        upsert_watchlist_item(
            user["id"],
            normalized,
            configured_item.get("name", normalized),
            True,
        )
    try:
        item = set_watchlist_default_strategy(
            user["id"], normalized, payload.strategy_id
        )
    except WatchlistNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"item": item}


@app.get("/api/stocks")
def stocks(user: Optional[Dict] = Depends(optional_user)):
    return _stock_catalog(user)


@app.post("/api/stocks/refresh")
def refresh_stocks(user: Dict = Depends(required_user)):
    try:
        return {"data": StockDataService().refresh_catalog()}
    except StockDataUnavailableError as error:
        raise _stock_error(error) from error


@app.post("/api/stocks/quotes/refresh")
def refresh_stock_quotes(user: Dict = Depends(required_user)):
    try:
        return {"data": StockDataService().refresh_quotes()}
    except StockDataUnavailableError as error:
        raise _stock_error(error) from error


# 市场总览：本地快照超过该时长视为过期，先尝试刷新一次
MARKET_OVERVIEW_MAX_AGE = timedelta(minutes=5)
# 涨跌停分类口径已移至 market_service.aggregate_breadth（C2，分板块判定）


def _in_trading_hours(moment: Optional[datetime] = None) -> bool:
    """是否处于 A 股常规交易时段（周一到周五 09:15–15:05，含集合竞价缓冲）。

    盘后/周末没有新行情，全市场快照刷新只会白白拖慢总览请求，
    因此只在此时段内允许总览触发 ``refresh_quotes``。不识别节假日，
    节假日当天会多触发一次刷新，但失败后会静默降级，无副作用。
    """
    now = moment or datetime.now()
    if now.weekday() >= 5:  # 周六/周日
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 5


def _parse_quote_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _latest_quote_time(rows) -> tuple:
    """返回 (最新 quote_time 的 datetime, 原始字符串)；无有效值返回 (None, "")。"""
    latest: Optional[datetime] = None
    raw = ""
    for row in rows:
        parsed = _parse_quote_time(row.get("quote_time"))
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
            raw = str(row.get("quote_time"))
    return latest, raw


def _market_overview_data() -> Dict[str, Any]:
    service = StockDataService()
    rows = load_stock_quotes()
    latest, quote_time = _latest_quote_time(rows)
    # 快照为空：无条件补一次行情；快照过期：只在交易时段内刷新，
    # 盘后/周末直接沿用旧快照并标记 stale，避免总览请求被全市场刷新拖慢
    snapshot_empty = not rows or latest is None
    snapshot_stale = (
        not snapshot_empty and datetime.now() - latest > MARKET_OVERVIEW_MAX_AGE
    )
    if snapshot_empty or (snapshot_stale and _in_trading_hours()):
        try:
            service.refresh_quotes()
        except StockDataUnavailableError:
            pass
        rows = load_stock_quotes()
        latest, quote_time = _latest_quote_time(rows)
    if not rows:
        raise StockDataUnavailableError("暂无全市场行情快照，请稍后重试")
    stale = latest is None or datetime.now() - latest > MARKET_OVERVIEW_MAX_AGE

    # C2：宽度聚合抽到 market_service.aggregate_breadth（共享纯函数，monitor 收盘例程
    # 与 data refresh-market 复用同一口径）。涨跌停分类改用 A7 price_limit_for_symbol
    # 分板块判定（主板 10%/创业板·科创板 20%/北交所 30%），替代旧的 ±9.8% 一刀切——
    # 顺带修复"创业板 +9.85% 被误判涨停"的口径错误。响应键沿用旧的 up/flat/down 命名。
    snapshot = aggregate_breadth(rows)
    breadth = {
        "total": snapshot["total"],
        "up": snapshot["advancers"],
        "flat": snapshot["unchanged"],
        "down": snapshot["decliners"],
        "limit_up": snapshot["limit_up"],
        "limit_down": snapshot["limit_down"],
    }
    turnover = snapshot["total_amount"]

    # 市场宽度优先用妙想（结构不保证，解析失败回退本地快照统计）
    breadth_from_mx = False
    try:
        mx_breadth = service.fetch_market_breadth_mx()
    except Exception:
        mx_breadth = None
    if mx_breadth:
        breadth["up"] = mx_breadth["up"]
        breadth["down"] = mx_breadth["down"]
        breadth["flat"] = mx_breadth["flat"]
        breadth["total"] = mx_breadth["up"] + mx_breadth["down"] + mx_breadth["flat"]
        breadth_from_mx = True

    total = breadth["total"]
    up_ratio = round(breadth["up"] / total, 2) if total else 0.0
    if up_ratio >= 0.6:
        label = "偏强"
    elif up_ratio <= 0.4:
        label = "偏弱"
    else:
        label = "均衡"

    # 涨跌幅榜：纯本地快照计算，不触发任何网络请求；
    # 跳过 change_pct 为 None 的行，名称取不到时回退为 symbol
    catalog_names = stock_catalog_names()

    def _mover(row: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "symbol": row["symbol"],
            "name": catalog_names.get(row["symbol"], row["symbol"]),
            "price": row.get("price"),
            "change_pct": row["change_pct"],
        }

    quoted = [row for row in rows if row.get("change_pct") is not None]
    movers = {
        "gainers": [
            _mover(row)
            for row in sorted(
                quoted, key=lambda item: item["change_pct"], reverse=True
            )[:5]
        ],
        "losers": [
            _mover(row)
            for row in sorted(quoted, key=lambda item: item["change_pct"])[:5]
        ],
    }

    indices_payload = service.fetch_index_quotes()
    indices = indices_payload.get("indices", [])
    index_source = indices_payload.get("source", "")
    index_label = {"mx": "mx", "sina": "sina", "akshare": "akshare"}.get(index_source, "")
    breadth_label = "mx" if breadth_from_mx else "snapshot"
    source_parts = []
    for part in (index_label, breadth_label):
        if part and part not in source_parts:
            source_parts.append(part)
    source = "+".join(source_parts) if source_parts else "snapshot"

    return {
        "quote_time": quote_time,
        "indices": indices,
        "breadth": breadth,
        "turnover": turnover,
        "sentiment": {"up_ratio": up_ratio, "label": label},
        "movers": movers,
        "stale": stale,
        "source": source,
    }


@app.get("/api/market/overview")
def market_overview(user: Dict = Depends(required_user)):
    try:
        return {"data": _market_overview_data()}
    except StockDataUnavailableError as error:
        raise _stock_error(error) from error


# ---------------------------------------------------------------------------
# C4：市场/行业历史只读端点（纯读 DB，不触发任何网络；登录可见）
# ---------------------------------------------------------------------------
_INDEX_NAME_BY_CODE = {code: name for code, name in INDEX_CODES}


def _validate_date_param(value: Optional[str], name: str) -> Optional[str]:
    """校验 ``YYYYMMDD`` 日期查询参数；空 → None，非法 → 422。"""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) != 8 or not text.isdigit():
        raise HTTPException(status_code=422, detail=f"{name} 应为 YYYYMMDD 8 位数字")
    return text


@app.get("/api/market/history")
def market_history(
    user: Dict = Depends(required_user),
    index_code: str = Query(default="000300.SH"),
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    limit: int = Query(default=250, ge=1, le=2000),
):
    """指数日线历史（C1 落库的 ``index_daily``）。``fetch=False`` 保证只读、零网络。"""
    rows = load_index_bars(
        index_code,
        start_date=_validate_date_param(start, "start"),
        end_date=_validate_date_param(end, "end"),
        fetch=False,
    )[-limit:]
    return {
        "data": {
            "index_code": index_code,
            "name": _INDEX_NAME_BY_CODE.get(index_code, ""),
            "count": len(rows),
            "rows": [dict(row) for row in rows],
        }
    }


@app.get("/api/market/breadth")
def market_breadth_history(
    user: Dict = Depends(required_user),
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    limit: int = Query(default=250, ge=1, le=2000),
):
    """市场宽度历史（C2 落库的 ``market_daily``）。

    无免费历史宽度 API → 增量积累制，新库可能为空（见 STRATEGIES.md C1/C2）。
    """
    rows = load_market_daily(
        start_date=_validate_date_param(start, "start"),
        end_date=_validate_date_param(end, "end"),
    )[-limit:]
    return {"data": {"count": len(rows), "rows": [dict(row) for row in rows]}}


@app.get("/api/industry/boards")
def industry_boards(user: Dict = Depends(required_user)):
    """行业板块登记（C3 落库的 ``industry_boards``）。"""
    boards = load_industry_boards()
    return {"data": {"count": len(boards), "boards": [dict(row) for row in boards]}}


@app.get("/api/industry/boards/{board_code}/bars")
def industry_board_bars(
    board_code: str,
    user: Dict = Depends(required_user),
    start: Optional[str] = Query(default=None),
    end: Optional[str] = Query(default=None),
    limit: int = Query(default=250, ge=1, le=2000),
):
    """板块日线历史（C3 落库的 ``industry_board_bars``）。"""
    start_date = _validate_date_param(start, "start")
    end_date = _validate_date_param(end, "end")
    rows = load_industry_board_bars(board_code)
    if start_date:
        rows = [row for row in rows if row["trade_date"] >= start_date]
    if end_date:
        rows = [row for row in rows if row["trade_date"] <= end_date]
    rows = rows[-limit:]
    return {"data": {"board_code": board_code, "count": len(rows), "rows": [dict(row) for row in rows]}}


@app.get("/api/industry/boards/{board_code}/members")
def industry_board_members(board_code: str, user: Dict = Depends(required_user)):
    """板块成分股最新快照（C3 落库的 ``industry_membership``，附 catalog 名称）。"""
    names = stock_catalog_names()
    members = [
        {
            "symbol": row["symbol"],
            "name": names.get(row["symbol"], row["symbol"]),
            "as_of": row["as_of"],
        }
        for row in load_industry_membership(board_code)
    ]
    return {"data": {"board_code": board_code, "count": len(members), "members": members}}


_BACKTEST_DEFAULT_STRATEGY = "rsi"
_BACKTEST_DEFAULT_EXECUTION = "next_open"


class BacktestParams(BaseModel):
    """回测参数（A5，全 Optional）：按 ``strategy`` 白名单校验，越界键 → 422。

    单策略用类构造器形参名（fast/slow/period/oversold/overbought/std_dev/signal/window）；
    combo_vote 用扁平三件套键（ma_fast/…/bb_std）+ vote_threshold(1~3)。同名字段（如 period）
    被多个单策略复用，白名单按 strategy 区分。Field 约束做值域预校验，构造器/parse_profile
    做最终合法性校验（非法值在端点转 422）。
    """

    # 单策略 canonical 形参名
    fast: Optional[int] = Field(None, ge=1)
    slow: Optional[int] = Field(None, ge=2)
    period: Optional[int] = Field(None, ge=2)
    oversold: Optional[float] = None
    overbought: Optional[float] = None
    std_dev: Optional[float] = Field(None, gt=0)
    signal: Optional[int] = Field(None, ge=1)
    window: Optional[int] = Field(None, ge=2)
    # combo_vote 扁平三件套键
    ma_fast: Optional[int] = Field(None, ge=1)
    ma_slow: Optional[int] = Field(None, ge=2)
    rsi_period: Optional[int] = Field(None, ge=2)
    rsi_oversold: Optional[float] = None
    rsi_overbought: Optional[float] = None
    bb_period: Optional[int] = Field(None, ge=2)
    bb_std: Optional[float] = Field(None, gt=0)
    vote_threshold: Optional[int] = Field(None, ge=1, le=3)


class BacktestRequest(BaseModel):
    symbol: str
    strategy: Literal["ma", "rsi", "macd", "bollinger", "donchian", "combo_vote"] = _BACKTEST_DEFAULT_STRATEGY
    bars: int = Field(252, ge=60, le=2500)
    cash: float = Field(100000.0, gt=0)
    execution: Literal["next_open", "close"] = _BACKTEST_DEFAULT_EXECUTION
    # 基准对比默认关：保持默认回测快、离线可测
    benchmark: bool = False
    # A5：显式参数 + 画像选择（"system" | config 画像名 | None）；解析优先级见
    # signals.backtest_profile.resolve_backtest_strategy（params > profile > 缺省链）
    params: Optional[BacktestParams] = None
    profile: Optional[str] = None

    @model_validator(mode="after")
    def _validate_params_whitelist(self) -> "BacktestRequest":
        """params 的键必须落在该 strategy 的白名单内，否则 422（pydantic ValidationError）。"""
        if self.params is None:
            return self
        clean = self.params.model_dump(exclude_none=True)
        if not clean:
            return self
        allowed = PARAM_WHITELIST.get(self.strategy, frozenset())
        illegal = sorted(key for key in clean if key not in allowed)
        if illegal:
            raise ValueError(
                f"策略 {self.strategy!r} 不接受参数 {', '.join(illegal)}；"
                f"可用参数：{', '.join(sorted(allowed)) or '（无）'}"
            )
        return self


# 策略/撮合模式中文名：单一来源，经 /api/meta/backtest-options 下发，
# 前端下拉与回测结果标题都以此为准（此前 index.html 与 app.js 各写一份，已漂移）。
# 策略键集与 signals.backtest_profile.BACKTEST_STRATEGIES 一致（四方一致性测试钉死）。
_BACKTEST_STRATEGY_LABELS = {
    "ma": "均线交叉 (MA)",
    "rsi": "RSI 反转",
    "macd": "MACD 趋势",
    "bollinger": "布林带",
    "donchian": "唐奇安通道",
    "combo_vote": "组合投票 (combo_vote)",
}

_BACKTEST_EXECUTION_LABELS = {
    "next_open": "次日开盘撮合",
    "close": "当日收盘撮合",
}


def _backtest_profile_options() -> list:
    """profile 下拉选项：``system``（按标的的系统策略）+ config.strategy_profiles 画像名。

    离线 / 无 config 时只返回 system 项，绝不抛错（meta 是公开端点）。
    """
    options = [{"value": "system", "label": "系统策略（按标的）"}]
    try:
        profiles = load_config().get("strategy_profiles") or {}
    except Exception:
        profiles = {}
    for name in sorted(profiles):
        options.append({"value": name, "label": f"配置画像：{name}"})
    return options


@app.get("/api/meta/backtest-options")
def backtest_options():
    """回测表单元数据（公开）：策略注册表 / 标签 / 参数 schema 单一来源，前端自动跟随。

    ``strategies[].params_schema`` 由 ``signals.backtest_profile.params_schema()`` 生成
    （单策略 default 取自类构造器 ``inspect.signature``，combo_vote 取自 profile.py 常量），
    前端据此动态渲染参数输入；``profiles`` 为画像下拉（system + config 画像名）。
    """
    schema = params_schema()
    return {
        "strategies": [
            {
                "value": key,
                "label": _BACKTEST_STRATEGY_LABELS[key],
                "params_schema": schema.get(key, []),
            }
            for key in BACKTEST_STRATEGIES
        ],
        "default_strategy": _BACKTEST_DEFAULT_STRATEGY,
        "executions": [
            {"value": key, "label": label}
            for key, label in _BACKTEST_EXECUTION_LABELS.items()
        ],
        "default_execution": _BACKTEST_DEFAULT_EXECUTION,
        "profiles": _backtest_profile_options(),
    }

_BENCHMARK_CODE = "000300.SH"
_BENCHMARK_NAME = "沪深300"


def _benchmark_unavailable() -> Dict[str, Any]:
    return {
        "code": _BENCHMARK_CODE,
        "name": _BENCHMARK_NAME,
        "available": False,
        "return": None,
        "curve": [],
    }


def _benchmark_payload(bars: list) -> Dict[str, Any]:
    """取沪深300基准并归一化（第一个点 value=1.0），日期区间与回测 bars 对齐。

    C1：改走 ``market_service.load_index_bars``（DB 优先 + 有界补拉，降级链
    tushare→akshare）；无数据 / 任何异常都返回 available=false，绝不抛错，
    保证离线也能优雅降级。归一化逻辑与迁移前一致，前端曲线不变。
    """
    if not bars:
        return _benchmark_unavailable()
    start_date = bars[0].timestamp.strftime("%Y%m%d")
    end_date = bars[-1].timestamp.strftime("%Y%m%d")
    try:
        rows = load_index_bars(
            _BENCHMARK_CODE, start_date=start_date, end_date=end_date
        )
    except Exception:
        logger.warning("沪深300基准获取失败，跳过基准对比", exc_info=True)
        return _benchmark_unavailable()

    if not rows:
        return _benchmark_unavailable()

    # 过滤无效收盘价，按首个有效点归一化；日期转为 YYYY-MM-DD
    curve = []
    base = None
    for row in rows:
        trade_date = str(row.get("trade_date") or "")
        if len(trade_date) != 8:
            continue
        try:
            close = float(row.get("close"))
        except (TypeError, ValueError):
            continue
        if close != close or close <= 0:
            continue
        if base is None:
            base = close
        curve.append(
            {
                "date": f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}",
                "value": round(close / base, 4),
            }
        )
    if not curve:
        return _benchmark_unavailable()
    return {
        "code": _BENCHMARK_CODE,
        "name": _BENCHMARK_NAME,
        "available": True,
        "return": round(curve[-1]["value"] - 1, 4),
        "curve": curve,
    }


@app.post("/api/backtest")
def run_web_backtest(payload: BacktestRequest, user: Dict = Depends(required_user)):
    """统一引擎回测：次日开盘撮合、涨跌停拦截、100 股整数倍、佣金+印花税+滑点。"""
    try:
        symbol = StockDataService.normalize_symbol(payload.symbol)
        # A5：解析回测策略（params > profile > 缺省链）+ provenance；非法参数/画像名 → 422
        config = load_config()
        try:
            resolved = resolve_backtest_strategy(
                symbol=symbol,
                strategy=payload.strategy,
                params=payload.params.model_dump(exclude_none=True) if payload.params else None,
                profile=payload.profile,
                config=config,
                default_threshold=get_vote_threshold(config),
            )
        except (IllegalParamError, UnknownProfileError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        rows = load_daily_bars(symbol)
        if len(rows) < payload.bars:
            # 本地日线不足时先刷新行情再回测
            StockDataService().refresh(symbol, initial_days=max(payload.bars + 60, 365))
            rows = load_daily_bars(symbol)
        if len(rows) < 60:
            raise StockDataUnavailableError(f"{symbol} 的日线数据不足，无法回测")

        rows = rows[-payload.bars:]
        bars = []
        for row in rows:
            open_price, high = float(row["open"]), float(row["high"])
            low, close = float(row["low"]), float(row["close"])
            if min(open_price, high, low, close) <= 0 or high < low:
                continue
            bars.append(
                Bar(
                    timestamp=datetime.strptime(row["trade_date"], "%Y%m%d"),
                    open=open_price,
                    high=high,
                    low=low,
                    close=close,
                    volume=float(row.get("vol") or 0),
                )
            )

        result = run_backtest(
            strategy=resolved.strategy,
            bars=bars,
            initial_cash=payload.cash,
            execution=payload.execution,
            market_rules=MarketRules(price_limit_pct=price_limit_for_symbol(symbol)),
        )
        metrics = compute_metrics(result.equity_curve, positions=result.positions)
        stats = compute_trade_stats(result.fills)
        data = serialize_backtest_result(
            symbol=symbol,
            strategy_key=resolved.strategy_key,
            execution=payload.execution,
            bars=bars,
            result=result,
            metrics=metrics,
            stats=stats,
            # 基准对比按需附加：仅 benchmark=true 时返回该字段，取不到则优雅降级
            benchmark=_benchmark_payload(bars) if payload.benchmark else None,
            # A5 provenance：来源 + 实际生效参数，进响应供前端结果卡片显示
            profile_source=resolved.profile_source,
            strategy_params=resolved.strategy_params,
        )
        # 落库到回测记录页（含完整结果，供历史回放）；记录失败不影响本次回测返回。
        # A5：改走 record_backtest_run（带 user_id），与 CLI 同表同格式，并写入 v12 溯源列
        # （run_kind/params_json/profile_source）——Web 回测的 provenance 由此可查。
        try:
            record_backtest_run(
                {
                    "symbol": symbol,
                    "name": stock_catalog_name(symbol) or symbol,
                    "start_date": bars[0].timestamp.strftime("%Y-%m-%d") if bars else "",
                    "end_date": bars[-1].timestamp.strftime("%Y-%m-%d") if bars else "",
                    "initial_capital": payload.cash,
                    "final_capital": (
                        result.equity_curve[-1] if result.equity_curve else payload.cash
                    ),
                    "total_return": metrics.total_return * 100,
                    "annual_return": metrics.annual_return * 100,
                    "max_drawdown": metrics.max_drawdown * 100,
                    "sharpe_ratio": metrics.sharpe,
                    "total_trades": stats.num_trades,
                    "win_rate": (stats.win_rate or 0.0) * 100,
                    "strategy_key": resolved.strategy_key,
                    "bar_count": len(bars),
                    "execution": payload.execution,
                    "result_json": json.dumps(data, ensure_ascii=False),
                },
                user_id=user["id"],
                run_kind="backtest",
                params_json=json.dumps(
                    {
                        "symbol": symbol,
                        "strategy": resolved.strategy_key,
                        "bars": payload.bars,
                        "cash": payload.cash,
                        "execution": payload.execution,
                        "profile": payload.profile,
                        "params": resolved.strategy_params,
                    },
                    ensure_ascii=False,
                ),
                profile_source=resolved.profile_source,
            )
        except Exception:
            logger.warning("回测结果落库失败：%s", symbol, exc_info=True)
        return {"data": data}
    except (InvalidStockSymbolError, StockDataUnavailableError) as error:
        raise _stock_error(error) from error


@app.post("/api/watchlist", status_code=status.HTTP_201_CREATED)
def add_to_watchlist(payload: WatchlistCreate, user: Dict = Depends(required_user)):
    service = StockDataService()
    try:
        symbol = service.normalize_symbol(payload.symbol)
        configured = _configured_stock_map()
        records = _stock_records(user)
        record = records.get(symbol)
        if (record and record["is_watched"]) or (record is None and symbol in configured):
            raise WatchlistExistsError("该股票已在观察池中")
        if record is not None or symbol in configured:
            name = record["name"] if record else configured[symbol].get("name", symbol)
            item = upsert_watchlist_item(user["id"], symbol, name, True)
            refreshed = None
        else:
            refreshed = service.refresh(symbol, initial_days=365)
            if refreshed["name"] == refreshed["symbol"]:
                raise StockDataUnavailableError(
                    f"已获取 {symbol} 的行情，但暂时无法解析股票名称，请稍后重试"
                )
            item = upsert_watchlist_item(
                user["id"],
                refreshed["symbol"],
                refreshed["name"],
                True,
                mark_updated=True,
            )
    except WatchlistExistsError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (InvalidStockSymbolError, StockDataUnavailableError) as error:
        raise _stock_error(error) from error
    return {"item": item, "data": refreshed}


@app.post("/api/watchlist/{symbol}/refresh")
def refresh_watchlist_stock(symbol: str, user: Dict = Depends(required_user)):
    service = StockDataService()
    try:
        normalized = service.normalize_symbol(symbol)
        records = _stock_records(user)
        record = records.get(normalized)
        is_watched = (
            record["is_watched"]
            if record is not None
            else normalized in _configured_stock_map()
        )
        if not is_watched:
            raise WatchlistNotFoundError("只能更新当前观察池中的股票")
        refreshed = service.refresh(normalized, initial_days=365)
        refreshed_name = (
            record["name"]
            if record is not None
            else _configured_stock_map()[normalized].get("name", normalized)
        )
        upsert_watchlist_item(
            user["id"],
            normalized,
            refreshed_name,
            True,
            mark_updated=True,
        )
    except WatchlistNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (InvalidStockSymbolError, StockDataUnavailableError) as error:
        raise _stock_error(error) from error
    return {"data": refreshed}


@app.delete("/api/watchlist/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
def remove_from_watchlist(symbol: str, user: Dict = Depends(required_user)):
    try:
        normalized = StockDataService.normalize_symbol(symbol)
        configured = _configured_stock_map()
        records = _stock_records(user)
        record = records.get(normalized)
        is_watched = (
            record["is_watched"] if record is not None else normalized in configured
        )
        if not is_watched:
            raise WatchlistNotFoundError("观察池中不存在该股票")
        name = (
            record["name"]
            if record is not None
            else configured[normalized].get("name", normalized)
        )
        upsert_watchlist_item(user["id"], normalized, name, False)
    except InvalidStockSymbolError as error:
        raise _stock_error(error) from error
    except WatchlistNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
