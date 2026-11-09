"""Build the valuation page (S&P 500 + CSI 300).

Usage:  python build.py            (writes site/index.html and site/data.json)
Env:    FRED_API_KEY  optional; without it the FRED-based parts (fed funds, CPI, real yield,
        Buffett indicator, risk panel) are left out and the 10-year yield falls back to the
        last value in the Shiller data.

Every data source is wrapped so that one failing source only removes its own section;
the page always builds.
"""
import json, os, re, sys, datetime as dt
from io import StringIO, BytesIO
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from scipy import stats

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
HEADERS = {"User-Agent": "sp500-cape-site/1.0"}
BROWSER = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
DATA_DIR = ROOT / "data"


def log(*a):
    print("[build]", *a, flush=True)


def rnd(x, k=2):
    return None if x is None or pd.isna(x) else round(float(x), k)


# ================================================================ sources
def load_shiller():
    r = requests.get(CFG["shiller_csv_url"], headers=HEADERS, timeout=60)
    r.raise_for_status()
    df = pd.read_csv(StringIO(r.text), parse_dates=["Date"]).set_index("Date").sort_index()
    log("Shiller rows:", len(df), "last:", df.index[-1].date())
    return df


def fred_latest(series, n=15):
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {"series_id": series, "api_key": FRED_KEY, "file_type": "json", "sort_order": "desc", "limit": n}
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    obs = [o for o in r.json()["observations"] if o["value"] not in (".", "")]
    return [(o["date"], float(o["value"])) for o in obs]


def fred_hist(series, start="1940-01-01", freq=None):
    """Full history as a pd.Series indexed by month start (freq='m' averages daily data)."""
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {"series_id": series, "api_key": FRED_KEY, "file_type": "json", "observation_start": start}
    if freq:
        params.update(frequency=freq, aggregation_method="avg")
    r = requests.get(url, params=params, headers=HEADERS, timeout=60)
    r.raise_for_status()
    obs = [o for o in r.json()["observations"] if o["value"] not in (".", "")]
    s = pd.Series([float(o["value"]) for o in obs], index=pd.to_datetime([o["date"] for o in obs]))
    log(f"FRED {series}: {len(s)} obs, last {s.index[-1].date() if len(s) else '-'}")
    return s


def multpl_cape():
    """Current Shiller PE from multpl.com (no API; parsing may break if the page changes)."""
    try:
        r = requests.get("https://www.multpl.com/shiller-pe", headers=BROWSER, timeout=30)
        r.raise_for_status()
        return parse_multpl(r.text)
    except Exception as e:
        log("multpl.com CAPE not available:", repr(e))
        return None


def parse_multpl(html):
    m = re.search(r'id="current".*?</b>\s*([0-9]+(?:\.[0-9]+)?)', html, re.S)
    if not m:
        return None
    v = float(m.group(1))
    if not 5 < v < 100:
        return None
    return {"value": v, "as_of": dt.date.today().isoformat(), "source": "multpl.com"}


def multpl_cape_monthly():
    """Monthly CAPE table from multpl.com, used to extend the Shiller mirror (which can lag by years)."""
    try:
        r = requests.get("https://www.multpl.com/shiller-pe/table/by-month", headers=BROWSER, timeout=30)
        r.raise_for_status()
        rows = re.findall(r"<td>\s*([A-Z][a-z]{2} \d{1,2}, \d{4})\s*</td>\s*<td>[^0-9<]*([0-9]+(?:\.[0-9]+)?)", r.text)
        s = pd.Series({pd.to_datetime(d).to_period("M").to_timestamp(): float(v) for d, v in rows}).sort_index()
        s = s[(s > 5) & (s < 100)]
        log("multpl monthly CAPE rows:", len(s))
        return s if len(s) > 12 else None
    except Exception as e:
        log("multpl.com monthly table not available:", repr(e))
        return None


# ================================================================ US: CAPE vs 10y return
def monthly_frame(df):
    p = df["SP500"]
    d = df["Dividend"].replace(0, np.nan).ffill()
    r = (p.shift(-1) + d / 12) / p
    tr = r.cumprod().shift(1).fillna(1.0)
    r10 = ((tr.shift(-120) / tr) ** 0.1 - 1) * 100
    cpi = df["Consumer Price Index"].replace(0, np.nan)
    return pd.DataFrame({
        "cape": df["PE10"].replace(0, np.nan),
        "y10": df["Long Interest Rate"].replace(0, np.nan),
        "r10": r10,
        "infl10": ((cpi / cpi.shift(120)) ** 0.1 - 1) * 100,
    })


