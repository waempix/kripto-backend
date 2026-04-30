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
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "") or os.environ.get("CLAUDE_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")

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

# ── Binance fiyat proxy ─────────────────────────────────────────────────────
_tickers_cache = {"ts": 0, "data": None}

@app.get("/api/tickers")
def tickers():
    try:
        now = time.time()
        if _tickers_cache["data"] and (now - _tickers_cache["ts"]) < 15:
            return _tickers_cache["data"]
        data = get_pub("/api/v3/ticker/24hr")
        result = {"success": True, "data": data, "count": len(data) if isinstance(data, list) else 0}
        _tickers_cache["data"] = result
        _tickers_cache["ts"]   = now
        return result
    except Exception as e:
        if _tickers_cache["data"]:
            return _tickers_cache["data"]
        return {"success": False, "error": str(e), "data": []}


# ══════════════════════════════════════════════════════════════════════════════
#   ENDPOINT: /api/opportunities — AGRESİF YENİ COİN KEŞFİ (5 kriter)
# ══════════════════════════════════════════════════════════════════════════════
_opp_cache = {"ts": 0, "data": None}
_new_listings_cache = {"ts": 0, "data": set()}

BLACKLIST = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "FDUSD", "PYUSD", "USDE", "RLUSD",
    "EUR", "GBP", "TRY", "UAH", "BIDR", "NGN", "RUB", "BRL", "AUD", "JPY",
    "USDTTRY", "BTCDOWN", "BTCUP", "ETHDOWN", "ETHUP",
}

def is_valid_symbol(symbol: str) -> bool:
    if not symbol.endswith("USDT"):
        return False
    base = symbol[:-4]
    if base in BLACKLIST:
        return False
    for suffix in ("3L", "3S", "5L", "5S", "DOWN", "UP", "BULL", "BEAR"):
        if base.endswith(suffix):
            return False
    return True


def get_new_listings():
    now = time.time()
    if _new_listings_cache["data"] and now - _new_listings_cache["ts"] < 3600:
        return _new_listings_cache["data"]
    try:
        info = get_pub("/api/v3/exchangeInfo")
        cutoff_ms = (now - 30*24*3600) * 1000
        new_set = set()
        for s in info.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            if not is_valid_symbol(s.get("symbol", "")):
                continue
            onboard = s.get("onboardDate", 0)
            if onboard and onboard > cutoff_ms:
                new_set.add(s["symbol"])
        _new_listings_cache["data"] = new_set
        _new_listings_cache["ts"]   = now
        return new_set
    except Exception:
        return set()


@app.get("/api/opportunities")
def opportunities(limit: int = 30, min_volume: float = 500000):
    try:
        now = time.time()
        if _opp_cache["data"] and now - _opp_cache["ts"] < 60:
            cached = _opp_cache["data"]
            return {**cached, "cached": True}

        tickers = get_pub("/api/v3/ticker/24hr")
        if not isinstance(tickers, list):
            return {"success": False, "error": "Binance ticker hatası"}

        new_listings = get_new_listings()

        opportunities_list = []
        for t in tickers:
            try:
                sym = t["symbol"]
                if not is_valid_symbol(sym):
                    continue

                vol_24h = float(t["quoteVolume"])
                if vol_24h < min_volume:
                    continue

                price_change = float(t["priceChangePercent"])
                price        = float(t["lastPrice"])
                high_24h     = float(t["highPrice"])
                low_24h      = float(t["lowPrice"])
                trades       = int(t.get("count", 0))

                if price <= 0 or high_24h <= 0 or low_24h <= 0:
                    continue

                base = sym[:-4]
                reasons = []
                score = 0
                categories = []

                if price_change > 25 and vol_24h > 5_000_000:
                    score += 35
                    reasons.append(f"🚀 Pump %{price_change:.1f}")
                    categories.append("pump")
                elif price_change > 15 and vol_24h > 2_000_000:
                    score += 25
                    reasons.append(f"📈 Momentum %{price_change:.1f}")
                    categories.append("pump")
                elif price_change > 8 and vol_24h > 1_000_000:
                    score += 12
                    reasons.append(f"↑ Yükseliş %{price_change:.1f}")
                    categories.append("momentum")

                if price_change < -20 and vol_24h > 3_000_000:
                    dip_distance = (price - low_24h) / low_24h * 100
                    if dip_distance < 5:
                        score += 30
                        reasons.append(f"🎯 Aşırı satım (%{price_change:.1f}) dip yakın")
                        categories.append("oversold")
                    else:
                        score += 15
                        reasons.append(f"⚠ Düşüş %{price_change:.1f}")
                elif price_change < -10 and vol_24h > 1_500_000:
                    score += 8
                    categories.append("oversold")

                if trades > 0:
                    if trades > 500_000 and vol_24h > 10_000_000:
                        score += 15
                        reasons.append(f"🐋 Yüksek aktivite ({trades//1000}K işlem)")
                        categories.append("volume")
                    elif trades > 200_000 and vol_24h > 5_000_000:
                        score += 8

                if sym in new_listings and vol_24h > 1_000_000:
                    score += 25
                    reasons.append("🆕 Yeni listing")
                    categories.append("new")

                volatility = (high_24h - low_24h) / low_24h * 100
                if volatility > 20 and vol_24h > 3_000_000:
                    high_distance = (high_24h - price) / price * 100
                    if high_distance < 3:
                        score += 20
                        reasons.append(f"💥 Breakout (zirveye %{high_distance:.1f})")
                        categories.append("breakout")
                    elif volatility > 30:
                        score += 10
                        reasons.append(f"⚡ Volatil %{volatility:.0f}")

                if vol_24h > 50_000_000:
                    score += 5
                elif vol_24h < 1_000_000:
                    score -= 3

                if score < 15 or not reasons:
                    continue

                opportunities_list.append({
                    "symbol":        base,
                    "full_symbol":   sym,
                    "price":         price,
                    "change_24h":    round(price_change, 2),
                    "volume_24h":    round(vol_24h, 0),
                    "volume_m":      round(vol_24h / 1_000_000, 2),
                    "high_24h":      high_24h,
                    "low_24h":       low_24h,
                    "volatility":    round(volatility, 2),
                    "trades":        trades,
                    "score":         score,
                    "reasons":       reasons[:4],
                    "categories":    categories,
                    "is_new_listing": sym in new_listings,
                })
            except Exception:
                continue

        opportunities_list.sort(key=lambda x: x["score"], reverse=True)
        result = {
            "success":       True,
            "count":         len(opportunities_list),
            "opportunities": opportunities_list[:limit],
            "timestamp":     int(now * 1000),
            "cached":        False,
        }
        _opp_cache["data"] = result
        _opp_cache["ts"]   = now
        return result
    except Exception as e:
        traceback.print_exc()
        if _opp_cache["data"]:
            return {**_opp_cache["data"], "cached": True, "error": str(e)}
        return {"success": False, "error": str(e), "opportunities": []}

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

def calc_ema(values, period):
    if len(values) < period:
        return sum(values) / len(values) if values else 0.0
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


# ── PİYASA DURUMU CACHE ──────────────────────────────────────────────────────
_market_state_cache = {"ts": 0, "state": None}

def get_market_state():
    now = time.time()
    if _market_state_cache["state"] and now - _market_state_cache["ts"] < 300:
        return _market_state_cache["state"]
    try:
        ticker = get_pub("/api/v3/ticker/24hr", {"symbol": "BTCUSDT"})
        btc_24h = float(ticker["priceChangePercent"])

        klines_4h = get_pub("/api/v3/klines", {"symbol": "BTCUSDT", "interval": "4h", "limit": 100})
        closes_4h = [float(k[4]) for k in klines_4h]
        ema20_4h  = calc_ema(closes_4h, 20)
        ema50_4h  = calc_ema(closes_4h, 50)
        btc_trend = "bullish" if ema20_4h > ema50_4h else "bearish"

        if btc_24h < -3.0:
            state = "bearish"
        elif btc_24h < -1.5 and btc_trend == "bearish":
            state = "bearish"
        elif btc_24h > 2.0 and btc_trend == "bullish":
            state = "bullish"
        else:
            state = "neutral"

        result = {
            "state":     state,
            "btc_24h":   round(btc_24h, 2),
            "btc_trend": btc_trend,
            "ema20_4h":  round(ema20_4h, 2),
            "ema50_4h":  round(ema50_4h, 2),
        }
        _market_state_cache["state"] = result
        _market_state_cache["ts"]    = now
        return result
    except Exception as e:
        return {"state": "neutral", "btc_24h": 0, "btc_trend": "unknown", "error": str(e)}


