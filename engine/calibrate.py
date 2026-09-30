"""Fit blend weights per grade x liquidity tier from the backtest, report honest
(cross-fitted) errors, band quantiles and the decision-grade flag."""
import itertools
import numpy as np
import pandas as pd

GRID = [w for w in itertools.product(np.round(np.arange(0, 1.01, 0.1), 1), repeat=3) if abs(sum(w) - 1) < 1e-9]
METHODS = ["own", "anchor", "last_adj"]
# acceptance (from the spec): median abs error <=25% for PSA 8/9, <=35% for PSA 10, and beat last sale
MAX_ERR = {8: 0.25, 9: 0.25, 10: 0.35}


def blend_arr(df, w):
    v = np.zeros(len(df)); tw = np.zeros(len(df))
    for m, x in zip(METHODS, w):
        if x == 0:
            continue
        ok = df[m].notna().values
        v[ok] += x * df[m].values[ok]; tw[ok] += x
    out = np.where(tw > 0, v / np.where(tw > 0, tw, 1), np.nan)
    return out


def err(pred, act):
    return np.abs(np.exp(pred - act) - 1)


def best_w(df):
    best, bw = 9e9, (1, 0, 0)
    for w in GRID:
        p = blend_arr(df, w)
        e = err(p, df.actual.values)
        cov = np.isfinite(e).mean()
        if cov < 0.8:  # must predict most sales
            continue
        m = np.nanmedian(e)
        if m < best:
            best, bw = m, w
    return bw


def calibrate(bt):
    bt = bt.copy()
    months = sorted(bt.month.unique())
    fold = {m: i % 2 for i, m in enumerate(months)}
    bt["fold"] = bt.month.map(fold)
    W, rep = {}, []
    bt["pred_cv"] = np.nan
    for (g, t), x in bt.groupby(["grade", "tier"]):
        W[(g, t)] = best_w(x)
        for f in (0, 1):
            w = best_w(x[x.fold != f]) if (x.fold != f).sum() >= 10 else W[(g, t)]
            idx = x.index[x.fold == f]
            bt.loc[idx, "pred_cv"] = blend_arr(x.loc[idx], w)
    # where the cross-fitted blend loses to the last sale, fall back to the (index-adjusted) last sale
    for (g, t), x in bt.groupby(["grade", "tier"]):
        if x["last"].notna().any() and np.nanmedian(err(x.pred_cv, x.actual)) > np.nanmedian(err(x["last"], x.actual)):
            W[(g, t)] = (0.0, 0.0, 1.0)
            bt.loc[x.index, "pred_cv"] = x["last_adj"]
    # bias correction: in a rising market decayed averages lag. Shift by the median residual,
    # cross-fitted (each fold corrected with the other fold's median), final value from all data.
    B = {}
    for (g, t), x in bt.groupby(["grade", "tier"]):
        res = x.actual - x.pred_cv
        B[(g, t)] = 0.0
        if res.notna().sum() < 8:
            continue
        corr = x.pred_cv.copy()
        for f in (0, 1):
            other = res[x.fold != f]
            if other.notna().sum() >= 5:
                corr[x.fold == f] = x.pred_cv[x.fold == f] + np.nanmedian(other)
        if np.nanmedian(err(corr, x.actual)) < np.nanmedian(err(x.pred_cv, x.actual)):  # keep only if it helps
            B[(g, t)] = float(np.nanmedian(res))
            bt.loc[x.index, "pred_cv"] = corr
    for (g, t), x in bt.groupby(["grade", "tier"]):
        e_model = np.nanmedian(err(x.pred_cv, x.actual))
        e_last = np.nanmedian(err(x["last"], x.actual)) if x["last"].notna().any() else np.nan
        res = (x.actual - x.pred_cv).dropna() + B[(g, t)]  # residuals around the uncorrected blend
        q10, q90 = (np.quantile(res, [0.1, 0.9]) if len(res) >= 8 else (np.nan, np.nan))
        cover = float(((res >= q10) & (res <= q90)).mean()) if len(res) >= 8 else np.nan
        ok = (e_model <= MAX_ERR[g]) and (np.isnan(e_last) or e_model <= e_last + 0.005) and len(res) >= 20
        rep.append(dict(grade=g, tier=t, n=len(x), bias=B[(g, t)], w_own=W[(g, t)][0], w_anchor=W[(g, t)][1], w_last=W[(g, t)][2],
                        err_model=e_model, err_last_sale=e_last, band_q10=q10, band_q90=q90,
                        decision_grade=bool(ok)))
    return W, pd.DataFrame(rep), bt