def extend_recent(m):
    """Append months after the Shiller mirror ends (CAPE from multpl, 10y yield from FRED GS10)."""
    last = m.dropna(subset=["cape"]).index[-1]
    cape = multpl_cape_monthly()
    if cape is None:
        return m
    new = cape[cape.index > last]
    if new.empty:
        return m
    ext = pd.DataFrame({"cape": new})
    if FRED_KEY:
        try:
            gs10 = fred_hist("GS10", start=(last + pd.DateOffset(months=1)).strftime("%Y-%m-%d"))
            ext["y10"] = gs10.reindex(ext.index)
        except Exception as e:
            log("FRED GS10 failed:", repr(e))
    m = pd.concat([m[~m.index.isin(ext.index)], ext]).sort_index()
    log(f"extended monthly CAPE from {new.index[0]:%Y-%m} to {new.index[-1]:%Y-%m}")
    return m


def current_levels(m):
    cur = {}
    hist = m.dropna(subset=["cape"])
    if CFG.get("cape_override"):
        cur["cape"] = {"value": float(CFG["cape_override"]), "as_of": CFG.get("cape_override_as_of") or "",
                       "source": "config.json"}
    else:
        live = multpl_cape()
        cur["cape"] = live or {"value": round(float(hist.cape.iloc[-1]), 2), "as_of": hist.index[-1].strftime("%Y-%m"),
                               "source": "monthly data (may lag)"}
    y = m.dropna(subset=["y10"])
    cur["y10"] = {"value": round(float(y.y10.iloc[-1]), 2), "as_of": y.index[-1].strftime("%Y-%m"),
                  "source": "Shiller data (monthly)"}
    cur["fed_low"] = cur["fed_high"] = cur["cpi"] = cur["real10"] = None
    if not FRED_KEY:
        log("FRED_API_KEY not set: skipping FRED parts")
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
            cur["cpi"] = {"value": round((last_v / prev[0] - 1) * 100, 1), "as_of": last_d[:7], "source": "FRED CPIAUCNS"}
    except Exception as e:
        log("FRED request failed:", repr(e))
    try:
        d, v = fred_latest("DFII10")[0]
        cur["real10"] = {"value": v, "as_of": d, "source": "FRED DFII10"}
    except Exception as e:
        log("FRED DFII10 failed:", repr(e))
    return cur


def signal_block(known, x_col, x_now, fitted_now, rate_now, horizon_months):
    """Spread statistics shared by the US and China pages."""
    err = known.excess - known.spread
    sigma = float(err.std(ddof=1))
    now = fitted_now - rate_now
    n_eff = len(known) / horizon_months
    dof = max(1.0, n_eff - 2)
    z = (x_now - known[x_col].mean()) / known[x_col].std(ddof=1)
    sigma_t = sigma * np.sqrt(1 + (1 + z * z) / n_eff)
    edges = [-np.inf, -4, -2, 0, 2, 4, 6, np.inf]
    buckets = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        g = known[(known.spread >= lo) & (known.spread < hi)]
        if g.empty:
            continue
        label = f"< {hi:g}" if lo == -np.inf else (f"≥ {lo:g}" if hi == np.inf else f"{lo:g} ~ {hi:g}")
        buckets.append({"bucket": label, "lo": None if lo == -np.inf else lo, "hi": None if hi == np.inf else hi,
                        "n": int(len(g)), "p_lose": float((g.excess < 0).mean()), "median_excess": float(g.excess.median()),
                        "first_year": int(g.index.year.min()), "last_year": int(g.index.year.max())})
    signal = {"sigma": sigma, "now_spread": float(now), "p_now": float(stats.norm.cdf(-now / sigma)),
              "cut75": float(stats.norm.ppf(0.25) * sigma), "cut90": float(stats.norm.ppf(0.10) * sigma),
              "n_eff": float(n_eff), "df": float(dof), "sigma_t": float(sigma_t),
              "p_now_t": float(stats.t.cdf(-now / sigma_t, dof)),
              "cut75_t": float(stats.t.ppf(0.25, dof) * sigma_t), "cut90_t": float(stats.t.ppf(0.10, dof) * sigma_t)}
    return signal, buckets


