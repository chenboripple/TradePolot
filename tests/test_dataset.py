"""D2 ``ml/dataset.py`` 的离线测试（含 ``tradepilot ml build-dataset`` CLI）。

覆盖：manifest 数字自洽（行数 = Σ可标注天数 = 标的数 ×(bars−horizon−1)）、csv.gz 回读、
标签抽样与 B1 ``horizon_label`` 手算逐值一致、exit_date = trade_date 后第 horizon+1 个交易日
（B3 purge 依赖）、A9 复权混接嫌疑拒入、无数据标的拒入、组开关列集合、缺市场数据
``_has_market=0`` + 警告、start/end 过滤、dataset_id 内容 hash 幂等、ml_datasets 表
register/round-trip、profile/data_version provenance、自定义 resolver，以及 CLI 三种
``--pool`` + ``--symbols`` 覆盖 + 非法组 exit 2。全程合成夹具、零网络。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import synth
from click.testing import CliRunner

from ripple_tradePilot.cli import cli
from ripple_tradePilot.ml.dataset import LABEL_COLUMNS, build_dataset, load_dataset_frame
from ripple_tradePilot.ml.features import FEATURE_GROUPS, expected_columns
from ripple_tradePilot.ml.labels import horizon_label
from ripple_tradePilot.signals.facade import coerce_bars
from ripple_tradePilot.signals.profile import ComponentSpec, ProfileSpec
from ripple_tradePilot.storage.database import (
    list_datasets,
    load_daily_bars,
    load_dataset_manifest,
    upsert_daily_bars,
)

SYMBOLS = ["002022.SZ", "600309.SH", "601816.SH"]


class _DatasetBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.db = root / "market.db"
        self.out = root / "ml"
        synth.seed_market_db(self.db, symbols=SYMBOLS, days=300)

    def build(self, symbols=SYMBOLS, **kwargs):
        kwargs.setdefault("db_path", self.db)
        kwargs.setdefault("out_dir", self.out)
        return build_dataset(symbols, **kwargs)

    def _feature_cols(self, frame):
        skip = {"symbol", "trade_date", "_has_market", "_has_industry", *LABEL_COLUMNS}
        return set(c for c in frame.columns if c not in skip)


class ManifestTest(_DatasetBase):
    def test_counts_self_consistent(self):
        m = self.build()
        # 300 bars，horizon=5 → 可标注 i 满足 i+1+5<300 → i<294 → 每标的 294 行
        self.assertEqual(m.n_rows, 3 * 294)
        self.assertEqual(m.symbols, tuple(SYMBOLS))
        self.assertEqual(m.rejected, ())
        self.assertEqual(m.n_positive, int(round(m.positive_rate * m.n_rows)))
        self.assertAlmostEqual(m.positive_rate, m.n_positive / m.n_rows)
        self.assertEqual(m.horizon, 5)
        self.assertEqual(m.aux_horizon, 10)
        self.assertFalse(m.industry_point_in_time)
        self.assertEqual(m.groups, FEATURE_GROUPS)

    def test_date_range_and_freshness(self):
        m = self.build()
        self.assertEqual(m.start_date, "20240102")  # synth DEFAULT_START
        self.assertEqual(m.max_trade_date, m.end_date)
        # 末日 = 第 294 个交易日（i=293）的 trade_date
        all_dates = [str(r["trade_date"]) for r in load_daily_bars(SYMBOLS[0], self.db)]
        self.assertEqual(m.end_date, all_dates[293])

    def test_csv_roundtrip_and_columns(self):
        m = self.build()
        frame = load_dataset_frame(m.csv_path)
        self.assertEqual(len(frame), m.n_rows)
        for column in LABEL_COLUMNS:
            self.assertIn(column, frame.columns)
        self.assertEqual(self._feature_cols(frame), set(expected_columns(FEATURE_GROUPS)))
        self.assertEqual(sorted(frame["label_win"].unique()), [0, 1])

    def test_profile_and_data_version_provenance(self):
        m = self.build()
        self.assertEqual(set(m.profile_snapshot.keys()), set(SYMBOLS))
        self.assertEqual(set(m.data_versions.keys()), set(SYMBOLS))
        self.assertTrue(all(v.startswith("synth|") for v in m.data_versions.values()))
        snap = m.profile_snapshot[SYMBOLS[0]]
        self.assertEqual(snap["board_code"], "BK0475")
        self.assertEqual(snap["spec"]["kind"], "combo_vote")
        self.assertEqual(
            [c["name"] for c in snap["spec"]["components"]], ["ma", "rsi", "bollinger"]
        )

    def test_point_in_time_warning_present(self):
        m = self.build()
        self.assertTrue(
            any("industry_point_in_time=False" in w for w in m.warnings), m.warnings
        )


class LabelAlignmentTest(_DatasetBase):
    def test_label_matches_b1_handcalc(self):
        symbol = SYMBOLS[0]
        m = self.build([symbol])
        frame = load_dataset_frame(m.csv_path)
        bars = coerce_bars(load_daily_bars(symbol, self.db))
        opens = [b.open for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        closes = [b.close for b in bars]
        index_by_date = {str(r["trade_date"]): i for i, r in enumerate(load_daily_bars(symbol, self.db))}

        for _, row in frame.iloc[[50, 150, 250]].iterrows():
            i = index_by_date[str(row["trade_date"])]
            label = horizon_label(opens, highs, lows, closes, i, horizon=5)
            self.assertIsNotNone(label)
            self.assertAlmostEqual(row["label_net_return"], label.net_return, places=9)
            self.assertAlmostEqual(row["label_mae"], label.mae, places=9)
            self.assertEqual(int(row["label_win"]), int(label.win))
            aux = horizon_label(opens, highs, lows, closes, i, horizon=10)
            if aux is not None:
                self.assertAlmostEqual(row["label_net_return_aux"], aux.net_return, places=9)

    def test_exit_date_is_horizon_plus_one_trading_days_ahead(self):
        symbol = SYMBOLS[0]
        m = self.build([symbol])
        frame = load_dataset_frame(m.csv_path)
        all_dates = [str(r["trade_date"]) for r in load_daily_bars(symbol, self.db)]
        index_by_date = {d: i for i, d in enumerate(all_dates)}
        for _, row in frame.iloc[[0, 100, 293]].iterrows():
            i = index_by_date[str(row["trade_date"])]
            self.assertEqual(str(row["exit_date"]), all_dates[i + 6])  # i+1+horizon


class RejectionTest(_DatasetBase):
    def test_rebase_suspect_rejected(self):
        bad_rows = synth.scale_rows(
            synth.daily_rows(synth.daily_bars(300, seed=55)), 0.9, start=150
        )
        upsert_daily_bars("999999.SH", bad_rows, "synth", self.db)
        m = self.build(SYMBOLS + ["999999.SH"])
        reasons = {r["symbol"]: r["reason"] for r in m.rejected}
        self.assertIn("999999.SH", reasons)
        self.assertIn("rebase_suspect", reasons["999999.SH"])
        self.assertNotIn("999999.SH", m.symbols)
        self.assertTrue(any("拒入" in w for w in m.warnings))

    def test_no_data_symbol_rejected(self):
        m = self.build(SYMBOLS + ["000001.SZ"])  # 000001 未灌库
        reasons = {r["symbol"]: r["reason"] for r in m.rejected}
        self.assertEqual(reasons["000001.SZ"], "no_data")

    def test_all_rejected_yields_empty_dataset(self):
        m = self.build(["000001.SZ"])  # 全部无数据
        self.assertEqual(m.n_rows, 0)
        self.assertEqual(m.symbols, ())
        self.assertTrue(Path(m.csv_path).exists())  # 仍产出空 csv + manifest


class GroupSwitchTest(_DatasetBase):
    def test_price_volume_only_no_market_flag(self):
        m = self.build(groups=("price_volume",))
        frame = load_dataset_frame(m.csv_path)
        self.assertNotIn("_has_market", frame.columns)
        self.assertEqual(self._feature_cols(frame), set(expected_columns(("price_volume",))))

    def test_signal_only_uses_default_trio(self):
        m = self.build(groups=("signal",))
        frame = load_dataset_frame(m.csv_path)
        for col in ("comp_ma", "comp_rsi", "comp_bollinger", "rec_buy", "state_age"):
            self.assertIn(col, frame.columns)

    def test_custom_resolver_spec(self):
        spec = ProfileSpec(
            kind="combo_vote",
            components=(ComponentSpec(kind="ma", name="ma", params={"fast": 5, "slow": 10}),),
            vote_threshold=1,
            source="resolver",
        )
        m = self.build([SYMBOLS[0]], profile_resolver=lambda s: spec)
        frame = load_dataset_frame(m.csv_path)
        self.assertIn("comp_ma", frame.columns)
        self.assertNotIn("comp_rsi", frame.columns)  # 单 MA 组件
        self.assertEqual(m.profile_snapshot[SYMBOLS[0]]["spec"]["vote_threshold"], 1)

    def test_missing_market_data_flag_zero_and_warning(self):
        db2 = Path(self._tmp.name) / "nomarket.db"
        synth.seed_market_db(db2, symbols=SYMBOLS, days=200, with_market=False)
        m = build_dataset(
            SYMBOLS, db_path=db2, out_dir=self.out, groups=("price_volume", "market")
        )
        frame = load_dataset_frame(m.csv_path)
        self.assertEqual(frame["_has_market"].sum(), 0)
        self.assertTrue(any("market 组全部 NaN" in w for w in m.warnings), m.warnings)

    def test_unknown_group_raises(self):
        with self.assertRaises(ValueError):
            self.build(groups=("bogus",))


class FilteringTest(_DatasetBase):
    def test_start_end_window(self):
        m_all = self.build([SYMBOLS[0]])
        m_win = self.build([SYMBOLS[0]], start="20240601", end="20240630")
        self.assertLess(m_win.n_rows, m_all.n_rows)
        frame = load_dataset_frame(m_win.csv_path)
        self.assertTrue((frame["trade_date"] >= 20240601).all())
        self.assertTrue((frame["trade_date"] <= 20240630).all())
        self.assertEqual(m_win.start_date, str(frame["trade_date"].min()))

    def test_horizon_change_shifts_labelable_count(self):
        m5 = self.build([SYMBOLS[0]], horizon=5)
        m10 = self.build([SYMBOLS[0]], horizon=10)
        # horizon 越大，尾部不可标注越多 → 行数越少
        self.assertGreater(m5.n_rows, m10.n_rows)
        self.assertEqual(m5.n_rows, 294)
        self.assertEqual(m10.n_rows, 289)  # 300-10-1


class PersistenceTest(_DatasetBase):
    def test_idempotent_dataset_id(self):
        m1 = self.build()
        m2 = self.build()
        self.assertEqual(m1.dataset_id, m2.dataset_id)
        self.assertEqual(len(list_datasets(self.db)), 1)  # upsert 不重复

    def test_register_roundtrip(self):
        m = self.build()
        dbm = load_dataset_manifest(m.dataset_id, self.db)
        self.assertIsNotNone(dbm)
        self.assertEqual(dbm["n_rows"], m.n_rows)
        self.assertEqual(dbm["symbols"], list(m.symbols))
        self.assertFalse(dbm["industry_point_in_time"])
        self.assertEqual(dbm["groups"], list(m.groups))
        self.assertEqual(dbm["csv_path"], m.csv_path)
        self.assertEqual(dbm["profile_snapshot"][SYMBOLS[0]]["board_code"], "BK0475")

    def test_no_register_skips_table_but_writes_files(self):
        m = self.build(register=False)
        self.assertIsNone(load_dataset_manifest(m.dataset_id, self.db))
        self.assertTrue(Path(m.csv_path).exists())
        manifest_json = Path(self.out) / f"{m.dataset_id}.manifest.json"
        self.assertTrue(manifest_json.exists())

    def test_manifest_json_file_matches(self):
        import json

        m = self.build()
        payload = json.loads(
            (Path(self.out) / f"{m.dataset_id}.manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["dataset_id"], m.dataset_id)
        self.assertEqual(payload["n_rows"], m.n_rows)


CONFIG_WITH_SYMBOLS = """
symbols:
  - code: "002022.SZ"
    name: "a"
  - code: "600309.SH"
    name: "b"
