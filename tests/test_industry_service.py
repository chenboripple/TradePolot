"""C3 ``data/industry_service.py`` 的离线测试。

覆盖：东财三接口归一化（板块列表/板块日线中文列名 + 换手率、成分股裸代码→规范 symbol、
malformed 跳过）、refresh_boards/board_bars/membership 的失败注入与 upsert 幂等、
refresh_for_symbols 的"membership 先行 → 板块去重 → 逐板块"编排（DB 成分优先 / catalog
静态标签兜底模糊匹配 / 无法解析记入 unresolved / 部分成功报告 / 限流可配）。

全程 mock 模块级 ``ak.*`` 函数，零网络（沿用 test_market_service.py 的 patch 模式）。
"""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from ripple_tradePilot.data import industry_service as isvc
from ripple_tradePilot.data.industry_service import (
    IndustryDataService,
    _match_board_by_industry,
    _normalize_board_cons,
    _normalize_board_hist,
    _normalize_board_list,
)
from ripple_tradePilot.data.stock_service import StockDataUnavailableError
from ripple_tradePilot.storage.database import (
    industry_board_for_symbol,
    init_database,
    load_industry_board_bars,
    load_industry_boards,
    load_industry_membership,
    upsert_industry_boards,
    upsert_industry_membership,
    upsert_stock_catalog,
)


def _board_list_frame():
    """东财 stock_board_industry_name_em 风格：含板块名称/板块代码（中文列）。"""
    return pd.DataFrame(
        {
            "排名": [1, 2],
            "板块名称": ["银行", "小金属"],
            "板块代码": ["BK0475", "BK1027"],
            "最新价": [1000.0, 2000.0],
        }
    )


def _board_hist_frame(count=5, start_close=1000.0):
    """东财 stock_board_industry_hist_em 风格：中文列、日期=YYYY-MM-DD、含换手率。"""
    base = date(2026, 1, 1)
    return pd.DataFrame(
        {
            "日期": [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(count)],
            "开盘": [start_close + i for i in range(count)],
            "收盘": [start_close + i * 2 for i in range(count)],
            "最高": [start_close + i + 5 for i in range(count)],
            "最低": [start_close + i - 5 for i in range(count)],
            "涨跌幅": [0.1 * i for i in range(count)],
            "成交额": [1e9] * count,
            "换手率": [1.0 + 0.1 * i for i in range(count)],
        }
    )


def _board_cons_frame(codes):
    """东财 stock_board_industry_cons_em 风格：代码列为 6 位裸代码。"""
    return pd.DataFrame({"代码": list(codes), "名称": ["x"] * len(codes)})


class _TmpDbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "industry.db"
        init_database(self.db)

    def service(self, **kwargs):
        # config={} 避免 load_config()；rate_limit_delay=0 避免测试 sleep
        kwargs.setdefault("config", {})
        kwargs.setdefault("path", self.db)
        kwargs.setdefault("rate_limit_delay", 0)
        return IndustryDataService(**kwargs)


# ---------------------------------------------------------------------------
# 归一化纯函数
# ---------------------------------------------------------------------------
class NormalizeTest(unittest.TestCase):
    def test_normalize_board_list(self):
        records = _normalize_board_list(_board_list_frame())
        self.assertEqual(
            records,
            [{"board_code": "BK0475", "board_name": "银行"},
             {"board_code": "BK1027", "board_name": "小金属"}],
        )

    def test_normalize_board_list_empty_or_bad_columns(self):
        self.assertEqual(_normalize_board_list(None), [])
        self.assertEqual(_normalize_board_list(pd.DataFrame()), [])
        self.assertEqual(_normalize_board_list(pd.DataFrame({"foo": [1]})), [])

    def test_normalize_board_hist_chinese_columns(self):
        rows = _normalize_board_hist(_board_hist_frame())
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["trade_date"], "20260101")  # 升序、YYYYMMDD
        self.assertEqual(rows[0]["close"], 1000.0)
        self.assertAlmostEqual(rows[0]["turnover_rate"], 1.0)
        self.assertAlmostEqual(rows[1]["pct_chg"], 0.1)

    def test_normalize_board_hist_skips_bad_rows(self):
        frame = _board_hist_frame(3)
        frame.loc[1, "收盘"] = float("nan")
        frame.loc[2, "日期"] = "garbage"
        rows = _normalize_board_hist(frame)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_date"], "20260101")

    def test_normalize_board_cons_normalizes_and_skips_malformed(self):
        symbols = _normalize_board_cons(_board_cons_frame(["600000", "000001", "bad", "300750"]))
        self.assertEqual(symbols, ["600000.SH", "000001.SZ", "300750.SZ"])  # bad 跳过

    def test_normalize_board_cons_empty(self):
        self.assertEqual(_normalize_board_cons(None), [])
        self.assertEqual(_normalize_board_cons(pd.DataFrame({"foo": [1]})), [])

    def test_match_board_by_industry(self):
        name_to_code = {"银行": "BK0475", "小金属": "BK1027"}
        self.assertEqual(_match_board_by_industry("银行", name_to_code), "BK0475")  # 精确
        self.assertEqual(_match_board_by_industry("银行类", name_to_code), "BK0475")  # 子串模糊
        self.assertEqual(_match_board_by_industry("小金属", name_to_code), "BK1027")
        self.assertIsNone(_match_board_by_industry("白酒", name_to_code))  # 匹配不上
        self.assertIsNone(_match_board_by_industry("", name_to_code))  # 空标签


