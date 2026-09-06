"""时序切分 + purge/embargo（B3 · 纯函数）。

普通 K 折在金融时序上会泄漏：5 日前瞻标签让相邻样本共享未来信息，随机打乱更把
"用未来预测过去"合法化。本模块提供**锚定式（expanding-window）滚动切分**，并按
每行自己的 ``exit_date`` 做 purge、再空 ``embargo`` 个交易日，杜绝边界重叠泄漏。

核心不变量（测试逐条钉住）：

1. **零泄漏**：任何训练/验证样本的 ``exit_date`` 严格早于其测试折的首日
   （标签前瞻窗口不触及测试期）；
2. **purge**：剔除 ``exit_date >= test_start_date`` 的训练样本——这是 5 日标签
   跨边界重叠的精确处理（不是简单丢最后 5 行，而是按每行真实 exit_date）；
3. **embargo**：再空 ``embargo`` 个交易日，吸收残留的序列依赖；
4. **锚定**：train = test 折首日之前的全部样本（expanding window），test 折把
   时间轴尾部等分；
5. **折间无重叠**：各 test 折互斥；
6. **holdout 隔离**：:func:`holdout_split` 的最终保留集永不进 train/val。

样本可为多标的池化（同一 trade_date 多行），故切分一律**按日期**而非按行号，
避免把同一天的不同标的拆进 train/test 两侧。``trade_dates``/``exit_dates`` 须
互相可比较（同为 ``YYYYMMDD`` 字符串或同为 ``date``）。
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from typing import List, Sequence, Tuple

__all__ = ["RollingSplit", "HoldoutSplit", "rolling_splits", "holdout_split"]


@dataclass(frozen=True)
class RollingSplit:
    """单个锚定滚动折。索引均为指向原数组的行号（升序 tuple）。"""

    split_index: int
    train_idx: Tuple[int, ...]
    val_idx: Tuple[int, ...]
    test_idx: Tuple[int, ...]
    test_start_date: object  # 测试折首日（purge/embargo 的基准）
    test_end_date: object  # 测试折末日
    n_purged: int  # 因 exit_date 越界被剔除的训练样本数
    n_embargoed: int  # 因 embargo 被剔除的训练样本数


@dataclass(frozen=True)
class HoldoutSplit:
    """最终保留集切分：holdout 永不进 train/val。"""

    train_idx: Tuple[int, ...]
    holdout_idx: Tuple[int, ...]
    holdout_start_date: object


def _block_bounds(n_items: int, n_blocks: int) -> List[int]:
    """把 n_items 个元素均分为 n_blocks 段的边界下标（含首尾，长度 n_blocks+1）。"""
    return [round(k * n_items / n_blocks) for k in range(n_blocks + 1)]


def rolling_splits(
    trade_dates: Sequence[object],
    exit_dates: Sequence[object],
    n_splits: int,
    val_ratio: float = 0.2,
    embargo: int = 2,
) -> List[RollingSplit]:
    """锚定式滚动切分：n_splits 个 test 折把时间轴尾部等分，train 为其前全部样本。

    把唯一交易日均分为 ``n_splits + 1`` 段：第 0 段为初始训练种子，第 1..n_splits
    段为 successive test 折。第 j 折：

    - ``test_idx`` = trade_date 落在第 j 段的行；
    - train 候选 = trade_date < 该折首日的全部行；
    - **purge**：剔除 ``exit_date >= test_start_date`` 的候选（标签前瞻触及测试期）；
    - **embargo**：再剔除 trade_date 落在测试首日前 ``embargo`` 个交易日内的候选；
    - ``val_idx`` = 余下 train 的日期尾部 ``val_ratio`` 段（已随 train 一并 purge），
      其余为 ``train_idx``。

    需要至少 ``n_splits + 1`` 个唯一交易日。
    """
    n = len(trade_dates)
    if len(exit_dates) != n:
        raise ValueError("trade_dates 与 exit_dates 长度必须一致")
    if n_splits < 1:
        raise ValueError("n_splits 必须 ≥ 1")
    if not (0.0 < val_ratio < 1.0):
        raise ValueError("val_ratio 必须落在 (0, 1)")
    if embargo < 0:
        raise ValueError("embargo 必须 ≥ 0")

    unique_dates = sorted(set(trade_dates))
    n_blocks = n_splits + 1
    if len(unique_dates) < n_blocks:
        raise ValueError(
            f"唯一交易日不足：需 ≥ n_splits+1={n_blocks}，实际 {len(unique_dates)}"
        )

    bounds = _block_bounds(len(unique_dates), n_blocks)
    splits: List[RollingSplit] = []
    for j in range(1, n_blocks):
        block_dates = unique_dates[bounds[j] : bounds[j + 1]]
        if not block_dates:
            continue
        test_start_date = block_dates[0]
        test_end_date = block_dates[-1]
        test_set = set(block_dates)
        test_idx = tuple(r for r in range(n) if trade_dates[r] in test_set)

        # embargo 截止日：测试首日前 embargo 个交易日
        start_pos = bisect_left(unique_dates, test_start_date)
        cut_pos = max(0, start_pos - embargo)
        embargo_cutoff_date = unique_dates[cut_pos]

        # train 候选 → purge（exit_date 越界）→ embargo（贴近测试期）
        candidates = [r for r in range(n) if trade_dates[r] < test_start_date]
        after_purge = [r for r in candidates if exit_dates[r] < test_start_date]
        n_purged = len(candidates) - len(after_purge)
        train_pool = [r for r in after_purge if trade_dates[r] < embargo_cutoff_date]
        n_embargoed = len(after_purge) - len(train_pool)

        # val = train 日期尾部 val_ratio 段
        train_idx: Tuple[int, ...] = ()
        val_idx: Tuple[int, ...] = ()
        if train_pool:
            train_dates = sorted({trade_dates[r] for r in train_pool})
            val_cut_pos = int(round(len(train_dates) * (1.0 - val_ratio)))
            val_cut_pos = max(0, min(val_cut_pos, len(train_dates) - 1))
            val_start_date = train_dates[val_cut_pos]
            val_idx = tuple(
                sorted(r for r in train_pool if trade_dates[r] >= val_start_date)
            )
            train_idx = tuple(
                sorted(r for r in train_pool if trade_dates[r] < val_start_date)
            )

        splits.append(
            RollingSplit(
                split_index=j - 1,
                train_idx=train_idx,
                val_idx=val_idx,
                test_idx=test_idx,
                test_start_date=test_start_date,
                test_end_date=test_end_date,
                n_purged=n_purged,
                n_embargoed=n_embargoed,
            )
        )
    return splits


def holdout_split(
    trade_dates: Sequence[object], holdout_start
) -> HoldoutSplit:
    """按日期把样本切成 train（< holdout_start）与最终保留集（>= holdout_start）。

    保留集永不进 train/val：用于在所有调参/校准结束后做一次性最终评估。
    调用方应先用本函数切出 holdout，再对 train 部分跑 :func:`rolling_splits`。
    """
    n = len(trade_dates)
    train_idx = tuple(r for r in range(n) if trade_dates[r] < holdout_start)
    holdout_idx = tuple(r for r in range(n) if trade_dates[r] >= holdout_start)
    return HoldoutSplit(
        train_idx=train_idx,
        holdout_idx=holdout_idx,
        holdout_start_date=holdout_start,
    )
