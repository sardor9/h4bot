"""TTFM°-style scanner: H4 sweep (C2) + M15 CISD (+ SMT). Runs on GitHub Actions -> Telegram + docs/signals.json"""
import os, json, time, random
from datetime import datetime, timezone
import numpy as np, requests, pandas as pd

SYMBOLS = {"EUR/USD": "OANDA:EURUSD", "GBP/USD": "OANDA:GBPUSD",
           "XAU/USD": "OANDA:XAUUSD", "XAG/USD": "OANDA:XAGUSD"}
PAIR = {"EUR/USD": "GBP/USD", "GBP/USD": "EUR/USD", "XAU/USD": "XAG/USD", "XAG/USD": "XAU/USD"}  # SMT pairs
BITGET = {"XAG/USD": "XAGUSDT"}            # fetched from Bitget (no key needed)
TP_LEVEL = float(os.getenv("TP_LEVEL", "2"))            # TP = projection -2 of the CISD range (TTFM)
LEVELS = [1, 2, 2.5, 4, 4.5]                            # projection levels shown in the message
BE_R = float(os.getenv("BE_R", "1"))                    # SL to entry after this R (0 = off)
MIN_RISK_ATR = float(os.getenv("MIN_RISK_ATR", "1.5"))  # ignore setups with SL tighter than this x M15 ATR
REQUIRE_SMT = os.getenv("REQUIRE_SMT", "0") == "1"
INFO_MSGS = os.getenv("INFO_MSGS", "1") == "1"
BEAT_H = float(os.getenv("BEAT_HOURS", "4"))
FRESH_MIN = 60
TZ = "America/New_York"                    # H4 candles open 17,21,01,05,09,13 NY (= TradingView FX)
H4_SHIFT, H4, M15 = pd.Timedelta(hours=1), pd.Timedelta(hours=4), pd.Timedelta(minutes=15)
KEY, TOKEN, CHAT = os.getenv("TWELVEDATA_KEY"), os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
FILE = "docs/signals.json"
SENT = [0]
_repo = os.getenv("GITHUB_REPOSITORY", "")
DASH = os.getenv("DASH_URL") or (f"https://{_repo.split('/')[0]}.github.io/{_repo.split('/')[1]}/" if "/" in _repo else "")
CHEER = ["😊 Signal yo'q, lekin bot uyg'oq va bozorni kuzatib turibdi!",
         "☕ Hozircha tinch. Sen choyingni ich, qolganini bot ko'radi!",
         "🧘 Sabr — trader kuchi. Signal yo'q, lekin hammasi nazoratda!",
         "🛰 Radar ishlayapti, sweep topilsa birinchi bo'lib aytaman!",
         "🎯 Signal yo'q. Yaxshi setup kutamiz, tasodifiy savdo kerak emas!"]

# ---------------------------------------------------------------- data
def _clean(df):
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    df = df.sort_values("t").set_index("t")[["open", "high", "low", "close"]]
    now = pd.Timestamp.now(TZ).tz_localize(None)
    return df[df.index + M15 <= now]       # closed bars only

def fetch_bitget(sym):
    r = requests.get("https://api.bitget.com/api/v2/mix/market/candles", timeout=30, params=dict(
        symbol=BITGET[sym], granularity="15m", limit=500, productType="usdt-futures")).json()
    if r.get("code") != "00000":
        raise RuntimeError(str(r))
    df = pd.DataFrame([x[:5] for x in r["data"]], columns=["ts", "open", "high", "low", "close"])
    df["t"] = pd.to_datetime(df.ts.astype("int64"), unit="ms", utc=True).dt.tz_convert(TZ).dt.tz_localize(None)
    return _clean(df)

def fetch(sym):
    if sym in BITGET:
        return fetch_bitget(sym)
    r = requests.get("https://api.twelvedata.com/time_series", timeout=30, params=dict(
        symbol=sym, interval="15min", outputsize=500, timezone=TZ, apikey=KEY)).json()
    if "values" not in r:
        raise RuntimeError(r.get("message", str(r)))
    df = pd.DataFrame(r["values"])
    df["t"] = pd.to_datetime(df.datetime)
    return _clean(df)