def analyse_us(m, cur):
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
    signal, buckets = signal_block(h.dropna(subset=["excess"]), "cape", cape_now, fitted_now, y10_now, 120)
    fit = {"from": fs, "n": int(len(fit_rows)), "slope": float(slope), "intercept": float(intercept), "r2": r2,
           "x_now": cape_now, "fitted_now": float(fitted_now)}
    points = [{"m": i.strftime("%Y-%m"), "x": rnd(r.cape), "y": rnd(r.r10)} for i, r in rows.iterrows()]
    series = [{"m": i.strftime("%Y-%m"), "s": rnd(r.spread), "e": rnd(r.excess)} for i, r in h.iterrows()]
    return {"fit": fit, "signal": signal, "buckets": buckets, "points": points, "spread": series}


# ================================================================ US: ECY, Buffett, risk panel
def pct_rank(s, v):
    return float((s < v).mean())


def ecy_block(m, cur):
    """Excess CAPE yield = 1/CAPE - real 10y yield (TIPS from 2003, before: 10y yield - past 10y inflation)."""
    if not cur.get("real10"):
        return None
    tips = fred_hist("DFII10", start="2003-01-01", freq="m")
    tips.index = tips.index.to_period("M").to_timestamp()
    d = m.loc[CFG["history_start"]:, ["cape", "y10", "infl10"]].copy()
    d["real"] = d.y10 - d.infl10
    d["tips"] = tips.reindex(d.index)
    d.loc[d.tips.notna(), "real"] = d.tips
    d = d.dropna(subset=["cape", "real"])
    d["ecy"] = 100 / d.cape - d.real
    now = 100 / cur["cape"]["value"] - cur["real10"]["value"]
    return {"series": [{"m": i.strftime("%Y-%m"), "v": rnd(r.ecy), "t": bool(pd.notna(r.tips))} for i, r in d.iterrows()],
            "now": now, "earn_yield": 100 / cur["cape"]["value"], "real_now": cur["real10"]["value"],
            "real_asof": cur["real10"]["as_of"], "median": float(d.ecy.median()), "pct": pct_rank(d.ecy, now)}


def buffett_block():
    eq = fred_hist("NCBEILQ027S", start="1945-01-01")  # millions of USD, quarterly
    gdp = fred_hist("GDP", start="1945-01-01")         # billions of USD, quarterly (SAAR)
    r = (eq / 1000 / gdp.reindex(eq.index) * 100).dropna()
    r = r.loc["1951":]
    last = r.index[-1]
    return {"series": [{"m": i.strftime("%Y-%m"), "v": rnd(v, 1)} for i, v in r.items()],
            "now": float(r.iloc[-1]), "as_of": f"{last.year}Q{(last.month - 1) // 3 + 1}",
            "median": float(r.median()), "pct": pct_rank(r, r.iloc[-1]), "max": float(r.max()), "max_at": f"{r.idxmax().year}"}


def risk_panel():
    out = {}
    try:  # high-yield credit spread
        h = fred_hist("BAMLH0A0HYM2", start="1996-12-01", freq="m")
        lat = fred_latest("BAMLH0A0HYM2", n=70)
        dd, v = lat[0]
        v3 = lat[-1][1] if len(lat) >= 60 else v  # ~3 months of trading days ago
        rise = v - v3
        lvl = "red" if v >= 6 or rise >= 1.5 else ("yellow" if v >= 4.5 or rise >= 0.75 else "green")
        out["hy"] = {"series": [{"m": i.strftime("%Y-%m"), "v": rnd(x)} for i, x in h.items()], "now": v, "as_of": dd,
                     "chg3m": rise, "level": lvl, "unit": "%"}
    except Exception as e:
        log("risk HY failed:", repr(e))
    try:  # yield curve 10y - 2y
        h = fred_hist("T10Y2Y", start="1976-06-01", freq="m")
        dd, v = fred_latest("T10Y2Y")[0]
        inverted_recently = bool((h.iloc[-24:] < 0).any())
        lvl = "red" if v < 0 else ("yellow" if v < 0.5 or inverted_recently else "green")
        out["curve"] = {"series": [{"m": i.strftime("%Y-%m"), "v": rnd(x)} for i, x in h.items()], "now": v, "as_of": dd,
                        "inv24": inverted_recently, "level": lvl, "unit": "pp"}
    except Exception as e:
        log("risk curve failed:", repr(e))
    try:  # Sahm rule (real-time)
        h = fred_hist("SAHMREALTIME", start="1959-12-01")
        v, dd = float(h.iloc[-1]), h.index[-1].strftime("%Y-%m")
        lvl = "red" if v >= 0.5 else ("yellow" if v >= 0.3 else "green")
        out["sahm"] = {"series": [{"m": i.strftime("%Y-%m"), "v": rnd(x)} for i, x in h.items()], "now": v, "as_of": dd,
                       "level": lvl, "unit": "pp"}
    except Exception as e:
        log("risk Sahm failed:", repr(e))
    return out or None