class NormalizeSymbolsTest(unittest.TestCase):
    def test_dedupe_and_skip_malformed(self):
        out = IndustryDataService._normalize_symbols(
            ["600000", "600000.SH", "bad", "", "000001.SZ"]
        )
        self.assertEqual(out, ["600000.SH", "000001.SZ"])  # 去重 + 跳过 malformed，保序


# ---------------------------------------------------------------------------
# refresh_boards / board_bars / membership
# ---------------------------------------------------------------------------
class RefreshBoardsTest(_TmpDbTestCase):
    def test_success(self):
        with patch.object(isvc.ak, "stock_board_industry_name_em", return_value=_board_list_frame()):
            report = self.service().refresh_boards()
        self.assertEqual(report["count"], 2)
        boards = load_industry_boards(self.db)
        self.assertEqual([b["board_code"] for b in boards], ["BK0475", "BK1027"])

    def test_empty_raises(self):
        with patch.object(isvc.ak, "stock_board_industry_name_em", return_value=pd.DataFrame()):
            with self.assertRaises(StockDataUnavailableError):
                self.service().refresh_boards()

    def test_network_error_raises(self):
        with patch.object(isvc.ak, "stock_board_industry_name_em", side_effect=RuntimeError("ak down")):
            with self.assertRaises(StockDataUnavailableError):
                self.service().refresh_boards()


class RefreshBoardBarsTest(_TmpDbTestCase):
    def test_success_and_idempotent(self):
        with patch.object(isvc.ak, "stock_board_industry_hist_em", return_value=_board_hist_frame()):
            service = self.service()
            report = service.refresh_board_bars("银行", "BK0475")
            service.refresh_board_bars("银行", "BK0475")  # 第二次覆盖不重复
        self.assertEqual(report["rows"], 5)
        self.assertEqual(len(load_industry_board_bars("BK0475", self.db)), 5)

    def test_empty_raises(self):
        with patch.object(isvc.ak, "stock_board_industry_hist_em", return_value=pd.DataFrame()):
            with self.assertRaises(StockDataUnavailableError):
                self.service().refresh_board_bars("银行", "BK0475")

    def test_uses_board_name_as_symbol(self):
        # hist_em 入参是板块**名称**而非代码——确认调用传的是名称
        with patch.object(isvc.ak, "stock_board_industry_hist_em", return_value=_board_hist_frame()) as m:
            self.service().refresh_board_bars("银行", "BK0475")
        self.assertEqual(m.call_args.kwargs["symbol"], "银行")


class RefreshMembershipTest(_TmpDbTestCase):
    def test_success_normalizes_codes(self):
        with patch.object(isvc.ak, "stock_board_industry_cons_em", return_value=_board_cons_frame(["600000", "000001"])):
            report = self.service().refresh_membership("银行", "BK0475", as_of="20260102")
        self.assertEqual(report["symbols"], 2)
        members = load_industry_membership("BK0475", self.db)
        self.assertEqual([m["symbol"] for m in members], ["000001.SZ", "600000.SH"])
        self.assertEqual(members[0]["as_of"], "20260102")

    def test_empty_raises(self):
        with patch.object(isvc.ak, "stock_board_industry_cons_em", return_value=pd.DataFrame()):
            with self.assertRaises(StockDataUnavailableError):
                self.service().refresh_membership("银行", "BK0475")


