import os, time, hmac, hashlib, json, traceback
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import urllib.request, urllib.parse

# ── Ortam değişkenleri ────────────────────────────────────────────────────────
READ_KEY       = os.environ.get("BINANCE_API_KEY", "")
READ_SECRET    = os.environ.get("BINANCE_API_SECRET", "")
TRADE_KEY      = os.environ.get("BINANCE_TRADE_KEY", "")
TRADE_SECRET   = os.environ.get("BINANCE_TRADE_SECRET", "")
NEWS_API_KEY   = os.environ.get("NEWS_API_KEY", "9b3eadd975b24497b940e46c2d3bb153")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")

BASE         = "https://api.binance.com"
FUTURES_BASE = "https://fapi.binance.com"

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── HTTP yardımcıları ─────────────────────────────────────────────────────────
def sign(params, secret):
    params["timestamp"]  = int(time.time() * 1000)
    params["recvWindow"] = 10000
    q   = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
    return q + "&signature=" + sig

def req(base, path, params, key, secret, method="GET"):
    q = sign(params, secret)
    if method == "POST":
        r = urllib.request.Request(base + path, data=q.encode(),
            headers={"X-MBX-APIKEY": key, "Content-Type": "application/x-www-form-urlencoded"})
    else:
        url = base + path + "?" + q
        r = urllib.request.Request(url, headers={"X-MBX-APIKEY": key})
    try:
        with urllib.request.urlopen(r, timeout=20) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode("utf-8"))
        raise HTTPException(status_code=e.code, detail=body.get("msg", str(e)))

def get_pub(path, params=None, base=BASE, timeout=15):
    q   = urllib.parse.urlencode(params or {})
    url = base + path + ("?" + q if q else "")
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def get_ext(url, timeout=10, headers=None):
    r = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(r, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))

# ── Temel ─────────────────────────────────────────────────────────────────────
@app.get("/api/ping")
def ping(): return {"status": "ok"}

@app.get("/api/portfolio")
def portfolio():
    try:
        account = req(BASE, "/api/v3/account", {}, READ_KEY, READ_SECRET)
        tickers = get_pub("/api/v3/ticker/price")
        px = {t["symbol"]: float(t["price"]) for t in tickers}
        res, tot = [], 0.0
        for b in account["balances"]:
            amt = float(b["free"]) + float(b["locked"])
            if amt <= 0: continue
            a = b["asset"]
            u = amt if a=="USDT" else amt*px.get(a+"USDT",0) or amt*px.get(a+"BTC",0)*px.get("BTCUSDT",1)
            tot += u
            res.append({"coin":a,"amount":round(amt,8),"usdtValue":round(u,2),"price":round(px.get(a+"USDT",0),6)})
        res.sort(key=lambda x: x["usdtValue"], reverse=True)
        return {"success":True,"portfolio":res,"totalUsdt":round(tot,2)}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/trades/{symbol}")
