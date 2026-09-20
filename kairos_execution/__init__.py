"""Kairos Execution —— 自研轻量交易执行框架（纯模拟，不接真实券商）。

模块一览：

- :mod:`types`  : 核心数据类型（Side / OrderType / OrderStatus / Order / Fill /
                  Position / Account / Bar）。
- :mod:`oms`    : 内存订单管理系统，含订单状态机与事件日志。
- :mod:`broker` : 券商抽象与 ``SimBroker`` 模拟撮合（延迟、参与率、滑点、限价/止损）。
- :mod:`algos`  : 执行算法（TWAP / VWAP / Iceberg / ImplementationShortfall）。
- :mod:`tca`    : 交易成本分析（到达价 / 滑点 / 成交率 / 价差 / 冲击 / 实现差额）。
- :mod:`sim`    : 合成 OHLCV 行情（离线、固定 seed 可复现）。

设计原则：纯 numpy/pandas 依赖、确定性、可扩展、面向模拟与教学，不做实盘下单。
"""
from .types import (
    Account,
    Bar,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
)
from .oms import IllegalTransitionError, OrderEvent, OrderManagementSystem
from .broker import Broker, SimBroker, execute_children, fills_to_frame
from .algos import (
    ExecutionAlgo,
    Iceberg,
    ImplementationShortfall,
    TWAP,
    VWAP,
)
from .tca import (
    TCAReport,
    analyze,
    average_fill_price,
    benchmark_vwap,
    delay_cost_bps,
    fill_ratio,
    market_impact_bps,
    realized_spread_bps,
    slippage_bps,
)
from .sim import make_ohlcv, to_bars

__version__ = "0.1.0"

__all__ = [
    # types
    "Side", "OrderType", "OrderStatus", "Order", "Fill", "Position", "Account", "Bar",
    # oms
    "OrderManagementSystem", "OrderEvent", "IllegalTransitionError",
    # broker
    "Broker", "SimBroker", "execute_children", "fills_to_frame",
    # algos
    "ExecutionAlgo", "TWAP", "VWAP", "Iceberg", "ImplementationShortfall",
    # tca
    "TCAReport", "analyze", "average_fill_price", "slippage_bps", "fill_ratio",
    "realized_spread_bps", "market_impact_bps", "delay_cost_bps", "benchmark_vwap",
    # sim
    "make_ohlcv", "to_bars",
    "__version__",
]
