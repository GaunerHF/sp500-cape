"""Build the S&P 500 CAPE vs 10-year return page.

Usage:  python build.py            (writes site/index.html and site/data.json)
Env:    FRED_API_KEY  optional; without it the fed funds range and CPI are left out
        and the 10-year yield falls back to the last value in the Shiller data.
"""
import json, os, sys, datetime as dt
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from scipy import stats

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
HEADERS = {"User-Agent": "sp500-cape-site/1.0"}


def log(*a):
    print("[build]", *a, flush=True)


# ---------------------------------------------------------------- data
def load_shiller():
    r = requests.get(CFG["shiller_csv_url"], headers=HEADERS, timeout=60)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text), parse_dates=["Date"]).set_index("Date").sort_index()
    log("Shiller rows:", len(df), "last:", df.index[-1].date())
    return df


def ten_year_returns(df):
    p = df["SP500"]
    d = df["Dividend"].replace(0, np.nan).ffill()
    r = (p.shift(-1) + d / 12) / p
    tr = r.cumprod().shift(1).fillna(1.0)
    r10 = ((tr.shift(-120) / tr) ** 0.1 - 1) * 100
    return pd.DataFrame({
        "cape": df["PE10"].replace(0, np.nan),
        "y10": df["Long Interest Rate"].replace(0, np.nan),
        "r10": r10,
    })


def fred_latest(series, n=15):
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {"series_id": series, "api_key": FRED_KEY, "file_type": "json",
              "sort_order": "desc", "limit": n}
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    obs = [o for o in r.json()["observations"] if o["value"] not in (".", "")]
    return [(o["date"], float(o["value"])) for o in obs]