def h4_key(idx):
    return (idx - H4_SHIFT).floor("4h") + H4_SHIFT

# ---------------------------------------------------------------- TTFM engine
class Htf:
    """One H4 candle with the CISD bookkeeping of Fractal Model° [Pro+]."""
    def __init__(s, key, o):
        s.key, s.o = key, o
        s.h = s.l = s.c = None
        s.bullc = s.bullb = s.bearc = s.bearb = None
        s.bullhit = s.bearhit = False
        s.bullhitb = s.bearhitb = None

    def update(s, i, o, h, l, c, bsO, bsB, usO, usB):
        s.c = c
        if s.l is None or l < s.l:                       # new low -> CISD = open of the bearish delivery series
            s.l, s.bullc, s.bullb, s.bullhit, s.bullhitb = l, bsO, bsB, False, None
        if s.h is None or h > s.h:                       # new high -> CISD = open of the bullish delivery series
            s.h, s.bearc, s.bearb, s.bearhit, s.bearhitb = h, usO, usB, False, None
        if not s.bullhit and s.bullc is not None and c > s.bullc and i > s.bullb:
            s.bullhit, s.bullhitb = True, i
        if not s.bearhit and s.bearc is not None and c < s.bearc and i > s.bearb:
            s.bearhit, s.bearhitb = True, i

