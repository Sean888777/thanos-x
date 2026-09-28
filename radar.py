#!/usr/bin/env python3
"""
Thanos X 雷達 v0.1
------------------
每個美股交易日收盤後，由 GitHub Actions 自動執行：
  1. 掃描事件跳空候選（做多＝可執行；放空＝只觀察）
  2. 依規則排序，每日最多 3 檔，同族群只取一檔
  3. 更新候選池資料（收盤、10 日均線、ATR），供網站比對你的持倉
  4. 對所有候選自動做紙上模擬，累積「系統本身」的成績

輸出：docs/data/radar.json、docs/data/history.json
規則來源：Thanos X 規格書 v0.2
"""
import datetime as dt
import json
import os
import sys
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
    "gap_min": 0.05,          # S1 跳空 ≥ 5%
    "vol_mult": 3.0,          # S2 成交量 ≥ 20 日均量 × 3
    "close_pos_min": 0.70,    # S3 收在當日區間上 30%
    "gap_hold_min": 0.03,     # S4 收盤仍 ≥ 前日收盤 +3%
    "price_min": 10.0,        # S5 股價 ≥ 10
    "dollar_vol_min": 50e6,   # S6 20 日均成交金額 ≥ 5,000 萬美元
    "stop_min_pct": 0.04,     # 停損距離下限 4%（手續費理由）
    "stop_max_pct": 0.12,     # X3 停損距離上限 12%
    "limit_atr": 0.3,         # 進場限價 = E 日收盤 + 0.3 ATR
    "buffer_atr": 0.5,        # 部位計算預留 0.5 ATR 跳空
    "t1_r": 2.0,              # E2 第一目標 = 2 倍 1R 價差
    "trail_ma": 10,           # E3 10 日均線
    "stall_days": 5,          # E4 停滯
    "max_hold": 10,           # E5 持有上限
    "earn_exit_days": 2,      # E6 距財報 ≤ 2 交易日出場
    "earn_exclude_days": 10,  # X2 10 交易日內有財報不做
    "max_candidates": 3,      # 每日最多 3 檔
    "max_positions": 3,       # M5 同時持倉上限
    "max_open_risk_r": 3.0,   # M6 總開放風險上限
    "vix_half": 25.0,         # M9
    "vix_stop": 35.0,         # M10
    "fee": 0.003,             # 單邊手續費
    "pool_days": 45,          # 候選池保留天數（日曆日，約 30 交易日）
}

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "docs", "data")
HIST_PATH = os.path.join(DATA, "history.json")
RADAR_PATH = os.path.join(DATA, "radar.json")
UNIV_CACHE = os.path.join(DATA, "universe_cache.txt")
INDEX_TICKERS = ("SPY", "^VIX")

WIKI_SOURCES = [
    ("S&P 500", "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"),
    ("S&P 400", "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"),
    ("Nasdaq-100", "https://en.wikipedia.org/wiki/Nasdaq-100"),
]


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


# ------------------------------------------------------------------
# 資料
# ------------------------------------------------------------------
def load_universe():
    tickers = set()
    headers = {"User-Agent": "Mozilla/5.0 (Thanos-X radar)"}
    for name, url in WIKI_SOURCES:
        try:
            html = requests.get(url, headers=headers, timeout=30).text
            for table in pd.read_html(StringIO(html)):
                cols = [c for c in table.columns if str(c).strip() in ("Symbol", "Ticker", "Ticker symbol")]
                if cols:
                    tickers.update(table[cols[0]].astype(str).str.strip())
                    break
        except Exception as e:
            print(f"[warn] 成分股清單讀取失敗 {name}: {e}")
    tickers = {t.replace(".", "-") for t in tickers if t and t.lower() != "nan"}
    if len(tickers) >= 300:
        with open(UNIV_CACHE, "w") as f:
            f.write("\n".join(sorted(tickers)))
    elif os.path.exists(UNIV_CACHE):
        print("[warn] 成分股清單不完整，改用上次的快取")
        with open(UNIV_CACHE) as f:
            tickers |= {l.strip() for l in f if l.strip()}
    return sorted(tickers)


