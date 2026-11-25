"""H4 sweep + M15 CISD / Model-1 scanner (forex + metals). Runs on GitHub Actions."""
import os, json, time
from datetime import datetime, timezone
import requests, pandas as pd

SYMBOLS = {"EUR/USD": "OANDA:EURUSD", "GBP/USD": "OANDA:GBPUSD",
           "XAU/USD": "OANDA:XAUUSD", "XAG/USD": "OANDA:XAGUSD"}
RR = float(os.getenv("RR", "2"))          # take profit = RR * risk
BE_R = float(os.getenv("BE_R", "1"))      # move SL to entry after this R (0 = off)
MODES = os.getenv("MODES", "cisd,model1").split(",")
FRESH_MIN = 90                            # ignore setups older than this
TZ = "Europe/Athens"                      # broker-like time => H4 candles 00,04,08...
KEY, TOKEN, CHAT = os.getenv("TWELVEDATA_KEY"), os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
FILE = "docs/signals.json"
BITGET = {"XAG/USD": "XAGUSDT"}           # symbols fetched from Bitget (no API key needed)
M15 = pd.Timedelta(minutes=15)

def fetch_bitget(sym):
    r = requests.get("https://api.bitget.com/api/v2/mix/market/candles", timeout=30, params=dict(
        symbol=BITGET[sym], granularity="15m", limit=500, productType="usdt-futures")).json()
    if r.get("code") != "00000":
        raise RuntimeError(str(r))
    df = pd.DataFrame([x[:5] for x in r["data"]], columns=["ts", "open", "high", "low", "close"])
    df["t"] = pd.to_datetime(df.ts.astype("int64"), unit="ms", utc=True).dt.tz_convert(TZ).dt.tz_localize(None)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    df = df.sort_values("t").set_index("t")[["open", "high", "low", "close"]]
    now = pd.Timestamp.now(TZ).tz_localize(None)
    return df[df.index + M15 <= now]

def fetch(sym):
    if sym in BITGET:
        return fetch_bitget(sym)
    r = requests.get("https://api.twelvedata.com/time_series", timeout=30, params=dict(
        symbol=sym, interval="15min", outputsize=500, timezone=TZ, apikey=KEY)).json()
    if "values" not in r:
        raise RuntimeError(r.get("message", str(r)))
    df = pd.DataFrame(r["values"])
    df["t"] = pd.to_datetime(df.datetime)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    df = df.sort_values("t").set_index("t")[["open", "high", "low", "close"]]
    now = pd.Timestamp.now(TZ).tz_localize(None)
    return df[df.index + M15 <= now]      # closed bars only

def mirror(d):  # buy = mirrored sell
    return pd.DataFrame({"open": -d.open, "high": -d.low, "low": -d.high, "close": -d.close})

def scan(sym, side, p, c, h4t):
    """Sell logic on (mirrored for buy) data. Returns (pending, signals)."""
    if side == "buy":
        p, c = mirror(p), mirror(c)
    k = 1 if side == "sell" else -1
    lvl = p.high.max()                    # C1 (previous H4) extreme = swept level
    rows = list(c.itertuples())
    ex, pend, sigs, done = None, None, [], set()
    for j, r in enumerate(rows):
        if r.high > lvl and (ex is None or r.high >= rows[ex].high):
            ex = j                        # sweep extreme (M15 bar)
        if ex is None or r.close >= lvl:  # must close back inside C1
            continue
        t_close = rows[j].Index + M15
        if pend is None:
            pend = dict(id=f"{sym}|{side}|{h4t}", sym=sym, side=side, level=k * lvl,
                        t=str(t_close), h4=str(h4t))
        if j <= ex:
            continue
        sl = rows[ex].high
        trig = {}
        if r.close < rows[ex].low:        # Model-1: sweep candle broken by body close
            trig["model1"] = True
        i, lo = ex, min(rows[ex].open, rows[ex].close)   # CISD: opposite-candle series
        while i - 1 >= 0 and rows[i - 1].close > rows[i - 1].open:
            i -= 1
            lo = min(lo, rows[i].open, rows[i].close)
        if r.close < lo:
            trig["cisd"] = True
        for mode in MODES:
            if mode in trig and mode not in done and sl > r.close:
                done.add(mode)
                risk = sl - r.close
                sigs.append(dict(id=f"{sym}|{side}|{mode}|{h4t}", sym=sym, side=side, mode=mode,
                                 entry=k * r.close, sl=k * sl, sl_cur=k * sl, tp=k * (r.close - RR * risk),
                                 t=str(t_close), h4=str(h4t), status="active", checked=str(t_close)))
    return pend, sigs