@app.get("/api/market-state")
def market_state_endpoint():
    return get_market_state()


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

    own_trend = "unknown"
    try:
        klines_4h  = get_pub("/api/v3/klines", {"symbol": sym, "interval": "4h", "limit": 60})
        closes_4h  = [float(k[4]) for k in klines_4h]
        if len(closes_4h) >= 50:
            own_ema20 = calc_ema(closes_4h, 20)
            own_ema50 = calc_ema(closes_4h, 50)
            own_trend = "bullish" if own_ema20 > own_ema50 else "bearish"
    except Exception:
        pass

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
    elif vol_ratio > 1.0: pass
    elif vol_ratio > 0.8: score -= 3
    elif vol_ratio > 0.6: score -= 7;  reasons.append(f"Hacim {vol_ratio:.1f}x zayıf")
    else:                 score -= 12; reasons.append(f"Hacim {vol_ratio:.1f}x çok zayıf")

    if   price_change > 25: score += 5
    elif price_change > 15: score += 3
    elif price_change > 8:  score += 2
    elif price_change < -15: score -= 8; reasons.append(f"%{price_change:.1f} düşüş")
    elif price_change < -8:  score -= 4

    if price_change < -3 and vol_ratio < 1.0:
        score -= 8
        reasons.append("⚠️ Düşüşte zayıf hacim (bıçak tuzağı)")

    if buy_pressure < 0.9 and price_change < -3:
        score -= 5
        reasons.append("⚠️ Satış baskısı + düşüş")

    market = get_market_state()
    if market["state"] == "bearish":
        if score >= 65:
            score = min(score, 60)
            reasons.append(f"⚠️ Piyasa bearish (BTC {market['btc_24h']:+.1f}%) — AL iptal")
        elif score >= 50:
            score -= 4

    if own_trend == "bearish":
        if score >= 65:
            score = min(score, 58)
            reasons.append("⚠️ Trend aşağı (4h EMA) — AL iptal")
        elif score >= 50:
            score -= 3

    score = max(10, min(95, score))

    if   score >= 85: signal = "ÇOK GÜÇLÜ AL"
    elif score >= 75: signal = "GÜÇLÜ AL"
    elif score >= 65: signal = "AL"
    elif score >= 50: signal = "DİKKATLİ AL"
    elif score >= 40: signal = "BEKLE"
    elif score >= 30: signal = "SATIŞ"
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
        "own_trend":    own_trend,
        "market_state": market["state"],
    }

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
        limit_1h = min(days * 24 + 100, 1000)
        klines_1h = get_pub("/api/v3/klines", {"symbol": sym, "interval": "1h", "limit": limit_1h})
        if len(klines_1h) < 100:
            return {"success": False, "error": "yeterli veri yok", "symbol": symbol.upper()}

        limit_4h = min(days * 6 + 60, 500)
        klines_4h = get_pub("/api/v3/klines", {"symbol": sym, "interval": "4h", "limit": limit_4h})

        closes_1h = [float(k[4]) for k in klines_1h]
        vols_1h   = [float(k[7]) for k in klines_1h]
        times_1h  = [int(k[0])   for k in klines_1h]

        closes_4h = [float(k[4]) for k in klines_4h]
        times_4h  = [int(k[0])   for k in klines_4h]

        signals_list = []
        capital      = 1000.0

        for i in range(50, len(closes_1h) - 24, 4):
            window = closes_1h[max(0, i-50):i+1]
            r      = calc_rsi(window, 14)
            macd_l, macd_s = calc_macd(window)
            macd_sig = 1 if macd_l > macd_s else -1
            bb_up, bb_mid, bb_lo = calc_bollinger(window, 20, 2)
            price = closes_1h[i]
            bb_pos = -1 if price <= bb_lo * 1.02 else 1 if price >= bb_up * 0.98 else 0

            if i >= 48:
                cur_v  = sum(vols_1h[i-24:i])
                prev_v = sum(vols_1h[i-48:i-24])
                vol_ratio = cur_v / prev_v if prev_v > 0 else 1.0
            else:
                vol_ratio = 1.0

            price_24h_ago = closes_1h[i-24] if i >= 24 else closes_1h[0]
            price_change = (price - price_24h_ago) / price_24h_ago * 100

            t_now = times_1h[i]
            closes_4h_upto = [c for c, t in zip(closes_4h, times_4h) if t <= t_now]
            if len(closes_4h_upto) >= 50:
                ema20 = calc_ema(closes_4h_upto, 20)
                ema50 = calc_ema(closes_4h_upto, 50)
                own_trend = "bullish" if ema20 > ema50 else "bearish"
            else:
                own_trend = "unknown"

            score = 50
            if   r < 25: score += 15
            elif r < 35: score += 10
            elif r < 45: score += 5
            elif r > 75: score -= 12
            elif r > 65: score -= 6

            if macd_sig == 1: score += 6
            else:             score -= 4

            if bb_pos == -1: score += 8
            elif bb_pos == 1: score -= 10

            if   vol_ratio > 2.5: score += 10
            elif vol_ratio > 1.8: score += 6
            elif vol_ratio > 1.3: score += 3
            elif vol_ratio > 1.0: pass
            elif vol_ratio > 0.8: score -= 3
            elif vol_ratio > 0.6: score -= 7
            else:                 score -= 12

            if   price_change > 15: score += 3
            elif price_change > 8:  score += 2
            elif price_change < -15: score -= 8
            elif price_change < -8:  score -= 4

            if price_change < -3 and vol_ratio < 1.0:
                score -= 8

            if own_trend == "bearish" and score >= 65:
                score = min(score, 58)

            score = max(10, min(95, score))

            sig = None
            if score >= 65: sig = "AL"
            elif score < 30: sig = "SAT"
            if not sig:
                continue

            entry = closes_1h[i]
            
            if score >= 75:
                sl_pct, tp1_pct, tp2_pct, max_hours = -4, 8, 15, 96
            elif score >= 65:
                sl_pct, tp1_pct, tp2_pct, max_hours = -3.5, 6, 12, 72
            else:
                sl_pct, tp1_pct, tp2_pct, max_hours = -3, 5, 10, 48
            
            exit_price   = entry
            exit_hour    = max_hours
            exit_reason  = "TIME"
            peak_pct     = 0
            
            max_check = min(i + max_hours + 1, len(closes_1h))
            for j in range(i + 1, max_check):
                h = j - i
                price_j = closes_1h[j]
                pct_j   = (price_j - entry) / entry * 100
                
                if pct_j > peak_pct:
                    peak_pct = pct_j
                
                if sig == "AL":
                    if pct_j <= sl_pct:
                        exit_price, exit_hour, exit_reason = entry * (1 + sl_pct/100), h, "SL"
                        break
                    if pct_j >= tp2_pct:
                        exit_price, exit_hour, exit_reason = entry * (1 + tp2_pct/100), h, "TP2"
                        break
                    if peak_pct >= tp1_pct and (peak_pct - pct_j) >= 3:
                        exit_price, exit_hour, exit_reason = price_j, h, "TRAIL"
                        break
                else:
                    if pct_j >= -sl_pct:
                        exit_price, exit_hour, exit_reason = entry * (1 + abs(sl_pct)/100), h, "SL"
                        break
                    if pct_j <= -tp2_pct:
                        exit_price, exit_hour, exit_reason = entry * (1 - tp2_pct/100), h, "TP2"
                        break
            else:
                if max_check > i + 1:
                    exit_price = closes_1h[max_check - 1]
                    exit_hour  = max_check - 1 - i
            
            change = (exit_price - entry) / entry * 100
            if sig == "SAT": change = -change
            success = change > 0

            if success:
                capital *= (1 + abs(change) / 100 * 0.5)
            else:
                capital *= (1 - abs(change) / 100 * 0.5)

            signals_list.append({
                "timestamp":   times_1h[i],
                "signal":      sig,
                "entry":       round(entry, 6),
                "exit":        round(exit_price, 6),
                "change":      round(change, 2),
                "score":       score,
                "exit_reason": exit_reason,
                "hold_hours":  exit_hour,
                "peak_pct":    round(peak_pct, 2),
                "rsi":       round(r, 1),
                "own_trend": own_trend,
                "success":   success,
            })

        wins   = sum(1 for s in signals_list if s["success"])
        losses = len(signals_list) - wins
        win_rate = round(wins / len(signals_list) * 100, 1) if signals_list else 0

        al_signals = [s for s in signals_list if s["signal"] == "AL"]
        sat_signals = [s for s in signals_list if s["signal"] == "SAT"]
        al_wins = sum(1 for s in al_signals if s["success"])
        al_wr = round(al_wins / len(al_signals) * 100, 1) if al_signals else 0
        al_avg_change = round(sum(s["change"] for s in al_signals) / len(al_signals), 2) if al_signals else 0
        al_capital = 1000.0
        for s in al_signals:
            if s["success"]:
                al_capital *= (1 + abs(s["change"]) / 100 * 0.5)
            else:
                al_capital *= (1 - abs(s["change"]) / 100 * 0.5)

        strong = [s for s in al_signals if s["score"] >= 75]
        normal = [s for s in al_signals if 65 <= s["score"] < 75]
        strong_wr = round(sum(1 for s in strong if s["success"]) / len(strong) * 100, 1) if strong else 0
        normal_wr = round(sum(1 for s in normal if s["success"]) / len(normal) * 100, 1) if normal else 0
        
        exit_dist = {}
        for s in al_signals:
            r = s.get("exit_reason", "?")
            exit_dist[r] = exit_dist.get(r, 0) + 1
        
        win_changes  = [s["change"] for s in al_signals if s["success"]]
        loss_changes = [s["change"] for s in al_signals if not s["success"]]
        avg_win  = round(sum(win_changes)  / len(win_changes),  2) if win_changes  else 0
        avg_loss = round(sum(loss_changes) / len(loss_changes), 2) if loss_changes else 0
        
        rr_ratio = round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else 0

        return {
            "success":         True,
            "symbol":          symbol.upper(),
            "days":            days,
            "total_signals":   len(al_signals),
            "wins":            al_wins,
            "losses":          len(al_signals) - al_wins,
            "win_rate":        al_wr,
            "final_capital":   round(al_capital, 2),
            "profit_pct":      round((al_capital - 1000) / 10, 2),
            "avg_change":      al_avg_change,
            "strong_signals":  len(strong),
            "strong_win_rate": strong_wr,
            "normal_signals":  len(normal),
            "normal_win_rate": normal_wr,
            "al_avg_win":      avg_win,
            "al_avg_loss":     avg_loss,
            "al_risk_reward":  rr_ratio,
            "exit_distribution": exit_dist,
            "sat_signals":     len(sat_signals),
            "sat_wins":        sum(1 for s in sat_signals if s["success"]),
            "sat_win_rate":    round(sum(1 for s in sat_signals if s["success"]) / len(sat_signals) * 100, 1) if sat_signals else 0,
            "all_signals":     len(signals_list),
            "all_win_rate":    win_rate,
            "last_signals":    signals_list[-20:],
            "note":            "İSTATİSTİK: SADECE AL sinyalleri (sen spot ediyorsun, short yok). SAT'lar bilgi.",
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
    try:
        now = time.time()
        if _whale_cache["data"] and (now - _whale_cache["ts"]) < 120:
            return _whale_cache["data"]

        watch = ["BTC","ETH","SOL","BNB","XRP","AVAX","INJ","TAO","HYPE","PEPE",
                 "DOGE","LINK","UNI","ARB","OP","NEAR","SUI","APT","SHIB","BONK"]

        alerts = []
        totals = {}

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
            "model":      "claude-haiku-4-5",
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
        try:
            with urllib.request.urlopen(r, timeout=30) as res:
                data = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as he:
            try:
                err_body = json.loads(he.read().decode("utf-8"))
                err_msg = err_body.get("error", {}).get("message", str(he))
            except Exception:
                err_msg = str(he)
            return {"success": False, "text": f"Anthropic API hatası ({he.code}): {err_msg}"}
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

# ══════════════════════════════════════════════════════════════════════════════
#                  ENDPOINT: /api/telegram  (push notification)
# ══════════════════════════════════════════════════════════════════════════════
TG_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")

class TelegramMsg(BaseModel):
    text:    str
    silent:  bool = False

@app.post("/api/telegram")
def telegram_send(msg: TelegramMsg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return {"success": False, "error": "Telegram yapılandırılmamış. Render env'e TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID ekleyin."}
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {
            "chat_id":              TG_CHAT_ID,
            "text":                 msg.text,
            "parse_mode":           "Markdown",
            "disable_notification": msg.silent,
        }
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req  = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as res:
            body = json.loads(res.read().decode("utf-8"))
        return {"success": body.get("ok", False)}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ══════════════════════════════════════════════════════════════════════════════
#                  ENDPOINT: /api/sentiment-analysis  (Claude + haberler)
# ══════════════════════════════════════════════════════════════════════════════
_sentiment_cache = {}

class SentimentReq(BaseModel):
    symbol:    str
    news_list: list = []

@app.post("/api/sentiment-analysis")
def sentiment_analysis(req: SentimentReq):
    if not CLAUDE_API_KEY:
        return {"success": False, "error": "Claude API key eksik"}
    try:
        sym = req.symbol.upper()
        if sym in _sentiment_cache:
            ts, cached = _sentiment_cache[sym]
            if time.time() - ts < 1800:
                return cached

        relevant = []
        name_map = {
            "BTC":["bitcoin","btc"],          "ETH":["ethereum","ether","eth"],
            "SOL":["solana","sol "],           "BNB":["binance","bnb"],
            "XRP":["ripple","xrp"],           "DOGE":["dogecoin","doge"],
            "AVAX":["avalanche","avax"],       "LINK":["chainlink","link"],
            "PEPE":["pepe"],                   "SHIB":["shiba","shib"],
            "HYPE":["hyperliquid","hype"],     "TAO":["bittensor","tao"],
            "INJ":["injective","inj"],         "ARB":["arbitrum","arb"],
            "OP":["optimism"," op "],          "NEAR":["near protocol","near"],
            "APT":["aptos"],                   "SUI":["sui"],
            "ADA":["cardano","ada"],           "DOT":["polkadot","dot"],
            "MATIC":["polygon","matic"],       "POL":["polygon","pol"],
            "LTC":["litecoin","ltc"],          "UNI":["uniswap","uni"],
            "AAVE":["aave"],                   "BONK":["bonk"],
            "WLD":["worldcoin","wld"],         "JUP":["jupiter","jup"],
            "RENDER":["render","rndr"],        "FET":["fetch.ai","fet"],
            "AKT":["akash"],                   "PENDLE":["pendle"],
            "STRK":["starknet","strk"],        "IMX":["immutable"],
        }
        keywords = name_map.get(sym, [sym.lower()])

        for n in req.news_list[:40]:
            title = (n.get("title") or "").lower()
            if any(k in title for k in keywords):
                relevant.append(n.get("title"))

        if len(relevant) == 0:
            result = {
                "success":  True,
                "symbol":   sym,
                "sentiment":"nötr",
                "score":    50,
                "summary":  f"{sym} ile ilgili güncel haber yok.",
                "topics":   [],
                "news_count": 0,
            }
            _sentiment_cache[sym] = (time.time(), result)
            return result

        prompt = (f"Aşağıda {sym} ile ilgili güncel haber başlıkları var. "
                  f"Bunları analiz et ve JSON olarak cevap ver:\n\n"
                  + "\n".join(f"- {t}" for t in relevant[:15])
                  + '\n\nSadece geçerli JSON döndür, başka hiçbir şey yazma:\n'
                  + '{"sentiment":"pozitif|negatif|nötr","score":0-100,"summary":"2-3 cümle Türkçe özet","topics":["konu1","konu2","konu3"]}')

        body = json.dumps({
            "model":      "claude-haiku-4-5",
            "max_tokens": 400,
            "system":     "Sen bir kripto piyasa analistisin. SADECE geçerli JSON döndür.",
            "messages":   [{"role": "user", "content": prompt}],
        }).encode("utf-8")

        r = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={"content-type": "application/json", "x-api-key": CLAUDE_API_KEY, "anthropic-version": "2023-06-01"},
        )
        with urllib.request.urlopen(r, timeout=20) as res:
            data = json.loads(res.read().decode("utf-8"))
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()

        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        parsed = json.loads(text)
        result = {
            "success":    True,
            "symbol":     sym,
            "sentiment":  parsed.get("sentiment", "nötr"),
            "score":      int(parsed.get("score", 50)),
            "summary":    parsed.get("summary", ""),
            "topics":     parsed.get("topics", [])[:5],
            "news_count": len(relevant),
        }
        _sentiment_cache[sym] = (time.time(), result)
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}