tushare:
  token: TESTTOKEN
"""


class CliBuildDatasetTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.config = root / "config.yaml"
        self.config.write_text(CONFIG_WITH_SYMBOLS, encoding="utf-8")
        self.db = root / "backtest.db"
        self.out = root / "ml"
        synth.seed_market_db(self.db, symbols=SYMBOLS, days=200)
        env = patch.dict(
            os.environ,
            {"TRADEPILOT_CONFIG": str(self.config), "TRADEPILOT_BACKTEST_DB": str(self.db)},
        )
        env.start()
        self.addCleanup(env.stop)
        self.runner = CliRunner()

    def test_config_pool(self):
        result = self.runner.invoke(
            cli, ["ml", "build-dataset", "--pool", "config", "--out-dir", str(self.out)]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("dataset_id", result.output)
        self.assertIn("构建 ML 数据集", result.output)
        self.assertEqual(len(list_datasets(self.db)), 1)

    def test_symbols_override(self):
        result = self.runner.invoke(
            cli,
            ["ml", "build-dataset", "--symbols", "002022.SZ", "--out-dir", str(self.out)],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        manifest = list_datasets(self.db)[0]
        self.assertEqual(manifest["symbols"], ["002022.SZ"])

    def test_catalog_pool_uses_all_bar_symbols(self):
        result = self.runner.invoke(
            cli, ["ml", "build-dataset", "--pool", "catalog", "--out-dir", str(self.out)]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        manifest = list_datasets(self.db)[0]
        self.assertEqual(set(manifest["symbols"]), set(SYMBOLS))

    def test_groups_option(self):
        result = self.runner.invoke(
            cli,
            ["ml", "build-dataset", "--groups", "price_volume", "--symbols", "002022.SZ",
             "--out-dir", str(self.out)],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(list_datasets(self.db)[0]["groups"], ["price_volume"])

    def test_bad_groups_exit_2(self):
        result = self.runner.invoke(
            cli, ["ml", "build-dataset", "--groups", "bogus", "--out-dir", str(self.out)]
        )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("未知特征组", result.output)

    def test_empty_pool_exit_2(self):
        self.config.write_text("symbols: []\ntushare:\n  token: T\n", encoding="utf-8")
        result = self.runner.invoke(
            cli, ["ml", "build-dataset", "--pool", "config", "--out-dir", str(self.out)]
        )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("无标的", result.output)


if __name__ == "__main__":
    unittest.main()