# ================================================================ China: CSI 300
CSI_URLS = [
    "https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/{kind}/000300{kind}.xls",
    "https://csi-web-dev.oss-cn-shanghai-finance-1-pub.aliyuncs.com/static/html/csindex/public/uploads/file/autofile/{kind}/000300{kind}.xls",
]


def csindex_xls(kind):
    """kind = 'indicator' (PE, dividend yield; last ~20 trading days) or 'perf' (prices)."""
    last = None
    for u in CSI_URLS:
        try:
            r = requests.get(u.format(kind=kind), headers=BROWSER, timeout=40)
            r.raise_for_status()
            return pd.read_excel(BytesIO(r.content))
        except Exception as e:
            last = e
    raise RuntimeError(f"csindex {kind} unavailable: {last!r}")


def pick(df, *keys):
    for c in df.columns:
        if any(k in str(c) for k in keys):
            return c
    raise KeyError(keys)


def cn_live():
    """Latest CSI 300 PE, dividend yield, close and China 10y yield. Each part may be None."""
    snap = {}
    try:
        ind = csindex_xls("indicator")
        dc = pick(ind, "日期", "Date")
        ind[dc] = pd.to_datetime(ind[dc].astype(str))
        last = ind.sort_values(dc).iloc[-1]
        snap["date"] = last[dc].strftime("%Y-%m-%d")
        snap["pe_raw"] = float(last[pick(ind, "市盈率2", "P/E2")])
        snap["dy_raw"] = float(last[pick(ind, "股息率2", "D/P2")])
        log("csindex indicator:", snap)
    except Exception as e:
        log("csindex indicator failed:", repr(e))
    try:  # close from Yahoo (reachable from GitHub runners); csindex perf as fallback
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/000300.SS",
                         params={"range": "1mo", "interval": "1d"}, headers=BROWSER, timeout=30)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        closes = [(t, c) for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]) if c]
        t, c = closes[-1]
        snap["close"], snap["close_date"] = float(c), dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d")
    except Exception as e:
        log("Yahoo 000300.SS failed:", repr(e))
        try:
            perf = csindex_xls("perf")
            dc = pick(perf, "日期", "Date")
            perf[dc] = pd.to_datetime(perf[dc].astype(str))
            last = perf.sort_values(dc).iloc[-1]
            snap["close"], snap["close_date"] = float(last[pick(perf, "收盘", "Close")]), last[dc].strftime("%Y-%m-%d")
        except Exception as e2:
            log("csindex perf failed:", repr(e2))
    try:  # China 10y government bond yield (Eastmoney data centre, the source akshare uses)
        r = requests.get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                         params={"reportName": "RPTA_WEB_TREASURYYIELD", "columns": "ALL", "sortColumns": "SOLAR_DATE",
                                 "sortTypes": "-1", "pageNumber": "1", "pageSize": "10"}, headers=BROWSER, timeout=30)
        r.raise_for_status()
        row = next(x for x in r.json()["result"]["data"] if x.get("EMM00166466") is not None)
        snap["y10"], snap["y10_date"] = float(row["EMM00166466"]), str(row["SOLAR_DATE"])[:10]
    except Exception as e:
        log("China 10y (eastmoney) failed:", repr(e))
    if CFG.get("cn_y10_override"):
        snap["y10"], snap["y10_date"] = float(CFG["cn_y10_override"]), "config.json"
    return snap