def simulate(sym, df, pdf):
    """Replay all closed M15 bars; return events (sweep / early / cisd / c2 / c2fail / c3)."""
    n = len(df)
    if n < 80:
        return []
    O, H, L, C = df.open.values, df.high.values, df.low.values, df.close.values
    keys = h4_key(df.index)
    bsO, bsB, usO, usB = [None] * n, [None] * n, [None] * n, [None] * n
    cb = cbb = cu = cub = None
    for i in range(n):                                   # consecutive bearish / bullish runs (Pine globals)
        pb = i > 0 and C[i - 1] < O[i - 1]
        pu = i > 0 and C[i - 1] > O[i - 1]
        if C[i] < O[i] and (not pb or cb is None):
            cb, cbb = O[i], i
        if C[i] > O[i] and (not pu or cu is None):
            cu, cub = O[i], i
        bsO[i], bsB[i], usO[i], usB[i] = cb, cbb, cu, cub
    prev_c = np.roll(C, 1); prev_c[0] = C[0]
    tr = np.maximum(H - L, np.maximum(abs(H - prev_c), abs(L - prev_c)))
    atr = pd.Series(tr).rolling(14, min_periods=1).mean().values
    pkeys = h4_key(pdf.index) if pdf is not None else None

    def smt(side, key, c1key, upto):
        if pdf is None:
            return None
        a, b = pdf[(pkeys == key) & (pdf.index <= upto)], pdf[pkeys == c1key]
        if a.empty or b.empty:
            return None
        return bool(a.low.min() >= b.low.min()) if side == "buy" else bool(a.high.max() <= b.high.max())

    ev = []
    def add(kind, side, j, h4, **k):
        ev.append(dict(kind=kind, sym=sym, side=side, t=str(df.index[j] + M15), h4=str(h4), **k))

    closed, cur, setups, watched = [], None, [], set()
    for i in range(n):
        if cur is None or keys[i] != cur.key:
            if cur is not None:
                closed.append(cur)
                cl = cur
                for s in setups:                          # advance at the H4 close
                    bull = s["side"] == "buy"
                    if s["state"] == "C2":
                        s["c3eq"] = (cl.h + cl.l) / 2
                        exp = cl.c > s["c2hi"] if bull else cl.c < s["c2lo"]
                        if s["cisd_done"] and exp:
                            s["state"] = "C3"
                            add("c3", s["side"], i - 1, s["h4"], lvl=s["c3eq"])
                        else:
                            s["state"] = "CONSOL"
                    elif s["state"] == "C3":
                        s["state"] = "DONE"
                if len(closed) >= 3:
                    c1 = closed[-2]
                    for side in ("buy", "sell"):
                        bull = side == "buy"
                        swept = cl.l < c1.l if bull else cl.h > c1.h
                        back = cl.c > c1.l if bull else cl.c < c1.h
                        if swept and back:                 # valid C2
                            setups.append(dict(
                                side=side, state="C2", h4=cl.key, c3eq=None,
                                c2ext=cl.l if bull else cl.h, c2hi=cl.h, c2lo=cl.l,
                                cisd_lvl=cl.bullc if bull else cl.bearc,
                                cisd_done=bool(cl.bullhit if bull else cl.bearhit),
                                smt=smt(side, cl.key, c1.key, df.index[i - 1])))
                            add("c2", side, i - 1, cl.key, cisd=setups[-1]["cisd_done"], smt=setups[-1]["smt"])
                        elif swept:                         # swept but closed beyond C1 -> no C2
                            add("c2fail", side, i - 1, cl.key)
                    setups = setups[-6:]
            cur = Htf(keys[i], O[i])
        cur.update(i, O[i], H[i], L[i], C[i], bsO[i], bsB[i], usO[i], usB[i])

        for s in setups:                                    # tick (after C2 closed)
            bull = s["side"] == "buy"
            if s["state"] == "C2":
                if (L[i] < s["c2ext"]) if bull else (H[i] > s["c2ext"]):
                    s["state"] = "FAILED"
                elif not s["cisd_done"] and s["cisd_lvl"] is not None and ((C[i] > s["cisd_lvl"]) if bull else (C[i] < s["cisd_lvl"])):
                    s["cisd_done"] = True
                    add("cisd", s["side"], i, s["h4"], entry=C[i], sl=s["c2ext"], cisd=s["cisd_lvl"],
                        smt=s["smt"], atr=atr[i])
            elif s["state"] == "C3":
                if s["c3eq"] is not None and ((L[i] < s["c3eq"]) if bull else (H[i] > s["c3eq"])):
                    s["state"] = "FAILED"

        if len(closed) >= 2:                                # C2 forming: sweep + early CISD
            pv = closed[-1]
            for side in ("buy", "sell"):
                bull = side == "buy"
                swept = cur.l < pv.l if bull else cur.h > pv.h
                inside = C[i] > pv.l if bull else C[i] < pv.h
                if not (swept and inside):
                    continue
                sm = smt(side, cur.key, pv.key, df.index[i])
                if (side, cur.key) not in watched:
                    watched.add((side, cur.key))
                    add("sweep", side, i, cur.key, lvl=pv.l if bull else pv.h, smt=sm)
                if (cur.bullhit and cur.bullhitb == i) if bull else (cur.bearhit and cur.bearhitb == i):
                    add("early", side, i, cur.key, entry=C[i], sl=cur.l if bull else cur.h,
                        cisd=cur.bullc if bull else cur.bearc, smt=sm, atr=atr[i])
    return ev

# ---------------------------------------------------------------- tracking
def monitor(df, s):
    e, risk = s["entry"], abs(s["entry"] - s["sl"])
    sell = s["side"] == "sell"
    for t, r in df[df.index > pd.Timestamp(s["checked"]) - M15 / 2].iterrows():
        if t < pd.Timestamp(s["t"]):
            continue
        sl_hit = r.high >= s["sl_cur"] if sell else r.low <= s["sl_cur"]
        tp_hit = r.low <= s["tp"] if sell else r.high >= s["tp"]
        reach = BE_R > 0 and (r.low <= e - BE_R * risk if sell else r.high >= e + BE_R * risk)
        end = None
        if sl_hit:
            end = "be" if s["sl_cur"] == e else "sl"
        elif tp_hit:
            end = "tp"
        elif reach:
            s["sl_cur"] = e
        s["checked"] = str(t + M15)
        if end:
            s["status"], s["closed"] = end, str(t + M15)
            return True
    return False

