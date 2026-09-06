"""机器学习信号质量模型（B/D 阶段）。

子模块按依赖分层，均为可独立导入的纯函数/轻量 IO：

- ``labels``：前瞻收益标签（B1，纯函数，镜像引擎成本口径）
- ``splits``：时序切分 + purge/embargo（B3，纯函数）
- ``calibration``：校准与决策价值评估（B4，numpy 纯函数，sklearn 可选加速）
- ``features`` / ``dataset`` / ``models`` / ``registry`` / ``evaluate`` / ``scoring``（D 阶段）

本 ``__init__`` 刻意保持空导入：sklearn 为可选依赖（懒加载），未安装时
``import ripple_tradePilot.ml`` 及其纯函数子模块都不应报错。需要 sklearn 的
模块（models/registry/scoring）在函数内部再导入。
"""