def cn_history():
    """Seed history in data/cn_csi300.csv, updated in place with this week's live values.
    The seed PE / dividend yield and the csindex file use slightly different definitions, so the first
    live value that lands in a month the seed already has fixes a splice ratio (data/cn_splice.json)."""
    path, spath = DATA_DIR / "cn_csi300.csv", DATA_DIR / "cn_splice.json"
    h = pd.read_csv(path, dtype={"month": str}).set_index("month")
    splice = json.loads(spath.read_text()) if spath.exists() else None
    snap = cn_live()
    stale = []
    month = (snap.get("date") or snap.get("close_date") or "")[:7]
    if month:
        if "pe_raw" in snap:
            if splice is None:
                # compare with the same month in the seed, or else its most recent month
                base = h.loc[month] if month in h.index else h.dropna(subset=["pe"]).iloc[-1]
                splice = {"pe": float(base.pe / snap["pe_raw"]) if base is not None and pd.notna(base.pe) else 1.0,
                          "dy": float(base.dy / snap["dy_raw"]) if base is not None and pd.notna(base.dy) else 1.0,
                          "set_on": month}
                spath.write_text(json.dumps(splice, indent=2))
                log("CSI300 splice ratios fixed:", splice)
            h.loc[month, "pe"] = round(snap["pe_raw"] * splice["pe"], 2)
            h.loc[month, "dy"] = round(snap["dy_raw"] * splice["dy"], 2)
        else:
            stale.append("pe")
        if "close" in snap:
            h.loc[month, "close"] = round(snap["close"], 2)
        if "y10" in snap:
            yd = snap.get("y10_date", "")
            h.loc[yd[:7] if yd[:4].isdigit() else month, "y10"] = round(snap["y10"], 4)
    else:
        stale.append("all")
    h = h.sort_index()
    h.to_csv(path)
    return h, snap, stale


def analyse_cn():
    h, snap, stale = cn_history()
    h.index = pd.to_datetime(h.index + "-01")
    h = h.dropna(subset=["close"])
    dy = h.dy.ffill()
    r = (h.close.shift(-1) / h.close + dy / 100 / 12)
    tr = r.cumprod().shift(1).fillna(1.0)
    h["r5"] = ((tr.shift(-60) / tr) ** 0.2 - 1) * 100
    h["y10f"] = h.y10.ffill()
    rows = h.dropna(subset=["pe", "r5"])
    slope, intercept = np.polyfit(rows.pe, rows.r5, 1)
    r2 = float(np.corrcoef(rows.pe, rows.r5)[0, 1] ** 2)
    last = h.dropna(subset=["pe"]).iloc[-1]
    pe_now, y_now = float(last.pe), float(h.y10f.iloc[-1])
    fitted_now = slope * pe_now + intercept
    s = h.dropna(subset=["pe", "y10f"]).copy()
    s["spread"] = slope * s.pe + intercept - s.y10f
    s["excess"] = s.r5 - s.y10f
    signal, buckets = signal_block(s.dropna(subset=["excess"]), "pe", pe_now, fitted_now, y_now, 60)
    y10_month = h.dropna(subset=["y10"]).index[-1].strftime("%Y-%m")
    return {
        "fit": {"from": rows.index[0].strftime("%Y-%m"), "n": int(len(rows)), "slope": float(slope),
                "intercept": float(intercept), "r2": r2, "x_now": pe_now, "fitted_now": float(fitted_now)},
        "signal": signal, "buckets": buckets,
        "points": [{"m": i.strftime("%Y-%m"), "x": rnd(rr.pe), "y": rnd(rr.r5)} for i, rr in rows.iterrows()],
        "spread": [{"m": i.strftime("%Y-%m"), "s": rnd(rr.spread), "e": rnd(rr.excess)} for i, rr in s.iterrows()],
        "current": {"pe": {"value": pe_now, "as_of": snap.get("date") or last.name.strftime("%Y-%m")},
                    "dy": {"value": float(last.dy), "as_of": snap.get("date") or last.name.strftime("%Y-%m")},
                    "y10": {"value": y_now, "as_of": snap.get("y10_date") or y10_month},
                    "pe_pct": pct_rank(h.pe.dropna(), pe_now)},
        "stale": stale,
    }


# ================================================================ output
def safe(name, fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        log(f"{name} skipped:", repr(e))
        return None


def main():
    m = monthly_frame(load_shiller())
    ext = safe("recent CAPE", extend_recent, m)
    m = ext if ext is not None else m
    cur = current_levels(m)
    res = analyse_us(m, cur)
    res["current"] = cur
    if FRED_KEY:
        res["ecy"] = safe("ECY", ecy_block, m, cur)
        res["buffett"] = safe("Buffett indicator", buffett_block)
        res["risk"] = safe("risk panel", risk_panel)
    res["cn"] = safe("China CSI300", analyse_cn)
    res["built_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    f, s = res["fit"], res["signal"]
    log(f"US fit since {f['from']}: slope {f['slope']:.3f}, R2 {f['r2']:.2f}; CAPE {cur['cape']['value']}, "
        f"10y {cur['y10']['value']}, spread {s['now_spread']:.2f}")
    if res["cn"]:
        c = res["cn"]
        log(f"CN PE {c['current']['pe']['value']}, y10 {c['current']['y10']['value']}, spread {c['signal']['now_spread']:.2f}")

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