# ══════════════════════════════════════════════════════════════════════════════
#                  ENDPOINT: /api/batch-sentiment
# ══════════════════════════════════════════════════════════════════════════════
_batch_sentiment_cache = {"ts": 0, "data": {}}

class BatchSentimentReq(BaseModel):
    symbols:   list
    news_list: list = []

@app.post("/api/batch-sentiment")
def batch_sentiment(req: BatchSentimentReq):
    if not CLAUDE_API_KEY:
        return {"success": False, "error": "Claude API key eksik"}
    try:
        now = time.time()
        if req.symbols and now - _batch_sentiment_cache["ts"] < 900:
            cached = _batch_sentiment_cache["data"]
            if all(s.upper() in cached for s in req.symbols):
                return {"success": True, "cached": True, "sentiments": {s.upper(): cached[s.upper()] for s in req.symbols}}

        name_map = {
            "BTC":["bitcoin","btc"], "ETH":["ethereum","ether"],
            "SOL":["solana"], "BNB":["binance","bnb"],
            "XRP":["ripple","xrp"], "DOGE":["dogecoin","doge"],
            "AVAX":["avalanche","avax"], "LINK":["chainlink"],
            "PEPE":["pepe"], "SHIB":["shiba","shib"],
            "HYPE":["hyperliquid"], "TAO":["bittensor","tao"],
            "INJ":["injective","inj"], "ARB":["arbitrum"],
            "OP":["optimism"], "NEAR":["near protocol","nearprotocol"],
            "APT":["aptos"], "SUI":["sui network","suinetwork"],
            "ADA":["cardano","ada"], "DOT":["polkadot"],
            "MATIC":["polygon","matic"], "POL":["polygon","pol"],
            "LTC":["litecoin"], "UNI":["uniswap"],
            "AAVE":["aave"], "BONK":["bonk"],
            "WLD":["worldcoin"], "JUP":["jupiter"],
            "RENDER":["render network","rndr"], "FET":["fetch.ai"],
            "AKT":["akash"], "PENDLE":["pendle"],
            "STRK":["starknet"], "IMX":["immutable"],
            "HBAR":["hedera"], "TRX":["tron"],
            "ATOM":["cosmos"], "ETC":["ethereum classic"],
            "FIL":["filecoin"], "ALGO":["algorand"],
        }

        per_coin_news = {}
        for sym in req.symbols:
            s = sym.upper()
            keywords = name_map.get(s, [s.lower()])
            relevant = []
            for n in (req.news_list or [])[:50]:
                title = (n.get("title") or "").lower()
                if any(k in title for k in keywords):
                    relevant.append(n.get("title"))
            per_coin_news[s] = relevant[:5]

        result = {}
        to_analyze = {}
        for s, news in per_coin_news.items():
            if len(news) == 0:
                result[s] = {"sentiment":"nötr", "score":50, "news_count":0}
            else:
                to_analyze[s] = news

        if len(to_analyze) == 0:
            out = {"success": True, "cached": False, "sentiments": result}
            return out

        prompt_parts = ["Aşağıda kripto coinleri için güncel haber başlıkları var. "
                        "Her coin için sentiment skoru (0-100), sentiment etiketi "
                        "(pozitif/negatif/nötr) üret. SADECE aşağıdaki JSON formatında cevap ver:\n\n"]
        for s, news in to_analyze.items():
            prompt_parts.append(f"\n### {s}\n")
            for n in news:
                prompt_parts.append(f"- {n}\n")
        prompt_parts.append(
            '\n\nÇıktı formatı (başka HİÇ bir şey yazma):\n'
            '{"BTC":{"sentiment":"pozitif","score":72},'
            '"ETH":{"sentiment":"negatif","score":30}}\n'
        )
        prompt = "".join(prompt_parts)

        body = json.dumps({
            "model":      "claude-haiku-4-5",
            "max_tokens": 800,
            "system":     "Sen bir kripto piyasa analistisin. SADECE geçerli JSON döndür, markdown/açıklama yazma.",
            "messages":   [{"role":"user","content":prompt}],
        }).encode("utf-8")

        r = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={"content-type":"application/json","x-api-key":CLAUDE_API_KEY,"anthropic-version":"2023-06-01"},
        )
        try:
            with urllib.request.urlopen(r, timeout=30) as res:
                data = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as he:
            return {"success": False, "error": f"Anthropic {he.code}: {he.read().decode('utf-8')[:200]}"}

        text = "".join(b.get("text","") for b in data.get("content",[]) if b.get("type")=="text").strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"): text = text[4:]
            text = text.strip()

        parsed = json.loads(text)
        for s, news in to_analyze.items():
            if s in parsed:
                result[s] = {
                    "sentiment":  parsed[s].get("sentiment","nötr"),
                    "score":      int(parsed[s].get("score",50)),
                    "news_count": len(news),
                }
            else:
                result[s] = {"sentiment":"nötr","score":50,"news_count":len(news)}

        _batch_sentiment_cache["ts"]   = now
        _batch_sentiment_cache["data"] = {**_batch_sentiment_cache.get("data",{}), **result}
        return {"success": True, "cached": False, "sentiments": result, "analyzed": len(to_analyze)}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#   INTELLIGENCE ENGINE — 5 kaynak birleşik analiz
