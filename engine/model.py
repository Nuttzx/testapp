"""Valuation engine.

Every function takes a `cutoff` and uses only sales strictly before it, so the
same code serves live valuation and the walk-forward backtest.

Layers
  1. Market index   : monthly log-price index per grade (all cards), plus a shrunk
                      deviation per segment (set x edition x lang x grade).
  2. Own value      : the card's own sales, index-adjusted to the cutoff,
                      time-decayed (90-day half-life), weighted median.
  3. Anchors        : for each linked pair of nodes (PSA 9<->10, 8<->9, 8<->10,
                      1st<->Unlimited, JP<->EN) the historic log price ratio,
                      each pair measured in its own time window (a sale of A is
                      paired with B's price interpolated at that date).
                      Implied value of A = current value of B x natural ratio.
  4. Trend          : segment momentum (3 and 12 months) and a lead-lag test
                      of PSA 10 index moves on PSA 9 index moves.
  5. Blend          : weighted mix of own, anchor and last-sale estimates;
                      weights fitted in the backtest.
"""
import numpy as np
import pandas as pd

HALF_LIFE_OWN = 90
HALF_LIFE_RATIO = 120
PAIR_MAX_GAP = 180      # max days between B's two bracketing sales for interpolation
PAIR_NEAR = 45          # else nearest B sale within this many days
SEG_SHRINK = 10         # sales needed for a segment deviation to count half
RATIO_SHRINK = 3        # pair samples needed for a card's own ratio to count half


def month(d):
    return d.dt.to_period("M")


# ---------------------------------------------------------------- 1. index
def fit_index(s, cutoff):
    """Returns dict[(segment)] -> pd.Series(month -> log index), plus dict[grade] global."""
    d = s[(s.date < cutoff) & s.status.eq("used")].copy()
    d["m"] = month(d.date)
    d["lp"] = np.log(d.px)
    last_m = pd.Timestamp(cutoff - pd.Timedelta(days=1)).to_period("M")
    glob, seg = {}, {}
    for g, dg in d.groupby("grade"):
        months = pd.period_range(dg.m.min(), last_m, freq="M")
        node = dg.card_id
        a = dg.groupby(node).lp.median()
        b = pd.Series(0.0, index=months)
        for _ in range(15):
            b_new = (dg.lp - node.map(a)).groupby(dg.m).median()
            b = b_new.reindex(months)
            b = b - b.dropna().iloc[-1] if b.notna().any() else b
            a = (dg.lp - dg.m.map(b).fillna(0)).groupby(node).median()
        cnt = dg.groupby("m").size().reindex(months).fillna(0)
        b = b.interpolate(limit_direction="both")
        # 3-month smoothing weighted by counts
        w = cnt.clip(lower=0.5)
        b = (b * w).rolling(3, min_periods=1, center=False).sum() / w.rolling(3, min_periods=1).sum()
        b = b - b.iloc[-1]
        glob[g] = b
        # segment deviations (shrunk)
        dg = dg.assign(res=dg.lp - node.map(a) - dg.m.map(b))
        for sg, ds in dg.groupby(["set", "edition", "lang"]):
            r = ds.groupby("m").res.agg(["median", "size"]).reindex(months)
            n = r["size"].fillna(0)
            dev = (r["median"].fillna(0) * n / (n + SEG_SHRINK))
            dev = dev.rolling(3, min_periods=1).mean()
            dev = dev - dev.iloc[-1]
            seg[(sg, g)] = b + dev
    return {"glob": glob, "seg": seg}


def idx_value(index, sg, g, m):
    if index is None:
        return 0.0
    ser = index["seg"].get((sg, g))
    if ser is None:
        ser = index["glob"].get(g)
    if ser is None:
        return 0.0
    if m < ser.index[0]:
        return float(ser.iloc[0])
    if m > ser.index[-1]:
        return float(ser.iloc[-1])
    return float(ser.get(m, ser.iloc[-1]))


def adjust(index, row_sg, g, from_m, to_m):
    return idx_value(index, row_sg, g, to_m) - idx_value(index, row_sg, g, from_m)


# ---------------------------------------------------------------- 2. own value
def wmedian(x, w):
    o = np.argsort(x)
    x, w = np.asarray(x)[o], np.asarray(w)[o]
    c = np.cumsum(w)
    return float(x[np.searchsorted(c, 0.5 * c[-1])])


