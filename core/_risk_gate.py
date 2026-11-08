"""Independent, persistent pre-execution risk gate. No strategy or broker imports."""
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from datetime import date
from .state import write_json as save_json


@dataclass(frozen=True)
class EntryLot:
    quantity: int
    unit_cost: float


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: int
    price: float
    average_cost: float
    entry_lots: tuple[EntryLot, ...] = ()  # Empty means explicitly one entry at average_cost.


@dataclass(frozen=True)
class Account:
    date: str
    equity: float
    positions: tuple[Position, ...] = ()


@dataclass(frozen=True)
class Order:
    symbol: str
    quantity: int  # Positive buys, negative sells; no short positions.
    reference_price: float
    reason: str = 'strategy intent'


@dataclass(frozen=True)
class Limits:
    max_account_drawdown: float = .15
    max_daily_loss: float = .03
    position_stop_loss: float = .05
    max_symbol_fraction: float = 1.0

    @classmethod
    def for_universe(cls, symbols, **overrides):
        count = len(set(symbols))
        if not count:
            raise ValueError('Nonempty trading universe required')
        overrides.setdefault('max_symbol_fraction',1.0 if count == 1 else .5)
        return cls(**overrides)

    def __post_init__(self):
        if not all(math.isfinite(v) and 0 < v <= 1 for v in asdict(self).values()):
            raise ValueError('Risk limits must be in (0, 1]')


@dataclass
class Decision:
    intended: list[Order]
    actual: list[Order]
    rejected: list[dict]
    reasons: list[str]
    forced_symbols: list[str]

    def to_dict(self):
        return asdict(self)


