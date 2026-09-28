#!/usr/bin/env python3
"""
Thanos X 雷達 v0.3
------------------
每個美股交易日收盤後，由 GitHub Actions 自動執行：
  1. 掃描兩種進場模板
     A 隔日延續：事件跳空當天收強，隔日開盤進場
     B 回測進場：事件後 3–20 個交易日內拉回、未破 E 日低點、收盤站回前日高點
  2. 依規則排序，每日最多 3 檔，同族群只取一檔
  3. 財報前記錄選擇權隱含波動（只記錄、不過濾），事件後算出「跳空 ÷ 預期波動」
  4. 更新候選池資料，供網站比對你的持倉
  5. 自動紙上模擬：系統帳、模板對照、入選／落選對照、三本影子帳

輸出：docs/data/radar.json、history.json、universe.json、implied_moves.json
回顧測試：python radar.py --backfill 120 → docs/data/backfill.json
規則來源：Thanos X 規格書 v0.3
"""
import datetime as dt
import json
import os
import sys
import time
import traceback
from io import StringIO

import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:  # 本機測試時可無
    yf = None

# ------------------------------------------------------------------
# 參數：規格書【預設】值。紙上階段一律不改。
# ------------------------------------------------------------------
CFG = {
    # 事件篩選（S1–S6）
    "gap_min": 0.05, "vol_mult": 3.0, "close_pos_min": 0.70, "gap_hold_min": 0.03,
    "price_min": 10.0, "dollar_vol_min": 50e6,
    # 進場
    "limit_atr_a": 1.0,       # A：限價 = E 日收盤 + 1 ATR（v0.2 為 0.3）
    "limit_atr_b": 0.5,       # B：限價 = 訊號日收盤 + 0.5 ATR
    "b_min_days": 3,          # B：E 日後第 3 個交易日起
    "b_max_days": 20,         # B：到第 20 個交易日止
    # 停損與部位
    "stop_min_pct": 0.04, "stop_max_pct": 0.12, "buffer_atr": 0.5,
    # 出場
    "t1_r": 2.0, "trail_ma": 10,
    "stall_days": 10,         # E4（v0.2 為 5）
    "max_hold": 20,           # E5（v0.2 為 10）
    "earn_exit_days": 2, "earn_exclude_days": 10,
    # 數量與風險
    "max_candidates": 3, "max_positions": 5, "max_open_risk_r": 5.0,
    "vix_half": 25.0, "vix_stop": 35.0,
    "fee": 0.003,
    "pool_days": 75,          # 候選池保留天數（日曆日）
}

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "docs", "data")
P = lambda name: os.path.join(DATA, name)
INDEX_TICKERS = ("SPY", "^VIX")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*"}


def r(x, n=4):
    return None if x is None else round(float(x), n)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def tdays_after(a, b):
    """a 之後到 b（含）之間的交易日數（以週一至五估算，不含假日）。"""
    a, b = pd.Timestamp(a), pd.Timestamp(b)
    if b <= a:
        return 0
    return len(pd.bdate_range(a + pd.Timedelta(days=1), b))


def now_utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------
# 掃描範圍：全美股 → 股價 ≥ 10、日均成交額 ≥ 5,000 萬（每週更新一次）
# ------------------------------------------------------------------
NASDAQ_FILES = [
    ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "Symbol"),
    ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", "ACT Symbol"),
]
WIKI_SOURCES = [
    "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "https://en.wikipedia.org/wiki/Nasdaq-100",
]


def listed_symbols():
    syms = set()
    for url, col in NASDAQ_FILES:
        try:
            df = pd.read_csv(StringIO(requests.get(url, headers=UA, timeout=30).text), sep="|")
            df = df[df[col].notna()]
            for flag in ("Test Issue", "ETF"):
                if flag in df.columns:
                    df = df[df[flag] != "Y"]
            for s in df[col].astype(str):
                if any(ch in s for ch in "$^+=/ "):
                    continue  # 特別股、權證單位、檔尾說明列
                syms.add(s.replace(".", "-"))
        except Exception as e:
            print(f"[warn] 上市清單讀取失敗 {url}: {e}")
    return syms