# ══════════════════════════════════════════════════════════════════════════════
_crypto_news_cache  = {"ts": 0, "data": {}}
_reddit_cache       = {"ts": 0, "data": {}}
_defillama_cache    = {"ts": 0, "data": {}}
_binance_ann_cache  = {"ts": 0, "data": []}
_whale_alert_cache  = {"ts": 0, "data": []}

COIN_PATTERNS = {
    "BTC": ["BITCOIN", "BTC"],
    "ETH": ["ETHEREUM", "ETH", "ETHER"],
    "SOL": ["SOLANA", "SOL"],
    "BNB": ["BINANCE COIN", "BNB"],
    "XRP": ["XRP", "RIPPLE"],
    "ADA": ["CARDANO", "ADA"],
    "DOGE": ["DOGECOIN", "DOGE"],
    "AVAX": ["AVALANCHE", "AVAX"],
    "DOT": ["POLKADOT", "DOT"],
    "LINK": ["CHAINLINK", "LINK"],
    "UNI": ["UNISWAP", "UNI"],
    "NEAR": ["NEAR PROTOCOL", "NEAR"],
    "APT": ["APTOS", "APT"],
    "SUI": ["SUI"],
    "ARB": ["ARBITRUM", "ARB"],
    "OP": ["OPTIMISM"],
    "INJ": ["INJECTIVE", "INJ"],
    "TAO": ["BITTENSOR", "TAO"],
    "PEPE": ["PEPE"],
    "SHIB": ["SHIBA INU", "SHIB"],
    "BONK": ["BONK"],
    "AAVE": ["AAVE"],
    "FET": ["FETCH.AI", "FET"],
    "RENDER": ["RENDER", "RNDR"],
    "WLD": ["WORLDCOIN", "WLD"],
    "JUP": ["JUPITER", "JUP"],
    "PENDLE": ["PENDLE"],
    "HYPE": ["HYPERLIQUID", "HYPE"],
    "ATOM": ["COSMOS", "ATOM"],
    "LTC": ["LITECOIN", "LTC"],
    "MATIC": ["POLYGON", "MATIC"],
    "POL": ["POL"],
    "TRX": ["TRON", "TRX"],
    "XMR": ["MONERO", "XMR"],
    "HBAR": ["HEDERA", "HBAR"],
}

POSITIVE_WORDS = {"surge","surges","rally","rallies","bullish","gains","soar","soars",
                  "moon","breakthrough","adoption","pump","green","rise","rising",
                  "higher","ath","record","approve","approval","launch","integrate",
                  "partnership","upgrade","milestone","success","boost","boosts","jump",
                  "skyrocket","explode","outperform","accumulate","institutional"}
NEGATIVE_WORDS = {"crash","crashes","plunge","plunges","bearish","drop","drops","fall",
                  "falls","ban","bans","hack","hacks","scam","dump","red","decline",
                  "lower","selloff","liquidat","panic","fear","reject","delay",
                  "lawsuit","sec","fraud","collapse","tumble","slump","bleed",
                  "exploit","drain","theft","stolen"}


def fetch_crypto_news_rss():
    now = time.time()
    if _crypto_news_cache["data"] and now - _crypto_news_cache["ts"] < 300:
        return _crypto_news_cache["data"]
    
    import re
    feeds = [
        ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("CoinTelegraph", "https://cointelegraph.com/rss"),
        ("Decrypt",       "https://decrypt.co/feed"),
    ]
    
    coin_data = {}
    for source_name, url in feeds:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                xml_text = r.read().decode("utf-8", errors="ignore")
        except Exception as e:
            print(f"{source_name} RSS hatası: {e}")
            continue
        
        items = re.findall(r'<item[^>]*>(.+?)</item>', xml_text, re.DOTALL)
        for item in items[:30]:
            title_m = (re.search(r'<title><!\[CDATA\[(.+?)\]\]></title>', item) or 
                       re.search(r'<title[^>]*>(.+?)</title>', item))
            desc_m  = (re.search(r'<description><!\[CDATA\[(.+?)\]\]></description>', item) or 
                       re.search(r'<description[^>]*>(.+?)</description>', item))
            date_m  = re.search(r'<pubDate>(.+?)</pubDate>', item)
            
            if not title_m:
                continue
            
            title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip()
            desc  = re.sub(r'<[^>]+>', '', desc_m.group(1)).strip() if desc_m else ""
            
            pub_ts = 0
            if date_m:
                try:
                    from email.utils import parsedate_to_datetime
                    pub_ts = parsedate_to_datetime(date_m.group(1)).timestamp()
                except Exception:
                    pass
            if pub_ts and (now - pub_ts) > 86400:
                continue
            
            full_text = (title + " " + desc).upper()
            full_lower = full_text.lower()
            
            mentioned = []
            for sym, patterns in COIN_PATTERNS.items():
                for pat in patterns:
                    if re.search(r'\b' + re.escape(pat) + r'\b', full_text):
                        mentioned.append(sym)
                        break
            
            if not mentioned:
                continue
            
            pos = sum(1 for w in POSITIVE_WORDS if w in full_lower)
            neg = sum(1 for w in NEGATIVE_WORDS if w in full_lower)
            
            for sym in mentioned:
                if sym not in coin_data:
                    coin_data[sym] = {
                        "news_count": 0, "positive": 0, "negative": 0, 
                        "titles": [], "sources": set()
                    }
                coin_data[sym]["news_count"] += 1
                coin_data[sym]["positive"]   += pos
                coin_data[sym]["negative"]   += neg
                coin_data[sym]["sources"].add(source_name)
                if len(coin_data[sym]["titles"]) < 3:
                    coin_data[sym]["titles"].append({
                        "title": title[:100],
                        "source": source_name,
                        "sentiment": "pozitif" if pos > neg else "negatif" if neg > pos else "nötr"
                    })
    
    for sym in coin_data:
        coin_data[sym]["sources"] = sorted(coin_data[sym]["sources"])
    
    _crypto_news_cache["data"] = coin_data
    _crypto_news_cache["ts"]   = now
    return coin_data


def fetch_reddit_trends():
    now = time.time()
    if _reddit_cache["data"] and now - _reddit_cache["ts"] < 600:
        return _reddit_cache["data"]
    try:
        coin_mentions = {}
        headers = {"User-Agent": "KriptoAI/1.0 (by /u/anon)"}
        for sub in ["CryptoCurrency", "CryptoMoonShots"]:
            try:
                url = f"https://www.reddit.com/r/{sub}/hot.json?limit=50"
                data = get_ext(url, timeout=10, headers=headers)
                posts = data.get("data", {}).get("children", [])
                for p in posts:
                    pd = p.get("data", {})
                    title = (pd.get("title", "") + " " + pd.get("selftext", ""))[:500].upper()
                    score = pd.get("score", 0) or 0
                    comments = pd.get("num_comments", 0) or 0
                    for coin in ["BTC","ETH","SOL","BNB","XRP","ADA","DOGE","AVAX","DOT","LINK",
                                 "UNI","NEAR","APT","SUI","ARB","OP","INJ","HYPE","TAO","PEPE",
                                 "SHIB","BONK","RENDER","FET","WLD","JUP","PENDLE","AAVE","ATOM",
                                 "LTC","FTM","MATIC","AKT","TRX","XMR","HBAR","POL","STRK"]:
                        if (f" {coin} " in f" {title} " or f"${coin}" in title 
                            or f"#{coin}" in title or title.startswith(coin + " ")):
                            if coin not in coin_mentions:
                                coin_mentions[coin] = {"mentions": 0, "score": 0, "comments": 0}
                            coin_mentions[coin]["mentions"] += 1
                            coin_mentions[coin]["score"]    += score
                            coin_mentions[coin]["comments"] += comments
            except Exception as e:
                print(f"Reddit /r/{sub} hatası: {e}")
                continue
        _reddit_cache["data"] = coin_mentions
        _reddit_cache["ts"]   = now
        return coin_mentions
    except Exception as e:
        print(f"Reddit hatası: {e}")
        return _reddit_cache["data"] or {}


def fetch_defillama_tvl():
    now = time.time()
    if _defillama_cache["data"] and now - _defillama_cache["ts"] < 1800:
        return _defillama_cache["data"]
    try:
        url = "https://api.llama.fi/protocols"
        data = get_ext(url, timeout=15)
        if not isinstance(data, list):
            return {}
        token_data = {}
        for proto in data[:300]:
            symbol = (proto.get("symbol") or "").upper().strip("-")
            if not symbol or symbol == "-":
                continue
            tvl_now    = proto.get("tvl") or 0
            change_1d  = proto.get("change_1d") or 0
            change_7d  = proto.get("change_7d") or 0
            if tvl_now < 1_000_000:
                continue
            if symbol not in token_data:
                token_data[symbol] = {
                    "tvl": 0, "tvl_change_1d": 0, "tvl_change_7d": 0,
                    "protocol_count": 0, "name": proto.get("name", "")
                }
            token_data[symbol]["tvl"]              += tvl_now
            token_data[symbol]["tvl_change_1d"]    += change_1d * tvl_now
            token_data[symbol]["tvl_change_7d"]    += change_7d * tvl_now
            token_data[symbol]["protocol_count"]   += 1
        for sym, d in token_data.items():
            if d["tvl"] > 0:
                d["tvl_change_1d"] = round(d["tvl_change_1d"] / d["tvl"], 2)
                d["tvl_change_7d"] = round(d["tvl_change_7d"] / d["tvl"], 2)
                d["tvl"] = round(d["tvl"] / 1_000_000, 1)
        _defillama_cache["data"] = token_data
        _defillama_cache["ts"]   = now
        return token_data
    except Exception as e:
        print(f"DefiLlama hatası: {e}")
        return _defillama_cache["data"] or {}