# ---------------------------------------------------------------- telegram
def tg(text, sym=None, count=True):
    if count:
        SENT[0] += 1
    if not (TOKEN and CHAT):
        print(text); return
    body = dict(chat_id=CHAT, text=text, parse_mode="HTML")
    btns = []
    if sym:
        btns.append({"text": "TradingView", "url": "https://www.tradingview.com/chart/?symbol=" + SYMBOLS[sym]})
    if DASH:
        btns.append({"text": "📊 Dashboard", "web_app": {"url": DASH}})
    if btns:
        body["reply_markup"] = {"inline_keyboard": [btns]}
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", timeout=20, json=body)

def fmt(sym, x):
    return f"{x:.{2 if sym == 'XAU/USD' else 3 if sym == 'XAG/USD' else 5}f}"

def tk(ts):  # NY time string -> Tashkent HH:MM
    t = pd.Timestamp(ts).tz_localize(TZ, ambiguous=True, nonexistent="shift_forward")
    return t.tz_convert("Asia/Tashkent").strftime("%H:%M")

def smt_txt(sym, v):
    return "—" if v is None else (f"✅ ({PAIR[sym]})" if v else "❌")

def fresh(ts):
    return (pd.Timestamp.now(TZ).tz_localize(None) - pd.Timestamp(ts)) <= pd.Timedelta(minutes=FRESH_MIN)

def make_signal(e):
    bull = e["side"] == "buy"
    entry, sl, cisd = e["entry"], e["sl"], e["cisd"]
    risk = entry - sl if bull else sl - entry
    rng = abs(cisd - sl)
    if risk <= 0 or rng <= 0 or risk < MIN_RISK_ATR * e["atr"]:
        return None
    tp = cisd + TP_LEVEL * rng if bull else cisd - TP_LEVEL * rng
    if (tp - entry if bull else entry - tp) <= 0:
        return None
    return dict(id=f"{e['sym']}|{e['side']}|{e['h4']}|{e['t']}", sym=e["sym"], side=e["side"], mode=e["kind"],
                entry=entry, sl=sl, sl_cur=sl, tp=tp, rng=rng, cisd=cisd, smt=e["smt"], t=e["t"], h4=e["h4"],
                status="active", checked=e["t"])