def download(tickers, period="4mo"):
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
                if len(d) >= 30:
                    d.index = pd.DatetimeIndex(d.index).tz_localize(None).normalize()
                    frames[t] = d
            except Exception:
                pass
    return frames


def enrich(ticker, session):
    """族群、下次財報日。抓不到就留空，網站會提示手動確認。"""
    out = {"sector": None, "next_earnings": None}
    if yf is None:
        return out
    try:
        tk = yf.Ticker(ticker)
        try:
            out["sector"] = (tk.info or {}).get("sector")
        except Exception:
            pass
        try:
            ed = tk.get_earnings_dates(limit=8)
            if ed is not None and len(ed):
                days = sorted({pd.Timestamp(x).tz_localize(None).normalize() for x in ed.index})
                fut = [x for x in days if x > pd.Timestamp(session)]
                if fut:
                    out["next_earnings"] = str(fut[0].date())
        except Exception:
            pass
    except Exception:
        pass
    return out


# ------------------------------------------------------------------
# 指標與掃描
# ------------------------------------------------------------------
def atr_series(d, n=14):
    pc = d["Close"].shift(1)
    tr = pd.concat([d["High"] - d["Low"], (d["High"] - pc).abs(), (d["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def regime(frames):
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


def scan(frames, session):
    longs, shorts = [], []
    for t, d in frames.items():
        if t in INDEX_TICKERS or len(d) < 25 or d.index[-1].date() != session:
            continue
        last = d.iloc[-1]
        if "Stock Splits" in d.columns and float(last.get("Stock Splits") or 0) != 0:
            continue  # 分割日的價格跳動不是事件
        o, h, l, c, v = (float(last[k]) for k in ("Open", "High", "Low", "Close", "Volume"))
        pc = float(d["Close"].iloc[-2])
        hist = d.iloc[-21:-1]
        avgv = float(hist["Volume"].mean())
        dv = float((hist["Close"] * hist["Volume"]).mean())
        if avgv <= 0 or h <= l or pc <= 0:
            continue
        a = float(atr_series(d).iloc[-1])
        if not a > 0:
            continue
        gap, vm, pos, hold = o / pc - 1, v / avgv, (c - l) / (h - l), c / pc - 1
        if c < CFG["price_min"] or dv < CFG["dollar_vol_min"] or vm < CFG["vol_mult"]:
            continue
        base = {"ticker": t, "e_date": str(session), "gap_pct": r(gap * 100, 2), "vol_mult": r(vm, 2),
                "close_pos": r(pos, 2), "hold_pct": r(hold * 100, 2), "e_open": r(o), "e_high": r(h),
                "e_low": r(l), "e_close": r(c), "atr14": r(a), "range_pct": r((h - l) / c * 100, 2)}
        if gap >= CFG["gap_min"] and pos >= CFG["close_pos_min"] and hold >= CFG["gap_hold_min"]:
            limit = c + CFG["limit_atr"] * a
            stop = min(l, limit * (1 - CFG["stop_min_pct"]))
            dist = (limit - stop) / limit
            rec = dict(base, side="long", limit=r(limit), stop_ref=r(stop), risk_ref=r(limit - stop),
                       t1_ref=r(limit + CFG["t1_r"] * (limit - stop)), stop_dist_pct=r(dist * 100, 2))
            if dist > CFG["stop_max_pct"]:
                rec["excluded"] = "X3 停損距離超過 12%"
            elif gap >= 0.08 and (h - l) / c < 0.015:
                rec["excluded"] = "X1 疑似現金併購（跳空後區間極窄）"
            longs.append(rec)
        elif gap <= -CFG["gap_min"] and (1 - pos) >= CFG["close_pos_min"] and hold <= -CFG["gap_hold_min"]:
            shorts.append(dict(base, side="short_watch"))
    return longs, shorts


def rank_candidates(longs, reg, session):
    size = 0.5 if (not reg["spy_above_ma50"] or reg["vix_level"] == "half") else 1.0
    blocked = reg["vix_level"] == "stop"
    for c in longs:
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
    eligible = sorted([c for c in longs if not c.get("excluded")], key=lambda c: -c["vol_mult"])
    seen, rank = set(), 0
    for c in eligible:
        sec = c.get("sector")
        if rank < CFG["max_candidates"] and (sec is None or sec not in seen):
            rank += 1
            c["selected"], c["rank"] = True, rank
            if sec:
                seen.add(sec)
        else:
            c["selected"] = False
            c["shadow_reason"] = "同族群已有更高排名" if (sec in seen and rank < CFG["max_candidates"]) else "超過每日上限"
    return longs


# ------------------------------------------------------------------
# 紙上模擬（完全照規格書第 6 節：收盤判斷、隔日開盤執行）
# ------------------------------------------------------------------
def simulate(c, d):
    after = d[d.index > pd.Timestamp(c["e_date"])]
    if len(after) == 0:
        return {"status": "waiting"}
    first = after.iloc[0]
    entry_date = str(after.index[0].date())
    o = float(first["Open"])
    if o > c["limit"]:
        return {"status": "no_fill", "entry_date": entry_date, "open": r(o)}
    entry = o
    stop = min(c["e_low"], entry * (1 - CFG["stop_min_pct"]))
    risk = entry - stop
    unit = risk + CFG["buffer_atr"] * c["atr14"]  # 每 1R 對應的價格變動
    t1 = entry + CFG["t1_r"] * risk
    ma = d["Close"].rolling(CFG["trail_ma"]).mean()
    rem, t1_hit, cur_stop, pending = 1.0, False, stop, None
    fills, t1_date = [], None
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
                break
        close, held = float(row["Close"]), i + 1
        ne = c.get("next_earnings")
        if close < cur_stop:
            pending = (rem, "E1 保本停損" if t1_hit else "E1 停損")
        elif ne and tdays_after(day, ne) <= CFG["earn_exit_days"]:
            pending = (rem, "E6 財報將近")
        elif held >= CFG["max_hold"]:
            pending = (rem, "E5 持有滿 10 日")
        elif held >= CFG["stall_days"] and not t1_hit and close < entry:
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
                   exit_reason=fills[-1]["reason"])
    else:
        last = float(after["Close"].iloc[-1])
        res.update(status="open", remaining=r(rem, 2), last_close=r(last), stop_now=r(cur_stop),
                   unrealized_r=r(rem * (last - entry) / unit, 3),
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
        out.update(win_rate=r(len(w) / len(p), 3), avg_r=r(sum(p) / len(p), 3), total_r=r(sum(p), 2),
                   avg_win=r(sum(w) / len(w), 3) if w else None, avg_loss=r(sum(lo) / len(lo), 3) if lo else None)
    return out


def run_paper(hist, frames):
    cands = [c for c in hist["candidates"] if not c.get("excluded")]
    sims = {}
    for c in cands:
        d = frames.get(c["ticker"])
        if d is not None:
            sims[(c["ticker"], c["e_date"])] = simulate(c, d)
    # 系統帳：只做入選者，套用持倉與風險上限
    book = []
    for c in sorted([c for c in cands if c.get("selected") and not c.get("blocked")],
                    key=lambda c: (c["e_date"], c.get("rank", 9))):
        s = sims.get((c["ticker"], c["e_date"]))
        if not s:
            continue
        t = dict(s, ticker=c["ticker"], e_date=c["e_date"], size=c.get("size_factor", 1.0))
        if s["status"] in ("open", "closed"):
            D = s["entry_date"]
            held = [b for b in book if b["status"] in ("open", "closed") and b["entry_date"] <= D
                    and (b["status"] == "open" or b["exit_date"] > D)]
            risk = sum(b["size"] for b in held if not (b.get("t1_date") and b["t1_date"] <= D))
            if len(held) >= CFG["max_positions"] or risk + t["size"] > CFG["max_open_risk_r"]:
                t = {"ticker": c["ticker"], "e_date": c["e_date"], "status": "skipped", "size": t["size"]}
            elif s["status"] == "closed":
                t["pnl_r"] = r(s["pnl_r"] * t["size"], 3)
        book.append(t)
    # 對照帳：每一檔合格候選各自獨立模擬，比較「入選」與「落選」
    chosen = {(c["ticker"], c["e_date"]) for c in cands if c.get("selected")}
    sel = [sims[k] for k in sims if k in chosen]
    rej = [sims[k] for k in sims if k not in chosen]
    return {
        "system": {"stats": stats(book),
                   "open": [b for b in book if b["status"] == "open"],
                   "recent_closed": [b for b in book if b["status"] == "closed"][-20:]},
        "compare": {"selected": stats(sel), "not_selected": stats(rej)},
    }


# ------------------------------------------------------------------
# 候選池：網站用來比對你的真實持倉
# ------------------------------------------------------------------
def build_pool(hist, frames, session):
    cutoff = pd.Timestamp(session) - pd.Timedelta(days=CFG["pool_days"])
    pool = {}
    for c in hist["candidates"]:
        if c.get("excluded") or pd.Timestamp(c["e_date"]) < cutoff:
            continue
        d = frames.get(c["ticker"])
        if d is None:
            continue
        pool[c["ticker"]] = {
            "last_date": str(d.index[-1].date()), "last_close": r(d["Close"].iloc[-1]),
            "ma10": r(d["Close"].rolling(CFG["trail_ma"]).mean().iloc[-1]),
            "atr14": r(atr_series(d).iloc[-1]), "next_earnings": c.get("next_earnings"),
            "e_date": c["e_date"], "e_low": c["e_low"], "limit": c["limit"], "selected": c.get("selected", False),
        }
    return pool


# ------------------------------------------------------------------
def main():
    os.makedirs(DATA, exist_ok=True)
    hist = load_json(HIST_PATH, {"last_session": None, "candidates": [], "short_watch": []})
    universe = load_universe()
    tickers = sorted(set(universe) | {c["ticker"] for c in hist["candidates"]} | set(INDEX_TICKERS))
    print(f"掃描範圍：{len(universe)} 檔")
    frames = download(tickers)
    if "SPY" not in frames:
        raise RuntimeError("SPY 資料下載失敗，無法判斷交易日")
    session = frames["SPY"].index[-1].date()
    reg = regime(frames)

    if str(session) != hist.get("last_session"):
        longs, shorts = scan(frames, session)
        longs = rank_candidates(longs, reg, session)
        hist["candidates"].extend(longs)
        hist["short_watch"] = sorted(shorts, key=lambda s: -s["vol_mult"])[:CFG["max_candidates"]]
        hist["last_session"] = str(session)
        print(f"{session}：做多符合 {len(longs)} 檔，入選 {sum(c.get('selected', False) for c in longs)} 檔")
    else:
        print(f"{session} 已處理過，只更新候選池與模擬")

    today = [c for c in hist["candidates"] if c["e_date"] == str(session)]
    out = {
        "version": "radar-0.1",
        "status": "ok",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "session": str(session),
        "universe_size": len(universe),
        "config": CFG,
        "regime": reg,
        "today": {
            "candidates": sorted([c for c in today if c.get("selected")], key=lambda c: c["rank"]),
            "not_selected_count": sum(1 for c in today if not c.get("excluded") and not c.get("selected")),
            "excluded": [{"ticker": c["ticker"], "reason": c["excluded"]} for c in today if c.get("excluded")],
            "short_watch": hist.get("short_watch", []),
        },
        "pool": build_pool(hist, frames, session),
        "paper": run_paper(hist, frames),
    }
    save_json(HIST_PATH, hist)
    save_json(RADAR_PATH, out)
    print("完成")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        os.makedirs(DATA, exist_ok=True)
        prev = load_json(RADAR_PATH, {})
        prev.update(status="error", error=str(e),
                    generated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        save_json(RADAR_PATH, prev)
        sys.exit(1)
