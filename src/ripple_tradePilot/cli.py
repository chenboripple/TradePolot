"""
TradePilot 命令行工具
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import click

from . import __version__
from .config_loader import load_config, init_config, get_tushare_token, get_vote_threshold
from .signals.backtest_profile import (
    BACKTEST_STRATEGIES,
    COMBO_VOTE,
    PARAM_WHITELIST,
    IllegalParamError,
    UnknownProfileError,
    build_strategy,
    resolve_backtest_strategy,
)


def _coerce_number(text: str):
    """--param 值数值化：能转 int 转 int，其次 float，否则原样字符串。"""
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def _parse_cli_params(param_pairs, vote_threshold):
    """``--param key=value``（可多次）+ ``--vote-threshold`` → params dict。

    key=value 形式非法 → click.BadParameter（用法错误，退出码 2）。值经 _coerce_number
    数值化；白名单/值域校验交给 resolve_backtest_strategy（非法 → IllegalParamError）。
    """
    params = {}
    for pair in param_pairs or ():
        if '=' not in pair:
            raise click.BadParameter(f"--param 需为 key=value 形式，收到 {pair!r}")
        key, _, raw = pair.partition('=')
        key = key.strip()
        if not key:
            raise click.BadParameter(f"--param 缺少键名：{pair!r}")
        params[key] = _coerce_number(raw.strip())
    if vote_threshold is not None:
        params['vote_threshold'] = vote_threshold
    return params


def _bars_to_daily_rows(bars):
    """Bar 列表 → upsert_daily_bars 行格式（含 pre_close/change/pct_chg，供 A9 巡检一致）。

    仅 --write-signals 用：把本次回测所用 bars 落库，使写入台账的信号能立即用同源
    bars 回填（entry/exit 口径与信号一致），避免"信号用一套价、回填用另一套价"。
    """
    rows = []
    previous_close = None
    for bar in bars:
        pre_close = previous_close if previous_close is not None else bar.open
        change = bar.close - pre_close
        pct_chg = (change / pre_close * 100.0) if pre_close else 0.0
        rows.append({
            "trade_date": bar.timestamp.strftime("%Y%m%d"),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "pre_close": round(pre_close, 4),
            "change": round(change, 4),
            "pct_chg": round(pct_chg, 4),
            "vol": bar.volume,
            "amount": round(bar.close * bar.volume, 2),
        })
        previous_close = bar.close
    return rows


@click.group()
def cli():
    """TradePilot - 波段交易系统"""
    pass


@cli.command()
@click.option('--config', '-c', type=click.Path(), help='配置文件路径')
def init(config):
    """初始化配置文件"""
    config_path = Path(config) if config else None
    init_config(config_path)
    target = config_path or Path.home() / ".tradepilot" / "config.yaml"
    click.echo(f"✅ 配置文件已创建: {target}")
    click.echo("请编辑配置文件设置您的 API Key")


@cli.command()
@click.argument('symbol')
@click.option('--days', '-d', type=int, default=252, help='回测天数')
@click.option('--strategy', '-s', type=click.Choice(sorted(BACKTEST_STRATEGIES)), default='rsi',
              help='策略名称（combo_vote=统一投票口径）')
@click.option('--cash', type=float, default=100000.0, help='初始资金')
@click.option('--execution', type=click.Choice(['next_open', 'close']), default='next_open',
              help='撮合模式：next_open=信号次日开盘成交（默认，贴近实盘），close=当根收盘（偏乐观）')
@click.option('--benchmark', '-b', is_flag=True, help='对比沪深300基准（超额收益/相对回撤）')
@click.option('--ledger', is_flag=True, help='把本次模拟成交记入模拟盘账本（跨会话可查）')
@click.option('--no-save', is_flag=True, help='不落库（默认写入 backtest_results 供前端回放/CLI 列表）')
@click.option('--write-signals', is_flag=True,
              help='把本次 combo 投票的 BUY 事件写入信号台账（并把 bars 落库以便立即回填）')
@click.option('--profile', default=None,
              help='策略画像：system=按标的系统策略；或 config.strategy_profiles 中的画像名（仅 combo_vote 生效）')
@click.option('--param', '-p', multiple=True, metavar='KEY=VALUE',
              help='显式策略参数（可多次），如 -p fast=5 -p slow=20；按策略白名单校验')
@click.option('--vote-threshold', type=click.IntRange(1, 3), default=None,
              help='combo_vote 投票阈值（1~3），等价于 -p vote_threshold=N')
def backtest(symbol, days, strategy, cash, execution, benchmark, ledger, no_save, write_signals,
             profile, param, vote_threshold):
    """运行回测（统一引擎：涨跌停/100 股整数倍/佣金+印花税+滑点）"""
    from .backtest.engine import run_backtest
    from .backtest.report import compute_metrics, compute_trade_stats
    from .backtest.rules import MarketRules, price_limit_for_symbol
    from .backtest.serialize import serialize_backtest_result
    from .data.tushare_loader import TushareDataLoader

    try:
        config = load_config()
        token = get_tushare_token(config)
    except Exception as e:
        click.echo(f"❌ 无法加载配置：{e}", err=True)
        sys.exit(1)

    loader = TushareDataLoader(token, rate_limit_delay=float(config.get('tushare', {}).get('rate_limit_delay', 1.5)))
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')

    click.echo(f"加载行情：{symbol} {start_date} ~ {end_date}（前复权）")
    bars = list(loader.load_bars(symbol, start_date=start_date, end_date=end_date))
    if len(bars) < 30:
        click.echo(f"❌ 行情数据不足（{len(bars)} 条），请检查 token 权限或股票代码", err=True)
        sys.exit(1)

    # A5：解析口径与 Web 端共用 resolve_backtest_strategy（显式 params > profile > 缺省链）。
    # 单策略默认 rsi 无参 → profile_source="default"；combo_vote 走 monitor 同款画像链。
    try:
        cli_params = _parse_cli_params(param, vote_threshold)
        resolved = resolve_backtest_strategy(
            symbol=symbol, strategy=strategy, params=cli_params or None,
            profile=profile, config=config, default_threshold=get_vote_threshold(config),
        )
    except (IllegalParamError, UnknownProfileError, ValueError) as e:
        click.echo(f"❌ 策略解析失败：{e}", err=True)
        sys.exit(2)

    limit_pct = price_limit_for_symbol(symbol)
    result = run_backtest(
        strategy=resolved.strategy,
        bars=bars,
        initial_cash=cash,
        execution=execution,
        market_rules=MarketRules(price_limit_pct=limit_pct),
    )

    metrics = compute_metrics(result.equity_curve, positions=result.positions)
    stats = compute_trade_stats(result.fills)

    click.echo(
        f"\n📊 回测结果（{symbol} / {resolved.strategy_key} / {len(bars)} 根 K 线"
        f" / 撮合={execution} / 涨跌停±{limit_pct:.0%}）"
    )
    click.echo(f"   画像来源：{resolved.profile_source}   参数：{resolved.strategy_params}")
    click.echo(f"   总收益：{metrics.total_return:+.2%}   年化：{metrics.annual_return:+.2%}")
    click.echo(f"   最大回撤：{metrics.max_drawdown:.2%}   夏普：{metrics.sharpe:.2f}")
    click.echo(f"   交易回合：{stats.num_trades}   胜率：{stats.win_rate:.0%}   总费用：{stats.total_fees:.2f} 元")
    if benchmark:
        from .backtest.report import compare_with_benchmark
        # C1：基准改走 DB 优先的 load_index_bars（离线可测，降级链 tushare→akshare）
        from .data.market_service import load_index_bars
        index_rows = load_index_bars('000300.SH', start_date=start_date, end_date=end_date)
        closes = [float(row['close']) for row in index_rows if row.get('close') is not None]
        if closes:
            comparison = compare_with_benchmark(result.equity_curve, closes)
            click.echo(
                f"   沪深300基准：{comparison.benchmark_return:+.2%}"
                f"   超额收益：{comparison.excess_return:+.2%}"
                f"   回撤改善：{comparison.drawdown_improvement:+.2%}"
            )
        else:
            click.echo("   ⚠️ 未取到沪深300基准数据，跳过基准对比")
    if result.halted_by_drawdown:
        click.echo("   ⚠️ 回撤闸门曾触发：其后不再开新仓")
    if result.skipped_fills:
        click.echo(f"   ⚠️ {len(result.skipped_fills)} 次委托因涨跌停无法成交")
    if ledger:
        import uuid
        from .storage.paper_ledger import record_run
        run_id = record_run(
            run_id=str(uuid.uuid4()),
            symbol=symbol,
            strategy=strategy,
            initial_cash=cash,
            final_equity=result.equity_curve[-1] if result.equity_curve else cash,
            fills=result.fills,
        )
        click.echo(f"   💾 已记入模拟盘账本：run_id={run_id}")
    saved_run_id = None
    if not no_save:
        import json
        from .storage.database import stock_catalog_name
        from .storage.user_store import record_backtest_run
        try:
            data = serialize_backtest_result(
                symbol=symbol, strategy_key=resolved.strategy_key, execution=execution,
                bars=bars, result=result, metrics=metrics, stats=stats,
                profile_source=resolved.profile_source,
                strategy_params=resolved.strategy_params,
            )
            saved_run_id = record_backtest_run(
                {
                    "symbol": symbol,
                    "name": stock_catalog_name(symbol) or symbol,
                    "start_date": bars[0].timestamp.strftime("%Y-%m-%d") if bars else "",
                    "end_date": bars[-1].timestamp.strftime("%Y-%m-%d") if bars else "",
                    "initial_capital": cash,
                    "final_capital": result.equity_curve[-1] if result.equity_curve else cash,
                    "total_return": metrics.total_return * 100,
                    "annual_return": metrics.annual_return * 100,
                    "max_drawdown": metrics.max_drawdown * 100,
                    "sharpe_ratio": metrics.sharpe,
                    "total_trades": stats.num_trades,
                    "win_rate": (stats.win_rate or 0.0) * 100,
                    "strategy_key": resolved.strategy_key,
                    "bar_count": len(bars),
                    "execution": execution,
                    "result_json": json.dumps(data, ensure_ascii=False),
                },
                run_kind="backtest",
                params_json=json.dumps(
                    {"days": days, "strategy": resolved.strategy_key, "cash": cash,
                     "execution": execution, "profile": profile, "params": cli_params},
                    ensure_ascii=False,
                ),
                profile_source=resolved.profile_source,
            )
            click.echo(f"   💾 已落库：backtest_results id={saved_run_id}（tradepilot backtests list 查看）")
        except Exception as e:
            click.echo(f"   ⚠️ 回测落库失败（不影响本次结果）：{e}", err=True)
    if write_signals:
        from .signals.facade import evaluate_symbol
        from .storage.database import upsert_daily_bars
        from .storage.signal_ledger import record_decision
        try:
            # 把本次回测 bars 落库，使写入的台账信号能用同源 bars 立即回填
            anchor = bars[-1].timestamp.strftime("%Y%m%d") if bars else ""
            upsert_daily_bars(
                symbol, _bars_to_daily_rows(bars), source="cli-backtest",
                adjust="qfq", adj_anchor_date=anchor,
                data_version=f"cli-backtest|{anchor}|--write-signals",
            )
            # 用 resolved.spec（combo_vote 时为解析出的画像；单策略为 None→facade 缺省 combo）
            # 评估事件，profile_name 记 resolved.profile_source 与本次回测口径一致可溯源。
            evaluation = evaluate_symbol(bars, resolved.spec)
            buy_events = [e for e in evaluation.events if e.decision.recommendation == "BUY"]
            for event in buy_events:
                record_decision(
                    symbol, event.decision, source="backtest", provisional=0,
                    trade_date=event.timestamp, horizon=5,
                    profile_name=resolved.profile_source,
                    backtest_id=saved_run_id,
                )
            click.echo(
                f"   📝 已写入信号台账：{len(buy_events)} 条 BUY 事件"
                "（tradepilot signals backfill 回填前瞻收益）"
            )
        except Exception as e:
            click.echo(f"   ⚠️ 信号台账写入失败（不影响本次结果）：{e}", err=True)
    click.echo("\n⚠️ 单标的样本内回测仅供参考，未经样本外验证的收益不可作为预期收益。")


_WALKFORWARD_GRIDS = {
    'ma': {'fast': [3, 5, 8], 'slow': [20, 30, 60]},
    'rsi': {'period': [7, 14, 21], 'oversold': [25, 30], 'overbought': [70, 75]},
    'bollinger': {'period': [10, 20, 30], 'std_dev': [1.5, 2.0, 2.5]},
    'donchian': {'window': [10, 20, 40]},
    # combo_vote：网格仅扫投票阈值，三件套画像基准由 --profile/缺省链解析（grid 在其上叠加）
    COMBO_VOTE: {'vote_threshold': [1, 2, 3]},
}


@cli.command()
@click.argument('symbol')
@click.option('--days', '-d', type=int, default=756, help='取数天数（建议 ≥2 年）')
@click.option('--strategy', '-s', type=click.Choice(sorted(_WALKFORWARD_GRIDS)), default='ma', help='策略名称')
@click.option('--splits', '-n', type=int, default=3, help='滚动分段数')
@click.option('--execution', type=click.Choice(['next_open', 'close']), default='next_open',
              help='撮合模式（与 backtest 命令同口径）：next_open=次日开盘成交，close=当根收盘')
@click.option('--warmup', type=int, default=60,
              help='每段评估窗前的预热 bar 数（消除指标冷启动哑区，建议 ≥ 最慢周期；0=旧版冷启动）')
@click.option('--select-by', type=click.Choice(['sharpe', 'return']), default='sharpe',
              help='训练段选参标准：sharpe=持仓日夏普（与展示口径一致，默认），return=总收益')
@click.option('--no-save', is_flag=True, help='不落库（默认写入 backtest_results，run_kind=walkforward）')
@click.option('--profile', default=None,
              help='combo_vote 基准画像：system 或 config.strategy_profiles 名（仅 combo_vote 生效）')
@click.option('--grid-json', default=None,
              help='自定义参数网格（JSON 对象，键须在该策略白名单内），覆盖内置默认网格')
def walkforward(symbol, days, strategy, splits, execution, warmup, select_by, no_save, profile, grid_json):
    """Walk-forward 验证：样本内选参、样本外评估，量化过拟合"""
    import json

    from .backtest.rules import MarketRules, price_limit_for_symbol
    from .backtest.serialize import serialize_walkforward_report
    from .backtest.walkforward import walk_forward
    from .data.tushare_loader import TushareDataLoader

    try:
        config = load_config()
        token = get_tushare_token(config)
    except Exception as e:
        click.echo(f"❌ 无法加载配置：{e}", err=True)
        sys.exit(1)

    loader = TushareDataLoader(token)
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
    bars = list(loader.load_bars(symbol, start_date=start_date, end_date=end_date))
    if len(bars) < splits * 60:
        click.echo(f"❌ 数据不足（{len(bars)} 根 K 线），walk-forward 建议至少 {splits * 60} 根", err=True)
        sys.exit(1)

    default_threshold = get_vote_threshold(config)

    # 网格：--grid-json 覆盖内置默认；键须落在该策略白名单内（与 backtest --param 同口径）
    if grid_json:
        try:
            grid = json.loads(grid_json)
        except json.JSONDecodeError as e:
            click.echo(f"❌ --grid-json 不是合法 JSON：{e}", err=True)
            sys.exit(2)
        if not isinstance(grid, dict) or not grid:
            click.echo("❌ --grid-json 需为非空 JSON 对象（键=参数名，值=候选列表）", err=True)
            sys.exit(2)
    else:
        grid = dict(_WALKFORWARD_GRIDS[strategy])
    allowed = PARAM_WHITELIST.get(strategy, frozenset())
    illegal = sorted(k for k in grid if k not in allowed)
    if illegal:
        click.echo(
            f"❌ 网格含 {strategy} 不支持的参数：{', '.join(illegal)}；"
            f"可用：{', '.join(sorted(allowed))}",
            err=True,
        )
        sys.exit(2)

    # combo_vote：--profile（或缺省链）解析基准画像，网格在其上叠加（默认扫 vote_threshold）；
    # 单策略无画像概念，忽略 --profile，base_params 为空 → build_strategy 直接构造单策略类。
    base_params: dict = {}
    resolved = None
    if strategy == COMBO_VOTE:
        try:
            resolved = resolve_backtest_strategy(
                symbol=symbol, strategy=COMBO_VOTE, profile=profile,
                config=config, default_threshold=default_threshold,
            )
        except (IllegalParamError, UnknownProfileError, ValueError) as e:
            click.echo(f"❌ 画像解析失败：{e}", err=True)
            sys.exit(2)
        spec_raw = dict(resolved.spec.raw) if resolved.spec else {}
        base_params = {k: v for k, v in spec_raw.items() if k != 'kind'}
    elif profile:
        click.echo("⚠️ --profile 仅对 combo_vote 生效，本次单策略 walk-forward 已忽略。", err=True)

    report = walk_forward(
        strategy_factory=lambda gp: build_strategy(
            strategy, {**base_params, **gp}, default_threshold=default_threshold
        ),
        bars=bars,
        param_grid=grid,
        n_splits=splits,
        # 与 backtest 命令同默认风控（initial_cash 用引擎默认 100000，execution 可选，
        # 涨跌停按标的板块推导）
        backtest_kwargs={
            'execution': execution,
            'market_rules': MarketRules(price_limit_pct=price_limit_for_symbol(symbol)),
        },
        warmup_bars=warmup,
        select_by=select_by,
    )

    click.echo(
        f"\n📊 Walk-forward 结果（{symbol} / {strategy} / {len(bars)} 根 K 线 / {splits} 段"
        f" / 撮合={execution} / 预热={warmup} / 选参={select_by}）"
    )
    if resolved is not None:
        click.echo(f"   基准画像来源：{resolved.profile_source}   网格：{grid}")
    for s in report.splits:
        click.echo(
            f"   段 {s.split_index + 1}：最佳参数 {s.best_params}"
            f"   样本内收益 {s.is_metrics.total_return:+.2%}"
            f"（持仓日夏普 {s.is_metrics.sharpe:.2f} / 全样本 {s.is_sharpe_full:.2f}）"
            f"   样本外收益 {s.oos_metrics.total_return:+.2%}"
            f"（持仓日夏普 {s.oos_metrics.sharpe:.2f} / 全样本 {s.oos_sharpe_full:.2f}）"
        )
    click.echo(f"   样本外拼接总收益：{report.oos_total_return:+.2%}")
    click.echo(f"   平均样本内收益：{report.avg_is_return:+.2%}   平均样本外收益：{report.avg_oos_return:+.2%}")
    click.echo(f"   过拟合差距（IS−OOS）：{report.overfit_gap:+.2%}")
    if warmup == 0:
        click.echo("   ⚠️ 预热=0：各段冷启动，指标未满窗口期无法出信号，OOS 表现被系统性低估。")
    if report.overfit_gap > 0.05:
        click.echo("   ⚠️ 样本内明显优于样本外：参数很可能是拟合噪音，勿按样本内收益预期。")
    else:
        click.echo("   ✅ 样本内外差距不大，但仍建议更长时间段与多标的复核。")

    if not no_save:
        from .storage.database import stock_catalog_name
        from .storage.user_store import record_backtest_run
        try:
            run_id = record_backtest_run(
                {
                    "symbol": symbol,
                    "name": stock_catalog_name(symbol) or symbol,
                    "start_date": bars[0].timestamp.strftime("%Y-%m-%d") if bars else "",
                    "end_date": bars[-1].timestamp.strftime("%Y-%m-%d") if bars else "",
                    "initial_capital": 0.0,
                    "final_capital": 0.0,
                    # 汇总列填样本外拼接口径（最能代表 walk-forward 结论）
                    "total_return": report.oos_total_return * 100,
                    "annual_return": 0.0,
                    "max_drawdown": 0.0,
                    "sharpe_ratio": report.avg_oos_sharpe,
                    "total_trades": 0,
                    "win_rate": 0.0,
                    "strategy_key": strategy,
                    "bar_count": len(bars),
                    "execution": execution,
                    "result_json": None,
                },
                run_kind="walkforward",
                params_json=json.dumps(
                    {"days": days, "strategy": strategy, "splits": splits,
                     "execution": execution, "warmup": warmup, "select_by": select_by,
                     "profile": profile, "grid": grid},
                    ensure_ascii=False,
                ),
                profile_source=(resolved.profile_source if resolved else "cli"),
                report_json=json.dumps(
                    serialize_walkforward_report(report), ensure_ascii=False
                ),
            )
            click.echo(f"   💾 已落库：backtest_results id={run_id}（run_kind=walkforward）")
        except Exception as e:
            click.echo(f"   ⚠️ walk-forward 落库失败（不影响本次结果）：{e}", err=True)


@cli.group()
def backtests():
    """CLI 回测 / walk-forward 运行记录（落库于 backtest_results，user_id 为 NULL）"""
    pass


@backtests.command('list')
@click.option('--kind', type=click.Choice(['backtest', 'walkforward']), default=None,
              help='只列指定类型')
@click.option('--limit', type=int, default=20, help='最多显示条数')
def backtests_list(kind, limit):
    """列出 CLI 回测/walk-forward 落库记录（按 id 倒序）"""
    from .storage.user_store import list_backtest_runs

    rows = list_backtest_runs(kind=kind, limit=limit)
    if not rows:
        click.echo("（暂无 CLI 回测记录；运行 tradepilot backtest/walkforward 后自动落库）")
        return
    suffix = f"，kind={kind}" if kind else ""
    click.echo(f"\n📚 CLI 回测记录（{len(rows)} 条{suffix}）")
    click.echo(
        f"   {'id':>5}  {'类型':<11}{'标的':<12}{'策略':<11}"
        f"{'收益':>9}{'夏普':>7}{'K线':>6}  时间"
    )
    for r in rows:
        click.echo(
            f"   {r['id']:>5}  {(r.get('run_kind') or 'backtest'):<11}"
            f"{(r.get('symbol') or '-'):<12}{(r.get('strategy_key') or '-'):<11}"
            f"{(r.get('total_return') or 0.0):>8.2f}%{(r.get('sharpe_ratio') or 0.0):>7.2f}"
            f"{(r.get('bar_count') or 0):>6}  {r.get('created_at') or ''}"
        )


@cli.command()
@click.argument('symbol', required=False)
def monitor(symbol):
    """启动实时监控；传股票代码则只检查该标的并输出信号"""
    from .config_loader import resolve_config_path
    from .monitor.main import MarketMonitor, main as monitor_main

    config_path = resolve_config_path()
    if not config_path.exists():
        click.echo(
            f"❌ 找不到配置文件（已尝试 {config_path}）："
            "请设置 TRADEPILOT_CONFIG 或先运行 tradepilot init",
            err=True,
        )
        sys.exit(1)

    if symbol:
        async def _once():
            m = MarketMonitor(str(config_path))
            results = []
            await m.check_symbol({'code': symbol.upper(), 'name': '', 'strategy_profile': None}, results)
            for r in results:
                name_part = f" {r['name']}" if r.get('name') and r['name'] != r['code'] else ""
                click.echo(f"{r['code']}{name_part}：{r['recommendation']} @ {r['price']}")
        asyncio.run(_once())
        return

    asyncio.run(monitor_main(str(config_path)))


@cli.command()
@click.option('--host', default='127.0.0.1', show_default=True,
              help='监听地址（容器内或远程访问用 0.0.0.0）')
@click.option('--port', '-p', type=int, default=8000, show_default=True, help='监听端口')
def serve(host, port):
    """启动 Web 监控台（浏览器访问 http://<host>:<port>）"""
    try:
        import uvicorn
    except ImportError:
        click.echo("❌ 缺少 uvicorn：请先执行 pip install -e . 或 pip install -r requirements.txt", err=True)
        sys.exit(1)
    click.echo(f"🌐 监控台启动中：http://{host}:{port}（Ctrl+C 停止）")
    uvicorn.run('ripple_tradePilot.api.app:app', host=host, port=port)