def fetch_binance_announcements():
    now = time.time()
    if _binance_ann_cache["data"] and now - _binance_ann_cache["ts"] < 900:
        return _binance_ann_cache["data"]
    try:
        url = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
               "?type=1&pageNo=1&pageSize=30&catalogId=48")
        data = get_ext(url, timeout=10)
        articles = data.get("data", {}).get("articles", [])
        listings = []
        cutoff = (now - 30*24*3600) * 1000
        for a in articles[:30]:
            title = a.get("title", "") or ""
            release = a.get("releaseDate", 0) or 0
            if release < cutoff:
                continue
            up = title.upper()
            if "WILL LIST" in up or "LISTING" in up or "LISTS" in up:
                import re
                m = re.search(r'\(([A-Z0-9]{2,10})\)', title)
                if m:
                    listings.append({
                        "symbol": m.group(1),
                        "title": title[:120],
                        "released": release,
                    })
        _binance_ann_cache["data"] = listings
        _binance_ann_cache["ts"]   = now
        return listings
    except Exception as e:
        print(f"Binance announcements hatası: {e}")
        return _binance_ann_cache["data"] or []


def fetch_whale_alerts():
    now = time.time()
    if _whale_alert_cache["data"] and now - _whale_alert_cache["ts"] < 300:
        return _whale_alert_cache["data"]
    try:
        url = "https://whale-alert.io/feed/all"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                xml_text = r.read().decode("utf-8", errors="ignore")
        except Exception:
            return _whale_alert_cache["data"] or []
        import re
        items = re.findall(r'<item>(.+?)</item>', xml_text, re.DOTALL)
        alerts = []
        cutoff = now - 24*3600
        for item in items[:50]:
            title_m = re.search(r'<title><!\[CDATA\[(.+?)\]\]></title>', item)
            date_m  = re.search(r'<pubDate>(.+?)</pubDate>', item)
            if not title_m:
                continue
            title = title_m.group(1)
            ts = 0
            if date_m:
                try:
                    from email.utils import parsedate_to_datetime
                    ts = parsedate_to_datetime(date_m.group(1)).timestamp()
                except Exception:
                    pass
            if ts and ts < cutoff:
                continue
            coin_m = re.search(r'#([A-Z]{2,10})', title)
            usd_m  = re.search(r'\$(\d[\d,]*)', title)
            if coin_m and usd_m:
                try:
                    usd = int(usd_m.group(1).replace(",", ""))
                    if usd >= 1_000_000:
                        alerts.append({
                            "symbol": coin_m.group(1),
                            "usd": usd,
                            "title": title[:150],
                            "ts": ts,
                        })
                except Exception:
                    continue
        _whale_alert_cache["data"] = alerts
        _whale_alert_cache["ts"]   = now
        return alerts
    except Exception as e:
        print(f"Whale Alert hatası: {e}")
        return _whale_alert_cache["data"] or []


@app.get("/api/intelligence/{symbol}")
def intelligence(symbol: str):
    sym = symbol.upper()
    try:
        news_data  = fetch_crypto_news_rss()
        rd_data    = fetch_reddit_trends()
        tvl_data   = fetch_defillama_tvl()
        ann_data   = fetch_binance_announcements()
        whale_data = fetch_whale_alerts()

        signals = []
        score   = 50

        nd = news_data.get(sym, {})
        news_count = nd.get("news_count", 0)
        n_pos = nd.get("positive", 0)
        n_neg = nd.get("negative", 0)
        sources = nd.get("sources", [])
        if news_count > 0:
            net = n_pos - n_neg
            multi_source = len(sources) >= 2
            if net >= 5:
                bonus = 14 if multi_source else 10
                score += bonus
                signals.append(f"📰 Çok pozitif haber ({news_count}x, {len(sources)} kaynak)")
            elif net >= 2:
                score += 6
                signals.append(f"📰 Olumlu haberler ({news_count})")
            elif net <= -5:
                score -= 14 if multi_source else 10
                signals.append(f"📰 Çok negatif haber ({news_count}x)")
            elif net <= -2:
                score -= 6
                signals.append(f"📰 Olumsuz haberler ({news_count})")
            elif news_count >= 5:
                score += 3
                signals.append(f"📰 Haber yoğun ({news_count})")

        rd = rd_data.get(sym, {})
        rd_mentions = rd.get("mentions", 0)
        rd_score    = rd.get("score", 0)
        if rd_mentions >= 5:
            score += 10
            signals.append(f"🔥 Reddit trending ({rd_mentions} post, {rd_score} upvote)")
        elif rd_mentions >= 3:
            score += 5
            signals.append(f"💬 Reddit'te konuşuluyor ({rd_mentions} post)")
        elif rd_mentions >= 1 and rd_score > 100:
            score += 3
            signals.append(f"💬 Reddit'te ilgi (+{rd_score} upvote)")

        tvl = tvl_data.get(sym, {})
        if tvl.get("tvl", 0) > 0:
            tvl_7d = tvl.get("tvl_change_7d", 0)
            if tvl_7d > 30:
                score += 15
                signals.append(f"🟢 TVL +{tvl_7d}% (7g) — büyük büyüme")
            elif tvl_7d > 10:
                score += 8
                signals.append(f"🟢 TVL +{tvl_7d}% (7g)")
            elif tvl_7d < -20:
                score -= 12
                signals.append(f"🔴 TVL {tvl_7d}% (7g) — düşüş")
            elif tvl_7d < -10:
                score -= 6
                signals.append(f"🔴 TVL {tvl_7d}% (7g)")

        for ann in ann_data:
            if ann["symbol"] == sym:
                age_hours = (time.time() - ann["released"]/1000) / 3600
                if age_hours < 168:
                    score += 25
                    signals.append(f"🆕 Binance listing! ({int(age_hours)}h önce)")
                    break

        sym_alerts = [a for a in whale_data if a["symbol"] == sym]
        if sym_alerts:
            total_usd = sum(a["usd"] for a in sym_alerts)
            count = len(sym_alerts)
            if total_usd > 100_000_000:
                score += 12
                signals.append(f"🐋 ${total_usd//1_000_000}M whale hareket ({count} işlem)")
            elif total_usd > 20_000_000:
                score += 7
                signals.append(f"🐋 ${total_usd//1_000_000}M whale hareket")
            elif count >= 3:
                score += 4
                signals.append(f"🐋 {count} whale işlem")

        score = max(10, min(95, score))

        return {
            "success": True,
            "symbol":  sym,
            "intelligence_score": int(round(score)),
            "signals": signals[:6],
            "details": {
                "news": {
                    "news_count":     news_count,
                    "positive_words": n_pos,
                    "negative_words": n_neg,
                    "sources":        sources,
                    "titles":         nd.get("titles", []),
                },
                "reddit": {
                    "mentions": rd_mentions,
                    "upvotes":  rd_score,
                    "comments": rd.get("comments", 0),
                },
                "defillama": {
                    "tvl_m":         tvl.get("tvl", 0),
                    "tvl_change_1d": tvl.get("tvl_change_1d", 0),
                    "tvl_change_7d": tvl.get("tvl_change_7d", 0),
                    "protocols":     tvl.get("protocol_count", 0),
                },
                "binance_listing": next((a for a in ann_data if a["symbol"] == sym), None),
                "whale_alerts": [
                    {"usd_m": a["usd"]//1_000_000, "title": a["title"][:80]} 
                    for a in sym_alerts[:5]
                ],
            }
        }
    except Exception as e:
        traceback.print_exc()
        return {"success": False, "symbol": sym, "error": str(e)}


@app.get("/api/intelligence-batch")
def intelligence_batch(symbols: str):
    try:
        sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()][:50]
        news_data  = fetch_crypto_news_rss()
        rd_data    = fetch_reddit_trends()
        tvl_data   = fetch_defillama_tvl()
        ann_data   = fetch_binance_announcements()
        whale_data = fetch_whale_alerts()
        
        result = {}
        for sym in sym_list:
            score = 50
            signals = []
            
            nd = news_data.get(sym, {})
            news_count = nd.get("news_count", 0)
            net = (nd.get("positive", 0) - nd.get("negative", 0))
            multi_source = len(nd.get("sources", [])) >= 2
            if net >= 5:
                score += 14 if multi_source else 10
                signals.append(f"📰 Çok pozitif ({news_count})")
            elif net >= 2:
                score += 6
            elif net <= -5:
                score -= 14 if multi_source else 10
                signals.append(f"📰 Negatif ({news_count})")
            elif net <= -2:
                score -= 6
            
            rd = rd_data.get(sym, {})
            m = rd.get("mentions", 0)
            if m >= 5:   score += 10; signals.append(f"🔥 Reddit trend ({m})")
            elif m >= 3: score += 5
            
            tvl = tvl_data.get(sym, {})
            tvl_7d = tvl.get("tvl_change_7d", 0) if tvl else 0
            if tvl_7d > 30:    score += 15; signals.append(f"🟢 TVL +{tvl_7d}%")
            elif tvl_7d > 10:  score += 8
            elif tvl_7d < -20: score -= 12; signals.append(f"🔴 TVL {tvl_7d}%")
            elif tvl_7d < -10: score -= 6
            
            for a in ann_data:
                if a["symbol"] == sym and (time.time() - a["released"]/1000) < 168*3600:
                    score += 25
                    signals.append("🆕 Yeni listing")
                    break
            
            alerts = [w for w in whale_data if w["symbol"] == sym]
            if alerts:
                total = sum(a["usd"] for a in alerts)
                if total > 100_000_000:   score += 12; signals.append(f"🐋 ${total//1_000_000}M whale")
                elif total > 20_000_000:  score += 7
                elif len(alerts) >= 3:    score += 4
            
            score = max(10, min(95, score))
            result[sym] = {
                "intelligence_score": int(round(score)),
                "signals": signals[:3],
            }
        
        return {"success": True, "intelligence": result, "count": len(result)}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# ▼▼▼ YENİ EKLENEN 3 ENDPOINT — 6-KAYNAK GÜVEN SİSTEMİ İÇİN ▼▼▼