# ---------------------------------------------------------------------------
# refresh_for_symbols 编排
# ---------------------------------------------------------------------------
class RefreshForSymbolsTest(_TmpDbTestCase):
    def _seed_boards_and_catalog(self):
        # 板块登记（refresh_boards 会再覆盖，但解析时需要 name<->code 映射）
        upsert_industry_boards(
            [{"board_code": "BK0475", "board_name": "银行"},
             {"board_code": "BK1027", "board_name": "小金属"}],
            "em", self.db,
        )
        # catalog 静态标签兜底：600000→银行，000001→银行，300750→小金属
        upsert_stock_catalog(
            [{"symbol": "600000.SH", "name": "浦发", "industry": "银行"},
             {"symbol": "000001.SZ", "name": "平安", "industry": "银行"},
             {"symbol": "300750.SZ", "name": "宁德", "industry": "小金属"}],
            "synth", self.db,
        )

    def test_empty_symbols_returns_empty_report(self):
        report = self.service().refresh_for_symbols([])
        self.assertEqual(report["symbols_total"], 0)
        self.assertEqual(report["boards_total"], 0)
        self.assertEqual(report["boards_refreshed"], [])

    def test_resolves_via_catalog_fallback_and_dedupes_boards(self):
        self._seed_boards_and_catalog()
        with patch.object(isvc.ak, "stock_board_industry_name_em", return_value=_board_list_frame()), \
             patch.object(isvc.ak, "stock_board_industry_cons_em", return_value=_board_cons_frame(["600000"])), \
             patch.object(isvc.ak, "stock_board_industry_hist_em", return_value=_board_hist_frame(3)):
            # 600000 与 000001 都属"银行"→ 板块去重为 1 个；300750 属"小金属"→ 共 2 板块
            report = self.service().refresh_for_symbols(["600000", "000001", "300750"])
        self.assertEqual(report["symbols_total"], 3)
        self.assertEqual(report["symbols_resolved"], 3)
        self.assertEqual(report["symbols_unresolved"], [])
        self.assertEqual(report["boards_total"], 2)  # 去重后银行 + 小金属
        self.assertEqual(sorted(report["boards_refreshed"]), ["BK0475", "BK1027"])
        self.assertEqual(report["boards_failed"], [])
        self.assertGreater(report["membership_rows"], 0)
        self.assertGreater(report["bar_rows"], 0)

    def test_db_membership_takes_priority(self):
        # DB 已有成分快照 → 直接命中，无需 catalog 兜底
        upsert_industry_boards([{"board_code": "BK0475", "board_name": "银行"}], "em", self.db)
        upsert_industry_membership("BK0475", ["600000.SH"], "20260101", "em", self.db)
        with patch.object(isvc.ak, "stock_board_industry_name_em", return_value=_board_list_frame()), \
             patch.object(isvc.ak, "stock_board_industry_cons_em", return_value=_board_cons_frame(["600000"])), \
             patch.object(isvc.ak, "stock_board_industry_hist_em", return_value=_board_hist_frame(2)):
            report = self.service().refresh_for_symbols(["600000"])
        self.assertEqual(report["boards_total"], 1)
        self.assertEqual(report["symbols_resolved"], 1)
        self.assertEqual(industry_board_for_symbol("600000.SH", self.db), "BK0475")

    def test_unresolved_symbol_reported(self):
        self._seed_boards_and_catalog()
        with patch.object(isvc.ak, "stock_board_industry_name_em", return_value=_board_list_frame()), \
             patch.object(isvc.ak, "stock_board_industry_cons_em", return_value=_board_cons_frame(["600000"])), \
             patch.object(isvc.ak, "stock_board_industry_hist_em", return_value=_board_hist_frame(2)):
            # 999999 既无 DB membership 也无 catalog 标签 → unresolved（行业特征将缺失）
            report = self.service().refresh_for_symbols(["600000", "999999"])
        self.assertEqual(report["symbols_resolved"], 1)
        self.assertEqual(report["symbols_unresolved"], ["999999.SH"])

    def test_partial_success_when_one_board_fails(self):
        self._seed_boards_and_catalog()

        def cons_side_effect(symbol):
            if symbol == "小金属":
                raise RuntimeError("东财限流")
            return _board_cons_frame(["600000"])

        def hist_side_effect(symbol, **kwargs):
            if symbol == "小金属":
                raise RuntimeError("东财限流")
            return _board_hist_frame(2)

        with patch.object(isvc.ak, "stock_board_industry_name_em", return_value=_board_list_frame()), \
             patch.object(isvc.ak, "stock_board_industry_cons_em", side_effect=cons_side_effect), \
             patch.object(isvc.ak, "stock_board_industry_hist_em", side_effect=hist_side_effect):
            report = self.service().refresh_for_symbols(["600000", "300750"])
        # 银行成功、小金属成分股+日线双失败 → 部分成功报告
        self.assertEqual(report["boards_refreshed"], ["BK0475"])
        self.assertEqual(len(report["boards_failed"]), 1)
        self.assertEqual(report["boards_failed"][0]["board_code"], "BK1027")
        self.assertIn("小金属", report["boards_failed"][0]["error"])

    def test_board_registry_failure_falls_back_to_db_boards(self):
        # refresh_boards 网络失败不致命：仍用 DB 既有登记解析板块
        self._seed_boards_and_catalog()
        with patch.object(isvc.ak, "stock_board_industry_name_em", side_effect=RuntimeError("ak down")), \
             patch.object(isvc.ak, "stock_board_industry_cons_em", return_value=_board_cons_frame(["600000"])), \
             patch.object(isvc.ak, "stock_board_industry_hist_em", return_value=_board_hist_frame(2)):
            report = self.service().refresh_for_symbols(["600000"])
        self.assertEqual(report["boards_total"], 1)
        self.assertEqual(report["boards_refreshed"], ["BK0475"])


if __name__ == "__main__":
    unittest.main()