def multpl_cape():
    """Current Shiller PE from multpl.com (no API; parsing may break if the page changes)."""
    try:
        r = requests.get("https://www.multpl.com/shiller-pe", headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        return parse_multpl(r.text)
    except Exception as e:
        log("multpl.com CAPE not available:", repr(e))
        return None


def parse_multpl(html):
    import re
    m = re.search(r'id="current".*?</b>\s*([0-9]+(?:\.[0-9]+)?)', html, re.S)
    if not m:
        return None
    v = float(m.group(1))
    if not 5 < v < 100:
        return None
    return {"value": v, "as_of": dt.date.today().isoformat(), "source": "multpl.com"}


def current_levels(m):
    """Latest CAPE, 10y yield, fed funds range, CPI YoY. Missing items -> None."""
    cur = {}
    hist = m.dropna(subset=["cape"])
    if CFG.get("cape_override"):
        cur["cape"] = {"value": float(CFG["cape_override"]),
                       "as_of": CFG.get("cape_override_as_of") or "", "source": "config.json"}
    else:
        live = multpl_cape()
        if live:
            cur["cape"] = live
        else:
            cur["cape"] = {"value": round(float(hist.cape.iloc[-1]), 2),
                           "as_of": hist.index[-1].strftime("%Y-%m"), "source": "Shiller data (may lag)"}
    y = m.dropna(subset=["y10"])
    cur["y10"] = {"value": round(float(y.y10.iloc[-1]), 2), "as_of": y.index[-1].strftime("%Y-%m"),
                  "source": "Shiller data (monthly)"}
    cur["fed_low"] = cur["fed_high"] = cur["cpi"] = None
    if not FRED_KEY:
        log("FRED_API_KEY not set: skipping fed funds and CPI; 10y yield from Shiller data")
        return cur
    try:
        d, v = fred_latest("DGS10")[0]
        cur["y10"] = {"value": v, "as_of": d, "source": "FRED DGS10"}
        dl, lo = fred_latest("DFEDTARL")[0]
        du, hi = fred_latest("DFEDTARU")[0]
        cur["fed_low"] = {"value": lo, "as_of": dl, "source": "FRED DFEDTARL"}
        cur["fed_high"] = {"value": hi, "as_of": du, "source": "FRED DFEDTARU"}
        cpi = fred_latest("CPIAUCNS", n=14)
        last_d, last_v = cpi[0]
        prev = [v for d, v in cpi if d[:4] == str(int(last_d[:4]) - 1) and d[5:7] == last_d[5:7]]
        if prev:
            cur["cpi"] = {"value": round((last_v / prev[0] - 1) * 100, 1), "as_of": last_d[:7],
                          "source": "FRED CPIAUCNS (YoY)"}
    except Exception as e:  # keep the page building even if FRED is down
        log("FRED request failed:", repr(e))
    return cur


# ---------------------------------------------------------------- analysis
def analyse(m, cur):
    fs, hs = CFG["fit_start"], CFG["history_start"]
    rows = m.dropna(subset=["cape", "r10"])
    fit_rows = rows.loc[fs:]
    slope, intercept = np.polyfit(fit_rows.cape, fit_rows.r10, 1)
    r2 = float(np.corrcoef(fit_rows.cape, fit_rows.r10)[0, 1] ** 2)
    cape_now, y10_now = cur["cape"]["value"], cur["y10"]["value"]
    fitted_now = slope * cape_now + intercept

    h = m.loc[hs:].dropna(subset=["cape", "y10"]).copy()
    h["spread"] = slope * h.cape + intercept - h.y10
    h["excess"] = h.r10 - h.y10
    known = h.dropna(subset=["excess"])
    err = known.excess - known.spread
    sigma = float(err.std(ddof=1))
    now = fitted_now - y10_now
    n_eff = len(known) / 120
    dof = max(1.0, n_eff - 2)
    z = (cape_now - known.cape.mean()) / known.cape.std(ddof=1)
    sigma_t = sigma * np.sqrt(1 + (1 + z * z) / n_eff)

    edges = [-np.inf, -4, -2, 0, 2, 4, 6, np.inf]
    buckets = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        g = known[(known.spread >= lo) & (known.spread < hi)]
        if g.empty:
            continue
        label = f"< {hi:g}" if lo == -np.inf else (f"≥ {lo:g}" if hi == np.inf else f"{lo:g} ~ {hi:g}")
        buckets.append({"bucket": label, "lo": None if lo == -np.inf else lo, "hi": None if hi == np.inf else hi,
                        "n": int(len(g)), "p_lose": float((g.excess < 0).mean()),
                        "median_excess": float(g.excess.median()),
                        "first_year": int(g.index.year.min()), "last_year": int(g.index.year.max())})
    signal = {
        "sigma": sigma, "now_spread": float(now),
        "p_now": float(stats.norm.cdf(-now / sigma)),
        "cut75": float(stats.norm.ppf(0.25) * sigma), "cut90": float(stats.norm.ppf(0.10) * sigma),
        "n_eff": float(n_eff), "df": float(dof), "sigma_t": float(sigma_t),
        "p_now_t": float(stats.t.cdf(-now / sigma_t, dof)),
        "cut75_t": float(stats.t.ppf(0.25, dof) * sigma_t), "cut90_t": float(stats.t.ppf(0.10, dof) * sigma_t),
    }
    fit = {"from": fs, "n": int(len(fit_rows)), "slope": float(slope), "intercept": float(intercept),
           "r2": r2, "cape_now": cape_now, "fitted_now": float(fitted_now)}

    def rnd(x):
        return None if pd.isna(x) else round(float(x), 2)
    points = [{"m": i.strftime("%Y-%m"), "x": rnd(r.cape), "y": rnd(r.r10)} for i, r in rows.iterrows()]
    series = [{"m": i.strftime("%Y-%m"), "s": rnd(r.spread), "e": rnd(r.excess)} for i, r in h.iterrows()]
    return {"fit": fit, "signal": signal, "buckets": buckets, "points": points, "spread": series}


# ---------------------------------------------------------------- output
def main():
    m = ten_year_returns(load_shiller())
    cur = current_levels(m)
    res = analyse(m, cur)
    res["current"] = cur
    res["built_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    f, s = res["fit"], res["signal"]
    log(f"fit since {f['from']}: slope {f['slope']:.3f}, R2 {f['r2']:.2f}, n {f['n']}")
    log(f"CAPE {cur['cape']['value']} ({cur['cape']['as_of']}), 10y {cur['y10']['value']} ({cur['y10']['as_of']})")
    log(f"fitted {f['fitted_now']:.2f}%, spread {s['now_spread']:.2f}, "
        f"P(lose) normal {s['p_now']:.0%}, t {s['p_now_t']:.0%}")

    out = ROOT / "site"
    out.mkdir(exist_ok=True)
    payload = json.dumps(res, ensure_ascii=False, separators=(",", ":"))
    (out / "data.json").write_text(payload, encoding="utf-8")
    tpl = (ROOT / "template.html").read_text(encoding="utf-8")
    if "/*__DATA__*/null" not in tpl:
        sys.exit("template.html is missing the /*__DATA__*/null placeholder")
    (out / "index.html").write_text(tpl.replace("/*__DATA__*/null", payload.replace("</", "<\\/")), encoding="utf-8")
    log("wrote", out / "index.html")


if __name__ == "__main__":
    main()