# ══════════════════════════════════════════════════════════════════════════════
# Tarih: 2026-04
# Eklenenler:
#   1. /api/futures-metrics  → Binance Futures funding/OI/L-S
#   2. /api/trending-coins   → CoinGecko trending coins
#   3. /api/sectors          → CoinGecko sektör performansı
#
# Tüm cache'ler ayrı, mevcut endpoint'lere DOKUNULMADI.
# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
#                  ENDPOINT: /api/futures-metrics
# ══════════════════════════════════════════════════════════════════════════════
# Binance Futures Public API üzerinden funding rate + OI + Long/Short ratio.
# Tüm coinler için tek toplu çağrı (premium index) + coin-spesifik OI/L-S.
# 3 dakika cache (rate limit + Render hızı).

_futures_cache = {"ts": 0, "key": "", "data": None}

@app.get("/api/futures-metrics")
def futures_metrics(symbols: str = ""):
    """
    Binance Futures'tan funding rate + OI + L/S oranı.

    Funding rate < -0.05% → short squeeze potansiyeli (AL fırsatı)
    Funding rate > +0.10% → long squeeze riski (DİKKAT)
    OI artışı = yeni para giriyor
    L/S > 3.5 = aşırı long, contrarian SAT sinyali
    L/S < 0.5 = aşırı short, contrarian AL sinyali
    """
    try:
        now = time.time()
        sym_list = [s.strip().upper() for s in symbols.split(",") if s.strip()][:50]
        if not sym_list:
            return {"success": False, "error": "symbols param gerekli", "metrics": {}}

        cache_key = ",".join(sorted(sym_list))
        if (_futures_cache["data"] and
            _futures_cache["key"] == cache_key and
            now - _futures_cache["ts"] < 180):
            return {**_futures_cache["data"], "cached": True}

        # 1) Tüm funding rate'leri tek çağrıda (premium index)
        all_funding = {}
        try:
            data = get_pub("/fapi/v1/premiumIndex", base=FUTURES_BASE, timeout=15)
            if isinstance(data, list):
                for item in data:
                    sym_full = item.get("symbol", "")
                    if sym_full.endswith("USDT"):
                        all_funding[sym_full[:-4]] = {
                            "funding_rate": float(item.get("lastFundingRate", 0)) * 100,
                            "mark_price":   float(item.get("markPrice", 0)),
                        }
        except Exception as e:
            print(f"[futures] premium index hata: {e}")

        # 2) Her sembol için OI ve L/S — sadece istenen semboller
        result = {}
        for sym in sym_list:
            metrics = {
                "funding_rate":     None,
                "oi_change_pct":    None,
                "long_short_ratio": None,
                "signal":           "neutral",
                "alerts":           [],
            }

            # Funding (cache'den)
            if sym in all_funding:
                fr = all_funding[sym]["funding_rate"]
                metrics["funding_rate"] = round(fr, 4)
                if fr < -0.05:
                    metrics["alerts"].append(f"💎 Negatif funding {fr:.3f}% — short squeeze")
                    metrics["signal"] = "bullish"
                elif fr > 0.10:
                    metrics["alerts"].append(f"⚠️ Yüksek funding {fr:.3f}% — long squeeze riski")
                    metrics["signal"] = "bearish"
                elif fr > 0.05:
                    metrics["alerts"].append(f"⚠ Funding {fr:.3f}% yüksek")

            pair = sym + "USDT"

            # OI değişim — son 1 saat
            try:
                oi_data = get_pub("/futures/data/openInterestHist",
                    {"symbol": pair, "period": "1h", "limit": 2},
                    base=FUTURES_BASE, timeout=10)
                if isinstance(oi_data, list) and len(oi_data) >= 2:
                    oi_now  = float(oi_data[-1].get("sumOpenInterest", 0))
                    oi_prev = float(oi_data[-2].get("sumOpenInterest", 0))
                    if oi_prev > 0:
                        oi_change = (oi_now - oi_prev) / oi_prev * 100
                        metrics["oi_change_pct"] = round(oi_change, 2)
                        if oi_change > 5:
                            metrics["alerts"].append(f"📈 OI +{oi_change:.1f}% — yeni para giriyor")
                            if metrics["signal"] == "neutral":
                                metrics["signal"] = "bullish"
                        elif oi_change < -5:
                            metrics["alerts"].append(f"📉 OI {oi_change:.1f}% — pozisyon kapanıyor")
            except Exception:
                pass

            # Long/Short ratio
            try:
                ls_data = get_pub("/futures/data/globalLongShortAccountRatio",
                    {"symbol": pair, "period": "1h", "limit": 1},
                    base=FUTURES_BASE, timeout=10)
                if isinstance(ls_data, list) and ls_data:
                    ls = float(ls_data[0].get("longShortRatio", 1.0))
                    metrics["long_short_ratio"] = round(ls, 2)
                    if ls > 3.5:
                        metrics["alerts"].append(f"🐂 L/S {ls:.1f} — aşırı long, contrarian SAT")
                        metrics["signal"] = "bearish"
                    elif ls < 0.5:
                        metrics["alerts"].append(f"🐻 L/S {ls:.2f} — aşırı short, AL fırsatı")
                        metrics["signal"] = "bullish"
            except Exception:
                pass

            result[sym] = metrics

        out = {
            "success":   True,
            "metrics":   result,
            "timestamp": int(now * 1000),
            "cached":    False,
        }
        _futures_cache["data"] = out
        _futures_cache["ts"]   = now
        _futures_cache["key"]  = cache_key
        return out
    except Exception as e:
        traceback.print_exc()
        if _futures_cache["data"]:
            return {**_futures_cache["data"], "cached": True, "error": str(e)}
        return {"success": False, "error": str(e), "metrics": {}}


# ══════════════════════════════════════════════════════════════════════════════
#                  ENDPOINT: /api/trending-coins
# ══════════════════════════════════════════════════════════════════════════════
# CoinGecko trending — son 24 saatte en çok aranan 7-15 coin.
# Retail FOMO göstergesi, özellikle memecoin pump tespiti için.
# 30 dakika cache (CoinGecko free 30 req/dk, sorun olmaz).

_trending_cache = {"ts": 0, "data": None}

@app.get("/api/trending-coins")
def trending_coins():
    """CoinGecko trending coins — retail ilgi göstergesi."""
    try:
        now = time.time()
        if _trending_cache["data"] and now - _trending_cache["ts"] < 1800:
            return {**_trending_cache["data"], "cached": True}

        data = get_ext("https://api.coingecko.com/api/v3/search/trending", timeout=15)
        coins = data.get("coins", [])

        result = []
        for i, item in enumerate(coins[:15]):
            c = item.get("item", {})
            symbol = (c.get("symbol") or "").upper()
            if not symbol:
                continue
            result.append({
                "symbol":          symbol,
                "name":            c.get("name", ""),
                "market_cap_rank": c.get("market_cap_rank") or 0,
                "rank":            i + 1,
                "thumb":           c.get("thumb", ""),
                "score":           c.get("score", 0),
            })

        out = {
            "success":   True,
            "trending":  result,
            "count":     len(result),
            "timestamp": int(now * 1000),
            "cached":    False,
        }
        _trending_cache["data"] = out
        _trending_cache["ts"]   = now
        return out
    except Exception as e:
        if _trending_cache["data"]:
            return {**_trending_cache["data"], "cached": True, "error": str(e)}
        return {"success": False, "error": str(e), "trending": []}


# ══════════════════════════════════════════════════════════════════════════════
#                  ENDPOINT: /api/sectors
# ══════════════════════════════════════════════════════════════════════════════
# CoinGecko kategori performansı — AI/DeFi/Layer1/Meme sektörleri 24h.
# Sektör rotation tespiti için kritik.
# 1 saat cache (sektör yavaş değişir).

_sectors_cache = {"ts": 0, "data": None}

@app.get("/api/sectors")
def sectors():
    """CoinGecko sektör (kategori) performansı."""
    try:
        now = time.time()
        if _sectors_cache["data"] and now - _sectors_cache["ts"] < 3600:
            return {**_sectors_cache["data"], "cached": True}

        data = get_ext("https://api.coingecko.com/api/v3/coins/categories", timeout=15)

        # CoinGecko kategori ID'leri zaman zaman değişir (meme-token → meme-coin gibi).
        # Bu yüzden geniş bir keyword listesi tutuyoruz — kelimenin geçtiği her ID dahil.
        relevant_keywords = [
            # AI
            "artificial-intelligence", "ai-meme", "ai-agent",
            # DeFi
            "decentralized-finance-defi", "decentralized-exchange", "defi",
            "yield", "lending", "liquid-staking",
            # Layers
            "layer-1", "layer-2", "smart-contract-platform", "rollup",
            # Memes (her türlü varyasyon)
            "meme", "meme-token", "meme-coin", "memes",
            "dog-themed", "cat-themed", "frog-themed",
            # Gaming
            "gaming", "gamefi", "play-to-earn", "metaverse",
            # Diğer önemli sektörler
            "real-world-assets-rwa", "infrastructure",
            "depin", "dePIN", "rwa",
            "solana-ecosystem", "ethereum-ecosystem", "base-ecosystem",
        ]

        result = []
        for cat in data:
            cat_id = cat.get("id", "")
            if not any(kw in cat_id for kw in relevant_keywords):
                continue
            mc_change_24h = cat.get("market_cap_change_24h") or 0
            volume = cat.get("volume_24h") or 0
            mc = cat.get("market_cap") or 0
            # Eşiği gevşettik — küçük sektörler de dahil olsun ($10M)
            if mc < 10_000_000:
                continue
            result.append({
                "id":           cat_id,
                "name":         cat.get("name", ""),
                "market_cap_b": round(mc / 1e9, 2),
                "volume_24h_m": round(volume / 1e6, 0),
                "change_24h":   round(mc_change_24h, 2),
                "top_3_coins":  cat.get("top_3_coins_id", [])[:3],
            })

        result.sort(key=lambda x: x["change_24h"], reverse=True)

        out = {
            "success":      True,
            "sectors":      result[:25],  # 12 → 25 (daha çok sektör görünsün)
            "best_sector":  result[0] if result else None,
            "worst_sector": result[-1] if result else None,
            "timestamp":    int(now * 1000),
            "cached":       False,
        }
        _sectors_cache["data"] = out
        _sectors_cache["ts"]   = now
        return out
    except Exception as e:
        if _sectors_cache["data"]:
            return {**_sectors_cache["data"], "cached": True, "error": str(e)}
        return {"success": False, "error": str(e), "sectors": []}