def index_symbols():
    """備援：指數成分股（v0.2 的範圍）。"""
    syms = set()
    for url in WIKI_SOURCES:
        try:
            html = requests.get(url, headers=UA, timeout=30).text
            for table in pd.read_html(StringIO(html)):
                cols = [c for c in table.columns if str(c).strip() in ("Symbol", "Ticker", "Ticker symbol")]
                if cols and len(table) >= 90:
                    syms.update(table[cols[0]].astype(str).str.strip().str.replace(".", "-"))
                    break
        except Exception as e:
            print(f"[warn] 指數清單讀取失敗 {url}: {e}")
    return {s for s in syms if s and s.lower() != "nan"}


def load_universe(force=False):
    cache = load_json(P("universe.json"), {})
    fresh = cache.get("updated") and (dt.date.today() - dt.date.fromisoformat(cache["updated"])).days < 7
    if fresh and not force and len(cache.get("tickers", [])) >= 300:
        return cache["tickers"]
    syms = listed_symbols()
    source = "全美股上市清單"
    if len(syms) < 3000:
        print(f"[warn] 上市清單只有 {len(syms)} 檔，改用指數成分股")
        syms, source = index_symbols(), "指數成分股（備援）"
    frames = download(sorted(syms), period="1mo", min_rows=15, pause=1.0)
    liquid = []
    for t, d in frames.items():
        tail = d.iloc[-20:]
        if float(tail["Close"].iloc[-1]) >= CFG["price_min"] and \
                float((tail["Close"] * tail["Volume"]).mean()) >= CFG["dollar_vol_min"]:
            liquid.append(t)
    if len(liquid) < 300:
        print(f"[warn] 流動性篩選後只剩 {len(liquid)} 檔，沿用舊清單")
        return cache.get("tickers") or sorted(syms)
    save_json(P("universe.json"), {"updated": str(dt.date.today()), "source": source,
                                   "listed": len(syms), "tickers": sorted(liquid)})
    print(f"掃描範圍更新：{source} {len(syms)} 檔 → 流動性合格 {len(liquid)} 檔")
    return sorted(liquid)


def download(tickers, period="4mo", min_rows=30, pause=0.0):
    frames = {}
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        try:
            df = yf.download(chunk, period=period, interval="1d", group_by="ticker",
                             auto_adjust=False, actions=True, threads=True, progress=False)
        except Exception as e:
            print(f"[warn] 下載失敗 {i}: {e}")
            continue
        for t in chunk:
            try:
                d = df[t] if isinstance(df.columns, pd.MultiIndex) else df
                keep = [c for c in ("Open", "High", "Low", "Close", "Volume", "Stock Splits") if c in d.columns]
                d = d[keep].dropna(subset=["Open", "High", "Low", "Close"])
                if len(d) >= min_rows:
                    d.index = pd.DatetimeIndex(d.index).tz_localize(None).normalize()
                    frames[t] = d
            except Exception:
                pass
        if pause:
            time.sleep(pause)
    return frames


_META = {}


def ticker_meta(ticker):
    """族群與財報日清單（每檔只抓一次）。抓不到就留空，網站會提示手動確認。"""
    if ticker in _META:
        return _META[ticker]
    out = {"sector": None, "earn_dates": []}
    if yf is not None:
        try:
            tk = yf.Ticker(ticker)
            try:
                out["sector"] = (tk.info or {}).get("sector")
            except Exception:
                pass
            try:
                ed = tk.get_earnings_dates(limit=12)
                if ed is not None and len(ed):
                    out["earn_dates"] = sorted({str(pd.Timestamp(x).tz_localize(None).date()) for x in ed.index})
            except Exception:
                pass
        except Exception:
            pass
    _META[ticker] = out
    return out


def enrich(ticker, session):
    m = ticker_meta(ticker)
    fut = [x for x in m["earn_dates"] if pd.Timestamp(x) > pd.Timestamp(session)]
    return {"sector": m["sector"], "next_earnings": fut[0] if fut else None}