def _mask_secret(value) -> str:
    """脱敏敏感字符串，保留首尾便于核对。"""
    if not value:
        return "(未设置)"
    value = str(value)
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-4:]


@cli.command()
def config():
    """显示当前配置（敏感信息已脱敏）"""
    import json
    cfg = load_config()
    # 隐藏敏感信息
    if 'tushare' in cfg and 'token' in cfg['tushare']:
        if cfg['tushare']['token']:
            cfg['tushare']['token'] = cfg['tushare']['token'][:8] + "..."
        else:
            cfg['tushare']['token'] = "(未设置)"
    for section, keys in (
        (('notifiers', 'feishu'), ('webhook', 'secret')),
        (('feishu',), ('webhook_url', 'webhook_secret')),
        (('mx',), ('api_key',)),
    ):
        target = cfg
        for part in section:
            target = target.get(part, {}) if isinstance(target, dict) else {}
        for key in keys:
            if isinstance(target, dict) and target.get(key):
                target[key] = _mask_secret(target[key])

    click.echo(json.dumps(cfg, indent=2, ensure_ascii=False))


@cli.command()
@click.argument('symbol')
def screen(symbol):
    """趋势筛选：均线多头排列 + 近 20 日涨幅"""
    from .data.tushare_loader import TushareDataLoader

    try:
        cfg = load_config()
        token = get_tushare_token(cfg)
    except Exception as e:
        click.echo(f"❌ 无法加载配置：{e}", err=True)
        sys.exit(1)

    loader = TushareDataLoader(token)
    bars = list(loader.load_bars(symbol, start_date=(datetime.now() - timedelta(days=90)).strftime('%Y%m%d')))
    if len(bars) < 25:
        click.echo(f"❌ 行情数据不足（{len(bars)} 条）", err=True)
        sys.exit(1)

    closes = [b.close for b in bars]
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    change20 = closes[-1] / closes[-21] - 1

    click.echo(f"\n🔍 {symbol} 趋势筛选")
    click.echo(f"   最新收盘：{closes[-1]:.2f}   近 20 日涨幅：{change20:+.2%}")
    click.echo(f"   MA5={ma5:.2f}  MA10={ma10:.2f}  MA20={ma20:.2f}")
    if ma5 > ma10 > ma20:
        click.echo("   ✅ 均线多头排列（强趋势）")
    elif ma5 < ma10 < ma20:
        click.echo("   🔻 均线空头排列（弱趋势）")
    else:
        click.echo("   ➖ 均线交织（震荡）")


