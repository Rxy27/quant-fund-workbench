"""Close-only trend/rank/ERC targets. No execution, engine, or RiskGate imports."""
import numpy as np
import pandas as pd


def equal_risk_weights(covariance):
    """Cyclic coordinate minimization of .5*x'C*x - sum(log(x))/n."""
    covariance = np.asarray(covariance, dtype=float)
    n = len(covariance)
    if n == 0 or covariance.shape != (n, n) or not np.isfinite(covariance).all():
        raise ValueError('Finite square covariance required')
    if np.linalg.eigvalsh(covariance).min() <= 0:
        raise ValueError('Positive definite covariance required')
    x = 1 / np.sqrt(np.diag(covariance))
    for _ in range(10000):
        old = x.copy()
        for i in range(n):
            a = covariance[i, i]
            b = covariance[i] @ x - a*x[i]
            x[i] = (np.sqrt(b*b+4*a/n)-b)/(2*a)
        if np.max(np.abs(x-old)/np.maximum(x, 1e-15)) < 1e-10:
            weights = x/x.sum()
            contributions = weights*(covariance@weights)
            if np.max(np.abs(contributions/contributions.sum()-1/n)) > 1e-6:
                raise ArithmeticError('ERC convergence failed')
            return weights
    raise ArithmeticError('ERC failed to converge; no silent equal-weight fallback')


def target_weights(history, parameters):
    """history contains only closes available on the signal date, each column one ETF."""
    p = parameters
    need = max(p['trend_days'], p['momentum_days']+1, p['covariance_days']+1)
    result = pd.Series(0.0, index=history.columns)
    if len(history) < need:
        return result, {'reason':'insufficient_history'}
    momentum = history.iloc[-1]/history.iloc[-p['momentum_days']-1]-1
    trend = history.iloc[-1] > history.iloc[-p['trend_days']:].mean()
    eligible = [s for s in history if trend[s] and momentum[s] > 0]
    selected = sorted(eligible, key=lambda s: (-momentum[s], s))[:p['top_k']]
    if not selected:
        return result, {'reason':'all_filtered', 'selected':[]}
    returns = history[selected].iloc[-p['covariance_days']-1:].pct_change(fill_method=None).iloc[1:]
    covariance = returns.cov().to_numpy()
    shrink = p['covariance_diagonal_shrinkage']
    covariance = (1-shrink)*covariance + shrink*np.diag(np.diag(covariance))
    covariance += np.eye(len(selected))*1e-12  # Numerical regularization fixed in source.
    weights = equal_risk_weights(covariance)
    volatility = np.sqrt(weights @ covariance @ weights * 252)
    scale = min(p['max_gross'], p['target_volatility']/volatility)
    result.loc[selected] = weights*scale
    contributions = weights*(covariance@weights)
    return result, {'reason':'monthly_trend_rank_erc', 'selected':selected,
        'predicted_annual_volatility':float(volatility*scale), 'gross':float(scale),
        'risk_contribution_fraction':dict(zip(selected, (contributions/contributions.sum()).tolist()))}


def signal_schedule(closes, start, parameters):
    """Emit only at close, with an explicit initial last-training-close signal."""
    targets, diagnostics = {}, []
    for i in range(start-1, len(closes)-1):
        if i != start-1 and closes.index[i].month == closes.index[i-1].month:
            continue
        # Bounded trailing history avoids exposing future data to the signal module.
        lookback = max(parameters['trend_days'], parameters['momentum_days']+1,
                       parameters['covariance_days']+1)
        weights, details = target_weights(closes.iloc[max(0,i-lookback+1):i+1].copy(), parameters)
        day = closes.index[i]
        targets[day] = weights.to_dict()
        diagnostics.append({'signal_date':str(day.date()), 'weights':weights.to_dict(), **details})
    return targets, diagnostics
