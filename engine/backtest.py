"""Walk-forward backtest: for each month M, fit on sales before M, predict every used sale in M."""
import sys, itertools
import numpy as np
import pandas as pd
from model import estimates, tier

def run(s, start="2025-10-01", end="2026-09-30", use_index=True, lang_map=None):
    rows = []
    for m in pd.period_range(start, end, freq="M"):
        cut = m.start_time
        nodes, ratios, index, _ = estimates(s, cut, lang_map, use_index)
        tgt = s[s.status.eq("used") & (s.date >= cut) & (s.date < (m + 1).start_time)]
        for r in tgt.itertuples():
            k = (r.card_id, int(r.grade))
            if k in nodes.index:
                n = nodes.loc[k]
                rows.append(dict(month=str(m), card_id=r.card_id, grade=int(r.grade), actual=np.log(r.px),
                                 own=n.own, anchor=n.anchor, last_adj=n.last_adj, last=n.last_lp,
                                 n_eff=n.n_eff, tier=tier(n.n_eff) if n.sales_all > 0 else 'new', days_since_last=(cut - n.last_date).days,
                                 mom3=n.mom3, bo=r.bo_unverified, ah=r.auction_house))
            else:
                # never sold before: anchor-only prediction possible if linked nodes exist (not in nodes table)
                rows.append(dict(month=str(m), card_id=r.card_id, grade=int(r.grade), actual=np.log(r.px),
                                 own=np.nan, anchor=np.nan, last_adj=np.nan, last=np.nan, n_eff=0, tier="new",
                                 days_since_last=np.nan, mom3=np.nan, bo=r.bo_unverified, ah=r.auction_house))
        print(m, len(tgt), file=sys.stderr)
    return pd.DataFrame(rows)

def mape(pred, act):
    e = np.abs(np.exp(pred - act) - 1)
    return float(np.nanmedian(e)), int(np.isfinite(e).sum())