def node_table(s, cutoff, index):
    """One row per (card_id, grade) with own value, last sale, n_eff, sales_12m."""
    d = s[(s.date < cutoff) & s.status.eq("used")].copy()
    tm = pd.Timestamp(cutoff - pd.Timedelta(days=1)).to_period("M")
    rows = []
    for (cid, g), x in d.groupby(["card_id", "grade"]):
        sg = (x.set.iloc[0], x.edition.iloc[0], x.lang.iloc[0])
        age = (cutoff - x.date).dt.days.values
        adj = np.array([adjust(index, sg, g, m, tm) for m in month(x.date)])
        lp = np.log(x.px.values) + adj
        w = 0.5 ** (age / HALF_LIFE_OWN)
        last = x.sort_values("date").iloc[-1]
        rows.append(dict(card_id=cid, grade=int(g), set=sg[0], edition=sg[1], lang=sg[2],
                         number=x.number.iloc[0], card=x.card_name.iloc[0],
                         own=wmedian(lp, w), n_eff=float(w.sum()),
                         last_lp=float(np.log(last.px)),
                         last_adj=float(np.log(last.px) + adjust(index, sg, g, month(pd.Series([last.date])).iloc[0], tm)),
                         last_date=last.date, last_px=float(last.px), last_venue=last.venue,
                         sales_12m=int((age <= 365).sum()), sales_all=len(x)))
    return pd.DataFrame(rows).set_index(["card_id", "grade"]) if rows else pd.DataFrame()


# ---------------------------------------------------------------- 3. anchors
def edges(nodes, lang_map=None, targets=None):
    """Linked node pairs (A, B, kind). A's value is implied from B (B must have sales).
    targets: nodes to value (default: nodes with sales)."""
    out = []
    keys = set(nodes.index)
    for cid, g in (targets if targets is not None else keys):
        for g2 in (8, 9, 10):
            if g2 != g and (cid, g2) in keys:
                out.append(((cid, g), (cid, g2), f"grade{g2}"))
        s_, n_, c_, e_, l_ = cid.split("|")
        for e2 in ("1st", "unl", "shadowless"):
            if e2 != e_:
                c2 = "|".join([s_, n_, c_, e2, l_])
                if (c2, g) in keys:
                    out.append(((cid, g), (c2, g), f"ed_{e2}"))
        if lang_map is not None:
            for c2 in lang_map.get(cid, []):
                if (c2, g) in keys:
                    out.append(((cid, g), (c2, g), "lang"))
    return out


def _interp(bd, blp, t):
    """log price of B at date t from its dated sales (bd sorted)."""
    i = np.searchsorted(bd, t)
    if 0 < i < len(bd):
        t1, t2 = bd[i - 1], bd[i]
        gap = (t2 - t1) / np.timedelta64(1, "D")
        if gap <= PAIR_MAX_GAP:
            f = 0 if gap == 0 else ((t - t1) / np.timedelta64(1, "D")) / gap
            return blp[i - 1] + f * (blp[i] - blp[i - 1])
    j = np.argmin(np.abs((bd - t) / np.timedelta64(1, "D")))
    if abs((bd[j] - t) / np.timedelta64(1, "D")) <= PAIR_NEAR:
        return blp[j]
    return None


def pair_ratios(s, cutoff, nodes, lang_map=None):
    """Natural log ratio log(A)-log(B) per edge, shrunk toward the pooled ratio of the same
    edge type within (set, edition, lang), then toward all sets."""
    d = s[(s.date < cutoff) & s.status.eq("used")]
    by = {k: v.sort_values("date") for k, v in d.groupby(["card_id", "grade"])}
    samples = []
    for a, b, kind in edges(nodes, lang_map):
        A, B = by[a], by[b]
        bd, blp = B.date.values, np.log(B.px.values)
        for t, p in zip(A.date.values, A.px.values):
            lb = _interp(bd, blp, t)
            if lb is not None:
                age = (np.datetime64(cutoff) - t) / np.timedelta64(1, "D")
                samples.append((a, b, kind, a[1], b[1], np.log(p) - lb, 0.5 ** (age / HALF_LIFE_RATIO)))
    S = pd.DataFrame(samples, columns=["a", "b", "kind", "ga", "gb", "r", "w"])
    if S.empty:
        return S
    S["seg"] = S.a.map(lambda x: tuple(x[0].split("|")[i] for i in (0, 3, 4)))
    S["etype"] = S.kind + ":" + S.ga.astype(str) + "<-" + S.gb.astype(str)
    pooled_all = S.groupby("etype").apply(lambda x: wmedian(x.r.values, x.w.values))
    pooled_seg = S.groupby(["seg", "etype"]).apply(lambda x: (wmedian(x.r.values, x.w.values), x.w.sum()))
    out = []
    for (a, b), x in S.groupby(["a", "b"]):
        et, sg = x.etype.iloc[0], x.seg.iloc[0]
        own_r, n = wmedian(x.r.values, x.w.values), x.w.sum()
        ps, pn = pooled_seg.get((sg, et), (pooled_all[et], 0))
        prior = (ps * pn + pooled_all[et] * RATIO_SHRINK) / (pn + RATIO_SHRINK)
        nat = (own_r * n + prior * RATIO_SHRINK) / (n + RATIO_SHRINK)
        cur = x.sort_values("w").r.iloc[-1]
        out.append(dict(a=a, b=b, etype=et, nat=nat, own_ratio=own_r, n_pairs=len(x), w_pairs=n,
                        last_pair_ratio=cur))
    R = pd.DataFrame(out)
    # edges with no paired samples still get the pooled ratio
    have = set(zip(R.a, R.b))
    extra = []
    card_ids = {k[0] for k in nodes.index}
    targets = {(c, g) for c in card_ids for g in (8, 9, 10)}
    for a, b, kind in edges(nodes, lang_map, targets):
        if (a, b) in have:
            continue
        et = f"{kind}:{a[1]}<-{b[1]}"
        if et in pooled_all:
            sg = tuple(a[0].split("|")[i] for i in (0, 3, 4))
            ps, pn = pooled_seg.get((sg, et), (pooled_all[et], 0))
            extra.append(dict(a=a, b=b, etype=et, nat=(ps * pn + pooled_all[et] * RATIO_SHRINK) / (pn + RATIO_SHRINK),
                              own_ratio=np.nan, n_pairs=0, w_pairs=0.0, last_pair_ratio=np.nan))
    return pd.concat([R, pd.DataFrame(extra)], ignore_index=True)