@cli.group()
def data():
    """数据维护（巡检 / 刷新）"""
    pass


@data.command()
@click.option('--adjust', is_flag=True, help='巡检全库日线复权一致性（pct_chg vs close 环比）')
@click.option('--symbol', default=None, help='只巡检指定标的（默认全库 daily_bars）')
@click.option('--tol', type=float, default=0.01,
              help='环比一致性容忍度（收益率 fraction，0.01=1 个百分点）')
def audit(adjust, symbol, tol):
    """离线巡检存量日线的复权混接点（A9）。

    用已有 pct_chg 列校验 close 环比一致性：同一复权锚内二者应吻合到舍入误差，
    新旧锚混接处会出现整段复权因子量级的跳变。
    """
    from .data.adjustment import audit_series
    from .storage.database import list_daily_bar_symbols, load_daily_bars

    if not adjust:
        click.echo("请用 --adjust 指定巡检类型（当前支持复权一致性巡检）", err=True)
        sys.exit(2)

    if symbol:
        from .data.stock_service import StockDataService
        symbols = [StockDataService.normalize_symbol(symbol)]
    else:
        symbols = list_daily_bar_symbols()

    if not symbols:
        click.echo("（daily_bars 为空，无可巡检数据；先运行行情刷新）")
        return

    total_anomalies = 0
    flagged = 0
    click.echo(f"\n🔎 复权一致性巡检（{len(symbols)} 个标的，tol={tol}）")
    for sym in symbols:
        rows = load_daily_bars(sym)
        report = audit_series(rows, tol=tol)
        if report.clean:
            continue
        flagged += 1
        total_anomalies += len(report.anomalies)
        worst = report.worst
        click.echo(
            f"   ⚠️ {sym}：{len(report.anomalies)} 处异常（共审计 {report.checked} 日），"
            f"最严重 {worst.trade_date} 环比 {worst.expected_ret:+.2%} vs 记录 {worst.actual_ret:+.2%}"
        )
    if flagged == 0:
        click.echo("   ✅ 未发现复权混接点，全库 close 环比与 pct_chg 一致。")
    else:
        click.echo(
            f"   共 {flagged} 个标的、{total_anomalies} 处疑似混接；"
            "建议对这些标的触发全量重拉（refresh 会在检测到漂移时自动整段覆盖）。"
        )