def detect(sym, df):
    key = df.index.floor("4h")
    starts = sorted(set(key))
    pends, sigs = [], []
    for n in range(max(1, len(starts) - 2), len(starts)):   # last two H4 candles
        if starts[n] - starts[n - 1] > pd.Timedelta(hours=8):
            continue
        p, c = df[key == starts[n - 1]], df[key == starts[n]]
        if len(p) < 12 or c.empty:
            continue
        for side in ("sell", "buy"):
            pe, s = scan(sym, side, p, c, starts[n])
            if pe: pends.append(pe)
            sigs += s
    return pends, sigs

def monitor(df, s):
    e, risk = s["entry"], abs(s["entry"] - s["sl"])
    sell = s["side"] == "sell"
    for t, r in df[df.index > pd.Timestamp(s["checked"]) - M15 / 2].iterrows():
        if t < pd.Timestamp(s["t"]):      # only bars after the entry candle
            continue
        sl_hit = r.high >= s["sl_cur"] if sell else r.low <= s["sl_cur"]
        tp_hit = r.low <= s["tp"] if sell else r.high >= s["tp"]
        reach = BE_R > 0 and (r.low <= e - BE_R * risk if sell else r.high >= e + BE_R * risk)
        end = None
        if sl_hit:
            end = "be" if s["sl_cur"] == e else "sl"   # SL first = conservative
        elif tp_hit:
            end = "tp"
        elif reach:
            s["sl_cur"] = e
        s["checked"] = str(t + M15)
        if end:
            s["status"], s["closed"] = end, str(t + M15)
            return True
    return False

def tg(text, sym):
    if not (TOKEN and CHAT):
        print(text); return
    url = "https://www.tradingview.com/chart/?symbol=" + SYMBOLS[sym]
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", timeout=20, json=dict(
        chat_id=CHAT, text=text, parse_mode="HTML",
        reply_markup={"inline_keyboard": [[{"text": "TradingView", "url": url}]]}))

def fresh(ts):
    return (pd.Timestamp.now(TZ).tz_localize(None) - pd.Timestamp(ts)) <= pd.Timedelta(minutes=FRESH_MIN)

def main():
    now = datetime.now(timezone.utc)
    if now.weekday() == 5 or (now.weekday() == 6 and now.hour < 21) or (now.weekday() == 4 and now.hour >= 22):
        print("market closed"); return
    st = json.load(open(FILE))
    sigs = {s["id"]: s for s in st["signals"]}
    pend = {p["id"]: p for p in st["pending"]}
    for sym in SYMBOLS:
        try:
            df = fetch(sym)
        except Exception as ex:
            print(sym, "ERR", ex); continue
        time.sleep(1)
        for s in [s for s in sigs.values() if s["sym"] == sym and s["status"] == "active"]:
            if monitor(df, s):
                icon = {"tp": "✅ TP", "sl": "❌ SL", "be": "➖ BE"}[s["status"]]
                tg(f"{icon} <b>{sym}</b> {s['side'].upper()} ({s['mode']})\nEntry {s['entry']:.5g} @ {s['t']}", sym)
        pends, news = detect(sym, df)
        for p in pends:
            if p["id"] not in pend and fresh(p["t"]):
                pend[p["id"]] = p
                tg(f"👀 <b>{sym}</b> H4 sweep ({p['side'].upper()}) tasdiqlandi\nLevel {p['level']:.5g}. CISD/Model-1 kutilmoqda", sym)
        for s in news:
            if s["id"] not in sigs and fresh(s["t"]):
                sigs[s["id"]] = s
                tg(f"🚨 <b>{sym} {s['side'].upper()}</b> [{s['mode'].upper()}]\nEntry {s['entry']:.5g}\nSL {s['sl']:.5g}\nTP {s['tp']:.5g} (RR {RR:g})", sym)
    new = dict(signals=sorted(sigs.values(), key=lambda x: x["t"])[-200:],
               pending=sorted(pend.values(), key=lambda x: x["t"])[-40:])
    if {k: new[k] for k in new} != {k: st[k] for k in new}:
        new["updated"] = now.strftime("%Y-%m-%d %H:%M UTC")
        json.dump(new, open(FILE, "w"), indent=1, default=float)

if __name__ == "__main__":
    main()
