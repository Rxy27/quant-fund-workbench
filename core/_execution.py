"""Deterministic multi-ETF order sizing, independent of strategy and backtest."""
import math
from ._risk_gate import Order


def plan_targets(weights, prices, shares, equity, lot_size=100):
    if set(weights) != set(prices) or set(shares) != set(prices):
        raise ValueError('Target/price/holding universe mismatch')
    values = list(weights.values())
    if (not all(math.isfinite(v) and v >= 0 for v in values)
            or sum(values) > 1+1e-10 or equity <= 0):
        raise ValueError('Long-only unlevered finite targets required')
    orders = []
    for symbol in sorted(prices):
        if not math.isfinite(prices[symbol]) or prices[symbol] <= 0:
            raise ValueError('Known positive open required')
        target = int(equity*weights[symbol]/prices[symbol]//lot_size)*lot_size
        quantity = target-shares[symbol]
        if quantity:
            orders.append(Order(symbol, quantity, prices[symbol], 'previous-close portfolio target'))
    return sorted(orders, key=lambda order:(order.quantity > 0, order.symbol))
