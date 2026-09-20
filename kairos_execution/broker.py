"""券商抽象与自研模拟券商 SimBroker。

:class:`Broker` 定义统一接口；:class:`SimBroker` 是一个**纯内存、离线**的撮合
模拟器，用于回测执行算法与做 TCA，不接任何真实券商通道。它支持：

- **成交延迟**：订单提交后第 ``latency`` 根 bar 才可成交（0 表示当根即可）。
- **参与率约束**：单根 bar 的成交量上限为 ``participation_rate × bar.volume``，
  超出部分留到后续 bar，形成部分成交（partial fill）累积。
- **滑点**：市价/止损成交价按 ``slippage_bps`` 向不利方向偏移。
- **限价撮合**：买单在 ``low ≤ limit`` 成交、卖单在 ``high ≥ limit`` 成交。
- **止损撮合**：价格穿越触发价后转市价（跳空则以开盘价成交）。

撮合只做「订单 ↔ 行情」的匹配与账户更新，订单生命周期委托给内部的
:class:`~kairos_execution.oms.OrderManagementSystem`。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import asdict
from typing import Dict, List, Mapping, Optional

import pandas as pd

from .oms import OrderManagementSystem
from .types import Account, Bar, Fill, Order, OrderStatus, OrderType, Side


class Broker(ABC):
    """券商接口。任何撮合/执行后端都应实现这三个方法。"""

    @abstractmethod
    def submit(self, order: Order, bar: Optional[int] = None) -> str:
        """提交订单，返回订单 id。"""

    @abstractmethod
    def cancel(self, order_id: str, reason: str = "") -> None:
        """撤销订单。"""

    @abstractmethod
    def on_bar(self, bar_index: int, market: Mapping[str, Bar]) -> None:
        """推进一根 bar：用行情 ``market`` 撮合当前活动订单。"""


class SimBroker(Broker):
    """内存模拟券商（详见模块 docstring）。

    参数
    ----
    oms              : 复用的 OMS；省略则内部新建。
    account          : 资金账户；提供时按成交更新现金与持仓（默认不新建）。
    latency          : 成交延迟 bar 数，0 表示当根 bar 即可成交。
    participation_rate : 单 bar 参与率上限（占该 bar 成交量的比例），
                       ``None`` 或 <=0 表示不限量。
    slippage_bps     : 市价/止损单滑点（基点）；买入抬价、卖出压价。
    commission_rate  : 成交额佣金比例。
    commission_min   : 单笔最低佣金。
    market_ref       : 市价单参考 bar 价，"close" 或 "open"。
    enforce_cash     : 为 True 时买入受现金约束（不透支）；默认 False。
    """

    def __init__(
        self,
        oms: Optional[OrderManagementSystem] = None,
        account: Optional[Account] = None,
        latency: int = 1,
        participation_rate: Optional[float] = 1.0,
        slippage_bps: float = 0.0,
        commission_rate: float = 0.0,
        commission_min: float = 0.0,
        market_ref: str = "close",
        enforce_cash: bool = False,
    ) -> None:
        if latency < 0:
            raise ValueError("latency 不能为负")
        if market_ref not in ("close", "open"):
            raise ValueError("market_ref 只能是 'close' 或 'open'")
        self.oms = oms or OrderManagementSystem()
        self.account = account
        self.latency = int(latency)
        self.participation_rate = participation_rate
        self.slippage_bps = float(slippage_bps)
        self.commission_rate = float(commission_rate)
        self.commission_min = float(commission_min)
        self.market_ref = market_ref
        self.enforce_cash = bool(enforce_cash)
        self._current_bar = 0

    # ------------------------------------------------------------------
    # Broker 接口
    # ------------------------------------------------------------------
    def submit(self, order: Order, bar: Optional[int] = None) -> str:
        """提交订单并立即受理（PENDING → ACCEPTED），随后可被撮合。"""
        if bar is not None:
            order.created_bar = int(bar)
        elif order.created_bar < 0:
            order.created_bar = self._current_bar
        self.oms.submit(order)
        self.oms.accept(order.order_id)
        return order.order_id

    def cancel(self, order_id: str, reason: str = "") -> None:
        """撤销订单（委托 OMS 校验状态机）。"""
        self.oms.cancel(order_id, reason)

    def on_bar(self, bar_index: int, market: Mapping[str, Bar]) -> None:
        """推进到第 ``bar_index`` 根 bar，撮合所有到期且可成交的活动订单。"""
        self._current_bar = int(bar_index)
        self.oms.set_current_bar(self._current_bar)
        # 快照活动订单，避免撮合过程中状态变更影响迭代
        for order in list(self.oms.active_orders()):
            if order.status not in (OrderStatus.ACCEPTED, OrderStatus.PARTIAL):
                continue
            if order.created_bar >= 0 and bar_index < order.created_bar + self.latency:
                continue                      # 尚未到达可成交的 bar
            bar = market.get(order.symbol)
            if bar is None:
                continue
            self._try_fill(order, bar, self._current_bar)

    # ------------------------------------------------------------------
    # 查询便捷方法
    # ------------------------------------------------------------------
    def fills(self, order_id: Optional[str] = None) -> List[Fill]:
        """成交回报列表（可指定订单）。"""
        return self.oms.fills(order_id)

    def fills_frame(self) -> pd.DataFrame:
        """把全部成交回报整理成 DataFrame，便于打印与分析。"""
        return fills_to_frame(self.oms.fills())

    # ------------------------------------------------------------------
    # 内部撮合
    # ------------------------------------------------------------------
    def _match_price(self, order: Order, bar: Bar):
        """按订单类型与 bar OHLC 判断能否成交。

        返回 ``(can_fill, reference_price, exec_price)``：
        reference_price 为滑点前参考价，exec_price 为最终成交价（限价单无滑点）。
        """
        sign = order.side.sign
        otype = order.order_type

        if otype is OrderType.LIMIT:
            if order.side is Side.BUY:
                if bar.low <= order.limit_price:
                    return True, order.limit_price, order.limit_price
                return False, 0.0, 0.0
            if bar.high >= order.limit_price:
                return True, order.limit_price, order.limit_price
            return False, 0.0, 0.0

        if otype is OrderType.STOP:
            # 止损单：价格穿越触发价后转市价；跳空穿越则以开盘价成交
            if order.side is Side.BUY:
                if bar.open >= order.stop_price:
                    ref = bar.open
                elif bar.high >= order.stop_price:
                    ref = order.stop_price
                else:
                    return False, 0.0, 0.0
            else:
                if bar.open <= order.stop_price:
                    ref = bar.open
                elif bar.low <= order.stop_price:
                    ref = order.stop_price
                else:
                    return False, 0.0, 0.0
            return True, ref, self._apply_slippage(ref, sign)

        # MARKET
        ref = bar.close if self.market_ref == "close" else bar.open
        return True, ref, self._apply_slippage(ref, sign)

    def _apply_slippage(self, price: float, sign: int) -> float:
        """对参考价施加滑点：买入(+1)抬价、卖出(-1)压价。"""
        return price * (1.0 + sign * self.slippage_bps / 10_000.0)

    def _commission(self, notional: float) -> float:
        """按成交额计算佣金（比例与最低收费取大）。"""
        value = abs(float(notional))
        if value <= 0:
            return 0.0
        return max(value * self.commission_rate, self.commission_min)

    def _fillable_qty(self, order: Order, bar: Bar) -> float:
        """受参与率约束后，本 bar 最多可成交的数量。"""
        remaining = order.remaining
        pr = self.participation_rate
        if pr is None or pr <= 0:
            return remaining
        cap = pr * bar.volume
        return max(0.0, min(remaining, cap))

    def _try_fill(self, order: Order, bar: Bar, bar_index: int) -> None:
        """尝试在给定 bar 上（部分）成交一张订单。"""
        can, ref, exec_price = self._match_price(order, bar)
        if not can or exec_price <= 0:
            return
        fill_qty = self._fillable_qty(order, bar)
        if fill_qty <= 0:
            return
        remaining = order.remaining

        # 现金约束（可选）：买入不得透支
        if self.enforce_cash and self.account is not None and order.side is Side.BUY:
            afford = self.account.cash / exec_price if exec_price > 0 else 0.0
            fill_qty = min(fill_qty, max(0.0, afford))
            if fill_qty <= 0:
                return

        if fill_qty >= remaining - 1e-9:
            fill = self.oms.fill(order.order_id, exec_price, qty=remaining, bar=bar_index)
        else:
            fill = self.oms.partial_fill(
                order.order_id, fill_qty, exec_price, bar=bar_index
            )
        # 补充成交回报的成本明细
        fill.reference_price = ref
        fill.slippage = (exec_price - ref) * order.side.sign
        fill.commission = self._commission(fill.qty * exec_price)
        self._update_account(order, fill)

    def _update_account(self, order: Order, fill: Fill) -> None:
        """把成交并入资金账户（现金 + 持仓）。"""
        if self.account is None:
            return
        sign = order.side.sign
        self.account.cash += -sign * fill.qty * fill.price - fill.commission
        pos = self.account.position(order.symbol)
        pos.apply_fill(order.side, fill.qty, fill.price)


def fills_to_frame(fills: List[Fill]) -> pd.DataFrame:
    """把成交回报列表转成便于打印的 DataFrame（空列表返回空表）。"""
    if not fills:
        return pd.DataFrame(
            columns=[
                "fill_id", "order_id", "symbol", "side", "qty",
                "price", "bar", "commission", "reference_price", "slippage",
            ]
        )
    rows = []
    for f in fills:
        d = asdict(f)
        d["side"] = f.side.name
        rows.append(d)
    return pd.DataFrame(rows)[
        [
            "fill_id", "order_id", "symbol", "side", "qty",
            "price", "bar", "commission", "reference_price", "slippage",
        ]
    ]


def execute_children(
    broker: Broker,
    children: List[Order],
    bars: List[Bar],
    symbol: str,
) -> List[Fill]:
    """按 bar 逐根提交子单并驱动撮合，返回全部成交回报。

    子单的 ``created_bar`` 决定它在哪一根 bar 被提交；配合 ``latency`` 决定
    实际成交的 bar。未被当根吃满的子单会留到后续 bar 继续撮合（参与率约束）。
    """
    buckets: Dict[int, List[Order]] = defaultdict(list)
    for child in children:
        b = int(child.created_bar) if child.created_bar is not None and child.created_bar >= 0 else 0
        buckets[b].append(child)
    for i, bar in enumerate(bars):
        for child in buckets.get(i, []):
            broker.submit(child, bar=i)
        broker.on_bar(i, {symbol: bar})
    return broker.fills()