def trades(symbol: str):
    try:
        ts = req(BASE, "/api/v3/myTrades", {"symbol":symbol.upper()+"USDT","limit":20}, READ_KEY, READ_SECRET)
        return {"success":True,"trades":[{"time":t["time"],"side":"AL" if t["isBuyer"] else "SAT","price":float(t["price"]),"qty":float(t["qty"]),"total":round(float(t["price"])*float(t["qty"]),2),"fee":float(t["commission"]),"feeCoin":t["commissionAsset"]} for t in ts]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/open-orders")
def open_orders():
    try:
        orders = req(BASE, "/api/v3/openOrders", {}, READ_KEY, READ_SECRET)
        return {"success":True,"orders":[{"symbol":o["symbol"],"side":"AL" if o["side"]=="BUY" else "SAT","price":float(o["price"]),"qty":float(o["origQty"]),"status":o["status"]} for o in orders]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ── Futures ───────────────────────────────────────────────────────────────────
@app.get("/api/futures/balance")
def futures_balance():
    try:
        balances = req(FUTURES_BASE, "/fapi/v2/balance", {}, TRADE_KEY, TRADE_SECRET)
        usdt = next((b for b in balances if b["asset"]=="USDT"), None)
        if not usdt: return {"success":True,"balance":0,"availableBalance":0}
        return {"success":True,"balance":round(float(usdt["balance"]),2),"availableBalance":round(float(usdt["availableBalance"]),2)}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/futures/positions")
def futures_positions():
    try:
        positions = req(FUTURES_BASE, "/fapi/v2/positionRisk", {}, TRADE_KEY, TRADE_SECRET)
        active = [p for p in positions if float(p["positionAmt"]) != 0]
        result = []
        for p in active:
            amt, entry = float(p["positionAmt"]), float(p["entryPrice"])
            mark, pnl  = float(p["markPrice"]), float(p["unRealizedProfit"])
            lev = int(p["leverage"])
            side = "LONG" if amt > 0 else "SHORT"
            pct  = (pnl / (abs(amt) * entry / lev)) * 100 if entry > 0 else 0
            result.append({"symbol":p["symbol"],"side":side,"size":round(abs(amt),4),"entry":round(entry,4),"mark":round(mark,4),"pnl":round(pnl,2),"pnlPct":round(pct,2),"leverage":lev,"liquidation":round(float(p["liquidationPrice"]),4)})
        return {"success":True,"positions":result}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ── Emir modelleri ────────────────────────────────────────────────────────────
class SpotOrder(BaseModel):
    symbol: str; side: str; quantity: float
    orderType: str = "MARKET"; price: float = 0.0

class FuturesOrder(BaseModel):
    symbol: str; side: str; quantity: float
    leverage: int = 5; orderType: str = "MARKET"; price: float = 0.0
    stopLoss: float = 0.0; takeProfit: float = 0.0

@app.post("/api/order/spot")
def spot_order(order: SpotOrder):
    if not TRADE_KEY: raise HTTPException(status_code=400, detail="Trading key eksik")
    try:
        sym = order.symbol.upper() + "USDT"
        params = {"symbol":sym,"side":order.side.upper(),"type":order.orderType.upper(),"quantity":order.quantity}
        if order.orderType.upper() == "LIMIT":
            params["price"] = order.price; params["timeInForce"] = "GTC"
        result = req(BASE, "/api/v3/order", params, TRADE_KEY, TRADE_SECRET, "POST")
        return {"success":True,"orderId":result["orderId"],"symbol":sym,"side":order.side,"qty":order.quantity,"status":result["status"]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/futures/order")
def futures_order(order: FuturesOrder):
    if not TRADE_KEY: raise HTTPException(status_code=400, detail="Trading key eksik")
    try:
        sym = order.symbol.upper() + "USDT"
        req(FUTURES_BASE, "/fapi/v1/leverage", {"symbol":sym,"leverage":order.leverage}, TRADE_KEY, TRADE_SECRET, "POST")
        params = {"symbol":sym,"side":order.side.upper(),"type":order.orderType.upper(),"quantity":order.quantity}
        if order.orderType.upper() == "LIMIT":
            params["price"] = order.price; params["timeInForce"] = "GTC"
        result = req(FUTURES_BASE, "/fapi/v1/order", params, TRADE_KEY, TRADE_SECRET, "POST")
        orders = [{"orderId":result["orderId"],"type":"Ana Emir","status":result["status"]}]
        if order.stopLoss > 0:
            sl_side = "SELL" if order.side.upper()=="BUY" else "BUY"
            sl = req(FUTURES_BASE, "/fapi/v1/order", {"symbol":sym,"side":sl_side,"type":"STOP_MARKET","stopPrice":order.stopLoss,"closePosition":"true"}, TRADE_KEY, TRADE_SECRET, "POST")
            orders.append({"orderId":sl["orderId"],"type":"Stop-Loss","status":sl["status"]})
        if order.takeProfit > 0:
            tp_side = "SELL" if order.side.upper()=="BUY" else "BUY"
            tp = req(FUTURES_BASE, "/fapi/v1/order", {"symbol":sym,"side":tp_side,"type":"TAKE_PROFIT_MARKET","stopPrice":order.takeProfit,"closePosition":"true"}, TRADE_KEY, TRADE_SECRET, "POST")
            orders.append({"orderId":tp["orderId"],"type":"Take-Profit","status":tp["status"]})
        return {"success":True,"symbol":sym,"side":order.side,"leverage":order.leverage,"qty":order.quantity,"orders":orders}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/futures/close/{symbol}")
def close_position(symbol: str):
    if not TRADE_KEY: raise HTTPException(status_code=400, detail="Trading key eksik")
    try:
        sym = symbol.upper() + "USDT"
        positions = req(FUTURES_BASE, "/fapi/v2/positionRisk", {}, TRADE_KEY, TRADE_SECRET)
        pos = next((p for p in positions if p["symbol"]==sym and float(p["positionAmt"])!=0), None)
        if not pos: raise HTTPException(status_code=404, detail="Açık pozisyon yok")
        amt = float(pos["positionAmt"]); side = "SELL" if amt > 0 else "BUY"
        result = req(FUTURES_BASE, "/fapi/v1/order", {"symbol":sym,"side":side,"type":"MARKET","quantity":abs(amt),"reduceOnly":"true"}, TRADE_KEY, TRADE_SECRET, "POST")
        return {"success":True,"message":sym+" pozisyon kapatıldı","orderId":result["orderId"]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ══════════════════════════════════════════════════════════════════════════════
#                  TEKNİK GÖSTERGELER
# ══════════════════════════════════════════════════════════════════════════════
def calc_rsi(prices, period=14):
    if len(prices) < period + 1: return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_g  = sum(gains[-period:]) / period
    avg_l  = sum(losses[-period:]) / period
    if avg_l == 0: return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))

def calc_macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow: return 0.0, 0.0
    def ema(data, period):
        k = 2 / (period + 1)
        v = data[0]
        for p in data[1:]: v = p * k + v * (1 - k)
        return v
    macd_hist = []
    for i in range(slow, len(prices)+1):
        ef = ema(prices[max(0,i-fast):i], fast)
        es = ema(prices[max(0,i-slow):i], slow)
        macd_hist.append(ef - es)
    macd_line   = macd_hist[-1] if macd_hist else 0.0
    signal_line = ema(macd_hist[-signal:], signal) if len(macd_hist) >= signal else macd_line
    return macd_line, signal_line

def calc_bollinger(prices, period=20, std_dev=2):
    if len(prices) < period:
        last = prices[-1] if prices else 0
        return last*1.02, last, last*0.98
    recent  = prices[-period:]
    middle  = sum(recent) / period
    variance = sum((p - middle) ** 2 for p in recent) / period
    std = variance ** 0.5
    return middle + std*std_dev, middle, middle - std*std_dev

# ══════════════════════════════════════════════════════════════════════════════
#              ORTAK SMART-SCORE HESAPLAYICI
# ══════════════════════════════════════════════════════════════════════════════
def compute_smart_score(symbol, use_orderbook=True):
    sym = symbol.upper() + "USDT"
    ticker = get_pub("/api/v3/ticker/24hr", {"symbol": sym})
    klines = get_pub("/api/v3/klines", {"symbol": sym, "interval": "1h", "limit": 100})

    closes = [float(k[4]) for k in klines]
    rsi = calc_rsi(closes, 14)

    macd_line, signal_line = calc_macd(closes)
    macd_signal = 1 if macd_line > signal_line else -1

    bb_up, bb_mid, bb_lo = calc_bollinger(closes, 20, 2)
    price = closes[-1]
    if   price <= bb_lo * 1.02: bb_pos = -1
    elif price >= bb_up * 0.98: bb_pos =  1
    else:                       bb_pos =  0

    buy_pressure = 1.0
    if use_orderbook:
        try:
            depth = get_pub("/api/v3/depth", {"symbol": sym, "limit": 100})
            bid_v = sum(float(b[1]) * float(b[0]) for b in depth["bids"][:20])
            ask_v = sum(float(a[1]) * float(a[0]) for a in depth["asks"][:20])
            buy_pressure = bid_v / ask_v if ask_v > 0 else 1.0
        except Exception:
            pass

    # Hacim oranı — aynı birim (USDT) k[7]
    try:
        if len(klines) >= 48:
            cur_v  = sum(float(k[7]) for k in klines[-24:])
            prev_v = sum(float(k[7]) for k in klines[-48:-24])
            vol_ratio = cur_v / prev_v if prev_v > 0 else 1.0
        else:
            avg_h = sum(float(k[7]) for k in klines[-24:]) / 24
            latest = float(klines[-1][7]) if klines else avg_h
            vol_ratio = latest / avg_h if avg_h > 0 else 1.0
    except Exception:
        vol_ratio = 1.0
    vol_ratio = max(0.1, min(20.0, vol_ratio))

    price_change = float(ticker["priceChangePercent"])

    score = 50
    reasons = []

    if   rsi < 25: score += 15; reasons.append(f"RSI {rsi:.0f} aşırı satım")
    elif rsi < 35: score += 10; reasons.append(f"RSI {rsi:.0f} alım bölgesi")
    elif rsi < 45: score += 5
    elif rsi > 75: score -= 12; reasons.append(f"RSI {rsi:.0f} aşırı alım")
    elif rsi > 65: score -= 6

    if macd_signal == 1: score += 6; reasons.append("MACD alım sinyali")
    else: score -= 4

    if bb_pos == -1: score += 8; reasons.append("BB alt bant (dip)")
    elif bb_pos == 1: score -= 10; reasons.append("BB üst bant (zirve)")

    if   buy_pressure > 3.0:  score += 15; reasons.append(f"Çok güçlü alım {buy_pressure:.1f}x")
    elif buy_pressure > 2.0:  score += 10; reasons.append(f"Güçlü alım {buy_pressure:.1f}x")
    elif buy_pressure > 1.3:  score += 5
    elif buy_pressure < 0.6:  score -= 12; reasons.append(f"Güçlü satış {buy_pressure:.1f}x")
    elif buy_pressure < 0.85: score -= 5

    if   vol_ratio > 2.5: score += 10; reasons.append(f"Hacim {vol_ratio:.1f}x arttı")
    elif vol_ratio > 1.8: score += 6;  reasons.append(f"Hacim {vol_ratio:.1f}x arttı")
    elif vol_ratio > 1.3: score += 3
    elif vol_ratio < 0.7: score -= 4;  reasons.append(f"Hacim {vol_ratio:.1f}x düştü")

    if   price_change > 25: score += 5
    elif price_change > 15: score += 3
    elif price_change > 8:  score += 2
    elif price_change < -15: score -= 8; reasons.append(f"%{price_change:.1f} düşüş")
    elif price_change < -8:  score -= 4

    score = max(10, min(95, score))

    if   score >= 90: signal = "ÇOK GÜÇLÜ AL"
    elif score >= 80: signal = "GÜÇLÜ AL"
    elif score >= 68: signal = "AL"
    elif score >= 55: signal = "DİKKATLİ AL"
    elif score >= 45: signal = "BEKLE"
    elif score >= 35: signal = "SATIŞ"
    else:             signal = "SAT"

    return {
        "score":  round(score),
        "signal": signal,
        "rec":    signal,
        "reasons": reasons[:4],
        "rsi":          round(rsi, 1),
        "macd":         macd_signal,
        "bb_pos":       bb_pos,
        "bb_position":  bb_pos,
        "buy_pressure": round(buy_pressure, 2),
        "vol_ratio":    round(vol_ratio, 2),
        "volume_ratio": round(vol_ratio, 2),
        "price_change": round(price_change, 2),
        "price":        price,
    }

# ══════════════════════════════════════════════════════════════════════════════
#                ENDPOINT: /api/smart-score/{symbol}
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/smart-score/{symbol}")
def smart_score(symbol: str):
    try:
        d = compute_smart_score(symbol, use_orderbook=True)
        return {
            "success": True,
            "symbol":  symbol.upper(),
            "score":   d["score"],
            "signal":  d["signal"],
            "reasons": d["reasons"],
            "indicators": {
                "rsi":          d["rsi"],
                "macd":         d["macd"],
                "bb_position":  d["bb_position"],
                "buy_pressure": d["buy_pressure"],
                "volume_ratio": d["volume_ratio"],
                "price_change": d["price_change"],
            },
        }
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ══════════════════════════════════════════════════════════════════════════════
#        ENDPOINT: /api/signals  (toplu smart-score, 1 istek = 30 coin)
# ══════════════════════════════════════════════════════════════════════════════
_signals_cache = {"ts": 0, "key": "", "data": {}}

@app.get("/api/signals")
def signals(symbols: str = ""):
    try:
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()][:60]
        if not syms:
            return {"success": False, "error": "symbols param gerekli"}

        now = time.time()
        key = ",".join(sorted(syms))
        if _signals_cache["key"] == key and (now - _signals_cache["ts"]) < 90:
            return {"success": True, "cached": True, "signals": _signals_cache["data"]}

        out = {}
        for sym in syms:
            try:
                # use_orderbook=False: 30 coin için depth çağrısı rate limit yakar
                out[sym] = compute_smart_score(sym, use_orderbook=False)
            except Exception as err:
                out[sym] = {"error": str(err)}

        _signals_cache.update({"ts": now, "key": key, "data": out})
        return {"success": True, "cached": False, "signals": out}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ══════════════════════════════════════════════════════════════════════════════
#          ENDPOINT: /api/market-sentiment (Fear & Greed + Global)
# ══════════════════════════════════════════════════════════════════════════════
_sent_cache = {"ts": 0, "data": None}

FG_LABELS_TR = {
    "Extreme Fear":  "Aşırı Korku",
    "Fear":          "Korku",
    "Neutral":       "Nötr",
    "Greed":         "Açgözlülük",
    "Extreme Greed": "Aşırı Açgözlülük",
}

@app.get("/api/market-sentiment")
def market_sentiment():
    try:
        now = time.time()
        if _sent_cache["data"] and (now - _sent_cache["ts"]) < 900:
            return _sent_cache["data"]

        out = {"success": True}

        try:
            fg = get_ext("https://api.alternative.me/fng/?limit=7")
            items = fg.get("data", [])
            if items:
                cur = items[0]
                history = []
                for h in items:
                    history.append({
                        "value": int(h.get("value", 0)),
                        "label": h.get("value_classification", ""),
                    })
                out["fear_greed"] = {
                    "value":    int(cur.get("value", 50)),
                    "label":    cur.get("value_classification", "Neutral"),
                    "label_tr": FG_LABELS_TR.get(cur.get("value_classification", ""), "Nötr"),
                    "history":  history,
                }
        except Exception as e:
            out["fear_greed_error"] = str(e)

        try:
            cg = get_ext("https://api.coingecko.com/api/v3/global", timeout=10)
            d = cg.get("data", {})
            out["global"] = {
                "btc_dominance":          round(d.get("market_cap_percentage", {}).get("btc", 0), 2),
                "eth_dominance":          round(d.get("market_cap_percentage", {}).get("eth", 0), 2),
                "market_cap_change_24h":  round(d.get("market_cap_change_percentage_24h_usd", 0), 2),
                "total_market_cap_usd":   int(d.get("total_market_cap", {}).get("usd", 0)),
                "total_volume_usd":       int(d.get("total_volume", {}).get("usd", 0)),
                "active_cryptocurrencies": d.get("active_cryptocurrencies", 0),
            }
        except Exception as e:
            out["global"] = {}
            out["global_error"] = str(e)

        _sent_cache.update({"ts": now, "data": out})
        return out
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ══════════════════════════════════════════════════════════════════════════════
#                 ENDPOINT: /api/backtest/{symbol}
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/backtest/{symbol}")
def backtest(symbol: str, days: int = 30):
    try:
        days = max(7, min(90, days))
        sym  = symbol.upper() + "USDT"
        limit = days * 24 + 24
        klines = get_pub("/api/v3/klines", {"symbol": sym, "interval": "1h", "limit": min(limit, 1000)})
        if len(klines) < 48:
            return {"success": False, "error": "yeterli veri yok", "symbol": symbol.upper()}

        closes = [float(k[4]) for k in klines]
        times  = [int(k[0])   for k in klines]

        signals_list = []
        capital = 1000.0

        for i in range(14, len(closes) - 24):
            window = closes[max(0, i-14):i+1]
            r = calc_rsi(window, 14)

            sig = None
            if   r < 35: sig = "AL"
            elif r > 65: sig = "SAT"
            if not sig: continue

            entry = closes[i]
            exit_ = closes[i + 24]
            change = (exit_ - entry) / entry * 100
            if sig == "SAT": change = -change

            success = change > 0
            if success:
                capital *= (1 + abs(change) / 100 * 0.5)
            else:
                capital *= (1 - abs(change) / 100 * 0.5)

            signals_list.append({
                "timestamp": times[i],
                "signal":    sig,
                "entry":     round(entry, 6),
                "exit":      round(exit_, 6),
                "change":    round(change, 2),
                "rsi":       round(r, 1),
                "success":   success,
            })

        wins   = sum(1 for s in signals_list if s["success"])
        losses = len(signals_list) - wins
        win_rate = round(wins / len(signals_list) * 100, 1) if signals_list else 0

        return {
            "success":         True,
            "symbol":          symbol.upper(),
            "days":            days,
            "total_signals":   len(signals_list),
            "wins":            wins,
            "losses":          losses,
            "win_rate":        win_rate,
            "final_capital":   round(capital, 2),
            "profit_pct":      round((capital - 1000) / 10, 2),
            "last_signals":    signals_list[-20:],
        }
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ══════════════════════════════════════════════════════════════════════════════
#               ENDPOINT: /api/whale-activity (ücretsiz)
# ══════════════════════════════════════════════════════════════════════════════
_whale_cache = {"ts": 0, "data": None}

@app.get("/api/whale-activity")
def whale_activity(min_usd: int = 25000):
    """
    Son 1 saatte Binance'de gerçekleşmiş BÜYÜK işlemleri tespit eder.

    Yöntem: /api/v3/aggTrades — aynı taker order'ın aynı fiyattan aldığı tüm
    parçaları tek satır olarak birleştirir. Gerçek "balina" emri budur.
    Son 1 saatlik pencerede min_usd üstü işlemler "balina işlemi" sayılır.

    ÖNCEKİ HATA: /api/v3/trades sadece son ~500 işlem döner (BTC için
    ~30 saniyelik bir pencere). O kısa pencerede $100K+ tek işlem hemen
    hemen hiç olmuyordu — sonuç: hep 0.

    2 dakika cache.
    """
    try:
        now = time.time()
        if _whale_cache["data"] and (now - _whale_cache["ts"]) < 120:
            return _whale_cache["data"]

        watch = ["BTC","ETH","SOL","BNB","XRP","AVAX","INJ","TAO","HYPE","PEPE",
                 "DOGE","LINK","UNI","ARB","OP","NEAR","SUI","APT","SHIB","BONK"]

        alerts = []
        totals = {}

        # Son 1 saat: startTime = now - 60dk (ms cinsinden)
        one_hour_ago_ms = int((now - 3600) * 1000)

        for sym in watch:
            try:
                pair = sym + "USDT"
                trades_list = get_pub("/api/v3/aggTrades", {
                    "symbol":    pair,
                    "startTime": one_hour_ago_ms,
                    "limit":     1000,
                })
                big_buy = 0.0
                big_sell = 0.0
                count_big = 0
                for t in trades_list:
                    qty   = float(t["q"])
                    price = float(t["p"])
                    usd   = qty * price
                    if usd < min_usd: continue
                    count_big += 1
                    # aggTrades'te "m" = was the buyer the maker?
                    # True  → taker satıyor (agresif SAT)
                    # False → taker alıyor (agresif AL)
                    is_sell = bool(t.get("m"))
                    if is_sell: big_sell += usd
                    else:       big_buy  += usd
                    alerts.append({
                        "symbol": sym,
                        "side":   "SAT" if is_sell else "AL",
                        "usd":    round(usd, 0),
                        "qty":    round(qty, 4),
                        "price":  round(price, 6),
                        "time":   int(t["T"]),
                    })
                net = big_buy - big_sell
                # Küçük coinlerde eşik daha düşük olmalı (PEPE'de $250K hayal)
                threshold = 100000 if sym in ("BTC","ETH","SOL","BNB") else 50000
                totals[sym] = {
                    "big_buy_usd":  round(big_buy, 0),
                    "big_sell_usd": round(big_sell, 0),
                    "net_usd":      round(net, 0),
                    "count":        count_big,
                    "sentiment":    "birikim" if net > threshold
                                    else "dağıtım" if net < -threshold
                                    else "nötr",
                }
            except Exception:
                continue

        alerts.sort(key=lambda a: a["time"], reverse=True)
        alerts = alerts[:40]

        result = {
            "success":    True,
            "min_usd":    min_usd,
            "window":     "60 dakika",
            "alerts":     alerts,
            "by_symbol":  totals,
        }
        _whale_cache.update({"ts": now, "data": result})
        return result
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ══════════════════════════════════════════════════════════════════════════════
#                  ENDPOINT: /api/ai  (Claude proxy)
# ══════════════════════════════════════════════════════════════════════════════
class AIRequest(BaseModel):
    message: str
    context: str = ""
    focus:   str = ""

@app.post("/api/ai")
def ai_chat(req_body: AIRequest):
    if not CLAUDE_API_KEY:
        return {"success": False, "text": "AI aktif değil. Render env variable CLAUDE_API_KEY ekleyin."}
    try:
        system = ("Sen KriptoAI adlı trading asistanısın. Kullanıcıya altcoin ve kripto "
                  "odaklı, kısa, net, Türkçe tavsiyeler ver. Sayılarla konuş. Asla kesin "
                  "'al/sat' garantisi verme — risk uyar. Mevcut piyasa verisi verilecek, "
                  "ona göre yorumla.")
        user_msg = req_body.message
        if req_body.context:
            user_msg += "\n\nMevcut piyasa verileri:\n" + req_body.context

        body = json.dumps({
            "model":      "claude-3-5-haiku-20241022",
            "max_tokens": 700,
            "system":     system,
            "messages":   [{"role": "user", "content": user_msg}],
        }).encode("utf-8")

        r = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "content-type":      "application/json",
                "x-api-key":         CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(r, timeout=30) as res:
            data = json.loads(res.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return {"success": True, "text": text or "Yanıt alınamadı."}
    except Exception as e:
        return {"success": False, "text": "AI hatası: " + str(e)}

# ══════════════════════════════════════════════════════════════════════════════
#                  ENDPOINT: /api/news
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/news")
def get_news():
    try:
        query = ("(bitcoin%20OR%20ethereum%20OR%20altcoin%20OR%20crypto%20OR%20BTC%20OR%20ETH)"
                 "%20AND%20(price%20OR%20market%20OR%20trading%20OR%20rally%20OR%20crash)")
        url = (f"https://newsapi.org/v2/everything?q={query}&language=en"
               f"&sortBy=publishedAt&pageSize=30&apiKey={NEWS_API_KEY}")
        data = get_ext(url, timeout=10)

        if data.get("status") != "ok" or "articles" not in data:
            raise Exception(f"NewsAPI hatası: {data.get('message', 'bilinmeyen')}")

        POSITIVE = {"surge","surges","rally","rallies","bullish","gains","soar","soars",
                    "moon","breakthrough","adoption","pump","green","rise","rising",
                    "higher","ath","record"}
        NEGATIVE = {"crash","crashes","plunge","plunges","bearish","drop","drops","fall",
                    "falls","ban","bans","hack","hacks","scam","dump","red","decline",
                    "lower","selloff"}

        ALIASES = {
            "BITCOIN":"BTC","ETHEREUM":"ETH","SOLANA":"SOL","BINANCE":"BNB",
            "RIPPLE":"XRP","CARDANO":"ADA","DOGECOIN":"DOGE","AVALANCHE":"AVAX",
            "POLKADOT":"DOT","POLYGON":"MATIC","CHAINLINK":"LINK","UNISWAP":"UNI",
            "COSMOS":"ATOM","LITECOIN":"LTC","ARBITRUM":"ARB","OPTIMISM":"OP",
            "INJECTIVE":"INJ","HYPERLIQUID":"HYPE","BITTENSOR":"TAO","APTOS":"APT",
        }
        COINS = set(ALIASES.keys()) | {
            "BTC","ETH","SOL","BNB","XRP","ADA","DOGE","AVAX","DOT","MATIC","LINK",
            "UNI","ATOM","LTC","NEAR","APT","SUI","ARB","OP","INJ","HYPE","TAO",
            "PEPE","SHIB","BONK"
        }

        news_out = []
        for art in data["articles"][:30]:
            title = art.get("title", "") or ""
            desc  = art.get("description", "") or ""
            text_upper = (title + " " + desc).upper()
            text_lower = text_upper.lower()

            currencies = []
            for coin in COINS:
                if coin in text_upper or ("$" + coin) in text_upper:
                    currencies.append(ALIASES.get(coin, coin))
            currencies = list(dict.fromkeys(currencies))[:5]

            pos = sum(1 for w in POSITIVE if w in text_lower)
            neg = sum(1 for w in NEGATIVE if w in text_lower)
            sentiment = 1 if pos > neg else -1 if neg > pos else 0

            ts = 0
            try:
                dt = datetime.fromisoformat(art.get("publishedAt","").replace("Z","+00:00"))
                ts = int(dt.timestamp())
            except Exception:
                pass

            if currencies:
                news_out.append({
                    "title":       title,
                    "url":         art.get("url", "#"),
                    "source":      art.get("source", {}).get("name", "NewsAPI"),
                    "published":   ts,
                    "currencies":  currencies,
                    "sentiment":   sentiment,
                    "positive":    pos,
                    "negative":    neg,
                    "imageurl":    art.get("urlToImage", ""),
                    "description": desc[:150],
                })

        return {"success": True, "news": news_out, "source": "newsapi", "count": len(news_out)}
    except Exception as e:
        return {"success": False, "news": [], "error": str(e), "trace": traceback.format_exc()}

# ══════════════════════════════════════════════════════════════════════════════
#              ENDPOINT: /api/market-analysis (tüm coinler)
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/api/market-analysis")
def market_analysis_all():
    try:
        tickers = get_pub("/api/v3/ticker/24hr")
        out = {}
        for t in tickers:
            if not t["symbol"].endswith("USDT"): continue
            sym = t["symbol"][:-4]
            vol = float(t["quoteVolume"])
            if vol < 1_000_000: continue
            try:
                depth = get_pub("/api/v3/depth", {"symbol": t["symbol"], "limit": 100})
                bid_v = sum(float(b[1]) * float(b[0]) for b in depth["bids"][:20])
                ask_v = sum(float(a[1]) * float(a[0]) for a in depth["asks"][:20])
                bp = bid_v / ask_v if ask_v > 0 else 1.0
            except Exception:
                bp = 1.0
            out[sym] = {
                "price":        float(t["lastPrice"]),
                "change_24h":   float(t["priceChangePercent"]),
                "volume_24h":   vol,
                "buy_pressure": round(bp, 2),
                "high_24h":     float(t["highPrice"]),
                "low_24h":      float(t["lowPrice"]),
                "trades":       int(t["count"]),
            }
        return {"success": True, "data": out}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
