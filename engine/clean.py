"""Load raw sales files, normalise, deduplicate and apply hygiene rules.

Output: one tidy sales table, every row keeping its source and a `status`
(used / excluded:<reason>) so every number traces back to a raw row.
"""
import re
import glob
import numpy as np
import pandas as pd

RAW = "data/raw"
ED = {"1st edition": "1st", "1st": "1st", "unlimited": "unl", "unl": "unl",
      "shadowless": "shadowless", "no rarity": "norarity"}

# Rows checked by hand (2026-09-30). Key: set, card, edition, grade, date, price
MANUAL_EXCLUDE = {
    ("Neo Genesis", "Lugia", "1st", 10, "2026-02-15", 1825.0): "same cert sold 382,918 a month later",
    ("Neo Genesis", "Pichu", "unl", 10, "2025-12-28", 74.95): "impossible price for PSA 10",
    ("Neo Genesis", "Pichu", "1st", 10, "2025-10-28", 1504.57): "same cert sold 19,200 three weeks earlier",
    ("Neo Genesis", "Pichu", "1st", 10, "2025-02-24", 596.78): "same cert sold 55,200 later",
    ("Neo Genesis", "Jumpluff", "unl", 10, "2026-09-28", 4000.0): "best-offer listing price; same cert sold 408",
    ("Neo Genesis", "Lugia", "1st", 10, "2025-01-15", 6000.0): "not credible for a pop-45 PSA 10 (other sales 146k-383k); likely fake/wrong card",
    ("Neo Genesis", "Lugia", "1st", 10, "2025-06-17", 1584.13): "not credible for a pop-45 PSA 10; likely fake/wrong card",
    ("Neo Genesis", "Lugia", "1st", 10, "2025-06-18", 1287.44): "not credible for a pop-45 PSA 10; likely fake/wrong card",
}

QUALIFIER = re.compile(r"\b(?:OC|MC|ST|PD|MK)\b|\(OC\)|off[- ]?cent|\b(?:8|9)\.5\b|\b(?:BGS|CGC|SGC|TAG|ACE)\b|\blot\b|proxy|reprint|candidate|potential|psa ready|ready for psa|celebrations?|classic coll|\bnot psa|like psa|custom|fan ?art|orica",
                       re.I)


def item_id(url):
    if not isinstance(url, str):
        return None
    m = re.search(r"/itm/(\d+)", url)
    if m:
        return "ebay:" + m.group(1)
    m = re.search(r"(fanaticscollect\.com/\S+|goldin\.co/item/\S+|ha\.com/\S+)", url)
    return m.group(1) if m else url


def load_raw(files=None):
    if files is None:
        files = sorted(glob.glob(f"{RAW}/sales_*.csv")) + [
            f"{RAW}/psa_sales_combined_NG_TR_2026-09-29.csv",
            f"{RAW}/neo_genesis_psa10_apr_additions_2026-09-29.csv",
        ]
    frames = []
    for f in files:
        d = pd.read_csv(f, dtype={"cert_number": str})
        d["file"] = f.split("/")[-1]
        frames.append(d)
    s = pd.concat(frames, ignore_index=True)
    s["lang"] = np.where(s["file"].str.contains("_jp_"), "JP", "EN")
    s["edition"] = s["edition"].astype(str).str.strip().str.lower().map(ED).fillna(s["edition"])
    s["grade"] = s["grade"].astype(str).str.extract(r"(\d+)")[0].astype(float).astype("Int64")
    s["number"] = s["card_number"].astype(str).str.split("/").str[0].str.strip()
    s["date"] = pd.to_datetime(s["sale_date"], errors="coerce")
    s["price"] = pd.to_numeric(s["price"], errors="coerce")
    s["accepted_price"] = pd.to_numeric(s["accepted_price"], errors="coerce")
    s["best_offer"] = s["best_offer"].astype(str).str.upper().eq("TRUE")
    s["card_id"] = s["set"] + "|" + s["number"] + "|" + s["card_name"] + "|" + s["edition"] + "|" + s["lang"]
    s["item"] = s["listing_url"].map(item_id)
    s["auction_house"] = s["venue"].fillna("").str.contains("Goldin|Heritage|Fanatics|PWCC|Robert Edward", case=False)
    s["status"] = "used"
    return s