# ------------------------------------------------------------------
# 預期波動：財報前以價平跨式估算（只記錄，不過濾；回顧測試無法取得）
# ------------------------------------------------------------------
def capture_implied_moves(session, frames, universe_set, max_n=80):
    store = load_json(P("implied_moves.json"), {})
    days = [session] + [d.date() for d in pd.bdate_range(pd.Timestamp(session) + pd.Timedelta(days=1), periods=2)]
    reporters = set()
    for day in days:
        try:
            js = requests.get(f"https://api.nasdaq.com/api/calendar/earnings?date={day}", headers=UA, timeout=30).json()
            for row in (js.get("data") or {}).get("rows") or []:
                sym = str(row.get("symbol", "")).replace(".", "-")
                if sym in universe_set:
                    reporters.add((sym, str(day)))
        except Exception as e:
            print(f"[warn] 財報日曆讀取失敗 {day}: {e}")
    todo = sorted(((s, d) for s, d in reporters if f"{s}|{d}" not in store),
                  key=lambda x: -float((frames[x[0]]["Close"] * frames[x[0]]["Volume"]).iloc[-20:].mean())
                  if x[0] in frames else 0)[:max_n]
    got = 0
    for sym, ed in todo:
        try:
            tk = yf.Ticker(sym)
            exp = next((e for e in tk.options if e > ed), None)
            if not exp or sym not in frames:
                continue
            spot = float(frames[sym]["Close"].iloc[-1])
            chain = tk.option_chain(exp)

            def mid(df):
                row = df.iloc[(df["strike"] - spot).abs().argsort()[:1]].iloc[0]
                bid, ask = float(row.get("bid") or 0), float(row.get("ask") or 0)
                return (bid + ask) / 2 if bid > 0 and ask > 0 else float(row.get("lastPrice") or 0)

            im = (mid(chain.calls) + mid(chain.puts)) / spot
            if im > 0:
                store[f"{sym}|{ed}"] = {"ticker": sym, "earnings_date": ed, "captured": str(session),
                                        "expiry": exp, "spot": r(spot, 2), "implied_move_pct": r(im * 100, 2)}
                got += 1
        except Exception:
            pass
    cutoff = str(pd.Timestamp(session).date() - dt.timedelta(days=90))
    store = {k: v for k, v in store.items() if v["earnings_date"] >= cutoff}
    save_json(P("implied_moves.json"), store)
    print(f"預期波動：待記錄 {len(todo)} 檔，成功 {got} 檔")
    return store


def tag_surprise(c, store):
    """E 日跳空 ÷ 財報前預期波動。財報日可能是 E 日（盤前）或 E 日前一天（盤後）。"""
    e = pd.Timestamp(c["e_date"])
    hits = [v for v in store.values() if v["ticker"] == c["ticker"]
            and e - pd.Timedelta(days=4) <= pd.Timestamp(v["earnings_date"]) <= e]
    if hits:
        v = max(hits, key=lambda x: x["earnings_date"])
        c["implied_move_pct"] = v["implied_move_pct"]
        c["surprise_ratio"] = r(abs(c["gap_pct"]) / v["implied_move_pct"], 2) if v["implied_move_pct"] else None


