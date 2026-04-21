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
# Frontend doğrudan Binance'e erişemediğinde (coğrafi blok, rate limit, CORS)
# backend üzerinden proxy — Render US sunucuları engellenmez.
_tickers_cache = {"ts": 0, "data": None}

@app.get("/api/tickers")
def tickers():
    try:
        now = time.time()
        # 15 saniye cache — rate limit koruması + hızlı cevap
        if _tickers_cache["data"] and (now - _tickers_cache["ts"]) < 15:
            return _tickers_cache["data"]
        data = get_pub("/api/v3/ticker/24hr")
        result = {"success": True, "data": data, "count": len(data) if isinstance(data, list) else 0}
        _tickers_cache["data"] = result
        _tickers_cache["ts"]   = now
        return result
    except Exception as e:
        if _tickers_cache["data"]:
            # Backend de başarısızsa eski cache'i dön — tamamen kör olma
            return _tickers_cache["data"]
        return {"success": False, "error": str(e), "data": []}


# ══════════════════════════════════════════════════════════════════════════════
#   ENDPOINT: /api/opportunities — AGRESİF YENİ COİN KEŞFİ (5 kriter)
# ══════════════════════════════════════════════════════════════════════════════
# Binance'in TÜM coinlerini tarar, potansiyel AL fırsatlarını bulur.
# Kriterler:
#   1. PUMP MOMENTUM:    24s > +15% ve hacim > $2M
#   2. OVERSOLD BOUNCE:  24s < -15% + hacim canlı (dip alım fırsatı)
#   3. VOLUME SPIKE:     hacim 24s öncesine göre 3x+ arttı
#   4. NEW LISTING:      son 30 günde eklenmiş + hacim kanıtı
#   5. BREAKOUT:         son 7 gün yatay + bugün hacim+fiyat patlaması
# ══════════════════════════════════════════════════════════════════════════════
_opp_cache = {"ts": 0, "data": None}
_new_listings_cache = {"ts": 0, "data": set()}

# Stable coin'ler ve yatırım değeri olmayanlar — dışla
BLACKLIST = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "FDUSD", "PYUSD", "USDE", "RLUSD",
    "EUR", "GBP", "TRY", "UAH", "BIDR", "NGN", "RUB", "BRL", "AUD", "JPY",
    "USDTTRY", "BTCDOWN", "BTCUP", "ETHDOWN", "ETHUP",
}

def is_valid_symbol(symbol: str) -> bool:
    """Spot USDT çifti + stable olmayan."""
    if not symbol.endswith("USDT"):
        return False
    base = symbol[:-4]
    if base in BLACKLIST:
        return False
    # Leveraged tokens (3L, 3S, DOWN, UP, BULL, BEAR)
    for suffix in ("3L", "3S", "5L", "5S", "DOWN", "UP", "BULL", "BEAR"):
        if base.endswith(suffix):
            return False
    return True