class RiskGate:
    """Single-process local state; unknown/corrupt state fails closed.

    Call observe with verified previous-close equity before a new trading day.
    No cash-flow adjustment is inferred. Deposits/withdrawals require reconciliation.
    State saves before returning decisions. Rejected additions never suppress exits.
    """
    def __init__(self, state_path, limits=None):
        self.path = Path(state_path)
        self.limits = limits or Limits()
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding='utf-8'))
            required = {'kill_switch','peak_equity','last_equity','date','day_start_equity','daily_stopped'}
            if not required.issubset(self.state) or not isinstance(self.state['kill_switch'], bool):
                raise ValueError('Invalid persisted risk state; manual reconciliation required')
            if not isinstance(self.state['daily_stopped'], bool):
                raise ValueError('Invalid daily latch')
            for key in ('peak_equity','last_equity','day_start_equity'):
                value = self.state[key]
                if value is not None and (not isinstance(value,(int,float)) or not math.isfinite(value) or value <= 0):
                    raise ValueError('Invalid persisted account values')
            if self.state['date'] is not None:
                date.fromisoformat(self.state['date'])
                if any(self.state[k] is None for k in ('peak_equity','last_equity','day_start_equity')):
                    raise ValueError('Incomplete persisted account history')
        else:
            self.state = {'kill_switch':True, 'peak_equity':None, 'last_equity':None,
                          'date':None, 'day_start_equity':None, 'daily_stopped':False}
            self._save()

    def _save(self):
        save_json(self.path, self.state)

    def set_kill_switch(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('Explicit bool required')
        self.state['kill_switch'] = enabled
        self._save()

    def observe(self, account):
        date.fromisoformat(account.date)
        if not math.isfinite(account.equity) or account.equity <= 0:
            raise ValueError('Verified positive finite account equity required')
        if self.state['date'] and account.date < self.state['date']:
            raise ValueError('Out-of-order account snapshot')
        if self.state['date'] != account.date:
            # Previous observed equity must be previous close in a real integration.
            self.state['day_start_equity'] = self.state['last_equity'] or account.equity
            self.state['date'] = account.date
            self.state['daily_stopped'] = False
        peak = max(self.state['peak_equity'] or account.equity, account.equity)
        self.state['peak_equity'] = peak
        if account.equity / self.state['day_start_equity'] - 1 <= -self.limits.max_daily_loss + 1e-12:
            self.state['daily_stopped'] = True
        self.state['last_equity'] = account.equity
        self._save()

    def evaluate(self, intended, account):
        positions = {}
        for position in account.positions:
            if (not position.symbol or position.symbol in positions or not isinstance(position.quantity, int)
                or isinstance(position.quantity, bool) or position.quantity <= 0
                or not all(math.isfinite(v) and v > 0 for v in (position.price, position.average_cost))):
                raise ValueError('Invalid/duplicate position snapshot')
            positions[position.symbol] = position
            if position.entry_lots:
                if (any(not isinstance(lot.quantity,int) or isinstance(lot.quantity,bool) or lot.quantity <= 0
                        or not math.isfinite(lot.unit_cost) or lot.unit_cost <= 0 for lot in position.entry_lots)
                    or sum(lot.quantity for lot in position.entry_lots) != position.quantity):
                    raise ValueError('Lot-level position reconciliation failed')
        cash = account.equity - sum(p.quantity*p.price for p in positions.values())
        if cash < -1e-7:
            raise ValueError('Inconsistent account equity and market values')
        self.observe(account)
        intended = list(intended)
        symbols = [order.symbol for order in intended]
        if len(symbols) != len(set(symbols)):
            raise ValueError('Aggregate duplicate-symbol intents before RiskGate')
        for order in intended:
            if (not order.symbol or not isinstance(order.quantity, int) or isinstance(order.quantity, bool)
                or not math.isfinite(order.reference_price) or order.reference_price <= 0):
                raise ValueError('Invalid order intent')
            if order.symbol in positions and not math.isclose(order.reference_price, positions[order.symbol].price, rel_tol=1e-9):
                raise ValueError('Order reference price must match account snapshot')
        reasons = []
        if self.state['kill_switch']:
            reasons.append('persistent_kill_switch')
        if account.equity / self.state['peak_equity'] - 1 <= -self.limits.max_account_drawdown + 1e-12:
            reasons.append('account_drawdown_15pct')
        if self.state['daily_stopped']:
            reasons.append('daily_loss_3pct')
        actual, rejected, forced = [], [], []
        for symbol, p in positions.items():
            entry_costs = [lot.unit_cost for lot in p.entry_lots] if p.entry_lots else [p.average_cost]
            if any(p.price / cost - 1 <= -self.limits.position_stop_loss + 1e-12 for cost in entry_costs):
                actual.append(Order(symbol, -p.quantity, p.price, 'forced_position_stop_5pct'))
                forced.append(symbol)
        # Reductions are admitted before additions; no presumed sale proceeds fund buys.
        for order in sorted(intended, key=lambda item:item.quantity):
            if order.symbol in forced:
                rejected.append({'order':asdict(order), 'reasons':['replaced_by_forced_full_exit']})
                continue
            p = positions.get(order.symbol)
            held = p.quantity if p else 0
            if order.quantity < 0:
                qty = min(-order.quantity, held)
                if qty:
                    actual.append(Order(order.symbol, -qty, order.reference_price, order.reason))
                else:
                    rejected.append({'order':asdict(order), 'reasons':['no_long_position_to_reduce']})
                continue
            if order.quantity == 0:
                continue
            blocks = reasons.copy()
            current_value = held * order.reference_price
            new_value = current_value + order.quantity * order.reference_price
            if current_value > account.equity*self.limits.max_symbol_fraction + 1e-8 or new_value > account.equity*self.limits.max_symbol_fraction + 1e-8:
                blocks.append('symbol_market_value_cap')
            if order.quantity*order.reference_price > cash + 1e-8:
                blocks.append('insufficient_cash_before_fees')
            if blocks:
                rejected.append({'order':asdict(order), 'reasons':blocks})
            else:
                actual.append(order)
                cash -= order.quantity*order.reference_price
        return Decision(intended, actual, rejected, reasons, forced)