@data.command('refresh')
@click.option('--market', is_flag=True, help='刷新指数日线（C1）+ 当日市场宽度（C2）')
@click.option('--industry', is_flag=True, help='刷新股票池所属行业板块（成分股 + 日线，C3）')
@click.option('--pool', type=click.Choice(['config', 'watchlist']), default='config',
              help='--industry 的股票池来源：config.yaml symbols / Web 观察池')
@click.option('--days', '-d', type=int, default=750, help='指数/板块历史深度（天）')
def data_refresh(market, industry, pool, days):
    """刷新市场/行业数据（C1-C3）。``--market`` 指数 + 宽度，``--industry`` 板块（按 ``--pool``）。

    数据供给入口：指数/板块走各自降级链（部分成功 + 显式报告），市场宽度走"增量积累制"
    （无免费历史宽度 API，每跑一天积一天，见 STRATEGIES.md C1/C2）。
    """
    if not market and not industry:
        click.echo("请至少指定 --market 或 --industry", err=True)
        sys.exit(2)

    if market:
        _refresh_market_data(days)
    if industry:
        _refresh_industry_data(pool, days)


def _refresh_market_data(days: int) -> None:
    """C1 指数日线 + C2 当日市场宽度（各自独立 try/except，单项失败不影响其他）。"""
    from .data.market_service import MarketDataService, record_market_breadth
    from .data.stock_service import StockDataService, StockDataUnavailableError
    from .storage.database import load_stock_quotes

    click.echo("\n📊 刷新市场数据（C1 指数日线 + C2 市场宽度）")
    # C1：四大指数日线落库（三级降级链，部分成功）
    try:
        report = MarketDataService().refresh_indexes(days=days)
        click.echo(
            f"   指数日线：成功 {len(report['refreshed'])} 个 / 失败 {len(report['failed'])} 个"
        )
        for item in report['refreshed']:
            click.echo(f"     ✓ {item['index_code']}（{item['source']}，{item['rows']} 行）")
        for item in report['failed']:
            click.echo(f"     ✗ {item['index_code']}：{item['error']}")
    except Exception as error:  # 网络/接口异常一律降级，不阻断宽度刷新
        click.echo(f"   ⚠️ 指数日线刷新异常：{error}")

    # C2：当日全市场快照终态 → aggregate_breadth → market_daily
    try:
        StockDataService().refresh_quotes()
    except StockDataUnavailableError as error:
        click.echo(f"   ⚠️ 全市场快照刷新失败，用库内既有快照聚合宽度：{error}")
    rows = load_stock_quotes()
    if not rows:
        click.echo("   ⚠️ 无全市场快照，跳过市场宽度聚合")
        return
    today = datetime.now().strftime("%Y%m%d")
    breadth = record_market_breadth(today, rows, "snapshot")
    click.echo(
        f"   市场宽度（{today}）：上涨 {breadth['advancers']} / 下跌 {breadth['decliners']} / "
        f"平盘 {breadth['unchanged']} / 涨停 {breadth['limit_up']} / 跌停 {breadth['limit_down']}"
    )