def get_new_listings():
    """Son 30 günde Binance'e eklenen coinleri tespit et (exchangeInfo kullanarak)."""
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
            # Binance exchangeInfo'da onboardDate bazen var, bazen yok
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
    """
    Agresif fırsat tarayıcı. Tüm Binance coinlerini 5 kriterle puanlar.
    
    Args:
        limit: Kaç coin dön (default 30)
        min_volume: Minimum 24s USDT hacmi (default $500K — meme dahil)
    """
    try:
        now = time.time()
        # 60 saniye cache — endpoint her dakika çağrılabilir
        if _opp_cache["data"] and now - _opp_cache["ts"] < 60:
            cached = _opp_cache["data"]
            return {**cached, "cached": True}

        # 1) Tüm ticker'ları çek (tek çağrı)
        tickers = get_pub("/api/v3/ticker/24hr")
        if not isinstance(tickers, list):
            return {"success": False, "error": "Binance ticker hatası"}

        # 2) Yeni listingler (hourly cache)
        new_listings = get_new_listings()

        # 3) Her coin için puan hesapla
        opportunities_list = []
        for t in tickers:
            try:
                sym = t["symbol"]
                if not is_valid_symbol(sym):
                    continue

                vol_24h      = float(t["quoteVolume"])
                if vol_24h < min_volume:
                    continue

                price_change = float(t["priceChangePercent"])
                price        = float(t["lastPrice"])
                high_24h     = float(t["highPrice"])
                low_24h      = float(t["lowPrice"])
                # Binance /ticker/24hr 'prevClosePrice' veya 'weightedAvgPrice' verir
                prev_vol     = float(t.get("prevClosePrice", price) or price)
                trades       = int(t.get("count", 0))

                if price <= 0 or high_24h <= 0 or low_24h <= 0:
                    continue

                base = sym[:-4]
                reasons = []
                score = 0
                categories = []

                # ── KRİTER 1: PUMP MOMENTUM (güçlü yükseliş + hacim) ──────────
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

                # ── KRİTER 2: OVERSOLD BOUNCE (dip + hacim canlı) ─────────────
                # 24s içinde düştü ama hacim var → dip alım fırsatı
                if price_change < -20 and vol_24h > 3_000_000:
                    # Fiyat dipten ne kadar uzakta?
                    dip_distance = (price - low_24h) / low_24h * 100
                    if dip_distance < 5:  # dibe çok yakın
                        score += 30
                        reasons.append(f"🎯 Aşırı satım (%{price_change:.1f}) dip yakın")
                        categories.append("oversold")
                    else:
                        score += 15
                        reasons.append(f"⚠ Düşüş %{price_change:.1f}")
                elif price_change < -10 and vol_24h > 1_500_000:
                    score += 8
                    categories.append("oversold")

                # ── KRİTER 3: VOLUME SPIKE (balina giriş sinyali) ─────────────
                # İşlem sayısı çok yüksekse (trade count) hacim normalin üstünde
                # Binance weightedAvgPrice üzerinden tahmini önceki hacim
                if trades > 0:
                    # Basit heuristic: vol_24h > ortalama olması beklenen
                    # 100k+ trade = büyük aktivite
                    if trades > 500_000 and vol_24h > 10_000_000:
                        score += 15
                        reasons.append(f"🐋 Yüksek aktivite ({trades//1000}K işlem)")
                        categories.append("volume")
                    elif trades > 200_000 and vol_24h > 5_000_000:
                        score += 8

                # ── KRİTER 4: NEW LISTING (son 30 gün) ────────────────────────
                if sym in new_listings and vol_24h > 1_000_000:
                    score += 25
                    reasons.append("🆕 Yeni listing")
                    categories.append("new")

                # ── KRİTER 5: BREAKOUT (volatilite + hacim) ───────────────────
                # 24h fiyat range'i > ortalama (volatilite arttı)
                volatility = (high_24h - low_24h) / low_24h * 100
                if volatility > 20 and vol_24h > 3_000_000:
                    # Günün zirvesine yakın? Breakout devam ediyor olabilir
                    high_distance = (high_24h - price) / price * 100
                    if high_distance < 3:  # zirveye çok yakın
                        score += 20
                        reasons.append(f"💥 Breakout (zirveye %{high_distance:.1f})")
                        categories.append("breakout")
                    elif volatility > 30:
                        score += 10
                        reasons.append(f"⚡ Volatil %{volatility:.0f}")

                # ── LIKIDITE BONUSU ─────────────────────────────────────────
                if vol_24h > 50_000_000:
                    score += 5  # çok likit
                elif vol_24h < 1_000_000:
                    score -= 3  # düşük likidite (risk)

                # Skor 0'dan küçükse listeye alma
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

        # Puana göre sırala, limit kadar dön
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

# ══════════════════════════════════════════════════════════════════════════════
#              ORTAK SMART-SCORE HESAPLAYICI
# ══════════════════════════════════════════════════════════════════════════════
def calc_ema(values, period):
    """Exponential Moving Average — son değere daha çok ağırlık verir."""
    if len(values) < period:
        return sum(values) / len(values) if values else 0.0
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period  # SMA başlangıç
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


# ── PİYASA DURUMU CACHE ──────────────────────────────────────────────────────
# BTC'nin 24 saat değişimi ve 4 saatlik trend durumu. Tüm sinyaller için
# ortak filtre — 5 dakika cache ile aşırı API çağrısını önle.
_market_state_cache = {"ts": 0, "state": None}

