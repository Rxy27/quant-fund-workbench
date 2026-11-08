"""EWMA lower partial second moments and weighted Ledoit-Wolf shrinkage.

Downside observations are NOT demeaned: a constant loss is still downside risk.
The shrinkage intensity estimates the sampling variance of the weighted moment
using sum(w_t**2 * ||x_t x_t' - S||_F**2), clipped to the spherical-target distance.
At equal weights this is the Ledoit-Wolf (known zero mean) estimator. Exponential
weights use the corresponding effective sample size rather than treating the
scaled observations as equally precise. All matrices are daily unless annualized.
"""
import numpy as np


def ewma_shrunk_covariance(returns, *, downside=True, halflife=20., annualization=252):
    values = np.asarray(returns, dtype=float)
    if values.ndim != 2 or len(values) < 2 or values.shape[1] == 0:
        raise ValueError('风险矩阵至少需要两日、一个资产')
    if not np.isfinite(values).all() or halflife <= 0 or annualization <= 0:
        raise ValueError('风险数据和参数必须为有效有限数')
    weights = np.exp2(-np.arange(len(values)-1, -1, -1) / halflife)
    weights /= weights.sum()
    if downside:
        values = np.minimum(values, -0.0001)
    else:
        values = values - weights @ values
    moment = (values.T * weights) @ values
    n = values.shape[1]
    target = np.eye(n) * np.trace(moment) / n
    distance = float(np.square(moment-target).sum())
    outer = values[:, :, None] * values[:, None, :]
    noise = float(np.sum(weights**2 * np.square(outer-moment).sum(axis=(1, 2))))
    shrinkage = min(1., max(0., noise / distance)) if distance > 0 else 1.
    matrix = (1-shrinkage)*moment + shrinkage*target
    if not downside:
        matrix /= 1-float(weights @ weights)
    # Explicit eigenvalue floor, including identical assets/all-up/flat samples.
    floor = max(float(np.trace(matrix)/n)*1e-8, 1e-12)
    matrix += np.eye(n)*max(0., floor-float(np.linalg.eigvalsh(matrix).min()))
    return (matrix+matrix.T)*(.5*annualization)


def risk_budget_weights(covariance, budgets):
    """Positive risk-budget generalization of the original cyclic ERC solver."""
    covariance = np.asarray(covariance, dtype=float)
    budgets = np.asarray(budgets, dtype=float)
    n = len(budgets)
    if covariance.shape != (n, n) or not np.isfinite(covariance).all():
        raise ValueError('风险矩阵与预算维度不符')
    if not np.isfinite(budgets).all() or np.any(budgets <= 0) or not np.isclose(budgets.sum(), 1):
        raise ValueError('风险预算须为正且合计为1')
    if not np.allclose(covariance, covariance.T) or np.linalg.eigvalsh(covariance).min() <= 0:
        raise ValueError('风险矩阵必须对称正定')
    # Scaling improves conditioning without changing the normalized solution.
    matrix = covariance / np.trace(covariance)
    x = np.sqrt(budgets / np.diag(matrix))
    for _ in range(10000):
        previous = x.copy()
        for i in range(n):
            a = matrix[i, i]
            b = matrix[i] @ x-a*x[i]
            root = np.sqrt(b*b+4*a*budgets[i])
            x[i] = 2*budgets[i]/(root+b) if b >= 0 else (root-b)/(2*a)
        if np.max(np.abs(x-previous)/np.maximum(x, 1e-15)) < 1e-10:
            weights = x/x.sum()
            contribution = weights*(matrix@weights)
            if np.max(np.abs(contribution/contribution.sum()-budgets)) > 1e-7:
                raise ArithmeticError('ERC风险预算误差超过容限')
            return weights
    raise ArithmeticError('ERC未收敛；未静默替换为等权')