def anchor_values(nodes, ratios, min_neff_b=0.5):
    """Implied log value of A from each anchor B; combined with weights = B's n_eff x pair evidence."""
    out = {}
    detail = {}
    if ratios is None or len(ratios) == 0:
        return out, detail
    for r in ratios.itertuples():
        if r.b not in nodes.index:
            continue
        B = nodes.loc[r.b]
        if B.n_eff < min_neff_b:
            continue
        v = B.own + r.nat
        w = min(B.n_eff, 5) * (1 + min(r.w_pairs, 5)) / 6
        out.setdefault(r.a, []).append((v, w, r.etype))
    comb = {}
    for a, lst in out.items():
        v = np.array([x[0] for x in lst]); w = np.array([x[1] for x in lst])
        comb[a] = float(np.sum(v * w) / w.sum())
        detail[a] = lst
    return comb, detail


# ---------------------------------------------------------------- 4. trend
def momentum(index, sg, g, cutoff, months):
    tm = pd.Timestamp(cutoff - pd.Timedelta(days=1)).to_period("M")
    return idx_value(index, sg, g, tm) - idx_value(index, sg, g, tm - months)


# ---------------------------------------------------------------- 5. estimates
def estimates(s, cutoff, lang_map=None, use_index=True):
    index = fit_index(s, cutoff) if use_index else None
    nodes = node_table(s, cutoff, index)
    ratios = pair_ratios(s, cutoff, nodes, lang_map)
    anc, detail = anchor_values(nodes, ratios)
    # nodes with no sales at all get an anchor-only row
    missing = [k for k in anc if k not in nodes.index]
    if missing:
        add = []
        for cid, g in missing:
            s_, n_, c_, e_, l_ = cid.split("|")
            add.append(dict(card_id=cid, grade=g, set=s_, edition=e_, lang=l_, number=n_, card=c_,
                            own=np.nan, n_eff=0.0, last_lp=np.nan, last_adj=np.nan, last_date=pd.NaT,
                            last_px=np.nan, last_venue=None, sales_12m=0, sales_all=0))
        nodes = pd.concat([nodes, pd.DataFrame(add).set_index(["card_id", "grade"])])
    nodes["anchor"] = [anc.get(k, np.nan) for k in nodes.index]
    nodes["n_anchor"] = [len(detail.get(k, [])) for k in nodes.index]
    nodes["mom3"] = [momentum(index, (r.set, r.edition, r.lang), k[1], cutoff, 3) for k, r in nodes.iterrows()]
    nodes["mom12"] = [momentum(index, (r.set, r.edition, r.lang), k[1], cutoff, 12) for k, r in nodes.iterrows()]
    return nodes, ratios, index, detail


def tier(n_eff):
    return "thin" if n_eff < 1 else ("mid" if n_eff < 3 else "liq")


def blend(row, W):
    """W: dict tier -> (w_own, w_anchor, w_last). Missing estimates are dropped and weights renormalised."""
    w = W[tier(row.n_eff)]
    parts = [(row.own, w[0]), (row.anchor, w[1]), (row.last_adj, w[2])]
    parts = [(v, x) for v, x in parts if pd.notna(v) and x > 0]
    if not parts:
        return np.nan
    return sum(v * x for v, x in parts) / sum(x for _, x in parts)