def _refresh_industry_data(pool: str, days: int) -> None:
    """C3 行业板块刷新：解析股票池 → 只为所属板块拉成分股 + 日线（部分成功 + 报告）。"""
    from .data.industry_service import IndustryDataService

    if pool == 'watchlist':
        from .storage.user_store import list_all_watched_symbols
        symbols = [str(item['symbol']) for item in list_all_watched_symbols()
                   if item.get('symbol')]
    else:
        cfg = load_config()
        symbols = [str(s.get('code')) for s in cfg.get('symbols', []) if s.get('code')]

    click.echo(f"\n🏭 刷新行业板块（C3，股票池 --pool {pool}，{len(symbols)} 只）")
    if not symbols:
        click.echo(f"   （--pool {pool} 无标的，跳过行业刷新）")
        return

    report = IndustryDataService().refresh_for_symbols(symbols, days=days)
    click.echo(
        f"   板块：目标 {report['boards_total']} 个 → "
        f"成功 {len(report['boards_refreshed'])} / 失败 {len(report['boards_failed'])}"
    )
    click.echo(f"   成分股 {report['membership_rows']} 行，板块日线 {report['bar_rows']} 行")
    unresolved = report['symbols_unresolved']
    if unresolved:
        preview = "、".join(unresolved[:5]) + ("…" if len(unresolved) > 5 else "")
        click.echo(
            f"   ⚠️ {len(unresolved)} 只无法映射到板块（行业特征将缺失，绝不造假）：{preview}"
        )
    for item in report['boards_failed']:
        click.echo(f"     ✗ {item['board_name'] or item['board_code']}：{item['error']}")


@cli.group()
def signals():
    """信号台账（记录 → 回填前瞻收益 → 统计 coverage/胜率/期望）"""
    pass


@signals.command('backfill')
@click.option('--symbol', default=None, help='只回填指定标的（默认全部 pending）')
@click.option('--aux-horizon', type=int, default=10, help='辅助 horizon（写 fwd_ret_aux，默认 10 日）')
def signals_backfill(symbol, aux_horizon):
    """用 DB 已有日线 + B1 标签回填 pending 信号的前瞻净收益/下行风险。

    决策日在库且未来 bar 足够 → filled；不足以走完整 horizon → 保持 pending；
    决策日缺 bar → bad_data（绝不截断尾价冒充）。
    """
    from .storage.signal_ledger import backfill_outcomes

    summary = backfill_outcomes(symbol=symbol, aux_horizon=aux_horizon)
    click.echo(
        f"\n📈 台账回填：待处理 {summary['total']} 条 → "
        f"filled {summary['filled']} / 仍 pending {summary['pending']} / bad_data {summary['bad_data']}"
    )
    if summary['total'] == 0:
        click.echo("   （无 pending 信号；monitor 收盘例程或 backtest --write-signals 会写入台账）")


@signals.command('stats')
@click.option('--source', type=click.Choice(['monitor', 'backtest', 'dataset', 'manual']),
              default=None, help='只统计指定来源（默认全部）')
def signals_stats(source):
    """台账汇总：coverage（有信号交易日/全部评估日）+ 胜率 + 平均净收益。

    coverage 是护栏——防"几乎不交易刷高胜率"；胜率/期望仅在已回填（filled）行上计算。
    """
    from .storage.signal_ledger import signal_stats

    stats = signal_stats(source=source)
    if stats['total_rows'] == 0:
        click.echo("（台账为空，暂无信号记录）")
        return
    lo, hi = stats['date_range']
    scope = f"source={source}" if source else "全部来源"
    click.echo(f"\n📊 信号台账统计（{scope}）")
    click.echo(f"   记录数：{stats['total_rows']}   标的：{stats['symbols']}   区间：{lo} ~ {hi}")
    click.echo(
        f"   状态分布：filled {stats['by_status'].get('filled', 0)} / "
        f"pending {stats['by_status'].get('pending', 0)} / "
        f"bad_data {stats['by_status'].get('bad_data', 0)} / "
        f"expired {stats['by_status'].get('expired', 0)}"
    )
    recs = stats['by_recommendation']
    click.echo(
        f"   建议分布：BUY {recs.get('BUY', 0)} / SELL {recs.get('SELL', 0)} / "
        f"CONFLICT {recs.get('CONFLICT', 0)} / HOLD {recs.get('HOLD', 0)}"
    )
    click.echo(
        f"   覆盖率：{stats['coverage']:.1%}（{stats['signal_days']}/{stats['evaluated_days']} 个标的·日有可执行信号）"
    )
    if stats['filled_count']:
        click.echo(
            f"   已回填 {stats['filled_count']} 条：胜率 {stats['win_rate']:.1%}   "
            f"平均净收益 {stats['avg_net_return']:+.3%}   平均 horizon {stats['avg_horizon']:.1f} 日"
        )
    else:
        click.echo("   尚无已回填信号（先运行 tradepilot signals backfill）")
    click.echo("\n⚠️ 台账绩效为历史信号的事后验证，含样本内/幸存者偏差，不构成收益承诺。")


