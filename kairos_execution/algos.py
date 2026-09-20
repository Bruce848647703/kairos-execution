"""执行算法：把一张父单拆分成一批带时序的子单（切片）。

所有算法共享统一接口 :meth:`ExecutionAlgo.schedule`，输入父单与市场信息
（bar 列表 / 成交量序列 / 切片数），输出子单列表。子单继承父单的标的、方向、
订单类型与限/止损价，并携带 ``parent_id`` 与 ``created_bar``（该切片应在哪根
bar 提交），可直接喂给 :func:`~kairos_execution.broker.execute_children`。

数量拆分统一走 :func:`_allocate`，保证「子单量之和 == 父单量」严格成立：
整数量用最大余额法（largest remainder），浮点量则用末位补齐消除累计误差。

包含四种经典算法（均为本仓库自研实现）：

- :class:`TWAP`：时间加权，在时间上均匀切片。
- :class:`VWAP`：成交量加权，按成交量分布切片。
- :class:`Iceberg`：冰山，只显露小量、分批补单。
- :class:`ImplementationShortfall`：前重后轻的衰减切片，权衡冲击成本与风险。
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import List, Optional, Sequence, Union

from .types import Bar, Order

# schedule 的第二参数：可以是切片数(int)、成交量序列、或 Bar 序列
MarketLike = Union[int, Sequence[float], Sequence[Bar]]


def _prepare(market: MarketLike):
    """把多形态的市场输入归一化为 ``(n, volumes, bar_indices)``。

    - ``int``           : 视为切片数，成交量全 1（等权），bar 序号 0..n-1。
    - ``Sequence[Bar]`` : 取每根 bar 的成交量与 index。
    - ``Sequence[float]``: 视为成交量序列，bar 序号 0..n-1。
    """
    if isinstance(market, int):
        n = int(market)
        if n <= 0:
            raise ValueError("切片数必须为正")
        return n, [1.0] * n, list(range(n))

    items = list(market)
    n = len(items)
    if n == 0:
        raise ValueError("市场序列不能为空")
    volumes: List[float] = []
    indices: List[int] = []
    for i, it in enumerate(items):
        if isinstance(it, Bar):
            volumes.append(float(it.volume))
            indices.append(int(it.index) if it.index >= 0 else i)
        else:
            volumes.append(float(it))
            indices.append(i)
    return n, volumes, indices


def _allocate(total: float, weights: Sequence[float], lot_size: int = 1) -> List[float]:
    """按 ``weights`` 比例把 ``total`` 拆成 len(weights) 份，和严格等于 total。

    lot_size>0 且 total 为整数倍时，用最大余额法做整 lot 分配（每份为 lot 的整数倍）；
    否则按浮点比例分配并用末位补齐消除累计误差。负权重会被清零。
    """
    n = len(weights)
    if n == 0:
        return []
    w = [max(0.0, float(x)) for x in weights]
    wsum = sum(w)
    if wsum <= 0:                       # 退化：等权
        w = [1.0] * n
        wsum = float(n)

    if lot_size and lot_size > 0 and _is_multiple(total, lot_size):
        lots_total = int(round(total / lot_size))
        raw = [lots_total * wi / wsum for wi in w]
        base = [int(math.floor(x)) for x in raw]
        rem = lots_total - sum(base)
        # 按小数部分从大到小补齐剩余 lot
        order = sorted(range(n), key=lambda i: (raw[i] - base[i]), reverse=True)
        k = 0
        while rem > 0 and n > 0:
            base[order[k % n]] += 1
            rem -= 1
            k += 1
        return [float(b * lot_size) for b in base]

    raw = [total * wi / wsum for wi in w]
    raw[-1] = total - sum(raw[:-1])      # 末位补齐，确保严格相等
    return raw


def _is_multiple(total: float, lot: int) -> bool:
    """total 是否为 lot 的整数倍（含浮点容差）。"""
    if lot <= 0:
        return False
    q = total / lot
    return abs(q - round(q)) < 1e-9


class ExecutionAlgo(ABC):
    """执行算法基类。子类实现 :meth:`schedule`。

    参数
    ----
    lot_size  : 最小交易单位（股），>0 时切片按整 lot 分配；None/0 表示允许碎股。
    id_prefix : 子单 id 前缀。
    """

    def __init__(self, lot_size: Optional[int] = 1, id_prefix: str = "child") -> None:
        self.lot_size = lot_size if (lot_size and lot_size > 0) else 0
        self.id_prefix = id_prefix

    @abstractmethod
    def schedule(self, parent_order: Order, market: MarketLike) -> List[Order]:
        """把父单拆成子单列表。"""

    # -- 供子类复用的工具 --
    def _child_id(self, parent_id: str, i: int) -> str:
        return f"{self.id_prefix}-{parent_id}-{i:03d}"

    def _make_child(
        self, parent: Order, qty: float, bar_index: int, i: int
    ) -> Order:
        """基于父单生成一张子单（继承标的/方向/类型/限止损价）。"""
        return Order(
            order_id=self._child_id(parent.order_id, i),
            symbol=parent.symbol,
            side=parent.side,
            qty=float(qty),
            order_type=parent.order_type,
            limit_price=parent.limit_price,
            stop_price=parent.stop_price,
            parent_id=parent.order_id,
            created_bar=int(bar_index),
        )


class TWAP(ExecutionAlgo):
    """时间加权平均价格：在时间上**均匀**切片。

    每个时间片下单相同数量，适合流动性均匀、希望弱化择时影响的场景。
    """

    def __init__(self, lot_size: Optional[int] = 1) -> None:
        super().__init__(lot_size=lot_size, id_prefix="twap")

    def schedule(self, parent_order: Order, market: MarketLike) -> List[Order]:
        n, _vols, indices = _prepare(market)
        qtys = _allocate(parent_order.qty, [1.0] * n, self.lot_size)
        children: List[Order] = []
        for i in range(n):
            if qtys[i] <= 0:
                continue
            children.append(self._make_child(parent_order, qtys[i], indices[i], i))
        return children


class VWAP(ExecutionAlgo):
    """成交量加权平均价格：按**成交量分布**切片。

    成交量大的时段多下单、成交量小的时段少下单，使执行节奏贴近市场自然成交，
    从而减小相对 VWAP 基准的偏离与市场冲击。
    """

    def __init__(self, lot_size: Optional[int] = 1) -> None:
        super().__init__(lot_size=lot_size, id_prefix="vwap")

    def schedule(self, parent_order: Order, market: MarketLike) -> List[Order]:
        n, vols, indices = _prepare(market)
        if sum(vols) <= 0:
            raise ValueError("VWAP 需要正的成交量分布")
        qtys = _allocate(parent_order.qty, vols, self.lot_size)
        children: List[Order] = []
        for i in range(n):
            if qtys[i] <= 0:
                continue
            children.append(self._make_child(parent_order, qtys[i], indices[i], i))
        return children


class Iceberg(ExecutionAlgo):
    """冰山算法：只**显露小量**、分批补单。

    把父单切成若干个不超过 ``display_size`` 的小单，逐批投放，隐藏真实意图、
    降低对市场的信号泄露。这里给出的是**静态批次计划**（一次性排好各批显露量）；
    真实冰山是「成交一批补一批」的反应式过程，可用 :meth:`peek` 动态取下一显露量。
    """

    def __init__(self, display_size: float, lot_size: Optional[int] = 1) -> None:
        super().__init__(lot_size=lot_size, id_prefix="ice")
        if display_size <= 0:
            raise ValueError("display_size 必须为正")
        self.display_size = float(display_size)

    def peek(self, remaining: float) -> float:
        """反应式接口：给定剩余量，返回本次应显露的数量（≤ display_size）。"""
        return max(0.0, min(self.display_size, float(remaining)))

    def schedule(
        self, parent_order: Order, market: Optional[MarketLike] = None
    ) -> List[Order]:
        total = parent_order.qty
        n_batches = int(math.ceil(total / self.display_size - 1e-12))
        # 若给了 bar 序列，则用其 index 作为提交 bar；否则用顺序序号
        indices: Optional[List[int]] = None
        if market is not None:
            _n, _v, indices = _prepare(market)
        children: List[Order] = []
        remaining = total
        i = 0
        while remaining > 1e-12 and i < max(n_batches, 1) + 1:
            qty = self.peek(remaining)
            if qty <= 0:
                break
            if self.lot_size and self.lot_size > 0:
                qty = float(int(round(qty / self.lot_size)) * self.lot_size) or self.lot_size
            qty = min(qty, remaining)
            bar_index = indices[i % len(indices)] if indices else i
            children.append(self._make_child(parent_order, qty, bar_index, i))
            remaining -= qty
            i += 1
        return children


class ImplementationShortfall(ExecutionAlgo):
    """执行差额（IS）算法：**前重后轻**的衰减切片。

    以几何衰减 ``decay^i`` 分配各时间片的权重：越早的切片越大，尽快完成大部分
    成交以降低「决策价 → 最终成交价」的价格漂移风险；保留一小部分尾单以缓和
    冲击成本。``decay`` 越小越激进（越前置），越大越接近 TWAP。
    """

    def __init__(self, decay: float = 0.7, lot_size: Optional[int] = 1) -> None:
        super().__init__(lot_size=lot_size, id_prefix="is")
        if not (0.0 < decay <= 1.0):
            raise ValueError("decay 应落在 (0, 1] 区间")
        self.decay = float(decay)

    def schedule(self, parent_order: Order, market: MarketLike) -> List[Order]:
        n, _vols, indices = _prepare(market)
        weights = [self.decay ** i for i in range(n)]
        qtys = _allocate(parent_order.qty, weights, self.lot_size)
        children: List[Order] = []
        for i in range(n):
            if qtys[i] <= 0:
                continue
            children.append(self._make_child(parent_order, qtys[i], indices[i], i))
        return children
