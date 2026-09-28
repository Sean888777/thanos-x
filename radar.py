#!/usr/bin/env python3
"""
Thanos X 雷達 v0.3.3
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
    "max_candidates": 3,
    "max_positions": None,    # 不限檔數（你的決定，2026-09-28）
    "max_open_risk_r": 40.0,  # M6 總開放風險上限 40R（你的決定）
    "capital_r": 200.0,       # 現金檢查：帳戶資金 ÷ R（複委託不能融資，買不起就不做）
    "cash_rate": 0.04,        # 【假設】閒置現金年化報酬（短債／貨幣市場），用於大盤比較
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


UNIVERSE_SCHEMA = 2


def load_universe(force=False):
    cache = load_json(P("universe.json"), {})
    fresh = cache.get("updated") and (dt.date.today() - dt.date.fromisoformat(cache["updated"])).days < 7
    if fresh and not force and cache.get("schema") == UNIVERSE_SCHEMA and len(cache.get("tickers", [])) >= 500:
        return cache["tickers"]
    listed, index = listed_symbols(), index_symbols()
    source = "全美股上市清單＋指數成分股"
    if len(listed) < 3000:
        print(f"[warn] 上市清單只有 {len(listed)} 檔，只用指數成分股")
        source = "指數成分股（備援）"
    frames = download(sorted(listed | index), period="1mo", min_rows=15, pause=1.0)
    liquid = set()
    for t, d in frames.items():
        tail = d.iloc[-20:]
        if float(tail["Close"].iloc[-1]) >= CFG["price_min"] and \
                float((tail["Close"] * tail["Volume"]).mean()) >= CFG["dollar_vol_min"]:
            liquid.add(t)
    # 指數成分股一律納入（下載失敗也不會漏掉大型股）；不夠流動的會在掃描時被 S6 擋下
    tickers = sorted(liquid | index)
    if len(tickers) < 500:
        print(f"[warn] 掃描範圍只有 {len(tickers)} 檔，沿用舊清單")
        return cache.get("tickers") or tickers
    save_json(P("universe.json"), {
        "schema": UNIVERSE_SCHEMA, "updated": str(dt.date.today()), "source": source,
        "listed": len(listed), "index": len(index), "liquid": len(liquid), "tickers_total": len(tickers),
        "download": dict(COVERAGE), "tickers": tickers})
    print(f"掃描範圍更新：上市 {len(listed)}、指數 {len(index)}、流動性合格 {len(liquid)} → 共 {len(tickers)} 檔")
    return tickers


COVERAGE = {"requested": 0, "received": 0, "missing_sample": []}


def _fetch(chunk, period, min_rows, frames):
    try:
        df = yf.download(chunk, period=period, interval="1d", group_by="ticker",
                         auto_adjust=False, actions=True, threads=True, progress=False)
    except Exception as e:
        print(f"[warn] 下載失敗：{e}")
        return
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


def download(tickers, period="4mo", min_rows=30, pause=0.0, retries=2):
    """分批下載；沒拿到的分更小批重試，避免被資料源擋掉時無聲漏股。"""
    frames = {}
    todo, size = list(tickers), 100
    for attempt in range(retries + 1):
        for i in range(0, len(todo), size):
            _fetch(todo[i:i + size], period, min_rows, frames)
            if pause:
                time.sleep(pause)
        todo = [t for t in todo if t not in frames]
        if not todo or attempt == retries:
            break
        print(f"重試第 {attempt + 1} 輪：{len(todo)} 檔")
        size, pause = 25, max(pause, 2.0)
    COVERAGE.update(requested=len(tickers), received=len(frames), missing_sample=sorted(todo)[:30])
    print(f"下載完成：{len(frames)}／{len(tickers)} 檔")
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
        c.update({k2: ev[k2] for k2 in ("adv", "tier", "half_spread") if k2 in ev})
        c.update(template="B", signal_date=str(session), side="long", days_after_event=k,
                 s_close=r(sc), atr14=r(a), **lv)
        if dist > CFG["stop_max_pct"]:
            c["excluded"] = "X3 停損距離超過 12%"
        ev["b_state"] = "triggered"
        out.append(c)
    return out


def rank_candidates(cands, reg, session, implied_store=None, enrich_on=True):
    size = 0.5 if (not reg["spy_above_ma50"] or reg["vix_level"] == "half") else 1.0
    blocked = reg["vix_level"] == "stop"
    for c in cands:
        if implied_store and c["template"] == "A":
            tag_surprise(c, implied_store)
        if c.get("excluded"):
            continue
        c.update(enrich(c["ticker"], session) if enrich_on else {"sector": None, "next_earnings": None})
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
def simulate(c, d, chase=False, no_stall=False, exit_mode="base"):
    """exit_mode：base＝現行規則；target_only＝2R 全數停利、取消時間上限；
    losers_time＝時間上限只砍未達 +1R 的部位，其餘改用 10 日均線移動停利。"""
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
    cost = CFG["fee"] + c.get("half_spread", 0.0)   # 每邊成本：手續費＋估計半價差
    ma = d["Close"].rolling(CFG["trail_ma"]).mean()
    rem, t1_hit, cur_stop, pending, trail_on = 1.0, False, stop, None, False
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
        winner = close >= entry + risk
        if exit_mode == "losers_time" and held >= CFG["max_hold"] and winner:
            trail_on = True
        time_up = held >= CFG["max_hold"] and exit_mode != "target_only" \
            and not (exit_mode == "losers_time" and winner)
        if close < cur_stop:
            pending = (rem, "E1 保本停損" if t1_hit else "E1 停損")
        elif ne and tdays_after(day, ne) <= CFG["earn_exit_days"]:
            pending = (rem, "E6 財報將近")
        elif time_up:
            pending = (rem, "E5 持有期滿")
        elif not no_stall and held >= CFG["stall_days"] and not t1_hit and close < entry:
            pending = (rem, "E4 停滯")
        elif not t1_hit and close >= t1:
            pending, t1_hit = ((rem, "E2 停利（全數）") if exit_mode == "target_only"
                               else (rem / 2, "E2 第一目標")), True
        elif (t1_hit or trail_on) and pd.notna(ma.loc[day]) and close < float(ma.loc[day]):
            pending = (rem, "E3 跌破 10 日均線")
    gross = sum(f["frac"] * (f["price"] - entry) for f in fills) / unit
    fees = sum(f["frac"] * cost * (f["price"] + entry) for f in fills) / unit
    res = {"entry_date": entry_date, "entry": r(entry), "stop": r(stop), "t1": r(t1), "unit": r(unit),
           "cost_side": r(cost, 5),
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


def holding_on(book, D, inclusive=False):
    """D 日開盤時仍持有的部位（inclusive=True 時含 D 日當天新進場者）。"""
    return [b for b in book if b["status"] in ("open", "closed")
            and (b["entry_date"] <= D if inclusive else b["entry_date"] <= D)
            and (b["status"] == "open" or b["exit_date"] > D)]


def notional_r(b, D):
    """持倉市值（以 R 計，用進場價估算）；第一目標已賣一半者算一半。"""
    half = 0.5 if (b.get("t1_date") and b["t1_date"] <= D) else 1.0
    return b["size"] * b["entry"] / b["unit"] * half


def equity_curve(filled, frames, sessions):
    """系統帳逐日損益（以 R 計，含手續費，按收盤價逐日評價）與逐日持倉市值（以 R 計）。"""
    idx = pd.DatetimeIndex(sessions)
    pnl, expo = pd.Series(0.0, index=idx), pd.Series(0.0, index=idx)
    for b in filled:
        d = frames.get(b["ticker"])
        if d is None:
            continue
        unit, entry, size = b["unit"], b["entry"], b["size"]
        ed = pd.Timestamp(b["entry_date"])
        last = pd.Timestamp(b["exit_date"]) if b["status"] == "closed" else d.index[-1]
        by_day = {}
        for f in b.get("fills", []):
            by_day.setdefault(pd.Timestamp(f["date"]), []).append(f)
        cost = b.get("cost_side", CFG["fee"])
        if ed in pnl.index:
            pnl[ed] -= size * cost * entry / unit
        rem, prev = 1.0, entry
        for day, row in d[(d.index >= ed) & (d.index <= last)].iterrows():
            for f in by_day.get(day, []):
                if day in pnl.index:
                    pnl[day] += size * f["frac"] * ((f["price"] - prev) - cost * f["price"]) / unit
                rem -= f["frac"]
            if rem <= 1e-9:
                break
            c = float(row["Close"])
            if day in pnl.index:
                pnl[day] += size * rem * (c - prev) / unit
                expo[day] += size * rem * c / unit
            prev = c
    return pnl, expo


def max_drawdown(daily):
    curve = (1 + daily).cumprod()
    return r(float((curve / curve.cummax() - 1).min()) * 100, 2)


def benchmark(filled, frames, sessions, index="SPY"):
    """同樣的資金、同樣的平均曝險，放在指數（預設 SPY，其餘放現金）會怎樣。"""
    if not len(sessions) or index not in frames:
        return None
    idx = pd.DatetimeIndex(sessions)
    pnl, expo = equity_curve(filled, frames, idx)
    cap, cash_d = CFG["capital_r"], CFG["cash_rate"] / 252
    exposure = (expo / cap).clip(upper=1.0)
    held = exposure.shift(1).fillna(0.0)                     # 前一日收盤的曝險，承擔今日漲跌
    spy_ret = frames[index]["Close"].pct_change().reindex(idx).fillna(0.0)
    trade = pnl / cap                                        # 交易損益（佔資金比例）
    system = trade + (1 - held) * cash_d                     # 交易損益＋閒置現金利息
    matched = held * spy_ret + (1 - held) * cash_d           # 同曝險大盤＋閒置現金
    comp = lambda x: r(float((1 + x).prod() - 1) * 100, 2)
    out = {
        "index": index, "range": [str(idx[0].date()), str(idx[-1].date())], "sessions": len(idx),
        "assumed_cash_rate_pct": CFG["cash_rate"] * 100,
        "avg_exposure_pct": r(float(exposure.mean()) * 100, 1),
        "max_exposure_pct": r(float(exposure.max()) * 100, 1),
        "system_pct": comp(system), "system_trading_only_pct": comp(trade),
        "matched_benchmark_pct": comp(matched), "spy_buy_hold_pct": comp(spy_ret),
        "cash_only_pct": comp(pd.Series(cash_d, index=idx)),
        "max_drawdown_pct": {"system": max_drawdown(system), "matched": max_drawdown(matched),
                             "spy": max_drawdown(spy_ret)},
    }
    out["excess_vs_matched_pct"] = r(out["system_pct"] - out["matched_benchmark_pct"], 2)
    out["beats_matched"] = out["excess_vs_matched_pct"] > 0
    return out


def key(c):
    return (c["ticker"], c["template"], c["signal_date"])


def run_paper(hist, frames, sessions=None):
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
            held = holding_on(book, D)
            risk = sum(b["size"] for b in held if not (b.get("t1_date") and b["t1_date"] <= D))
            cash = sum(notional_r(b, D) for b in held)
            need = t["size"] * s["entry"] / s["unit"]
            reason = None
            if any(b["ticker"] == c["ticker"] for b in held):
                reason = "同標的已持有"
            elif CFG["max_positions"] and len(held) >= CFG["max_positions"]:
                reason = "持倉檔數上限"
            elif risk + t["size"] > CFG["max_open_risk_r"]:
                reason = "總風險上限"
            elif cash + need > CFG["capital_r"]:
                reason = "現金不足"
            if reason:
                t = {"ticker": c["ticker"], "template": c["template"], "signal_date": c["signal_date"],
                     "status": "skipped", "skip_reason": reason, "size": t["size"]}
            elif s["status"] == "closed":
                t["pnl_r"] = r(s["pnl_r"] * t["size"], 3)
        book.append(t)
    filled = [b for b in book if b["status"] in ("open", "closed")]
    # 高峰負載：同時持有幾檔、總風險、現金使用率（每個進場日檢查一次）
    peak = {"positions": 0, "open_risk_r": 0.0, "cash_used_pct": 0.0}
    for D in sorted({b["entry_date"] for b in filled}):
        held = holding_on(filled, D, inclusive=True)
        peak["positions"] = max(peak["positions"], len(held))
        peak["open_risk_r"] = max(peak["open_risk_r"],
                                  sum(b["size"] for b in held if not (b.get("t1_date") and b["t1_date"] <= D)))
        peak["cash_used_pct"] = max(peak["cash_used_pct"],
                                    r(sum(notional_r(b, D) for b in held) / CFG["capital_r"] * 100, 1))
    skips = {}
    for b in book:
        if b["status"] == "skipped":
            skips[b["skip_reason"]] = skips.get(b["skip_reason"], 0) + 1

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
    # 影子帳四、五：出場方式
    shadow_target = [simulate(c, frames[c["ticker"]], exit_mode="target_only") for c in sel if c["ticker"] in frames]
    shadow_losers = [simulate(c, frames[c["ticker"]], exit_mode="losers_time") for c in sel if c["ticker"] in frames]
    # 預期波動標籤（只有往前累積的資料才會有）
    tagged = [c for c in sel_a if c.get("surprise_ratio") is not None]
    if sessions is None and filled and "SPY" in frames:
        first = min(pd.Timestamp(b["entry_date"]) for b in filled)
        sessions = frames["SPY"].index[frames["SPY"].index >= first]
    bench = benchmark(filled, frames, sessions) if sessions is not None else None
    bench_iwm = benchmark(filled, frames, sessions, "IWM") if sessions is not None and "IWM" in frames else None
    return {
        "benchmark": bench, "benchmark_iwm": bench_iwm,
        "system": {"stats": stats(book), "filled_trades": len(filled), "peak": peak, "skip_reasons": skips,
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
            "target_only": {"note": "2R 全數停利、取消持有期上限（大爺的提議）", **stats(shadow_target)},
            "losers_time": {"note": "持有期滿只砍未達 +1R 者，其餘改 10 日均線移動停利（發財的版本）",
                            **stats(shadow_losers)},
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
    paper = run_paper(hist, frames, sessions)
    out = {
        "version": "backfill-0.3.3", "status": "ok", "generated_at": now_utc(),
        "warning": ("倖存者偏差（用今日上市、今日流動性合格的股票回看過去）、樣本小；"
                    "且 v0.3 是看過 v0.2 回顧測試之後修改的，成績屬於樣本內，不具驗證力。"
                    "只用來確認雷達會動與候選頻率，不得作為跳過紙上階段或再次修改規則的依據。"),
        "range": [str(sessions[0].date()), str(sessions[-1].date())],
        "universe_size": len(universe), "coverage": dict(COVERAGE),
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
# 樣本外測試：放寬流動性、分層、估計價差成本；判準在執行前寫死，結果自動判定
# ------------------------------------------------------------------
OOS = {
    "price_min": 5.0,        # 事件當日股價 ≥ 5 美元（排除仙股）
    "adv_floor": 2e6,        # 事件前 20 日均成交額 ≥ 200 萬美元
    "screen_price": 3.0,     # 目前仍掛牌且股價 ≥ 3、日均成交額 ≥ 100 萬者納入下載
    "screen_adv": 1e6,
    "spread_cap": 0.03,      # 估計價差上限 3%
    "min_tier_trades": 30,   # 每一層至少 30 筆才算有結論
}
TIERS = [("T1", "日均成交 ≥ 5,000 萬", 50e6, float("inf")),
         ("T2", "1,000 萬–5,000 萬", 10e6, 50e6),
         ("T3", "200 萬–1,000 萬", 2e6, 10e6)]


def tier_of(adv):
    for code, _, lo, hi in TIERS:
        if lo <= adv < hi:
            return code
    return None


def cs_half_spread(high, low):
    """Corwin–Schultz 價差估計（用日高低價），回傳半價差。"""
    import numpy as np
    k = 3 - 2 * np.sqrt(2)
    hl = np.log(high / low)
    beta = hl[:-1] ** 2 + hl[1:] ** 2
    gamma = np.log(np.maximum(high[:-1], high[1:]) / np.minimum(low[:-1], low[1:])) ** 2
    alpha = (np.sqrt(2 * beta) - np.sqrt(beta)) / k - np.sqrt(gamma / k)
    spread = np.clip(2 * (np.exp(alpha) - 1) / (1 + np.exp(alpha)), 0, None)
    val = float(np.nanmean(spread)) if len(spread) else 0.0
    return min(val, OOS["spread_cap"]) / 2


def detect_events(t, d, start, end):
    """向量化偵測整段期間的做多事件（條件與 scan_events 相同，流動性門檻改用 OOS）。"""
    pc = d["Close"].shift(1)
    avgv = d["Volume"].shift(1).rolling(20).mean()
    dv = (d["Close"] * d["Volume"]).shift(1).rolling(20).mean()
    rng = d["High"] - d["Low"]
    gap, vm = d["Open"] / pc - 1, d["Volume"] / avgv
    pos, hold = (d["Close"] - d["Low"]) / rng, d["Close"] / pc - 1
    atr = atr_series(d)
    split = (d["Stock Splits"].fillna(0) != 0) if "Stock Splits" in d.columns else pd.Series(False, index=d.index)
    base = (d.index >= start) & (d.index <= end) & (rng > 0) & (pc > 0) & (avgv > 0) & (atr > 0) & ~split \
        & (d["Close"] >= OOS["price_min"]) & (dv >= OOS["adv_floor"]) & (vm >= CFG["vol_mult"])
    longm = base & (gap >= CFG["gap_min"]) & (pos >= CFG["close_pos_min"]) & (hold >= CFG["gap_hold_min"])
    shortm = base & (gap <= -CFG["gap_min"]) & ((1 - pos) >= CFG["close_pos_min"]) & (hold <= -CFG["gap_hold_min"])
    H, L = d["High"].to_numpy(), d["Low"].to_numpy()
    events = []
    for ts in d.index[longm.fillna(False).to_numpy()]:
        i = d.index.get_loc(ts)
        if i < 22:
            continue
        o, h, l, c = (float(d[k].iloc[i]) for k in ("Open", "High", "Low", "Close"))
        ev = {"ticker": t, "e_date": str(ts.date()), "gap_pct": r(gap.iloc[i] * 100, 2), "vol_mult": r(vm.iloc[i], 2),
              "close_pos": r(pos.iloc[i], 2), "hold_pct": r(hold.iloc[i] * 100, 2), "e_open": r(o), "e_high": r(h),
              "e_low": r(l), "e_close": r(c), "atr14": r(atr.iloc[i]), "range_pct": r((h - l) / c * 100, 2),
              "adv": r(dv.iloc[i], 0), "tier": tier_of(float(dv.iloc[i])),
              "half_spread": r(cs_half_spread(H[i - 21:i], L[i - 21:i]), 5)}
        if gap.iloc[i] >= 0.08 and (h - l) / c < 0.015:
            ev["x1"] = True
        events.append(ev)
    shorts = [str(x.date()) for x in d.index[shortm.fillna(False).to_numpy()]]
    return events, shorts


def broad_universe():
    syms = listed_symbols() | index_symbols()
    frames = download(sorted(syms), period="1mo", min_rows=15, pause=1.0)
    keep = []
    for t, d in frames.items():
        tail = d.iloc[-20:]
        if float(tail["Close"].iloc[-1]) >= OOS["screen_price"] and \
                float((tail["Close"] * tail["Volume"]).mean()) >= OOS["screen_adv"]:
            keep.append(t)
    print(f"樣本外掃描範圍：上市 {len(syms)} → 目前股價 ≥ {OOS['screen_price']:.0f}、日均成交 ≥ 100 萬 {len(keep)} 檔")
    return sorted(keep)


def oos(start, end):
    """樣本外測試。判準（2026-09-28 執行前寫死）：
    一、系統帳扣除手續費與估計價差後，同時贏過「同曝險 SPY」與「同曝險 IWM」，且平均 R > 0。
    二、流動性假設要成立：T2、T3 兩層的平均 R 都要高於 T1；任一層不足 30 筆視為不成立。
    兩條都過 → 進紙上階段；任一條不過 → Thanos X 停止。"""
    os.makedirs(DATA, exist_ok=True)
    universe = broad_universe()
    frames = download(sorted(set(universe) | {"SPY", "^VIX", "IWM"}), period="5y", pause=0.5)
    s_ts, e_ts = pd.Timestamp(start), pd.Timestamp(end)
    ev_by_day, short_count = {}, {}
    for t, d in frames.items():
        if t in ("SPY", "^VIX", "IWM"):
            continue
        evs, sh = detect_events(t, d, s_ts, e_ts)
        for ev in evs:
            ev_by_day.setdefault(ev["e_date"], []).append(ev)
        for x in sh:
            short_count[x] = short_count.get(x, 0) + 1
    spy_idx = frames["SPY"].index
    sessions = spy_idx[(spy_idx >= s_ts) & (spy_idx <= e_ts)]
    print(f"樣本外測試：{len(universe)} 檔 × {len(sessions)} 個交易日，事件 {sum(len(v) for v in ev_by_day.values())} 個")
    hist = {"events": [], "candidates": []}
    by_day = []
    for ts in sessions:
        day = ts.date()
        todays = ev_by_day.get(str(day), [])
        active = {e["ticker"] for e in hist["events"] if not e.get("b_state") and not e.get("x1")}
        sub = {t: frames[t][frames[t].index <= ts] for t in active if t in frames}
        cands = candidates_a(todays) + candidates_b(hist["events"], sub, day)
        cands = rank_candidates(cands, regime(frames, ts), day, enrich_on=False)
        hist["events"].extend(todays)
        cutoff = ts - pd.Timedelta(days=45)
        hist["events"] = [e for e in hist["events"] if pd.Timestamp(e["e_date"]) >= cutoff]
        hist["candidates"].extend(cands)
        by_day.append({"date": str(day), "passed": len(cands),
                       "selected": sum(1 for c in cands if c.get("selected")),
                       "selected_A": sum(1 for c in cands if c.get("selected") and c["template"] == "A"),
                       "selected_B": sum(1 for c in cands if c.get("selected") and c["template"] == "B"),
                       "short_watch": short_count.get(str(day), 0)})
    paper = run_paper(hist, frames, sessions)
    sel = [c for c in hist["candidates"] if c.get("selected") and not c.get("excluded")]
    by_tier, spread_tier = {}, {}
    for code, label, _, _ in TIERS:
        cs = [c for c in sel if c.get("tier") == code]
        by_tier[code] = dict(label=label, **stats([simulate(c, frames[c["ticker"]]) for c in cs if c["ticker"] in frames]))
        by_tier[code]["A"] = stats([simulate(c, frames[c["ticker"]]) for c in cs if c["template"] == "A" and c["ticker"] in frames])
        by_tier[code]["B"] = stats([simulate(c, frames[c["ticker"]]) for c in cs if c["template"] == "B" and c["ticker"] in frames])
        spread_tier[code] = r(sum(c.get("half_spread", 0) for c in cs) / len(cs) * 200, 3) if cs else None
    bm, bmi, st = paper["benchmark"], paper.get("benchmark_iwm"), paper["system"]["stats"]
    c1 = bool(bm and bmi and bm["beats_matched"] and bmi["beats_matched"] and (st.get("avg_r") or -9) > 0)
    t1, t2, t3 = (by_tier[k] for k in ("T1", "T2", "T3"))
    enough = all(x["closed"] >= OOS["min_tier_trades"] for x in (t1, t2, t3))
    c2 = bool(enough and t2["avg_r"] > t1["avg_r"] and t3["avg_r"] > t1["avg_r"])
    out = {
        "version": "oos-0.1", "status": "ok", "generated_at": now_utc(),
        "range": [str(sessions[0].date()), str(sessions[-1].date())],
        "warning": ("倖存者偏差：只能用今日仍掛牌的股票回看過去，已下市的失敗案例不在樣本中，"
                    "小型股受影響最大，結果偏樂觀。價差為 Corwin–Schultz 估計值。"
                    "未套用族群與財報日排除（免費資料無法取得完整歷史）。"),
        "criteria": {
            "c1": "系統帳（含手續費與估計價差）同時贏過同曝險 SPY 與同曝險 IWM，且平均 R > 0",
            "c2": "T2、T3 兩層平均 R 皆高於 T1；任一層少於 30 筆視為不成立",
            "rule": "兩條都過 → 進紙上階段；任一條不過 → Thanos X 停止",
        },
        "verdict": {"c1_pass": c1, "c2_pass": c2, "tier_sample_enough": enough,
                    "result": "通過：進入紙上階段" if (c1 and c2) else "未通過：Thanos X 停止"},
        "universe_size": len(universe), "coverage": dict(COVERAGE), "oos_config": OOS,
        "frequency": frequency(paper["system"]["filled_trades"], by_day, len(sessions)),
        "by_tier": by_tier, "avg_est_spread_pct_by_tier": spread_tier,
        "paper": paper, "by_day": by_day,
    }
    save_json(P("oos.json"), out)
    print(f"判定：{out['verdict']['result']}（判準一 {c1}、判準二 {c2}）")


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
        "version": "radar-0.3.3", "status": "ok", "generated_at": now_utc(),
        "session": str(session), "universe_size": len(universe), "coverage": dict(COVERAGE),
        "config": CFG, "regime": reg,
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
    if len(sys.argv) >= 4 and sys.argv[1] == "--oos":
        oos(sys.argv[2], sys.argv[3])
        sys.exit(0)
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
