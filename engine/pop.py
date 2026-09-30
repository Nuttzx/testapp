"""PSA population (Pikawiz) -> one row per set x number x edition x variant."""
import glob
import pandas as pd

LABEL = {"1st Edition Holo": ("1st", "holo"), "1st Edition": ("1st", "holo"), "Unlimited Holo": ("unl", "holo"),
         "Shadowless Holo": ("shadowless", "holo"), "Holo": ("unl", "holo"), "Reverse Holo": ("unl", "reverse"),
         "Cosmos Holo Variant": ("unl", "cosmos"), "Gold Star Holo": ("unl", "holo")}


def load_pop():
    fs = sorted(glob.glob("data/raw/pop/*.csv"))
    if not fs:
        return None
    p = pd.concat([pd.read_csv(f) for f in fs], ignore_index=True)
    p = p.sort_values("captured_date").drop_duplicates(["set", "card_name", "card_number", "pikawiz_label"], keep="last")
    p = p[p.pikawiz_label.isin(LABEL)].copy()   # drops prerelease, no-symbol, 4th print, errors, blank labels
    p["edition"] = p.pikawiz_label.map(lambda x: LABEL[x][0])
    p["variant"] = p.pikawiz_label.map(lambda x: LABEL[x][1])
    p["number"] = p.card_number.astype(str).str.split("/").str[0]
    p = p.rename(columns={"psa_10": "pop10", "psa_9": "pop9", "psa_8": "pop8", "psa_total": "pop_total"})
    # same card listed twice with the same label (Pikawiz duplicates): keep the larger total
    p = p.sort_values("pop_total").drop_duplicates(["set", "number", "edition", "variant"], keep="last")
    p["gem_rate"] = p.pop10 / p.pop_total
    return p[["set", "number", "card_name", "edition", "variant", "pop10", "pop9", "pop8", "pop_total", "gem_rate", "captured_date"]]