def main():
    now = datetime.now(timezone.utc)
    if now.weekday() == 5 or (now.weekday() == 6 and now.hour < 21) or (now.weekday() == 4 and now.hour >= 22):
        print("market closed"); return
    st = json.load(open(FILE))
    if st.get("v") != 2:                                  # new schema -> start clean
        st = dict(v=2, signals=[], pending=[], seen=[], last_beat=st.get("last_beat", ""), updated="")
    sigs = {s["id"]: s for s in st["signals"]}
    pend = {p["id"]: p for p in st["pending"]}
    seen = list(st.get("seen", []))
    dfs = {}
    for sym in SYMBOLS:
        try:
            dfs[sym] = fetch(sym)
        except Exception as ex:
            print(sym, "ERR", ex)
        time.sleep(1)
    for sym, df in dfs.items():
        for s in [s for s in sigs.values() if s["sym"] == sym and s["status"] == "active"]:
            if monitor(df, s):
                icon = {"tp": "✅ TP", "sl": "❌ SL", "be": "➖ BE"}[s["status"]]
                tg(f"{icon} <b>{sym}</b> {s['side'].upper()} ({s['mode']})\nEntry {fmt(sym, s['entry'])} · {tk(s['t'])} (Toshkent)", sym)
        for e in simulate(sym, df, dfs.get(PAIR[sym])):
            if not fresh(e["t"]):
                continue
            side, h4, kind = e["side"], e["h4"], e["kind"]
            eid = "|".join([kind, sym, side, h4] + ([e["t"]] if kind in ("early", "cisd") else []))
            if eid in seen:
                continue
            seen.append(eid)
            S = side.upper()
            if kind == "sweep":
                pend[f"{sym}|{side}|{h4}"] = dict(id=f"{sym}|{side}|{h4}", sym=sym, side=side, level=e["lvl"],
                                                  t=e["t"], h4=h4, smt=e["smt"])
                tg(f"👀 <b>{sym}</b> H4 sweep ({S}) — M15 C1 ichiga qaytdi\nLevel {fmt(sym, e['lvl'])}\n"
                   f"SMT: {smt_txt(sym, e['smt'])}\nCISD kutilmoqda", sym)
            elif kind in ("early", "cisd"):
                if any(x["sym"] == sym and x["side"] == side and x["h4"] == h4 and x["status"] == "active" for x in sigs.values()):
                    continue
                if REQUIRE_SMT and e["smt"] is not True:
                    continue
                sg = make_signal(e)
                if not sg:
                    continue
                sigs[sg["id"]] = sg
                bull = side == "buy"
                proj = " · ".join(f"-{k:g}: {fmt(sym, sg['cisd'] + (k if bull else -k) * sg['rng'])}" for k in LEVELS)
                rr = abs(sg["tp"] - sg["entry"]) / abs(sg["entry"] - sg["sl"])
                tag = "⚠️ ERTA CISD (H4 hali yopilmagan)" if kind == "early" else "✅ CISD (C2 yopilgan)"
                tg(f"🚨 <b>{sym} {S}</b>\n{tag}\nSMT: {smt_txt(sym, sg['smt'])}\n\n"
                   f"Entry {fmt(sym, sg['entry'])}\nSL {fmt(sym, sg['sl'])}\nTP {fmt(sym, sg['tp'])} (RR {rr:.1f})\n\n"
                   f"Proyeksiya: {proj}\n🕐 {tk(sg['t'])} (Toshkent)", sym)
            elif kind == "c2fail":
                if any(x["sym"] == sym and x["side"] == side and x["h4"] == h4 and x["status"] == "active" for x in sigs.values()):
                    tg(f"⚠️ <b>{sym} {S}</b>: H4 sham C1 ichiga yopilmadi — erta signal asosi yo'q. Pozitsiyani qayta ko'r!", sym)
            elif INFO_MSGS and kind == "c2":
                tg(f"✅ <b>{sym}</b> C2 shakllandi ({S})\nCISD: {'✅' if e['cisd'] else 'kutilmoqda'} · SMT: {smt_txt(sym, e['smt'])}", sym)
            elif INFO_MSGS and kind == "c3":
                tg(f"🎯 <b>{sym}</b> C3 shakllandi ({S})\nC4 T-Spot (C3 EQ): {fmt(sym, e['lvl'])}", sym)
    beat = st.get("last_beat", "")
    due = not beat or (now - datetime.fromisoformat(beat)).total_seconds() >= BEAT_H * 3600
    if SENT[0] > 0:
        beat = now.isoformat()
    elif BEAT_H > 0 and due:
        n = sum(1 for x in sigs.values() if x["status"] == "active")
        tg(random.choice(CHEER) + f"\n\nKuzatilmoqda: {', '.join(SYMBOLS)}\nFaol signallar: {n}", count=False)
        beat = now.isoformat()
    new = dict(v=2, last_beat=beat, seen=seen[-600:],
               signals=sorted(sigs.values(), key=lambda x: x["t"])[-200:],
               pending=sorted(pend.values(), key=lambda x: x["t"])[-40:])
    if {k: new[k] for k in new} != {k: st.get(k) for k in new}:
        new["updated"] = now.strftime("%Y-%m-%d %H:%M UTC")
        json.dump(new, open(FILE, "w"), indent=1, default=lambda o: bool(o) if isinstance(o, np.bool_) else float(o))

if __name__ == "__main__":
    main()
