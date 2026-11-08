"""Explicit, shared definitions for strategy, benchmarks, noise, costs and Gate."""
import numpy as np
import pandas as pd


def curve_metrics(curve):
    returns = curve.daily_return.to_numpy(dtype=float)
    nav = np.r_[1., np.cumprod(1+returns)]
    days = len(returns)
    years = days/252
    std = returns.std(ddof=1) if days > 1 else 0.
    cagr = float(nav[-1]**(1/years)-1)
    drawdown = 1-nav/np.maximum.accumulate(nav)
    maximum_drawdown = float(drawdown.max())
    rolling = pd.Series(nav[1:], index=curve.index).div(pd.Series(nav[1:], index=curve.index).shift(252))-1
    # First 252-day window starts at the initial capital, which is absent from curve rows.
    if days >= 252:
        rolling.iloc[251] = nav[252]-1
    annual = []
    grouped = curve.daily_return.groupby(curve.index.year)
    for year, subset in grouped:
        # Complete calendar years strictly interior to evaluation endpoints.
        complete = year > curve.index[0].year and year < curve.index[-1].year
        annual.append({'year':int(year), 'return':float((1+subset).prod()-1),
                       'trading_days':len(subset), 'full_calendar_year':complete})
    complete_returns = [row['return'] for row in annual if row['full_calendar_year']]
    # Track both completed peak-to-recovery durations and right-censored underwater spell.
    high, peak, longest_recovered = 1., 0, 0
    recovered = []
    for i, value in enumerate(nav[1:], 1):
        if value >= high-1e-12:
            if i-peak > 1:
                duration = i-peak
                longest_recovered = max(longest_recovered, duration)
                recovered.append(duration)
            high, peak = max(high,value), i
    current_underwater = days-peak if nav[-1] < high-1e-12 else 0
    longest_underwater = max(longest_recovered, current_underwater)
    return {'trading_days':days, 'total_return':float(nav[-1]-1), 'annual_return':cagr,
        'sharpe':float(returns.mean()/std*np.sqrt(252)) if std > 1e-14 else None,
        'realized_annual_volatility':float(std*np.sqrt(252)), 'max_drawdown':maximum_drawdown,
        'calmar':cagr/maximum_drawdown if maximum_drawdown > 1e-14 else None,
        'rolling_12m_win_rate':float((rolling.dropna() > 0).mean()) if rolling.notna().any() else None,
        'rolling_12m_observations':int(rolling.notna().sum()),
        'worst_full_year_return':min(complete_returns) if complete_returns else None,
        'worst_observed_year_return':min(row['return'] for row in annual), 'annual_returns':annual,
        'longest_recovered_drawdown_trading_days':longest_recovered,
        'current_unrecovered_drawdown_trading_days':current_underwater,
        'longest_underwater_trading_days':longest_underwater,
        'recovery_is_right_censored':current_underwater > 0}


def calculate(result):
    m = curve_metrics(result.curve)
    cycles = np.array([cycle['net_return'] for cycle in result.cycles])
    wins, losses = cycles[cycles > 0], cycles[cycles < 0]
    m.update({'closed_cycles':len(cycles), 'unclosed_cycles':len(result.open_cycles),
        'win_rate':float((cycles > 0).mean()) if len(cycles) else None,
        'payoff_ratio':float(wins.mean()/-losses.mean()) if len(wins) and len(losses) else None,
        'average_holding_trading_days':float(np.mean([c['holding_trading_days'] for c in result.cycles])) if len(cycles) else None,
        'annual_turnover_two_way':float(result.curve.turnover_two_way.mean()*252),
        'annual_turnover_one_way':float(result.curve.turnover_two_way.mean()*126),
        'filled_orders':len(result.orders), 'total_commission':float(result.curve.commission.sum()),
        'total_slippage_cost':float(result.curve.slippage_cost.sum())})
    return m


def relative(strategy, benchmark):
    if not strategy.index.equals(benchmark.index):
        raise ValueError('Benchmark must use the exact strategy calendar')
    active = strategy.daily_return-benchmark.daily_return
    std = active.std(ddof=1)
    return {'excess_total_return':float(strategy.nav.iloc[-1]-benchmark.nav.iloc[-1]),
            'excess_annual_return':curve_metrics(strategy)['annual_return']-curve_metrics(benchmark)['annual_return'],
            'information_ratio':float(active.mean()/std*np.sqrt(252)) if std > 1e-14 else None,
            'tracking_error':float(std*np.sqrt(252))}