@signals.command('list')
@click.option('--status', type=click.Choice(['pending', 'filled', 'expired', 'bad_data']),
              default=None, help='按回填状态过滤')
@click.option('--source', type=click.Choice(['monitor', 'backtest', 'dataset', 'manual']),
              default=None, help='按来源过滤')
@click.option('--symbol', default=None, help='按标的过滤')
@click.option('--limit', type=int, default=20, help='最多显示条数')
def signals_list(status, source, symbol, limit):
    """列出台账行（决策 / 回填结果）。"""
    from .storage.signal_ledger import list_signals

    rows = list_signals(status=status, source=source, symbol=symbol, limit=limit)
    if not rows:
        click.echo("（无匹配的信号记录）")
        return
    click.echo(f"\n📋 信号台账（{len(rows)} 条）")
    click.echo(f"   {'日期':<10}{'标的':<12}{'来源':<10}{'建议':<10}{'状态':<10}{'净收益':>10}")
    for row in rows:
        fwd = row.get('fwd_net_return')
        fwd_text = f"{fwd:+.2%}" if fwd is not None else "—"
        prov = "·盘中" if row.get('provisional') else ""
        click.echo(
            f"   {row['trade_date']:<10}{row['symbol']:<12}"
            f"{(row.get('source') or ''):<10}{(row.get('recommendation') or '—') + prov:<10}"
            f"{(row.get('label_status') or ''):<10}{fwd_text:>10}"
        )


def _resolve_symbol_pool(pool: str, symbols: str = None) -> list:
    """解析股票池（``ml build-dataset`` / ``ml pipeline`` 共用，避免两处口径漂移）。

    优先级：显式 ``--symbols`` > ``--pool``（config.yaml symbols / Web 观察池 / 库内全部标的）。
    """
    if symbols:
        return [s.strip() for s in symbols.split(',') if s.strip()]
    if pool == 'watchlist':
        from .storage.user_store import list_all_watched_symbols
        return [str(item['symbol']) for item in list_all_watched_symbols()
                if item.get('symbol')]
    if pool == 'catalog':
        from .storage.database import list_daily_bar_symbols
        return list(list_daily_bar_symbols())
    cfg = load_config()
    return [str(s.get('code')) for s in cfg.get('symbols', []) if s.get('code')]


def _promotion_icon(status: str) -> str:
    """晋升结果图标（``ml promote`` / ``ml pipeline`` 共用）：在位 🎖️ / 演示 🎭 / 未放行 ⛔。

    ``demo`` 不给奖章——它是 force 放行的演示工件，``load_promoted`` 默认不取，
    图标上就该和真在位模型区分开，否则一眼看过去像"晋升成功了"。
    """
    return {"promoted": "🎖️", "demo": "🎭"}.get(status, "⛔")


def _parse_feature_groups(groups: str) -> tuple:
    """``--groups`` 逗号串 → 合法特征组元组；有未知组则报错退出（exit 2）。"""
    from .ml.features import FEATURE_GROUPS

    parsed = tuple(g.strip() for g in groups.split(',') if g.strip())
    bad_groups = [g for g in parsed if g not in FEATURE_GROUPS]
    if bad_groups:
        click.echo(f"未知特征组：{bad_groups}；合法值 {list(FEATURE_GROUPS)}", err=True)
        sys.exit(2)
    return parsed


@cli.group()
def ml():
    """ML 信号质量模型（数据集构建 → 训练 → 评估 → 晋升 → 一键管道；D 阶段）"""


@ml.command('build-dataset')
@click.option('--pool', type=click.Choice(['config', 'watchlist', 'catalog']), default='config',
              help='股票池来源：config.yaml symbols / Web 观察池 / 库内有日线的全部标的')
@click.option('--symbols', default=None, help='显式标的（逗号分隔，覆盖 --pool）')
@click.option('--start', default=None, help='决策日起点 YYYYMMDD（含）')
@click.option('--end', default=None, help='决策日终点 YYYYMMDD（含）')
@click.option('--horizon', type=int, default=5, help='主前瞻标签天数（T+1 开盘进、T+1+h 开盘出）')
@click.option('--aux-horizon', type=int, default=10, help='辅助前瞻标签天数')
@click.option('--groups', default='signal,price_volume,market,industry',
              help='启用特征组（逗号分隔；signal/price_volume/market/industry）')
@click.option('--index-code', default='000300.SH', help='market 组基准指数')
@click.option('--out-dir', default=None, help='csv.gz / manifest 输出目录（默认 data/ml）')
@click.option('--no-register', is_flag=True, help='只产文件，不写 ml_datasets 表')
def ml_build_dataset(pool, symbols, start, end, horizon, aux_horizon, groups,
                     index_code, out_dir, no_register):
    """构建 ML 数据集（D2）：特征（D1）+ 前瞻标签（B1）→ csv.gz + manifest 落库。

    逐标的过 A9 复权巡检（混接嫌疑拒入并记录）；行业成分为最新单快照，manifest 标
    ``industry_point_in_time=False`` 前视警告。同库同参数重跑 → 同 dataset_id（幂等）。
    """
    from .ml.dataset import build_dataset

    symbol_list = _resolve_symbol_pool(pool, symbols)
    group_list = _parse_feature_groups(groups)
    if not symbol_list:
        click.echo(f"⚠️ 股票池（--pool {pool}）无标的，无法构建数据集", err=True)
        sys.exit(2)

    click.echo(
        f"\n📦 构建 ML 数据集（D2）：{len(symbol_list)} 标的 · 组 {','.join(group_list)} · "
        f"horizon {horizon}(+{aux_horizon})"
    )
    manifest = build_dataset(
        symbol_list,
        start=start,
        end=end,
        horizon=horizon,
        aux_horizon=aux_horizon,
        groups=group_list,
        index_code=index_code,
        out_dir=Path(out_dir) if out_dir else None,
        register=not no_register,
    )
    click.echo(f"   dataset_id：{manifest.dataset_id}")
    click.echo(
        f"   行数 {manifest.n_rows}（正例 {manifest.n_positive}，{manifest.positive_rate:.1%}）· "
        f"日期 {manifest.start_date}~{manifest.end_date} · 新鲜度 {manifest.max_trade_date}"
    )
    click.echo(f"   特征列 {len(manifest.feature_columns)} · 输出 {manifest.csv_path}")
    if manifest.rejected:
        click.echo(f"   ⚠️ 拒入 {len(manifest.rejected)} 只：")
        for item in manifest.rejected:
            click.echo(f"     ✗ {item['symbol']}：{item['reason']}")
    for warning in manifest.warnings:
        click.echo(f"   ⚠️ {warning}")


@ml.command('datasets')
def ml_datasets():
    """列出已注册数据集（D2）——train 需从中取 dataset_id。"""
    from .storage.database import list_datasets

    datasets = list_datasets()
    if not datasets:
        click.echo("（无数据集，先用 ml build-dataset 构建）")
        return
    click.echo(
        f"\n{'dataset_id':<14}{'rows':>7}{'pos%':>7}{'horizon':>9}"
        f"{'fresh':>11}{'groups':>22}"
    )
    for item in datasets:
        groups = ",".join(item.get("groups") or [])
        click.echo(
            f"{item['dataset_id']:<14}{item.get('n_rows', 0):>7}"
            f"{(item.get('positive_rate') or 0) * 100:>6.1f}%{item.get('horizon', 5):>9}"
            f"{str(item.get('max_trade_date') or ''):>11}{groups[:21]:>22}"
        )


