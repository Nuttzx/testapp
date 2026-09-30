# Slab Signal — PSA opportunity model (vintage Pokémon)

Everything lives here: raw captures, cleaning rules, engine, outputs, app. Nothing depends on a chat.

## Run
```
python3 engine/run.py --backtest          # full run incl. walk-forward backtest (~1 min)
python3 engine/run.py --listings data/listings/listings_YYYY-MM-DD.csv
python3 engine/build_app.py               # app/index.html from app/data.json
```

## Layout
- `data/raw/` — every captured file, untouched. New Chrome captures go here as `sales_<run>.csv` (English) or `sales_jp_<run>.csv` (Japanese); `load_raw` picks them up automatically.
- `data/holdings.csv` — the collection (add `cost`, `cost_currency`, `buy_date` to enable the flip rule).
- `engine/clean.py` — hygiene: manual exclusions, title qualifiers, duplicates, cert-collapse, below-lower-grade, local outliers. Every excluded row keeps its reason in `status`.
- `engine/model.py` — index (trend), own value, anchors (grade, edition, language pairs, each measured in its own time window), blend.
- `engine/backtest.py`, `engine/calibrate.py` — walk-forward test, blend weights per grade × liquidity, bands, decision-grade flag.
- `engine/signals.py` — opportunities, holdings verdicts, listing matches (21% import VAT for non-EU sellers).
- `capture/` — Chrome prompts (sales, populations, Japanese, eBay listings).
- `data/out/` — outputs (valuations, opportunities, holdings, model report, lead-lag, anchor ratios).

## Rules encoded
- PSA only, 1st/Unlimited/Shadowless never mixed, holo only.
- Flip rule: max buy = band low × (1 − 10% fee − 2% insurance) ÷ 1.20; US/non-EU sellers ÷ 1.21 more for VAT.
- Buy signal: price ≥30% below fair value. Sell signal: median of last 3–5 sales in 60 days ≥5% above fair value.
- A segment is "verified" only if its backtest error is ≤25% (PSA 8/9) or ≤35% (PSA 10) and no worse than last sale.