# ════════════════════════════════════════════════════════════════════════════════
#                          7/24 SİNYAL TRACKER MODÜLÜ
# ════════════════════════════════════════════════════════════════════════════════
# Bu modül backend'de sürekli çalışır ve frontend'den bağımsız:
#   - Her 5 dakikada Binance'ten fiyat çeker, skoru hesaplar
#   - Skor 68+ olan coinleri Firebase'e signalHistory'ye ekler (24h cooldown)
#   - Her 30 saniyede bekleyen sinyalleri kontrol eder (SL/TP/TIME)
#   - Yeni sinyal/exit olunca Telegram bildirimi gönderir
#   - Frontend sadece /api/tracker/signals'tan okur
# ════════════════════════════════════════════════════════════════════════════════

import threading

# ── Firebase Admin SDK ───────────────────────────────────────────────────────
_firebase_db = None
_firebase_init_error = None

def _init_firebase():
    """Firebase Admin SDK'yı başlatır. FIREBASE_CREDENTIALS env'i bekler."""
    global _firebase_db, _firebase_init_error
    if _firebase_db is not None:
        return _firebase_db

    try:
        creds_str = os.environ.get("FIREBASE_CREDENTIALS", "")
        if not creds_str:
            _firebase_init_error = "FIREBASE_CREDENTIALS env eksik"
            return None

        import firebase_admin
        from firebase_admin import credentials, firestore

        # Aynı app birden fazla initialize edilemez
        if not firebase_admin._apps:
            creds_dict = json.loads(creds_str)
            cred = credentials.Certificate(creds_dict)
            firebase_admin.initialize_app(cred)

        _firebase_db = firestore.client()
        print("[TRACKER] ✅ Firebase Admin başlatıldı")
        return _firebase_db
    except Exception as e:
        _firebase_init_error = str(e)
        print(f"[TRACKER] ❌ Firebase başlatma hatası: {e}")
        return None

def _fb_get_signals():
    """Firebase'den signalHistory dizisini çeker. Bulamazsa boş liste."""
    db = _init_firebase()
    if not db:
        return []
    try:
        doc = db.collection("users").document("user_main").get()
        if not doc.exists:
            return []
        data = doc.to_dict() or {}
        return data.get("signalHistory", []) or []
    except Exception as e:
        print(f"[TRACKER] ❌ Firebase okuma hatası: {e}")
        return []

def _fb_set_signals(signals):
    """signalHistory'yi Firebase'e yazar (merge ile diğer alanları korur)."""
    db = _init_firebase()
    if not db:
        return False
    try:
        # Son 30 gün, max 500 kayıt
        cutoff = int((time.time() - 30 * 24 * 60 * 60) * 1000)
        trimmed = [s for s in signals if s.get("ts", 0) > cutoff][-500:]
        db.collection("users").document("user_main").set(
            {"signalHistory": trimmed}, merge=True
        )
        return True
    except Exception as e:
        print(f"[TRACKER] ❌ Firebase yazma hatası: {e}")
        return False

# ── Telegram bildirimi ───────────────────────────────────────────────────────
def _tg_notify(text):
    """Telegram'a mesaj gönder. Hata olursa sessizce geç."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        r = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(r, timeout=8) as res:
            return res.status == 200
    except Exception as e:
        print(f"[TRACKER] Telegram hatası: {e}")
        return False

# ── Tracker durumu (frontend için) ───────────────────────────────────────────
_tracker_state = {
    "running":          False,
    "last_scan":        0,
    "last_exit_check":  0,
    "scans_done":       0,
    "exits_done":       0,
    "signals_added":    0,
    "errors":           [],
    "started_at":       0,
}

def _log_error(prefix, err):
    msg = f"[{datetime.now().strftime('%H:%M:%S')}] {prefix}: {err}"
    print(f"[TRACKER] ❌ {msg}")
    _tracker_state["errors"].append(msg)
    _tracker_state["errors"] = _tracker_state["errors"][-20:]

# ── Skor hesaplama yardımcıları ──────────────────────────────────────────────
def _exit_params(score):
    """Skor bandına göre SL/TP/MaxHours.
    Önceki versiyon: TP'lere nadiren ulaşılıyordu, çoğu trade TIME ile kapanıyordu.
    Yeni: TP1 daha yakın (sık kâr al), SL daha sıkı (küçük kayıplar), max time kısalt.
    """
    if score >= 75:
        # Güçlü sinyal: biraz geniş hedef, ama hala makul
        return {"sl": -3.0, "tp1": 5.0, "tp2": 10.0, "max_hours": 48}
    if score >= 70:
        # Orta-güçlü: sıkı yönetim, hızlı kâr al
        return {"sl": -2.5, "tp1": 4.0, "tp2": 8.0, "max_hours": 36}
    # 68-70 zaten girmemeli (eşik 70'e çıkarıldı), ama güvenlik için
    return {"sl": -2.0, "tp1": 3.0, "tp2": 6.0, "max_hours": 24}

def _get_current_prices():
    """Tüm Binance USDT pair'lerinin son fiyatını getir."""
    try:
        tickers = get_pub("/api/v3/ticker/price")
        prices = {}
        for t in tickers:
            sym = t["symbol"]
            if sym.endswith("USDT"):
                prices[sym[:-4]] = float(t["price"])
        return prices
    except Exception as e:
        _log_error("Fiyat çekme", e)
        return {}

# ── EXIT KONTROL — bekleyen sinyalleri kapat ────────────────────────────────
def _check_exits():
    """Bekleyen sinyalleri SL/TP/TIME kuralları ile kontrol eder."""
    try:
        signals = _fb_get_signals()
        prices = _get_current_prices()
        if not prices:
            return

        now_ms = int(time.time() * 1000)
        FORCE_CLOSE_HOURS = 96
        changed = False
        exits_log = []

        for h in signals:
            if h.get("verified"):
                continue

            sym = h.get("sym", "")
            entry = h.get("entry", 0)
            ts = h.get("ts", 0)

            if not entry or entry <= 0 or not ts:
                continue

            age_ms = now_ms - ts
            age_h = age_ms / (60 * 60 * 1000)

            # Fiyat eşleştirme — birden fazla format dene
            cur = prices.get(sym)
            if cur is None and sym.endswith("USDT"):
                cur = prices.get(sym[:-4])
            if cur is None:
                cur = prices.get(sym + "USDT")

            # Fiyat yoksa ve 96 saat geçtiyse zorla kapat
            if cur is None:
                if age_h >= FORCE_CLOSE_HOURS:
                    h["exit"] = entry
                    h["change"] = 0
                    h["success"] = False
                    h["verified"] = True
                    h["verifiedAt"] = now_ms
                    h["exitReason"] = "NO_PRICE"
                    h["holdHours"] = round(age_h)
                    changed = True
                    exits_log.append(f"{sym} NO_PRICE")
                continue

            pct = ((cur - entry) / entry) * 100
            params = _exit_params(h.get("score", 68))

            # Trailing stop için zirve takibi
            peak = h.get("peakPrice", 0)
            if not peak or cur > peak:
                h["peakPrice"] = cur
                h["peakPct"] = pct
                changed = True

            peak_pct = h.get("peakPct", 0)
            exit_reason = None

            # 1) STOP-LOSS
            if pct <= params["sl"]:
                exit_reason = "SL"
            # 2) TP2
            elif pct >= params["tp2"]:
                exit_reason = "TP2"
            # 3) TRAILING — zirveden 3% düştü ve TP1'e ulaşmıştı
            elif peak_pct >= params["tp1"] and (peak_pct - pct) >= 3:
                exit_reason = "TRAIL"
            # 4) MAX TIME
            elif age_h >= params["max_hours"]:
                exit_reason = "TIME"
            # 5) ZORUNLU (96h)
            elif age_h >= FORCE_CLOSE_HOURS:
                exit_reason = "TIME"

            if exit_reason:
                h["exit"] = cur
                h["change"] = round(pct, 2)
                h["success"] = pct > 0
                h["verified"] = True
                h["verifiedAt"] = now_ms
                h["exitReason"] = exit_reason
                h["holdHours"] = round(age_h)
                changed = True
                exits_log.append(f"{sym} {exit_reason} {pct:+.2f}%")

                # Telegram bildirimi
                emoji = "✅" if pct > 0 else "❌"
                _tg_notify(
                    f"{emoji} <b>{sym}</b> kapandı\n"
                    f"Skor: {h.get('score', '?')}\n"
                    f"Sebep: {exit_reason}\n"
                    f"Giriş: ${entry:.6g}\n"
                    f"Çıkış: ${cur:.6g}\n"
                    f"Değişim: <b>{pct:+.2f}%</b>\n"
                    f"Süre: {round(age_h)}sa"
                )

        if changed:
            _fb_set_signals(signals)
            _tracker_state["exits_done"] += len(exits_log)
            if exits_log:
                print(f"[TRACKER] 🎯 Exits: {', '.join(exits_log)}")
        _tracker_state["last_exit_check"] = int(time.time())
    except Exception as e:
        _log_error("Exit kontrolü", e)

