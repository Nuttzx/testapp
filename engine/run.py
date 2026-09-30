"""Full pipeline.  Usage:  python engine/run.py [--backtest] [--listings data/listings/<file>.csv]

1. load + clean all sales files in data/raw
2. (optional) walk-forward backtest -> data/out/backtest.pkl
3. calibrate blend weights / bands / decision-grade flags
4. value every card x grade today, generate opportunities, evaluate holdings
5. write data/out/*.csv and app/data.json
"""
import sys, os, json, argparse
sys.path.insert(0, os.path.dirname(__file__))
import numpy as np
import pandas as pd
from clean import load_raw, clean, fit_best_offer_factor
from model import estimates, fit_index
from backtest import run as backtest
from calibrate import calibrate
from signals import valuations, opportunities, holdings_eval, match_listings
from pop import load_pop
LOW_POP = 100

OUT = "data/out"


def load_collectr():
    f = "data/raw/collectr_prices_B01-B17partial.csv"
    if not os.path.exists(f):
        return None
    c = pd.read_csv(f)
    c = c[c.grade.isin(["PSA10", "PSA9", "PSA8"]) & c.language.eq("EN")]
    c["number"] = c.card_number.astype(str).str.split("/").str[0]
    c["edition"] = c.edition.map({"1st": "1st", "unlimited": "unl", "shadowless": "shadowless"})
    c["grade"] = c.grade.str[3:].astype(int)
    c = c.rename(columns={"market_price": "collectr"})
    return c.groupby(["set", "number", "edition", "grade"], as_index=False).collectr.first()


def lead_lag(s, cutoff):
    import model
    keep = model.POP_SEGMENTS
    model.POP_SEGMENTS = False           # set-level PSA 10 index for this test
    ix = fit_index(model.add_segments(s), cutoff)
    model.POP_SEGMENTS = keep
    rows = []
    for (seg, g), ser in ix["seg"].items():
        if g != 10 or (seg, 9) not in ix["seg"]:
            continue
        d10, d9 = ser.diff(), ix["seg"][(seg, 9)].diff()
        z = pd.concat([d10, d9.shift(-1)], axis=1).iloc[-16:].dropna()
        r = z.corr().iloc[0, 1] if len(z) > 3 else np.nan
        n = len(z)
        tcrit = 0.514 if n >= 15 else 0.6
        rows.append(dict(segment=seg, psa10_leads_psa9_corr=r, months=n, significant=bool(abs(r) > tcrit),
                         psa10_12m=float(np.exp(ser.iloc[-1] - ser.iloc[-13]) - 1),
                         psa9_12m=float(np.exp(ix["seg"][(seg, 9)].iloc[-1] - ix["seg"][(seg, 9)].iloc[-13]) - 1),
                         psa8_12m=float(np.exp(ix["seg"][(seg, 8)].iloc[-1] - ix["seg"][(seg, 8)].iloc[-13]) - 1)
                         if (seg, 8) in ix["seg"] else np.nan))
    return pd.DataFrame(rows)


