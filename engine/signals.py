"""Turn valuations into opportunities, holdings verdicts and listing matches."""
import numpy as np
import pandas as pd
from model import tier

SELL_FEE, INSURANCE, FLIP_NET = 0.10, 0.02, 0.20
VAT_IMPORT = 0.21          # Spain import VAT when the price excludes it (non-EU seller)
BUY_DISCOUNT = 0.30        # buy signal: price >=30% below fair value
SELL_PREMIUM = 0.05        # sell signal: market >=5% above fair value
ANCHOR_GAP = 0.30          # grade lagging its anchors by >=30%
MOMENTUM = np.log(1.25)    # segment up >=25% in 3 months
STALE_DAYS = 60


def valuations(nodes, W, rep, collectr=None, sales=None, today=None):
    rp = rep.set_index(["grade", "tier"])
    rows = []
    for (cid, g), n in nodes.iterrows():
        t = tier(n.n_eff) if n.sales_all > 0 else "new"
        w = W.get((g, t), (1, 0, 0))
        parts = [(n.own, w[0]), (n.anchor, w[1]), (n.last_adj, w[2])]
        parts = [(v, x) for v, x in parts if pd.notna(v) and x > 0]
        if not parts:  # fall back to whatever exists
            parts = [(v, 1) for v in (n.own, n.anchor, n.last_adj) if pd.notna(v)]
        if not parts:
            continue
        lfv = sum(v * x for v, x in parts) / sum(x for _, x in parts)
        r = rp.loc[(g, t)] if (g, t) in rp.index else None
        q10 = r.band_q10 if r is not None and pd.notna(r.band_q10) else -0.5
        q90 = r.band_q90 if r is not None and pd.notna(r.band_q90) else 1.0
        bias = r.bias if r is not None and pd.notna(r.bias) else 0.0
        fv = float(np.exp(lfv + bias))
        rows.append(dict(card_id=cid, set=n.set, number=n.number, card=n.card, edition=n.edition, lang=n.lang,
                         grade=int(g), fair_value=fv, band_low=float(np.exp(lfv + q10)), band_high=float(np.exp(lfv + q90)),
                         own_value=np.exp(n.own) if pd.notna(n.own) else np.nan,
                         anchor_value=np.exp(n.anchor) if pd.notna(n.anchor) else np.nan,
                         n_anchors=int(n.n_anchor),
                         last_sale=n.last_px, last_sale_date=n.last_date, last_venue=n.last_venue,
                         sales_12m=int(n.sales_12m), liquidity=t,
                         decision_grade=bool(r.decision_grade) if r is not None else False,
                         model_error=float(r.err_model) if r is not None else np.nan,
                         seg_mom_3m=float(np.exp(n.mom3) - 1), seg_mom_12m=float(np.exp(n.mom12) - 1)))
    V = pd.DataFrame(rows)
    if sales is not None:
        u = sales[sales.status.eq("used") & (sales.date >= today - pd.Timedelta(days=60))].sort_values("date")
        g = u.groupby(["card_id", "grade"]).px
        mk = g.apply(lambda x: x.tail(5).median()).rename("market_60d")
        nn = g.size().rename("n_60d")
        V = V.merge(mk.reset_index(), how="left", on=["card_id", "grade"]).merge(nn.reset_index(), how="left", on=["card_id", "grade"])
        V["n_60d"] = V.n_60d.fillna(0).astype(int)
    k = (1 - SELL_FEE - INSURANCE) / (1 + FLIP_NET)
    V["max_buy_eu"] = V.band_low * k
    V["max_buy_us_vat"] = V.band_low * k / (1 + VAT_IMPORT)
    V["last_vs_fv"] = V.last_sale / V.fair_value - 1
    V["anchor_gap"] = V.anchor_value / V.own_value - 1
    if collectr is not None:
        V = V.merge(collectr, how="left", on=["set", "number", "edition", "grade"])
        V["collectr_vs_fv"] = V.collectr / V.fair_value - 1
    return V