@ml.command('train')
@click.option('--dataset', required=True, help='数据集 ID（见 ml datasets）')
@click.option('--model', 'kind', type=click.Choice(['logreg', 'hgb']), default='logreg',
              help='logreg=线性+中位数插补 / hgb=梯度提升(原生吞 NaN)')
@click.option('--target', type=click.Choice(['win5', 'ret5', 'mae5']), default='win5',
              help='win5=胜率(分类) / ret5=净收益(回归) / mae5=下行风险(回归)')
@click.option('--splits', type=int, default=5, help='锚定滚动 OOS 折数')
@click.option('--embargo', type=int, default=2, help='purge 后再空的交易日数')
@click.option('--val-ratio', type=float, default=0.2, help='验证折占训练段比例（选参/校准）')
@click.option('--threshold', type=float, default=0.5, help='覆盖率/决策表默认阈值')
@click.option('--models-dir', default=None, help='工件输出目录（默认 data/ml/models）')
@click.option('--no-register', is_flag=True, help='只产工件，不写 ml_models 表')
def ml_train(dataset, kind, target, splits, embargo, val_ratio, threshold,
             models_dir, no_register):
    """训练 ML 模型（D3）：数据集 → 滚动 OOS（选参/校准只看 train/val）→ 工件 + ml_models。

    产出 status='candidate' 模型；用 `ml promote` 过晋升门禁后才进在位（数据恢复前
    新鲜度门必触发，默认拒晋升）。sklearn 未装时干净报错退出（exit 3），不影响信号链路。
    """
    from .ml.registry import train_from_dataset

    click.echo(
        f"\n🧠 训练模型（D3）：dataset={dataset} · {kind}/{target} · "
        f"splits={splits} embargo={embargo} val_ratio={val_ratio}"
    )
    try:
        model_id, result = train_from_dataset(
            dataset, kind, target,
            n_splits=splits, embargo=embargo, val_ratio=val_ratio, threshold=threshold,
            models_dir=Path(models_dir) if models_dir else None,
            register=not no_register,
        )
    except ImportError as exc:
        click.echo(f"❌ {exc}", err=True)
        sys.exit(3)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"❌ {exc}", err=True)
        sys.exit(2)

    metrics = result.metrics
    click.echo(f"   model_id：{model_id}")
    if result.is_classification:
        auc = metrics.get("oos_auc")
        auc_txt = "n/a" if auc is None or auc != auc else f"{auc:.4f}"
        click.echo(
            f"   OOS {metrics.get('n_oos', 0)} 行 · AUC {auc_txt} · "
            f"Brier {metrics.get('oos_brier', 0):.4f}（基线 {metrics.get('base_rate_brier', 0):.4f}）· "
            f"ECE {metrics.get('oos_ece', 0):.4f}"
        )
        click.echo(
            f"   覆盖率@{threshold:g} {metrics.get('coverage_at_threshold', 0):.3f} · "
            f"胜率 {metrics.get('win_rate_at_threshold', 0):.3f} · "
            f"末折训练 {result.per_split[-1]['n_train'] if result.per_split else 0} 行"
        )
    else:
        click.echo(
            f"   OOS {metrics.get('n_oos', 0)} 行 · MAE {metrics.get('oos_mae', 0):.4f} · "
            f"R² {metrics.get('oos_r2', 0):.4f}"
        )
    click.echo("   状态 candidate（用 ml promote 过门禁晋升；ml eval 看完整报告）")


@ml.command('list')
@click.option('--status', default=None,
              help='按状态过滤：candidate|promoted|retired|demo')
def ml_list(status):
    """列出已注册模型（D3）。"""
    from .storage.database import list_models

    models = list_models(status=status)
    if not models:
        click.echo("（无模型，先用 ml train 训练）")
        return
    click.echo(
        f"\n{'model_id':<26}{'kind':<8}{'target':<7}{'status':<11}"
        f"{'stale':<6}{'AUC':>7}{'Brier':>8}{'n_oos':>7}"
    )
    for item in models:
        auc = item.get("oos_auc")
        auc_txt = "n/a" if auc is None or auc != auc else f"{auc:.3f}"
        brier = item.get("oos_brier")
        brier_txt = "n/a" if brier is None else f"{brier:.4f}"
        stale_txt = "yes" if item.get("stale") else "—"
        click.echo(
            f"{item['model_id']:<26}{str(item.get('kind') or ''):<8}"
            f"{str(item.get('target') or ''):<7}{str(item.get('status') or ''):<11}"
            f"{stale_txt:<6}{auc_txt:>7}{brier_txt:>8}{item.get('n_oos', 0):>7}"
        )


@ml.command('promote')
@click.argument('model_id')
@click.option('--force', is_flag=True, help='门禁不过仍放行（打 stale/demo 标记）')
@click.option('--min-samples', type=int, default=None, help='样本量门（默认 800 行）')
@click.option('--min-positives', type=int, default=None, help='正例数门（默认 80）')
@click.option('--max-staleness', type=int, default=None, help='新鲜度门（天，默认 120）')
@click.option('--models-dir', default=None, help='工件目录（默认 data/ml/models）')
def ml_promote(model_id, force, min_samples, min_positives, max_staleness, models_dir):
    """晋升模型（D3 门禁）：样本量/正例数/新鲜度/OOS 判别力四道闸。

    默认拒绝晋升（exit 1）；--force 放行：仅新鲜度不过 → promoted+stale，质量门不过 →
    demo（演示工件，scoring 默认不取）。数据恢复前新鲜度门必触发——这是"演示模型不冒充
    可用模型"的核心护栏。
    """
    from .ml import registry

    kwargs = {}
    if min_samples is not None:
        kwargs["min_samples"] = min_samples
    if min_positives is not None:
        kwargs["min_positives"] = min_positives
    if max_staleness is not None:
        kwargs["max_staleness_days"] = max_staleness
    try:
        report = registry.promote(
            model_id, force=force,
            models_dir=Path(models_dir) if models_dir else None,
            **kwargs,
        )
    except FileNotFoundError as exc:
        click.echo(f"❌ {exc}", err=True)
        sys.exit(2)

    icon = _promotion_icon(report.status)
    click.echo(
        f"\n{icon} {model_id} → {report.status}{'（stale）' if report.stale else ''}"
        f"{'（force）' if report.forced else ''}"
    )
    for gate in report.gates:
        click.echo(f"   {'✓' if gate.passed else '✗'} {gate.name}：{gate.detail}")
    for warning in report.warnings:
        click.echo(f"   ⚠️ {warning}")
    if not report.promoted:
        sys.exit(1)


@ml.command('eval')
@click.option('--model', 'model_id', required=True, help='模型 ID（见 ml list）')
@click.option('--threshold', type=float, multiple=True,
              help='决策表阈值（可多次；默认 0.5/0.55/0.6）')