def low_pop_layer(s, cutoff, P):
    """PSA 10 price index for low-pop (<100) vs higher-pop holos, pooled across captured sets.
    Tests the thesis that low-pop vintage PSA 10s are repricing faster."""
    if P is None:
        return pd.DataFrame()
    ph = P[P.variant == "holo"][["set", "number", "edition", "pop10"]]
    x = s.merge(ph, how="left", on=["set", "number", "edition"])
    x = x[x.grade.eq(10) & x.pop10.notna()].copy()
    rows = []
    for name, sub in (("PSA 10 pop < 100", x[x.pop10 < LOW_POP]), ("PSA 10 pop >= 100", x[x.pop10 >= LOW_POP])):
        sub = sub.assign(seg="pool")
        if sub.status.eq("used").sum() < 20:
            continue
        ser = fit_index(sub, cutoff)["glob"][10]
        chg = lambda k: float(np.exp(ser.iloc[-1] - ser.iloc[-1 - k]) - 1) if len(ser) > k else np.nan
        rows.append(dict(group=name, cards=int(sub.card_id.nunique()), sales_12m=int(((cutoff - sub.date).dt.days <= 365).sum()),
                         chg_3m=chg(3), chg_6m=chg(6), chg_12m=chg(12)))
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--listings")
    ap.add_argument("--today", default=pd.Timestamp.today().normalize().strftime("%Y-%m-%d"))
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    today = pd.Timestamp(a.today)
    cutoff = today + pd.Timedelta(days=1)

    s = clean(load_raw())
    P = load_pop()
    import model
    if P is not None:
        ph = P[P.variant == "holo"]
        model.POP10 = {(a, str(n), e): v for a, n, e, v in zip(ph.set, ph.number, ph.edition, ph.pop10)}
    s = model.add_segments(s)
    bo_factor, bo_n = fit_best_offer_factor(s)
    s.to_csv(f"{OUT}/sales_clean.csv", index=False)

    if a.backtest or not os.path.exists(f"{OUT}/backtest.pkl"):
        bt = backtest(s, end=(today - pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
        bt.to_pickle(f"{OUT}/backtest.pkl")
    bt = pd.read_pickle(f"{OUT}/backtest.pkl")
    W, rep, btc = calibrate(bt)
    rep.to_csv(f"{OUT}/model_report.csv", index=False)

    nodes, ratios, index, detail = estimates(s, cutoff)
    V = valuations(nodes, W, rep, load_collectr(), s, today)
    if P is not None:
        ph = P[P.variant == "holo"]
        V = V.merge(ph[["set", "number", "edition", "pop10", "pop9", "pop8", "pop_total", "gem_rate"]],
                    how="left", on=["set", "number", "edition"])
        V["pop_this_grade"] = [r[f"pop{int(r.grade)}"] if pd.notna(r.pop10) else np.nan for _, r in V.iterrows()]
        V["low_pop"] = V.pop10 < LOW_POP
    V.to_csv(f"{OUT}/valuations.csv", index=False)
    O = opportunities(V, today)
    O.to_csv(f"{OUT}/opportunities.csv", index=False)
    H = holdings_eval(pd.read_csv("data/holdings.csv"), V)
    H.to_csv(f"{OUT}/holdings_eval.csv", index=False)
    LL = lead_lag(s, cutoff)
    LP = low_pop_layer(s, cutoff, P)
    LP.to_csv(f"{OUT}/low_pop_layer.csv", index=False)
    LL.to_csv(f"{OUT}/lead_lag.csv", index=False)
    ratios.assign(nat_ratio=np.exp(ratios.nat)).to_csv(f"{OUT}/anchor_ratios.csv", index=False)
    M = None
    if a.listings:
        L = pd.read_csv(a.listings)
        M = match_listings(L, V, today)
        M.to_csv(f"{OUT}/listing_matches.csv", index=False)

    status = s.status.str.split(":").str[:2].str.join(":").value_counts().to_dict()
    meta = dict(run_date=today.strftime("%Y-%m-%d"), sales_used=int((s.status == "used").sum()),
                sales_excluded=int((s.status != "used").sum()), exclusions=status,
                best_offer_factor=bo_factor, best_offer_pairs=bo_n,
                sets=sorted(s.set.unique().tolist()), backtest_sales=int(len(bt)),
                date_range=[s.date.min().strftime("%Y-%m-%d"), s.date.max().strftime("%Y-%m-%d")])
    os.makedirs("app", exist_ok=True)

    def rec(df):
        df = df.copy()
        for c in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[c]):
                df[c] = df[c].dt.strftime("%Y-%m-%d")
        return json.loads(df.to_json(orient="records", double_precision=4))
    json.dump(dict(meta=meta, report=rec(rep), lead_lag=rec(LL), low_pop=rec(LP), valuations=rec(V), opportunities=rec(O),
                   holdings=rec(H), listings=rec(M) if M is not None else []),
              open("app/data.json", "w"))
    print(json.dumps(meta, indent=1))
    print(rep.round(3).to_string())
    print(f"opportunities: {len(O)}")


if __name__ == "__main__":
    main()