# ── SİNYAL TARAMA — yeni AL fırsatları ─────────────────────────────────────
def _scan_for_signals():
    """Backend'in /api/signals endpoint'ini çağırır, skor 68+ olanları kaydet."""
    try:
        # Mevcut sinyalleri çek
        existing = _fb_get_signals()
        now_ms = int(time.time() * 1000)
        # Cooldown: aynı coin için 72 saat (3 gün) bekle
        # Sebep: Bir trade tipik olarak 48-96 saat tutuluyor, açık trade varken
        # yeni AL eklemek mantıksız. 24 saat çok kısaydı, spam üretiyordu.
        cooldown_ms = 72 * 60 * 60 * 1000

        # Her sembol için son sinyal zamanı (verified+pending fark etmez)
        last_by_sym = {}
        for h in existing:
            sym = h.get("sym", "")
            ts = h.get("ts", 0)
            if sym and ts > last_by_sym.get(sym, 0):
                last_by_sym[sym] = ts

        # Sabit COINS listesi (frontend'dekiyle aynı)
        SCAN_SYMBOLS = [
            "BTC", "ETH", "BNB", "SOL", "XRP",
            "AVAX", "NEAR", "SUI", "INJ", "APT",
            "ARB", "OP", "STRK", "POL",
            "LINK", "AAVE", "HYPE", "PENDLE", "JUP", "UNI",
            "TAO", "AKT", "RENDER", "FET", "WLD",
            "IMX", "DOGE", "PEPE", "BONK", "SHIB",
        ]

        # Mevcut /api/signals endpoint'ini iç olarak çağırırsak skor üretir,
        # ama kendi içinde fiyat ve indikatör çekiyor. Daha basit yol:
        # Direkt smart_score'u her sembol için çağıralım.
        added = 0
        skipped_cooldown = 0
        new_signals_summary = []
        cooldown_debug = []  # debug için
        prices = _get_current_prices()

        for sym in SCAN_SYMBOLS:
            try:
                # Cooldown kontrolü
                last_ts = last_by_sym.get(sym, 0)
                if last_ts > 0 and now_ms - last_ts < cooldown_ms:
                    skipped_cooldown += 1
                    hours_since = (now_ms - last_ts) / (60 * 60 * 1000)
                    cooldown_debug.append(f"{sym}({hours_since:.0f}h)")
                    continue

                # Skoru hesapla
                try:
                    result = compute_smart_score(sym)
                except HTTPException as he:
                    # 400 = coin Binance Spot'ta yok (HYPE, AKT gibi DEX-only coinler)
                    # Bunları sessizce atla, error log'a yazma (her 15 dakika tekrar olmasın)
                    if he.status_code == 400:
                        continue
                    raise

                if not result or not result.get("success"):
                    continue

                score = result.get("score", 0)
                # Skor eşiği: 68 → 70 (kaliteyi artırmak için yükseltildi)
                # Gerekçe: 68'de win rate %50 (rastgele), 70+ daha seçici olmalı
                if score < 70:
                    continue

                price = prices.get(sym)
                if not price or price <= 0:
                    continue

                # Yeni sinyal ekle
                new_sig = {
                    "sym":       sym,
                    "score":     score,
                    "techScore": result.get("tech_score", score),
                    "signal":    result.get("rec", "AL"),
                    "entry":     price,
                    "ts":        now_ms,
                    "verified":  False,
                }
                existing.append(new_sig)
                last_by_sym[sym] = now_ms
                added += 1
                new_signals_summary.append(f"{sym}({score})")

                # Telegram bildirimi
                _tg_notify(
                    f"🔔 <b>YENİ AL SİNYALİ</b>\n"
                    f"<b>{sym}</b> · Skor: {score}\n"
                    f"Giriş: <b>${price:.6g}</b>\n"
                    f"RSI: {result.get('rsi', '—')} · "
                    f"MACD: {'🟢' if result.get('macd', 0) == 1 else '🔴'}\n"
                    f"Sebep: {', '.join(result.get('reasons', [])[:3])}"
                )
            except Exception as e:
                _log_error(f"Sembol {sym} taraması", e)
                continue

        if added > 0:
            _fb_set_signals(existing)
            _tracker_state["signals_added"] += added
            print(f"[TRACKER] 📝 {added} yeni: {', '.join(new_signals_summary)} (cooldown: {skipped_cooldown})")
        elif skipped_cooldown > 0:
            print(f"[TRACKER] 📝 0 yeni, {skipped_cooldown} cooldown'da")

        _tracker_state["last_scan"] = int(time.time())
        _tracker_state["scans_done"] += 1
    except Exception as e:
        _log_error("Sinyal taraması", e)

# ── BACKGROUND THREAD ────────────────────────────────────────────────────────
def _tracker_loop():
    """Sürekli çalışan ana döngü. Exit her 30sn, scan her 5dk."""
    print("[TRACKER] 🚀 Background thread başladı")
    _tracker_state["running"] = True
    _tracker_state["started_at"] = int(time.time())

    last_scan = 0
    SCAN_INTERVAL = 900       # 15 dakika (1h mum yapısına uygun)
    EXIT_INTERVAL = 30        # 30 saniye (fiyat anlık değişimi yakalar)

    while True:
        try:
            now = time.time()

            # Her 30 saniyede exit check
            _check_exits()

            # Her 5 dakikada scan
            if now - last_scan >= SCAN_INTERVAL:
                _scan_for_signals()
                last_scan = now

            time.sleep(EXIT_INTERVAL)
        except Exception as e:
            _log_error("Ana döngü", e)
            time.sleep(EXIT_INTERVAL)

# ── BAŞLATMA — uygulama yüklendiğinde thread'i çalıştır ──────────────────────
_tracker_thread_started = False

@app.on_event("startup")
def _start_tracker():
    global _tracker_thread_started
    if _tracker_thread_started:
        return
    if not os.environ.get("FIREBASE_CREDENTIALS"):
        print("[TRACKER] ⚠️ FIREBASE_CREDENTIALS yok, tracker başlatılmıyor")
        return
    _init_firebase()
    if _firebase_db is None:
        print(f"[TRACKER] ⚠️ Firebase başlamadı: {_firebase_init_error}")
        return

    t = threading.Thread(target=_tracker_loop, daemon=True, name="tracker")
    t.start()
    _tracker_thread_started = True
    _tg_notify("🤖 KriptoAI Tracker başlatıldı (7/24 mod)")

# ── ENDPOINTS — frontend için ─────────────────────────────────────────────────
@app.get("/api/tracker/status")
def tracker_status():
    """Tracker durumu — uptime, son tarama, hatalar."""
    state = dict(_tracker_state)
    if state["started_at"]:
        state["uptime_seconds"] = int(time.time()) - state["started_at"]
    state["firebase_ok"] = _firebase_db is not None
    state["firebase_error"] = _firebase_init_error
    state["telegram_configured"] = bool(TG_TOKEN and TG_CHAT_ID)
    return state

@app.get("/api/tracker/signals")
def tracker_signals():
    """Tüm sinyal geçmişi — frontend bunu okuyup gösterir."""
    signals = _fb_get_signals()
    return {
        "success":   True,
        "signals":   signals,
        "total":     len(signals),
        "pending":   sum(1 for s in signals if not s.get("verified")),
        "verified":  sum(1 for s in signals if s.get("verified")),
        "wins":      sum(1 for s in signals if s.get("verified") and s.get("success")),
        "losses":    sum(1 for s in signals if s.get("verified") and not s.get("success")),
    }

@app.get("/api/tracker/stats")
def tracker_stats():
    """Win rate ve skor bandı analizi."""
    signals = _fb_get_signals()
    verified = [s for s in signals if s.get("verified")]
    wins = [s for s in verified if s.get("success")]
    losses = [s for s in verified if not s.get("success")]

    def avg(lst, key):
        if not lst: return 0
        return round(sum(s.get(key, 0) for s in lst) / len(lst), 2)

    # Skor bandı
    bands = {
        "GÜÇLÜ AL (≥80)":  [s for s in verified if s.get("score", 0) >= 80],
        "AL (68-79)":       [s for s in verified if 68 <= s.get("score", 0) < 80],
        "DİKKATLİ AL":     [s for s in verified if 55 <= s.get("score", 0) < 68],
    }
    band_stats = {}
    for name, band in bands.items():
        if not band:
            band_stats[name] = {"count": 0, "win_rate": 0, "avg_change": 0}
        else:
            won = sum(1 for s in band if s.get("success"))
            band_stats[name] = {
                "count":       len(band),
                "win_rate":    round(100 * won / len(band), 1),
                "avg_change":  avg(band, "change"),
            }

    return {
        "success":      True,
        "total":        len(signals),
        "verified":     len(verified),
        "pending":      len(signals) - len(verified),
        "win_rate":     round(100 * len(wins) / len(verified), 1) if verified else 0,
        "avg_win":      avg(wins, "change"),
        "avg_loss":     avg(losses, "change"),
        "total_pnl":    avg(verified, "change"),
        "bands":        band_stats,
    }

@app.post("/api/tracker/clear")
def tracker_clear():
    """TÜM sinyal geçmişini siler. Test/sıfırlama için."""
    if _fb_set_signals([]):
        return {"success": True, "message": "Tüm geçmiş silindi"}
    return {"success": False, "error": "Firebase yazma başarısız"}

@app.post("/api/tracker/clear-pending")
def tracker_clear_pending():
    """Sadece bekleyen sinyalleri siler, doğrulanmışları korur."""
    signals = _fb_get_signals()
    verified = [s for s in signals if s.get("verified")]
    if _fb_set_signals(verified):
        return {
            "success": True,
            "kept": len(verified),
            "removed": len(signals) - len(verified),
        }
    return {"success": False, "error": "Firebase yazma başarısız"}

# Manuel tarama tetikleme (test için)
@app.post("/api/tracker/scan")
def tracker_force_scan():
    """Hemen tarama yap (5dk beklemeden). Test için."""
    threading.Thread(target=_scan_for_signals, daemon=True).start()
    return {"success": True, "message": "Tarama başlatıldı"}

@app.post("/api/tracker/check-exits")
def tracker_force_exits():
    """Hemen exit kontrolü yap. Test için."""
    threading.Thread(target=_check_exits, daemon=True).start()
    return {"success": True, "message": "Exit kontrolü başlatıldı"}

@app.post("/api/tracker/clear-errors")
def tracker_clear_errors():
    """Errors listesini temizle (eski Render restart hatalarını sil)."""
    _tracker_state["errors"] = []
    return {"success": True, "message": "Errors temizlendi"}