@click.option('--index-code', default='000300.SH', help='指数同窗基准')
@click.option('--json', 'json_path', default=None, help='同时写 JSON 报告到该路径')
@click.option('--models-dir', default=None, help='工件目录（默认 data/ml/models）')
def ml_eval(model_id, threshold, index_code, json_path, models_dir):
    """评估模型 OOS 表现（D4）：判别 + 校准 + 决策对比表（四方）+ 警告区。

    只读训练时存下的 OOS 预测，纯 numpy 计算——评估链路无需 sklearn。决策对比表在
    同一 OOS 折、同一标签上并列：规则 BUY 基线 | 模型过滤 p≥阈值 | 买入持有 | 指数同窗。
    """
    from .ml.evaluate import DEFAULT_THRESHOLDS, dump_json, evaluate_model, render_text

    thresholds = tuple(threshold) if threshold else DEFAULT_THRESHOLDS
    try:
        report = evaluate_model(
            model_id, thresholds=thresholds, index_code=index_code,
            models_dir=Path(models_dir) if models_dir else None,
        )
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"❌ {exc}", err=True)
        sys.exit(2)
    click.echo(render_text(report))
    if json_path:
        dump_json(report, json_path)
        click.echo(f"   📄 JSON 报告：{json_path}")


@ml.command('pipeline')
@click.option('--pool', type=click.Choice(['config', 'watchlist', 'catalog']), default='config',
              help='股票池来源（同 ml build-dataset）')
@click.option('--symbols', default=None, help='显式标的（逗号分隔，覆盖 --pool）')
@click.option('--start', default=None, help='决策日起点 YYYYMMDD（含）')
@click.option('--end', default=None, help='决策日终点 YYYYMMDD（含）')
@click.option('--horizon', type=int, default=5, help='主前瞻标签天数')
@click.option('--aux-horizon', type=int, default=10, help='辅助前瞻标签天数')
@click.option('--groups', default='signal,price_volume,market,industry',
              help='启用特征组（逗号分隔；同 ml build-dataset）')
@click.option('--index-code', default='000300.SH', help='market 组基准指数 + 决策表指数同窗')
@click.option('--models', 'kinds', default='logreg,hgb', help='训练哪些模型（逗号分隔）')
@click.option('--target', type=click.Choice(['win5', 'ret5', 'mae5']), default='win5',
              help='主目标（决策对比表只对分类目标出）')
@click.option('--aux-targets', default='ret5,mae5', help='辅助目标（逗号分隔；空串=只训主目标）')
@click.option('--splits', type=int, default=5, help='锚定滚动 OOS 折数')
@click.option('--embargo', type=int, default=2, help='purge 后再空的交易日数')
@click.option('--val-ratio', type=float, default=0.2, help='验证折占训练段比例（选参/校准）')
@click.option('--threshold', type=float, multiple=True,
              help='决策表阈值（可多次；默认 0.5/0.55/0.6）')
@click.option('--out-dir', default=None, help='数据集输出目录（默认 data/ml）')
@click.option('--models-dir', default=None, help='工件输出目录（默认 data/ml/models）')
@click.option('--report-dir', default='reports', show_default=True, help='markdown 报告输出目录')
@click.option('--promote', is_flag=True,
              help='跑完对每个模型过晋升门禁（默认不晋升，在位模型不受影响）')
@click.option('--force', is_flag=True, help='与 --promote 连用：门禁不过也放行（打 stale/demo 标记）')
def ml_pipeline(pool, symbols, start, end, horizon, aux_horizon, groups, index_code,
                kinds, target, aux_targets, splits, embargo, val_ratio, threshold,
                out_dir, models_dir, report_dir, promote, force):
    """一键跑通 D 阶段全管道（D6）：build-dataset → train → eval →〔可选〕promote → markdown。

    演示与回归入口：数据恢复并扩池后重跑本命令，才会产生第一个能过门禁的在位模型。
    **默认不晋升**（要动在位模型须显式 --promote），以免一次回归跑悄悄换掉看板/监控正在用的
    模型。退出码：2=空池/非法参数，3=sklearn 未装，0=跑通（哪怕门禁未过）。
    """
    from .ml.evaluate import DEFAULT_THRESHOLDS
    from .ml.pipeline import MODEL_KINDS, collect_warnings, fmt_number, run_pipeline, write_report

    symbol_list = _resolve_symbol_pool(pool, symbols)
    if not symbol_list:
        click.echo(f"⚠️ 股票池（--pool {pool}）无标的，无法跑管道", err=True)
        sys.exit(2)
    group_list = _parse_feature_groups(groups)
    kind_list = tuple(k.strip() for k in kinds.split(',') if k.strip())
    bad_kinds = [k for k in kind_list if k not in MODEL_KINDS]
    if bad_kinds:
        click.echo(f"未知模型 kind：{bad_kinds}；合法值 {list(MODEL_KINDS)}", err=True)
        sys.exit(2)
    aux_list = tuple(t.strip() for t in aux_targets.split(',') if t.strip())

    click.echo(
        f"\n🔁 ML 全管道（D6）：{len(symbol_list)} 标的 · 组 {','.join(group_list)} · "
        f"模型 {','.join(kind_list)} · 目标 {target}"
        f"{'+' + ','.join(aux_list) if aux_list else ''}"
    )
    try:
        report = run_pipeline(
            symbol_list,
            start=start, end=end, horizon=horizon, aux_horizon=aux_horizon,
            groups=group_list, index_code=index_code,
            kinds=kind_list, target=target, aux_targets=aux_list,
            n_splits=splits, embargo=embargo, val_ratio=val_ratio,
            thresholds=tuple(threshold) if threshold else DEFAULT_THRESHOLDS,
            out_dir=Path(out_dir) if out_dir else None,
            models_dir=Path(models_dir) if models_dir else None,
            promote_models=promote, force=force,
            progress=lambda message: click.echo(f"   {message}"),
        )
    except ImportError as exc:
        click.echo(f"❌ {exc}", err=True)
        sys.exit(3)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"❌ {exc}", err=True)
        sys.exit(2)

    click.echo(f"\n📋 数据集 {report.dataset_id} · 模型 {len(report.trained)} 个"
               f" · 评估 {len(report.evaluations)} 份")
    primary = report.primary
    if primary is not None:
        click.echo(
            f"   主模型 {primary.model_id}：AUC {fmt_number(primary.auc)} · "
            f"Brier {fmt_number(primary.brier)}（基线 {fmt_number(primary.base_rate_brier)}）· "
            f"{'✓ 跑赢' if primary.beats_base_rate else '✗ 未跑赢'}常数基线"
        )
    if report.promotions:
        for outcome in report.promotions:
            icon = _promotion_icon(outcome.status)
            click.echo(f"   {icon} 晋升 {outcome.model_id} → {outcome.status}"
                       f"{'（stale）' if outcome.stale else ''}")
    else:
        click.echo("   （未请求晋升：模型停在 candidate，看板/监控的在位模型未受影响）")
    warnings = collect_warnings(report)
    for warning in warnings[:8]:
        # 警告自带严重度标记：evaluate 的硬警告以 ⚠️ 起头、口径提示以（…）起头，manifest
        # 的则是裸串。只给裸串补 ⚠️，否则会叠成"⚠️ ⚠️"或把口径提示误标成硬警告。
        prefix = "" if (warning.startswith("⚠️") or warning.startswith("（")) else "⚠️ "
        click.echo(f"   {prefix}{warning}")
    if len(warnings) > 8:
        click.echo(f"   …另 {len(warnings) - 8} 条见报告")
    path = write_report(report, Path(report_dir))
    click.echo(f"\n📄 管道报告：{path}")


@cli.command()
def version():
    """显示版本"""
    click.echo(f"TradePilot v{__version__}")


if __name__ == '__main__':
    cli()