# ------------------------------------------------------------------
# 指標與掃描
# ------------------------------------------------------------------
def atr_series(d, n=14):
    pc = d["Close"].shift(1)
    tr = pd.concat([d["High"] - d["Low"], (d["High"] - pc).abs(), (d["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def regime(frames, upto=None):
    if upto is not None:
        frames = {k: frames[k][frames[k].index <= pd.Timestamp(upto)] for k in INDEX_TICKERS if k in frames}
    spy = frames["SPY"]["Close"]
    ma50 = spy.rolling(50).mean()
    s, m = float(spy.iloc[-1]), float(ma50.iloc[-1])
    vix = float(frames["^VIX"]["Close"].iloc[-1]) if "^VIX" in frames else None
    level = "normal"
    if vix is not None and vix > CFG["vix_stop"]:
        level = "stop"
    elif vix is not None and vix > CFG["vix_half"]:
        level = "half"
    return {"spy_close": r(s, 2), "spy_ma50": r(m, 2), "spy_above_ma50": bool(s > m),
            "vix": r(vix, 2), "vix_level": level}


def entry_levels(ref_close, atr, e_low, limit_atr):
    limit = ref_close + limit_atr * atr
    stop = min(e_low, limit * (1 - CFG["stop_min_pct"]))
    dist = (limit - stop) / limit
    return {"limit": r(limit), "stop_ref": r(stop), "risk_ref": r(limit - stop),
            "t1_ref": r(limit + CFG["t1_r"] * (limit - stop)), "stop_dist_pct": r(dist * 100, 2)}, dist


def scan_events(frames, session):
    """事件日掃描：做多事件（模板 A 候選＋B 的事件池）與放空觀察。"""
    events, shorts = [], []
    for t, d in frames.items():
        if t in INDEX_TICKERS or len(d) < 25 or d.index[-1].date() != session:
            continue
        last = d.iloc[-1]
        if "Stock Splits" in d.columns and float(last.get("Stock Splits") or 0) != 0:
            continue
        o, h, l, c, v = (float(last[k]) for k in ("Open", "High", "Low", "Close", "Volume"))
        pc = float(d["Close"].iloc[-2])
        hist = d.iloc[-21:-1]
        avgv = float(hist["Volume"].mean())
        dv = float((hist["Close"] * hist["Volume"]).mean())
        if avgv <= 0 or h <= l or pc <= 0:
            continue
        gap, vm, pos, hold = o / pc - 1, v / avgv, (c - l) / (h - l), c / pc - 1
        if c < CFG["price_min"] or dv < CFG["dollar_vol_min"] or vm < CFG["vol_mult"] or abs(gap) < CFG["gap_min"]:
            continue
        a = float(atr_series(d.iloc[-30:]).iloc[-1])
        if not a > 0:
            continue
        base = {"ticker": t, "e_date": str(session), "gap_pct": r(gap * 100, 2), "vol_mult": r(vm, 2),
                "close_pos": r(pos, 2), "hold_pct": r(hold * 100, 2), "e_open": r(o), "e_high": r(h),
                "e_low": r(l), "e_close": r(c), "atr14": r(a), "range_pct": r((h - l) / c * 100, 2)}
        if gap >= CFG["gap_min"] and pos >= CFG["close_pos_min"] and hold >= CFG["gap_hold_min"]:
            ev = dict(base)
            if gap >= 0.08 and (h - l) / c < 0.015:
                ev["x1"] = True
            events.append(ev)
        elif gap <= -CFG["gap_min"] and (1 - pos) >= CFG["close_pos_min"] and hold <= -CFG["gap_hold_min"]:
            shorts.append(dict(base, side="short_watch"))
    return events, shorts


def candidates_a(events):
    out = []
    for ev in events:
        lv, dist = entry_levels(ev["e_close"], ev["atr14"], ev["e_low"], CFG["limit_atr_a"])
        c = dict(ev, template="A", signal_date=ev["e_date"], side="long", **lv)
        c.pop("x1", None)
        if ev.get("x1"):
            c["excluded"] = "X1 疑似現金併購（跳空後區間極窄）"
        elif dist > CFG["stop_max_pct"]:
            c["excluded"] = "X3 停損距離超過 12%"
        out.append(c)
    return out


def candidates_b(event_pool, frames, session):
    """模板 B：事件後拉回、未破 E 日低點、收盤站回前日高點。每個事件只觸發一次。"""
    out, ts = [], pd.Timestamp(session)
    for ev in event_pool:
        if ev.get("x1") or ev.get("b_state"):
            continue
        d = frames.get(ev["ticker"])
        if d is None or d.index[-1] != ts:
            continue
        after = d[d.index > pd.Timestamp(ev["e_date"])]
        k = len(after)
        if k < CFG["b_min_days"]:
            continue
        if k > CFG["b_max_days"]:
            ev["b_state"] = "expired"
            continue
        closes = after["Close"]
        if float(closes.min()) < ev["e_low"]:
            ev["b_state"] = "broken"
            continue
        pulled_back = float(closes.iloc[:-1].min()) < ev["e_close"]
        reclaim = float(closes.iloc[-1]) > float(after["High"].iloc[-2])
        if not (pulled_back and reclaim):
            continue
        a = float(atr_series(d.iloc[-30:]).iloc[-1])
        sc = float(closes.iloc[-1])
        lv, dist = entry_levels(sc, a, ev["e_low"], CFG["limit_atr_b"])
        c = {k2: ev[k2] for k2 in ("ticker", "e_date", "gap_pct", "vol_mult", "e_high", "e_low", "e_close")}
        c.update(template="B", signal_date=str(session), side="long", days_after_event=k,
                 s_close=r(sc), atr14=r(a), **lv)
        if dist > CFG["stop_max_pct"]:
            c["excluded"] = "X3 停損距離超過 12%"
        ev["b_state"] = "triggered"
        out.append(c)
    return out


def rank_candidates(cands, reg, session, implied_store=None):
    size = 0.5 if (not reg["spy_above_ma50"] or reg["vix_level"] == "half") else 1.0
    blocked = reg["vix_level"] == "stop"
    for c in cands:
        if implied_store and c["template"] == "A":
            tag_surprise(c, implied_store)
        if c.get("excluded"):
            continue
        c.update(enrich(c["ticker"], session))
        flags = ["請手動確認：非現金併購標的"]
        if c.get("next_earnings"):
            if tdays_after(session, c["next_earnings"]) <= CFG["earn_exclude_days"]:
                c["excluded"] = f"X2 {c['next_earnings']} 有財報"
        else:
            flags.append("財報日未知，請手動確認")
        if not reg["spy_above_ma50"]:
            flags.append("大盤在 50 日均線下，0.5R")
        if reg["vix_level"] == "half":
            flags.append("VIX > 25，0.5R")
        if blocked:
            flags.append("VIX > 35，停止新進場")
        c["flags"], c["size_factor"], c["blocked"] = flags, size, blocked
    # 排序：A（新事件）優先，其次 B；各自依事件成交量倍數
    eligible = sorted([c for c in cands if not c.get("excluded")],
                      key=lambda c: (0 if c["template"] == "A" else 1, -c["vol_mult"]))
    seen_sec, seen_tk, rank = set(), set(), 0
    for c in eligible:
        sec = c.get("sector")
        ok = rank < CFG["max_candidates"] and (sec is None or sec not in seen_sec) and c["ticker"] not in seen_tk
        if ok:
            rank += 1
            c["selected"], c["rank"] = True, rank
            seen_tk.add(c["ticker"])
            if sec:
                seen_sec.add(sec)
        else:
            c["selected"] = False
            c["shadow_reason"] = "超過每日上限" if rank >= CFG["max_candidates"] else "同族群或同標的已有更高排名"
    return cands


def process_session(hist, frames, session, reg, implied_store=None):
    """單一交易日的完整掃描（每日執行與回顧測試共用）。"""
    events, shorts = scan_events(frames, session)
    cands = candidates_a(events) + candidates_b(hist["events"], frames, session)
    cands = rank_candidates(cands, reg, session, implied_store)
    hist["events"].extend(events)
    cutoff = pd.Timestamp(session) - pd.Timedelta(days=45)
    hist["events"] = [e for e in hist["events"] if pd.Timestamp(e["e_date"]) >= cutoff]
    hist["candidates"].extend(cands)
    hist["short_watch"] = sorted(shorts, key=lambda s: -s["vol_mult"])[:CFG["max_candidates"]]
    return cands, shorts


# ------------------------------------------------------------------
# 紙上模擬（規格書第 6 節：收盤判斷、隔日開盤執行）
# ------------------------------------------------------------------
def simulate(c, d, chase=False, no_stall=False):
    after = d[d.index > pd.Timestamp(c["signal_date"])]
    if len(after) == 0:
        return {"status": "waiting"}
    entry_date = str(after.index[0].date())
    o = float(after["Open"].iloc[0])
    if not chase and o > c["limit"]:
        return {"status": "no_fill", "entry_date": entry_date, "open": r(o)}
    entry = o
    stop = min(c["e_low"], entry * (1 - CFG["stop_min_pct"]))
    risk = entry - stop
    unit = risk + CFG["buffer_atr"] * c["atr14"]
    t1 = entry + CFG["t1_r"] * risk
    ma = d["Close"].rolling(CFG["trail_ma"]).mean()
    rem, t1_hit, cur_stop, pending = 1.0, False, stop, None
    fills, t1_date, exit_i = [], None, None
    for i, day in enumerate(after.index):
        row = after.iloc[i]
        if pending is not None:
            frac, reason = pending
            fills.append({"date": str(day.date()), "frac": frac, "price": r(float(row["Open"])), "reason": reason})
            rem -= frac
            if reason.startswith("E2"):
                cur_stop, t1_date = max(cur_stop, entry), str(day.date())
            pending = None
            if rem <= 1e-9:
                exit_i = i
                break
        close, held = float(row["Close"]), i + 1
        ne = c.get("next_earnings")
        if close < cur_stop:
            pending = (rem, "E1 保本停損" if t1_hit else "E1 停損")
        elif ne and tdays_after(day, ne) <= CFG["earn_exit_days"]:
            pending = (rem, "E6 財報將近")
        elif held >= CFG["max_hold"]:
            pending = (rem, "E5 持有期滿")
        elif not no_stall and held >= CFG["stall_days"] and not t1_hit and close < entry:
            pending = (rem, "E4 停滯")
        elif not t1_hit and close >= t1:
            pending, t1_hit = (rem / 2, "E2 第一目標"), True
        elif t1_hit and pd.notna(ma.loc[day]) and close < float(ma.loc[day]):
            pending = (rem, "E3 跌破 10 日均線")
    gross = sum(f["frac"] * (f["price"] - entry) for f in fills) / unit
    fees = sum(f["frac"] * CFG["fee"] * (f["price"] + entry) for f in fills) / unit
    res = {"entry_date": entry_date, "entry": r(entry), "stop": r(stop), "t1": r(t1), "unit": r(unit),
           "fills": fills, "t1_date": t1_date, "realized_r": r(gross - fees, 3)}
    if rem <= 1e-9:
        res.update(status="closed", exit_date=fills[-1]["date"], pnl_r=res["realized_r"],
                   exit_reason=fills[-1]["reason"], hold_days=exit_i)
    else:
        last = float(after["Close"].iloc[-1])
        res.update(status="open", remaining=r(rem, 2), last_close=r(last), stop_now=r(cur_stop),
                   unrealized_r=r(rem * (last - entry) / unit, 3), held_days=len(after),
                   next_action=pending[1] if pending else None)
    return res


def stats(trades):
    closed = [t for t in trades if t.get("status") == "closed"]
    out = {"closed": len(closed), "open": sum(t.get("status") == "open" for t in trades),
           "no_fill": sum(t.get("status") == "no_fill" for t in trades),
           "skipped_capacity": sum(t.get("status") == "skipped" for t in trades)}
    if closed:
        p = [t["pnl_r"] for t in closed]
        w, lo = [x for x in p if x > 0], [x for x in p if x <= 0]
        hd = [t["hold_days"] for t in closed if t.get("hold_days") is not None]
        out.update(win_rate=r(len(w) / len(p), 3), avg_r=r(sum(p) / len(p), 3), total_r=r(sum(p), 2),
                   avg_win=r(sum(w) / len(w), 3) if w else None, avg_loss=r(sum(lo) / len(lo), 3) if lo else None,
                   avg_hold_days=r(sum(hd) / len(hd), 1) if hd else None)
    return out


def key(c):
    return (c["ticker"], c["template"], c["signal_date"])


def run_paper(hist, frames):
    allc = hist["candidates"]
    elig = [c for c in allc if not c.get("excluded")]
    sims = {}
    for c in elig:
        d = frames.get(c["ticker"])
        if d is not None:
            sims[key(c)] = simulate(c, d)

    # 系統帳：入選者，套用持倉上限、風險上限、同標的不重複
    book = []
    for c in sorted([c for c in elig if c.get("selected") and not c.get("blocked")],
                    key=lambda c: (c["signal_date"], c.get("rank", 9))):
        s = sims.get(key(c))
        if not s:
            continue
        t = dict(s, ticker=c["ticker"], template=c["template"], e_date=c["e_date"],
                 signal_date=c["signal_date"], size=c.get("size_factor", 1.0))
        if s["status"] in ("open", "closed"):
            D = s["entry_date"]
            held = [b for b in book if b["status"] in ("open", "closed") and b["entry_date"] <= D
                    and (b["status"] == "open" or b["exit_date"] > D)]
            risk = sum(b["size"] for b in held if not (b.get("t1_date") and b["t1_date"] <= D))
            if len(held) >= CFG["max_positions"] or risk + t["size"] > CFG["max_open_risk_r"] \
                    or any(b["ticker"] == c["ticker"] for b in held):
                t = {"ticker": c["ticker"], "template": c["template"], "signal_date": c["signal_date"],
                     "status": "skipped", "size": t["size"]}
            elif s["status"] == "closed":
                t["pnl_r"] = r(s["pnl_r"] * t["size"], 3)
        book.append(t)
    filled = [b for b in book if b["status"] in ("open", "closed")]

    sel = [c for c in elig if c.get("selected")]
    pick = lambda cs: [sims[key(c)] for c in cs if key(c) in sims]
    sel_a, sel_b = [c for c in sel if c["template"] == "A"], [c for c in sel if c["template"] == "B"]

    # 影子帳一：未成交改以開盤價追進
    nf = [c for c in sel if sims.get(key(c), {}).get("status") == "no_fill"]
    shadow_chase = [simulate(c, frames[c["ticker"]], chase=True) for c in nf if c["ticker"] in frames]
    # 影子帳二：同一批入選者，關閉 E4
    shadow_nostall = [simulate(c, frames[c["ticker"]], no_stall=True) for c in sel if c["ticker"] in frames]
    # 影子帳三：因 X3 被排除者，若照做
    x3 = [c for c in allc if str(c.get("excluded", "")).startswith("X3")]
    shadow_x3 = [simulate(c, frames[c["ticker"]]) for c in x3 if c["ticker"] in frames]
    # 預期波動標籤（只有往前累積的資料才會有）
    tagged = [c for c in sel_a if c.get("surprise_ratio") is not None]
    return {
        "system": {"stats": stats(book), "filled_trades": len(filled),
                   "open": [b for b in book if b["status"] == "open"],
                   "recent_closed": [b for b in book if b["status"] == "closed"][-20:]},
        "compare": {
            "template_A": stats(pick(sel_a)), "template_B": stats(pick(sel_b)),
            "selected": stats(pick(sel)),
            "not_selected": stats(pick([c for c in elig if not c.get("selected")])),
        },
        "shadow": {
            "chase_no_fill": {"note": "開盤高於限價、未成交者，若以開盤價追進", **stats(shadow_chase)},
            "baseline_same_set": {"note": "入選者照現行規則（與下一列比較用）", **stats(pick(sel))},
            "no_stall": {"note": "同一批入選者，若不執行 E4 停滯出場", **stats(shadow_nostall)},
            "x3_excluded": {"note": "因停損距離 > 12% 被排除者，若照做", **stats(shadow_x3)},
        },
        "surprise_tag": {
            "note": "跳空 ÷ 財報前預期波動。只記錄、不過濾",
            "within_expected": stats(pick([c for c in tagged if c["surprise_ratio"] < 1])),
            "beyond_expected": stats(pick([c for c in tagged if c["surprise_ratio"] >= 1])),
        },
    }


# ------------------------------------------------------------------
# 候選池：網站用來比對你的真實持倉
# ------------------------------------------------------------------
def build_pool(hist, frames, session):
    cutoff = pd.Timestamp(session) - pd.Timedelta(days=CFG["pool_days"])
    pool = {}
    for c in sorted(hist["candidates"], key=lambda c: c["signal_date"]):
        if c.get("excluded") or not c.get("selected") or pd.Timestamp(c["signal_date"]) < cutoff:
            continue
        d = frames.get(c["ticker"])
        if d is None:
            continue
        pool[c["ticker"]] = {
            "last_date": str(d.index[-1].date()), "last_close": r(d["Close"].iloc[-1]),
            "ma10": r(d["Close"].rolling(CFG["trail_ma"]).mean().iloc[-1]),
            "atr14": r(atr_series(d.iloc[-30:]).iloc[-1]), "next_earnings": c.get("next_earnings"),
            "template": c["template"], "e_date": c["e_date"], "signal_date": c["signal_date"],
            "e_low": c["e_low"], "limit": c["limit"],
        }
    return pool


def frequency(book_filled, by_day, n_sessions):
    weeks = max(n_sessions / 5, 1)
    sel = sum(b["selected"] for b in by_day)
    return {"sessions": n_sessions, "days_with_candidate": sum(1 for b in by_day if b["selected"] > 0),
            "selected_total": sel, "selected_A": sum(b.get("selected_A", 0) for b in by_day),
            "selected_B": sum(b.get("selected_B", 0) for b in by_day),
            "selected_per_week": r(sel / weeks, 2),
            "filled_total": book_filled, "filled_per_week": r(book_filled / weeks, 2),
            "weeks_to_30_trades": r(30 / (book_filled / weeks), 1) if book_filled else None,
            "short_watch_total": sum(b["short_watch"] for b in by_day)}


# ------------------------------------------------------------------
# 回顧測試：用真實行情重播過去 N 個交易日。只寫 backfill.json，不碰每日紀錄。
# ------------------------------------------------------------------
def backfill(n_days):
    os.makedirs(DATA, exist_ok=True)
    universe = load_universe()
    frames = download(sorted(set(universe) | set(INDEX_TICKERS)), period="2y", pause=0.5)
    sessions = list(frames["SPY"].index[-n_days:])
    print(f"回顧測試：{len(universe)} 檔 × {len(sessions)} 個交易日")
    hist = {"events": [], "candidates": [], "short_watch": []}
    by_day = []
    for ts in sessions:
        day = ts.date()
        sub = {t: d[d.index <= ts] for t, d in frames.items() if t not in INDEX_TICKERS and len(d) and d.index[0] <= ts}
        cands, shorts = process_session(hist, sub, day, regime(frames, ts))
        by_day.append({"date": str(day), "passed": len(cands),
                       "selected": sum(1 for c in cands if c.get("selected")),
                       "selected_A": sum(1 for c in cands if c.get("selected") and c["template"] == "A"),
                       "selected_B": sum(1 for c in cands if c.get("selected") and c["template"] == "B"),
                       "short_watch": len(shorts)})
    paper = run_paper(hist, frames)
    out = {
        "version": "backfill-0.3", "status": "ok", "generated_at": now_utc(),
        "warning": ("倖存者偏差（用今日上市、今日流動性合格的股票回看過去）、樣本小；"
                    "且 v0.3 是看過 v0.2 回顧測試之後修改的，成績屬於樣本內，不具驗證力。"
                    "只用來確認雷達會動與候選頻率，不得作為跳過紙上階段或再次修改規則的依據。"),
        "range": [str(sessions[0].date()), str(sessions[-1].date())],
        "universe_size": len(universe),
        "frequency": frequency(paper["system"]["filled_trades"], by_day, len(sessions)),
        "paper": paper,
        "candidates": [{k: c.get(k) for k in ("ticker", "template", "e_date", "signal_date", "gap_pct", "vol_mult",
                                              "sector", "selected", "rank", "excluded", "shadow_reason")}
                       for c in hist["candidates"]],
        "by_day": by_day,
    }
    save_json(P("backfill.json"), out)
    f = out["frequency"]
    print(f"完成：入選 {f['selected_total']}（A {f['selected_A']}／B {f['selected_B']}），"
          f"成交 {f['filled_total']}，每週 {f['filled_per_week']} 筆")


# ------------------------------------------------------------------
def main():
    os.makedirs(DATA, exist_ok=True)
    hist = load_json(P("history.json"), {})
    hist.setdefault("events", [])
    hist.setdefault("candidates", [])
    hist.setdefault("short_watch", [])
    for c in hist["candidates"]:  # v0.2 舊紀錄相容
        c.setdefault("template", "A")
        c.setdefault("signal_date", c["e_date"])
    universe = load_universe()
    tickers = sorted(set(universe) | {c["ticker"] for c in hist["candidates"]} | set(INDEX_TICKERS))
    print(f"掃描範圍：{len(universe)} 檔")
    frames = download(tickers, pause=0.5)
    if "SPY" not in frames:
        raise RuntimeError("SPY 資料下載失敗，無法判斷交易日")
    session = frames["SPY"].index[-1].date()
    reg = regime(frames)
    implied = capture_implied_moves(session, frames, set(universe)) if yf is not None else {}

    if str(session) != hist.get("last_session"):
        cands, _ = process_session(hist, frames, session, reg, implied)
        hist["last_session"] = str(session)
        print(f"{session}：候選 {len(cands)} 檔，入選 {sum(c.get('selected', False) for c in cands)} 檔")
    else:
        print(f"{session} 已處理過，只更新候選池與模擬")

    today = [c for c in hist["candidates"] if c["signal_date"] == str(session)]
    out = {
        "version": "radar-0.3", "status": "ok", "generated_at": now_utc(),
        "session": str(session), "universe_size": len(universe), "config": CFG, "regime": reg,
        "today": {
            "candidates": sorted([c for c in today if c.get("selected")], key=lambda c: c["rank"]),
            "not_selected_count": sum(1 for c in today if not c.get("excluded") and not c.get("selected")),
            "excluded": [{"ticker": c["ticker"], "template": c["template"], "reason": c["excluded"]}
                         for c in today if c.get("excluded")],
            "short_watch": hist.get("short_watch", []),
        },
        "pool": build_pool(hist, frames, session),
        "paper": run_paper(hist, frames),
    }
    save_json(P("history.json"), hist)
    save_json(P("radar.json"), out)
    print("完成")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--backfill":
        backfill(int(sys.argv[2]))
        sys.exit(0)
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        os.makedirs(DATA, exist_ok=True)
        prev = load_json(P("radar.json"), {})
        prev.update(status="error", error=str(e), generated_at=now_utc())
        save_json(P("radar.json"), prev)
        sys.exit(1)