def get_market_state():
    """BTC'nin genel durumu — bearish/neutral/bullish."""
    now = time.time()
    if _market_state_cache["state"] and now - _market_state_cache["ts"] < 300:
        return _market_state_cache["state"]
    try:
        # BTC 24h değişim
        ticker = get_pub("/api/v3/ticker/24hr", {"symbol": "BTCUSDT"})
        btc_24h = float(ticker["priceChangePercent"])

        # BTC 4h trend: EMA20 vs EMA50 (son 100 4-saatlik mum)
        klines_4h = get_pub("/api/v3/klines", {"symbol": "BTCUSDT", "interval": "4h", "limit": 100})
        closes_4h = [float(k[4]) for k in klines_4h]
        ema20_4h  = calc_ema(closes_4h, 20)
        ema50_4h  = calc_ema(closes_4h, 50)
        btc_trend = "bullish" if ema20_4h > ema50_4h else "bearish"

        # Durum belirle
        if btc_24h < -3.0:
            state = "bearish"   # sert düşüş — risky, AL'lar filtrelenmeli
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
        # Hata durumunda nötr — filtre kapatma
        return {"state": "neutral", "btc_24h": 0, "btc_trend": "unknown", "error": str(e)}


@app.get("/api/market-state")
def market_state_endpoint():
    """Piyasa durumunu dışa aç — frontend de okusun istersen."""
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

    # ── TREND DOĞRULAMA — coin'in kendi 4h EMA'sı ────────────────────────
    # Coin'in teknik göstergeleri iyi olabilir ama trend hâlâ aşağıysa dip yapmamış.
    # Yükseliş trendi için EMA20(4h) > EMA50(4h) şartı aranır.
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

    # ── Hacim skorlama — zayıf hacimli yükselişler TUZAK olabilir ──
    # Büyük hacim artışı = güçlü hareket (birikim veya panik satış)
    # Düşük hacim = "kimse ilgilenmiyor" — dip yapsa bile alım gelmeyebilir
    if   vol_ratio > 2.5: score += 10; reasons.append(f"Hacim {vol_ratio:.1f}x arttı")
    elif vol_ratio > 1.8: score += 6;  reasons.append(f"Hacim {vol_ratio:.1f}x arttı")
    elif vol_ratio > 1.3: score += 3
    elif vol_ratio > 1.0: pass                                         # normal
    elif vol_ratio > 0.8: score -= 3                                    # hafif zayıf
    elif vol_ratio > 0.6: score -= 7;  reasons.append(f"Hacim {vol_ratio:.1f}x zayıf")
    else:                 score -= 12; reasons.append(f"Hacim {vol_ratio:.1f}x çok zayıf")

    if   price_change > 25: score += 5
    elif price_change > 15: score += 3
    elif price_change > 8:  score += 2
    elif price_change < -15: score -= 8; reasons.append(f"%{price_change:.1f} düşüş")
    elif price_change < -8:  score -= 4

    # ── BIÇAK YAKALAMA GUARDI ──
    # Fiyat düşerken hacim de düşükse: kimse almıyor demek, dip burada DEĞİL.
    # Klasik "bıçağı düşerken yakalama" tuzağı — bunu aktif engelliyoruz.
    if price_change < -3 and vol_ratio < 1.0:
        score -= 8
        reasons.append("⚠️ Düşüşte zayıf hacim (bıçak tuzağı)")

    # ── SATIŞ TARAFINDA BASKI + DÜŞÜŞ = kaçış sinyali ──
    if buy_pressure < 0.9 and price_change < -3:
        score -= 5
        reasons.append("⚠️ Satış baskısı + düşüş")

    # ── PİYASA YÖNÜ FİLTRESİ — BTC düşüşte ise AL sinyalini yumuşat ──
    # Altcoin'ler BTC'yi takip eder. BTC düşerken altcoin "AL fırsatı" çoğu zaman tuzak.
    # Eski veri: 19 AL sinyali, %26 isabet — sebep: piyasa bearish iken AL verildi.
    market = get_market_state()
    if market["state"] == "bearish":
        if score >= 68:
            score = min(score, 64)  # AL → DİKKATLİ AL
            reasons.append(f"⚠️ Piyasa bearish (BTC {market['btc_24h']:+.1f}%) — AL iptal")
        elif score >= 55:
            score -= 4  # DİKKATLİ AL'ı zayıflat

    # ── TREND FİLTRESİ — coin kendi trendinde değilse AL verme ──
    # 4h EMA20 < EMA50 = aşağı trend. Bu trendde AL = bıçak tuzağı.
    if own_trend == "bearish":
        if score >= 68:
            score = min(score, 62)
            reasons.append("⚠️ Trend aşağı (4h EMA) — AL iptal")
        elif score >= 55:
            score -= 3

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
        "own_trend":    own_trend,
        "market_state": market["state"],
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
    """
    Gerçekçi backtest: Şu anki skorlama sistemiyle geçmişi simüle eder.
    Filtreler: RSI + MACD + BB + hacim + EMA trend + bıçak yakalama.
    """
    try:
        days = max(7, min(90, days))
        sym  = symbol.upper() + "USDT"
        limit_1h = min(days * 24 + 100, 1000)
        klines_1h = get_pub("/api/v3/klines", {"symbol": sym, "interval": "1h", "limit": limit_1h})
        if len(klines_1h) < 100:
            return {"success": False, "error": "yeterli veri yok", "symbol": symbol.upper()}

        # 4h verisi de al — trend filtreleri için
        limit_4h = min(days * 6 + 60, 500)
        klines_4h = get_pub("/api/v3/klines", {"symbol": sym, "interval": "4h", "limit": limit_4h})

        closes_1h = [float(k[4]) for k in klines_1h]
        vols_1h   = [float(k[7]) for k in klines_1h]
        times_1h  = [int(k[0])   for k in klines_1h]

        closes_4h = [float(k[4]) for k in klines_4h]
        times_4h  = [int(k[0])   for k in klines_4h]

        signals_list = []
        capital      = 1000.0
        filtered_out = 0  # filtreler kaç sinyali iptal etti

        # Her 4 saatlik bir kez kontrol et (spam'i önle)
        for i in range(50, len(closes_1h) - 24, 4):
            # 1h göstergeler
            window = closes_1h[max(0, i-50):i+1]
            r      = calc_rsi(window, 14)
            macd_l, macd_s = calc_macd(window)
            macd_sig = 1 if macd_l > macd_s else -1
            bb_up, bb_mid, bb_lo = calc_bollinger(window, 20, 2)
            price = closes_1h[i]
            bb_pos = -1 if price <= bb_lo * 1.02 else 1 if price >= bb_up * 0.98 else 0

            # Hacim oranı (son 24h vs önceki 24h)
            if i >= 48:
                cur_v  = sum(vols_1h[i-24:i])
                prev_v = sum(vols_1h[i-48:i-24])
                vol_ratio = cur_v / prev_v if prev_v > 0 else 1.0
            else:
                vol_ratio = 1.0

            # 24h fiyat değişimi
            price_24h_ago = closes_1h[i-24] if i >= 24 else closes_1h[0]
            price_change = (price - price_24h_ago) / price_24h_ago * 100

            # 4h EMA trend (o zamanki duruma göre)
            t_now = times_1h[i]
            closes_4h_upto = [c for c, t in zip(closes_4h, times_4h) if t <= t_now]
            if len(closes_4h_upto) >= 50:
                ema20 = calc_ema(closes_4h_upto, 20)
                ema50 = calc_ema(closes_4h_upto, 50)
                own_trend = "bullish" if ema20 > ema50 else "bearish"
            else:
                own_trend = "unknown"

            # ── SKORLAMA (frontend'deki ile aynı mantık) ────────────
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

            # Bıçak yakalama
            if price_change < -3 and vol_ratio < 1.0:
                score -= 8

            # TREND FİLTRESİ
            if own_trend == "bearish" and score >= 68:
                score = min(score, 62)

            score = max(10, min(95, score))

            # AL sinyali mi?
            sig = None
            if score >= 68: sig = "AL"
            elif score < 35: sig = "SAT"
            if not sig:
                continue

            entry  = closes_1h[i]
            exit_  = closes_1h[i + 24]
            change = (exit_ - entry) / entry * 100
            if sig == "SAT": change = -change

            success = change > 0
            if success:
                capital *= (1 + abs(change) / 100 * 0.5)
            else:
                capital *= (1 - abs(change) / 100 * 0.5)

            signals_list.append({
                "timestamp": times_1h[i],
                "signal":    sig,
                "entry":     round(entry, 6),
                "exit":      round(exit_, 6),
                "change":    round(change, 2),
                "score":     score,
                "rsi":       round(r, 1),
                "own_trend": own_trend,
                "success":   success,
            })

        wins   = sum(1 for s in signals_list if s["success"])
        losses = len(signals_list) - wins
        win_rate = round(wins / len(signals_list) * 100, 1) if signals_list else 0

        # Bant bazlı analiz
        strong = [s for s in signals_list if s["score"] >= 80]
        normal = [s for s in signals_list if 68 <= s["score"] < 80]
        strong_wr = round(sum(1 for s in strong if s["success"]) / len(strong) * 100, 1) if strong else 0
        normal_wr = round(sum(1 for s in normal if s["success"]) / len(normal) * 100, 1) if normal else 0

        return {
            "success":         True,
            "symbol":          symbol.upper(),
            "days":            days,
            "total_signals":   len(signals_list),
            "wins":            wins,
            "losses":          losses,
            "win_rate":        win_rate,
            "strong_signals":  len(strong),
            "strong_win_rate": strong_wr,
            "normal_signals":  len(normal),
            "normal_win_rate": normal_wr,
            "final_capital":   round(capital, 2),
            "profit_pct":      round((capital - 1000) / 10, 2),
            "last_signals":    signals_list[-20:],
            "note":            "Yeni skorlama: RSI+MACD+BB+hacim+EMA trend+bıçak tuzağı filtresi",
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
# Kullanım: Render env'e TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID ekleyin.
# Bot token almak için:
#   1. Telegram'da @BotFather'a /newbot yaz
#   2. İsim ver, token alırsın
#   3. Bot'a bir mesaj at, sonra https://api.telegram.org/bot<TOKEN>/getUpdates
#      ziyaret et, chat_id buradan al
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
# Verilen coin'in güncel haberlerini toplayıp Claude ile sentiment analizi yapar.
# Çıktı: pozitif/negatif/nötr + özet + önemli konular
_sentiment_cache = {}  # sym → (ts, result)

class SentimentReq(BaseModel):
    symbol:    str
    news_list: list = []  # frontend'den zaten çekilmiş haberler

@app.post("/api/sentiment-analysis")
def sentiment_analysis(req: SentimentReq):
    if not CLAUDE_API_KEY:
        return {"success": False, "error": "Claude API key eksik"}
    try:
        sym = req.symbol.upper()
        # 30 dakika cache
        if sym in _sentiment_cache:
            ts, cached = _sentiment_cache[sym]
            if time.time() - ts < 1800:
                return cached

        # Haberleri filtrele — bu coinden bahsedenler
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
            # Herhangi bir anahtar kelime geçerse eşleştir
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

        # JSON'u parse et (bazen markdown bloğunda geliyor)
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
# Birden çok coin için tek Claude çağrısı ile sentiment analizi.
# Maliyet optimizasyonu: 30 coin × tek tek = $0.06, toplu = $0.003.
_batch_sentiment_cache = {"ts": 0, "data": {}}

class BatchSentimentReq(BaseModel):
    symbols:   list          # ["BTC","ETH","SOL",...]
    news_list: list = []     # global haber havuzu

@app.post("/api/batch-sentiment")
def batch_sentiment(req: BatchSentimentReq):
    if not CLAUDE_API_KEY:
        return {"success": False, "error": "Claude API key eksik"}
    try:
        # 15 dakika cache — çok sık çağrılmasın
        now = time.time()
        if req.symbols and now - _batch_sentiment_cache["ts"] < 900:
            cached = _batch_sentiment_cache["data"]
            # İstenen semboller cache'de varsa direkt dön
            if all(s.upper() in cached for s in req.symbols):
                return {"success": True, "cached": True, "sentiments": {s.upper(): cached[s.upper()] for s in req.symbols}}

        # Her coin için relevant haberleri topla
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

        # Her coin için haber sayısını ve başlıkları derle
        per_coin_news = {}
        for sym in req.symbols:
            s = sym.upper()
            keywords = name_map.get(s, [s.lower()])
            relevant = []
            for n in (req.news_list or [])[:50]:
                title = (n.get("title") or "").lower()
                if any(k in title for k in keywords):
                    relevant.append(n.get("title"))
            per_coin_news[s] = relevant[:5]  # her coin için max 5 haber

        # Hiç haber olmayan coinler için doğrudan nötr dön, Claude'a sorma
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

        # Toplu prompt — tek Claude çağrısı, tüm coinleri birden analiz et
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

        # Cache'e yaz
        _batch_sentiment_cache["ts"]   = now
        _batch_sentiment_cache["data"] = {**_batch_sentiment_cache.get("data",{}), **result}
        return {"success": True, "cached": False, "sentiments": result, "analyzed": len(to_analyze)}
    except Exception as e:
        return {"success": False, "error": str(e)}