def clean(s):
    s = s.copy()
    s = s[s["grade"].isin([8, 9, 10]) & s["price"].gt(0) & s["date"].notna()].copy()

    # 1. manual exclusions
    for i, r in s.iterrows():
        k = (r["set"], r["card_name"], r["edition"], int(r["grade"]), r["date"].strftime("%Y-%m-%d"), float(r["price"]))
        if k in MANUAL_EXCLUDE:
            s.at[i, "status"] = "excluded:manual:" + MANUAL_EXCLUDE[k]

    # 2. qualifier / other grader in title
    q = s["listing_title"].fillna("").str.contains(QUALIFIER)
    s.loc[q & s.status.eq("used"), "status"] = "excluded:title_qualifier"

    # 2b. title states the other edition
    tl = s["listing_title"].fillna("").str.lower()
    says_1st = tl.str.contains(r"1st|1\. ?edition|first edition|1 ?ed\b", regex=True)
    says_unl = tl.str.contains(r"unlimited|\bunl\b", regex=True)
    s.loc[s.status.eq("used") & s.edition.eq("unl") & says_1st & ~says_unl, "status"] = "excluded:title_says_other_edition"
    s.loc[s.status.eq("used") & s.edition.eq("1st") & says_unl & ~says_1st, "status"] = "excluded:title_says_other_edition"

    # 3. same listing recorded under two editions -> edition unknown, exclude both;
    #    same listing twice under the same edition -> keep first
    it = s[s["item"].notna() & s.status.eq("used")]
    conflict = it.groupby(["item", "grade"]).edition.transform("nunique") > 1
    s.loc[it.index[conflict], "status"] = "excluded:edition_conflict_same_listing"
    d = s["item"].notna() & s.status.eq("used") & s[s.status.eq("used")].duplicated(["item", "grade"], keep="first").reindex(s.index, fill_value=False)
    s.loc[d, "status"] = "excluded:duplicate_item"

    # 4. cross-source duplicates: same card+grade, date within 2 days, price within 1%
    s = s.sort_values(["card_id", "grade", "date"])
    used = s[s.status.eq("used")]
    for (_, _), g in used.groupby(["card_id", "grade"]):
        idx = g.index.tolist()
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                ia, ib = idx[a], idx[b]
                if (s.at[ib, "date"] - s.at[ia, "date"]).days > 2:
                    break
                if s.at[ib, "status"] != "used" or s.at[ia, "status"] != "used":
                    continue
                if s.at[ia, "file"] == s.at[ib, "file"] and s.at[ia, "item"] != s.at[ib, "item"]:
                    continue  # two genuine sales in same source
                if abs(s.at[ia, "price"] / s.at[ib, "price"] - 1) <= 0.01:
                    drop = ib if s.at[ib, "source"] == "PSA APR" else ia
                    s.at[drop, "status"] = "excluded:duplicate_cross_source"

    # 5. same cert sold >5x apart within 18 months: the LOWER sale is excluded only if it is also
    #    far (<1/3) below other same card+grade sales within +/-120 days (so genuine repricing survives)
    c = s[s.status.eq("used") & s["cert_number"].notna()]
    for _, g in c.groupby("cert_number"):
        if len(g) < 2:
            continue
        for ia in g.index:
            for ib in g.index:
                if ia == ib or s.at[ia, "status"] != "used":
                    continue
                if abs((s.at[ia, "date"] - s.at[ib, "date"]).days) <= 548 and s.at[ia, "price"] * 5 < s.at[ib, "price"]:
                    nb = s[(s.card_id == s.at[ia, "card_id"]) & (s.grade == s.at[ia, "grade"]) & s.status.eq("used")
                           & (s.index != ia) & ((s.date - s.at[ia, "date"]).abs().dt.days <= 120)]
                    if len(nb) == 0 or s.at[ia, "price"] * 3 < nb.price.median():
                        s.at[ia, "status"] = "excluded:cert_price_collapse"

    # 5b. a PSA g sale priced below 60% of the median of PSA g-1 for the same card within +/-90 days
    #     (>=3 lower-grade sales) is not a credible PSA g sale
    u = s[s.status.eq("used")]
    for i, r in u[u.grade > 8].iterrows():
        low = u[(u.card_id == r.card_id) & (u.grade == r.grade - 1) & ((u.date - r.date).abs().dt.days <= 90)]
        if len(low) < 3:
            low = u[(u.card_id == r.card_id) & (u.grade == r.grade - 1) & ((u.date - r.date).abs().dt.days <= 180)]
        if len(low) >= 3 and r.price < 0.6 * low.price.median():
            s.at[i, "status"] = "excluded:below_lower_grade"

    # 5c. local outlier: >4x from the median of >=3 same card+grade sales within +/-60 days
    u = s[s.status.eq("used")]
    for _, g in u.groupby(["card_id", "grade"]):
        for i, r in g.iterrows():
            nb = g[(g.index != i) & ((g.date - r.date).abs().dt.days <= 60)]
            if len(nb) >= 3 and abs(np.log(r.price) - np.log(nb.price).median()) > np.log(4):
                s.at[i, "status"] = "excluded:outlier_4x_local"

    # 6. best-offer price handling
    s["bo_unverified"] = s["best_offer"] & (s["accepted_price"].isna() | (s["accepted_price"] == s["price"]))
    s["px"] = np.where(s["best_offer"] & s["accepted_price"].notna() & (s["accepted_price"] != s["price"]),
                       s["accepted_price"], s["price"])
    return s.sort_index()


def fit_best_offer_factor(s, window_days=45):
    """Median ratio of unverified best-offer prices to same card+grade non-best-offer sales nearby.
    Used to convert listing prices into estimated accepted prices."""
    u = s[s.status.eq("used")]
    ratios = []
    for _, g in u.groupby(["card_id", "grade"]):
        bo = g[g.bo_unverified]
        ref = g[~g.best_offer]
        if bo.empty or len(ref) < 2:
            continue
        for _, r in bo.iterrows():
            near = ref[(ref.date - r.date).abs().dt.days <= window_days]
            if len(near) >= 2:
                ratios.append(np.log(r.px) - np.log(near.px).median())
    ratios = np.array(ratios)
    return float(np.exp(np.median(ratios))), len(ratios)


def outlier_pass(s, index_fn=None, k=np.log(3.0), window=120, min_nb=3):
    """Flag sales >3x away from the median of same card+grade sales within +/-window days
    (needs at least min_nb neighbours). Neighbours are index-adjusted when index_fn is given."""
    u = s[s.status.eq("used")]
    flags = []
    for _, g in u.groupby(["card_id", "grade"]):
        for i, r in g.iterrows():
            nb = g[(g.index != i) & ((g.date - r.date).abs().dt.days <= window)]
            if len(nb) < min_nb:
                continue
            lp = np.log(nb.px_adj)
            if index_fn is not None:
                lp = lp + np.array([index_fn(r, d) for d in nb.date])
            if abs(np.log(r.px_adj) - np.median(lp)) > k:
                flags.append(i)
    s.loc[flags, "status"] = "excluded:outlier_3x_vs_neighbours"
    return s