def opportunities(V, today):
    out = []
    for r in V.itertuples():
        age = (today - r.last_sale_date).days if pd.notna(r.last_sale_date) else None
        base = dict(card_id=r.card_id, set=r.set, number=r.number, card=r.card, edition=r.edition, grade=r.grade,
                    fair_value=r.fair_value, band_low=r.band_low, max_buy_eu=r.max_buy_eu,
                    max_buy_us_vat=r.max_buy_us_vat, last_sale=r.last_sale, last_sale_date=r.last_sale_date,
                    decision_grade=r.decision_grade, liquidity=r.liquidity, model_error=r.model_error)
        if pd.notna(r.anchor_gap) and r.anchor_gap >= ANCHOR_GAP and r.n_anchors >= 1:
            out.append({**base, "type": "LAGGING GRADE", "strength": r.anchor_gap,
                        "why": f"Linked grades/editions imply {r.anchor_value:,.0f} vs own sales {r.own_value:,.0f} "
                               f"(+{r.anchor_gap:.0%}). This grade has not caught up with its anchors."})
        if pd.notna(r.last_sale) and age is not None and age <= 45 and r.last_sale <= (1 - BUY_DISCOUNT) * r.fair_value:
            out.append({**base, "type": "CHEAP RECENT SALE", "strength": 1 - r.last_sale / r.fair_value,
                        "why": f"Sold at {r.last_sale:,.0f} on {r.last_sale_date:%Y-%m-%d}, {1 - r.last_sale / r.fair_value:.0%} "
                               f"below fair value {r.fair_value:,.0f}. Copies are clearing cheap: target the next one."})
        if r.seg_mom_3m >= np.exp(MOMENTUM) - 1 and (age is None or age >= STALE_DAYS):
            out.append({**base, "type": "STALE IN A RISING SEGMENT", "strength": r.seg_mom_3m,
                        "why": f"Its segment is up {r.seg_mom_3m:.0%} in 3 months, but this card-grade last sold "
                               f"{'never' if age is None else f'{age} days ago'}. Sellers may still price off old comps."})
    O = pd.DataFrame(out)
    if len(O):
        O = O.sort_values(["decision_grade", "strength"], ascending=[False, False])
    return O


def holdings_eval(H, V):
    rows = []
    for h in H.itertuples():
        base = h._asdict()
        if h.grader != "PSA" or str(h.grade) in ("", "nan") or int(float(h.grade)) not in (8, 9, 10):
            rows.append({**base, "verdict": "NOT MODELLED", "reason": "outside PSA 8-10 scope or no data"})
            continue
        m = V[(V.set == h.set) & (V.number.astype(str) == str(h.card_number)) & (V.edition == h.edition)
              & (V.grade == int(float(h.grade))) & (V.lang == h.lang)]
        if m.empty:
            rows.append({**base, "verdict": "NO DATA YET", "reason": "set not captured yet"})
            continue
        v = m.iloc[0]
        net_fv = v.fair_value * (1 - SELL_FEE - INSURANCE)
        mkt = v.market_60d if pd.notna(v.market_60d) else np.nan
        if pd.notna(mkt) and v.n_60d >= 3 and mkt >= (1 + SELL_PREMIUM) * v.fair_value:
            verdict, reason = "SELL CANDIDATE", (f"Median of last {int(min(v.n_60d, 5))} sales ({mkt:,.0f}) is "
                                                 f"{mkt / v.fair_value - 1:+.0%} vs fair value")
        elif pd.notna(h.collectr_value_usd) and h.collectr_value_usd < 0.8 * v.fair_value:
            verdict, reason = "HOLD (Collectr understates)", f"Collectr {h.collectr_value_usd:,.0f} vs fair value {v.fair_value:,.0f}"
        else:
            verdict, reason = "HOLD", "Market in line with fair value"
        if pd.notna(h.cost) and h.cost != "":
            flip_ok = net_fv >= (1 + FLIP_NET) * float(h.cost)
            reason += f"; flip rule {'met' if flip_ok else 'not met'} at fair value"
        else:
            reason += "; cost unknown, flip rule not checked"
        rows.append({**base, "verdict": verdict, "reason": reason, "fair_value": v.fair_value,
                     "band_low": v.band_low, "band_high": v.band_high, "net_if_sold_at_fv": net_fv,
                     "last_sale": v.last_sale, "last_sale_date": v.last_sale_date,
                     "decision_grade": v.decision_grade})
    return pd.DataFrame(rows)


def match_listings(L, V, today):
    """L: active listings from the Chrome scan. Returns listings at or below max buy, ranked."""
    out = []
    for l in L.itertuples():
        m = V[(V.set == l.set) & (V.number.astype(str) == str(l.number)) & (V.edition == l.edition) & (V.grade == int(l.grade))]
        if m.empty:
            continue
        v = m.iloc[0]
        eu = str(getattr(l, "seller_region", "")).upper() in ("EU", "ES", "DE", "FR", "IT", "NL", "BE", "PT", "AT", "IE")
        landed = l.price_usd * (1 if eu or getattr(l, "vat_included", False) else 1 + VAT_IMPORT)
        cap = v.max_buy_eu * (1 + VAT_IMPORT) if not eu else v.max_buy_eu
        out.append(dict(**l._asdict(), fair_value=v.fair_value, band_low=v.band_low, landed_cost=landed,
                        discount_to_fv=1 - landed / v.fair_value, meets_flip_rule=landed <= v.max_buy_eu,
                        buy_30pct_below_fv=landed <= (1 - BUY_DISCOUNT) * v.fair_value,
                        decision_grade=v.decision_grade))
    M = pd.DataFrame(out)
    if len(M):
        M = M[M.buy_30pct_below_fv | M.meets_flip_rule].sort_values("discount_to_fv", ascending=False)
    return M
