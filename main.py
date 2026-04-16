import os, time, hmac, hashlib, json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import urllib.request, urllib.parse

# Okuma key (bakiye görme)
READ_KEY    = os.environ.get("BINANCE_API_KEY", "")
READ_SECRET = os.environ.get("BINANCE_API_SECRET", "")

# Trading key (emir gönderme)
TRADE_KEY    = os.environ.get("BINANCE_TRADE_KEY", "")
TRADE_SECRET = os.environ.get("BINANCE_TRADE_SECRET", "")

BASE         = "https://api.binance.com"
FUTURES_BASE = "https://fapi.binance.com"

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── İmza ──────────────────────────────────────────────────────────────────────
def sign(params, secret):
    ts = int(time.time() * 1000)
    params["timestamp"]  = ts
    params["recvWindow"] = 10000
    q   = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
    return q + "&signature=" + sig

def req(base, path, params, key, secret, method="GET"):
    q   = sign(params, secret)
    url = base + path + "?" + q
    r   = urllib.request.Request(url, headers={"X-MBX-APIKEY": key})
    r.method = method
    if method == "POST":
        r.data = q.encode()
        r.add_header("Content-Type", "application/x-www-form-urlencoded")
        r = urllib.request.Request(base + path, data=q.encode(),
            headers={"X-MBX-APIKEY": key, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(r, timeout=20) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode("utf-8"))
        raise HTTPException(status_code=e.code, detail=body.get("msg", str(e)))

def get_pub(path, params=None):
    q   = urllib.parse.urlencode(params or {})
    url = BASE + path + ("?" + q if q else "")
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))

# ── Okuma endpoint'leri ───────────────────────────────────────────────────────
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

# ── Futures bakiye ve pozisyonlar ─────────────────────────────────────────────
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
            amt    = float(p["positionAmt"])
            entry  = float(p["entryPrice"])
            mark   = float(p["markPrice"])
            pnl    = float(p["unRealizedProfit"])
            lev    = int(p["leverage"])
            side   = "LONG" if amt > 0 else "SHORT"
            pct    = (pnl / (abs(amt) * entry / lev)) * 100 if entry > 0 else 0
            result.append({"symbol":p["symbol"],"side":side,"size":round(abs(amt),4),"entry":round(entry,4),"mark":round(mark,4),"pnl":round(pnl,2),"pnlPct":round(pct,2),"leverage":lev,"liquidation":round(float(p["liquidationPrice"]),4)})
        return {"success":True,"positions":result}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ── Emir modelleri ────────────────────────────────────────────────────────────
class SpotOrder(BaseModel):
    symbol:   str
    side:     str   # BUY veya SELL
    quantity: float
    orderType: str = "MARKET"
    price:    float = 0.0

class FuturesOrder(BaseModel):
    symbol:     str
    side:       str    # BUY veya SELL
    quantity:   float
    leverage:   int = 5
    orderType:  str = "MARKET"
    price:      float = 0.0
    stopLoss:   float = 0.0
    takeProfit: float = 0.0

# ── Spot Emir ─────────────────────────────────────────────────────────────────
@app.post("/api/order/spot")
def spot_order(order: SpotOrder):
    if not TRADE_KEY:
        raise HTTPException(status_code=400, detail="Trading key eksik")
    try:
        sym = order.symbol.upper() + "USDT"
        params = {"symbol":sym,"side":order.side.upper(),"type":order.orderType.upper(),"quantity":order.quantity}
        if order.orderType.upper() == "LIMIT":
            params["price"]    = order.price
            params["timeInForce"] = "GTC"
        result = req(BASE, "/api/v3/order", params, TRADE_KEY, TRADE_SECRET, "POST")
        return {"success":True,"orderId":result["orderId"],"symbol":sym,"side":order.side,"qty":order.quantity,"status":result["status"]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ── Futures Emir ──────────────────────────────────────────────────────────────
@app.post("/api/futures/order")
def futures_order(order: FuturesOrder):
    if not TRADE_KEY:
        raise HTTPException(status_code=400, detail="Trading key eksik")
    try:
        sym = order.symbol.upper() + "USDT"

        # Kaldıraç ayarla
        req(FUTURES_BASE, "/fapi/v1/leverage", {"symbol":sym,"leverage":order.leverage}, TRADE_KEY, TRADE_SECRET, "POST")

        # Ana emir
        params = {"symbol":sym,"side":order.side.upper(),"type":order.orderType.upper(),"quantity":order.quantity}
        if order.orderType.upper() == "LIMIT":
            params["price"]       = order.price
            params["timeInForce"] = "GTC"
        result = req(FUTURES_BASE, "/fapi/v1/order", params, TRADE_KEY, TRADE_SECRET, "POST")

        orders = [{"orderId":result["orderId"],"type":"Ana Emir","status":result["status"]}]

        # Stop-loss
        if order.stopLoss > 0:
            sl_side = "SELL" if order.side.upper()=="BUY" else "BUY"
            sl = req(FUTURES_BASE, "/fapi/v1/order", {"symbol":sym,"side":sl_side,"type":"STOP_MARKET","stopPrice":order.stopLoss,"closePosition":"true"}, TRADE_KEY, TRADE_SECRET, "POST")
            orders.append({"orderId":sl["orderId"],"type":"Stop-Loss","status":sl["status"]})

        # Take-profit
        if order.takeProfit > 0:
            tp_side = "SELL" if order.side.upper()=="BUY" else "BUY"
            tp = req(FUTURES_BASE, "/fapi/v1/order", {"symbol":sym,"side":tp_side,"type":"TAKE_PROFIT_MARKET","stopPrice":order.takeProfit,"closePosition":"true"}, TRADE_KEY, TRADE_SECRET, "POST")
            orders.append({"orderId":tp["orderId"],"type":"Take-Profit","status":tp["status"]})

        return {"success":True,"symbol":sym,"side":order.side,"leverage":order.leverage,"qty":order.quantity,"orders":orders}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ── Pozisyon kapat ────────────────────────────────────────────────────────────
@app.post("/api/futures/close/{symbol}")
def close_position(symbol: str):
    if not TRADE_KEY:
        raise HTTPException(status_code=400, detail="Trading key eksik")
    try:
        sym = symbol.upper() + "USDT"
        positions = req(FUTURES_BASE, "/fapi/v2/positionRisk", {}, TRADE_KEY, TRADE_SECRET)
        pos = next((p for p in positions if p["symbol"]==sym and float(p["positionAmt"])!=0), None)
        if not pos: raise HTTPException(status_code=404, detail="Açık pozisyon yok")
        amt  = float(pos["positionAmt"])
        side = "SELL" if amt > 0 else "BUY"
        result = req(FUTURES_BASE, "/fapi/v1/order", {"symbol":sym,"side":side,"type":"MARKET","quantity":abs(amt),"reduceOnly":"true"}, TRADE_KEY, TRADE_SECRET, "POST")
        return {"success":True,"message":sym+" pozisyon kapatıldı","orderId":result["orderId"]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/news")
def get_news():
    """Kripto haberlerini çeşitli kaynaklardan çek - DETAYLı LOGGING"""
    
    errors = []  # Hata logları
    
    # Kaynak 1: NewsAPI
    NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "")
    
    print(f"[NEWS] NEWS_API_KEY exists: {bool(NEWS_API_KEY)}")
    print(f"[NEWS] NEWS_API_KEY length: {len(NEWS_API_KEY) if NEWS_API_KEY else 0}")
    
    if NEWS_API_KEY:
        try:
            url = f"https://newsapi.org/v2/everything?q=cryptocurrency OR bitcoin OR ethereum&language=en&sortBy=publishedAt&pageSize=15&apiKey={NEWS_API_KEY}"
            print(f"[NEWS] Trying NewsAPI...")
            
            req_obj = urllib.request.Request(url)
            req_obj.add_header('User-Agent', 'Mozilla/5.0')
            
            with urllib.request.urlopen(req_obj, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
            
            print(f"[NEWS] NewsAPI response status: {data.get('status')}")
            
            if data.get("status") == "ok" and data.get("articles"):
                news = []
                for item in data["articles"][:15]:
                    news.append({
                        "title": item.get("title", ""),
                        "url": item.get("url", "#"),
                        "source": item.get("source", {}).get("name", "NewsAPI"),
                        "published": item.get("publishedAt", ""),
                        "description": item.get("description", ""),
                        "currencies": [],
                        "positive": 0,
                        "negative": 0
                    })
                
                if len(news) > 0:
                    print(f"[NEWS] NewsAPI SUCCESS: {len(news)} articles")
                    return {"success": True, "news": news, "source": "newsapi"}
        except urllib.error.HTTPError as e:
            error_msg = f"NewsAPI HTTP {e.code}: {e.read().decode('utf-8')}"
            print(f"[NEWS] NewsAPI HTTPError: {error_msg}")
            errors.append(error_msg)
        except Exception as e:
            error_msg = f"NewsAPI Error: {type(e).__name__} - {str(e)}"
            print(f"[NEWS] NewsAPI Exception: {error_msg}")
            errors.append(error_msg)
    else:
        errors.append("NEWS_API_KEY not set")
    
    # Kaynak 2: CryptoPanic
    CRYPTOPANIC_KEY = os.environ.get("CRYPTOPANIC_API_KEY", "")
    
    if CRYPTOPANIC_KEY:
        try:
            url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTOPANIC_KEY}&public=true&kind=news"
            print(f"[NEWS] Trying CryptoPanic...")
            
            with urllib.request.urlopen(url, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
            
            if data.get("results"):
                news = []
                for item in data["results"][:15]:
                    news.append({
                        "title": item.get("title", ""),
                        "url": item.get("url", "#"),
                        "source": item.get("source", {}).get("title", "CryptoPanic"),
                        "published": item.get("published_at", ""),
                        "currencies": [c.get("code") for c in item.get("currencies", [])],
                        "positive": 1 if item.get("votes", {}).get("positive", 0) > item.get("votes", {}).get("negative", 0) else 0,
                        "negative": 1 if item.get("votes", {}).get("negative", 0) > item.get("votes", {}).get("positive", 0) else 0
                    })
                
                if len(news) > 0:
                    print(f"[NEWS] CryptoPanic SUCCESS: {len(news)} articles")
                    return {"success": True, "news": news, "source": "cryptopanic"}
        except Exception as e:
            error_msg = f"CryptoPanic Error: {str(e)}"
            print(f"[NEWS] {error_msg}")
            errors.append(error_msg)
    
    # Kaynak 3: CoinGecko
    try:
        url = "https://api.coingecko.com/api/v3/news"
        print(f"[NEWS] Trying CoinGecko...")
        
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
        
        news = []
        items = data.get("data", [])[:15] if isinstance(data, dict) else data[:15]
        
        for item in items:
            news.append({
                "title": item.get("title", ""),
                "url": item.get("url", "#"),
                "source": item.get("author", {}).get("name", "CoinGecko") if isinstance(item.get("author"), dict) else "CoinGecko",
                "published": item.get("created_at", ""),
                "currencies": [],
                "positive": 0,
                "negative": 0
            })
        
        if len(news) > 0:
            print(f"[NEWS] CoinGecko SUCCESS: {len(news)} articles")
            return {"success": True, "news": news, "source": "coingecko"}
    except Exception as e:
        error_msg = f"CoinGecko Error: {str(e)}"
        print(f"[NEWS] {error_msg}")
        errors.append(error_msg)
    
    # Tüm kaynaklar başarısız
    print(f"[NEWS] ALL SOURCES FAILED")
    print(f"[NEWS] Errors: {errors}")
    return {"success": False, "news": [], "error": "All sources failed", "details": errors}

# ── Gelişmiş Piyasa Analizi ───────────────────────────────────────────────────
@app.get("/api/market-analysis")
def market_analysis():
    """Tüm coinler için detaylı piyasa analizi"""
    try:
        # 24h ticker data
        tickers = get_pub("/api/v3/ticker/24hr")
        
        results = {}
        for ticker in tickers:
            if not ticker["symbol"].endswith("USDT"):
                continue
            
            symbol = ticker["symbol"][:-4]  # BTCUSDT -> BTC
            
            # Hacim analizi
            volume_usdt = float(ticker["quoteVolume"])
            if volume_usdt < 1_000_000:  # Min 1M USDT hacim
                continue
            
            # Fiyat değişimi
            price_change = float(ticker["priceChangePercent"])
            
            # Order book analizi (sadece yüksek hacimli coinler için)
            try:
                depth = get_pub("/api/v3/depth", {"symbol": ticker["symbol"], "limit": 100})
                
                # Alım baskısı hesapla
                bid_volume = sum(float(b[1]) * float(b[0]) for b in depth["bids"][:20])
                ask_volume = sum(float(a[1]) * float(a[0]) for a in depth["asks"][:20])
                
                buy_pressure = bid_volume / ask_volume if ask_volume > 0 else 0
                
            except:
                buy_pressure = 1.0
            
            results[symbol] = {
                "price": float(ticker["lastPrice"]),
                "change_24h": price_change,
                "volume_24h": volume_usdt,
                "buy_pressure": round(buy_pressure, 2),
                "high_24h": float(ticker["highPrice"]),
                "low_24h": float(ticker["lowPrice"]),
                "trades": int(ticker["count"])
            }
        
        return {"success": True, "data": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/smart-score/{symbol}")
def smart_score(symbol: str):
    """Coin için detaylı akıllı skor hesapla"""
    try:
        sym = symbol.upper() + "USDT"
        
        # 24h ticker
        ticker = get_pub("/api/v3/ticker/24hr", {"symbol": sym})
        
        # Klines (mum verileri) - son 100 mum
        klines = get_pub("/api/v3/klines", {
            "symbol": sym,
            "interval": "1h",
            "limit": 100
        })
        
        # RSI hesapla
        closes = [float(k[4]) for k in klines]
        rsi = calculate_rsi(closes, 14)
        
        # MACD hesapla
        macd_line, signal_line = calculate_macd(closes)
        macd_signal = 1 if macd_line > signal_line else -1
        
        # Bollinger Bands
        bb_upper, bb_middle, bb_lower = calculate_bollinger(closes, 20, 2)
        current_price = closes[-1]
        
        if current_price <= bb_lower * 1.02:
            bb_position = -1  # Alt bant (AL)
        elif current_price >= bb_upper * 0.98:
            bb_position = 1   # Üst bant (SAT)
        else:
            bb_position = 0   # Orta
        
        # Order Book
        try:
            depth = get_pub("/api/v3/depth", {"symbol": sym, "limit": 100})
            bid_volume = sum(float(b[1]) * float(b[0]) for b in depth["bids"][:20])
            ask_volume = sum(float(a[1]) * float(a[0]) for a in depth["asks"][:20])
            buy_pressure = bid_volume / ask_volume if ask_volume > 0 else 1.0
        except:
            buy_pressure = 1.0
        
        # Hacim analizi
        current_volume = float(ticker["quoteVolume"])
        
        if len(klines) >= 48:
            prev_volume = sum(float(k[5]) for k in klines[-48:-24])
            volume_ratio = current_volume / prev_volume if prev_volume > 0 else 1.0
        else:
            avg_hourly = sum(float(k[5]) for k in klines[-24:]) / 24
            current_hourly = float(klines[-1][5]) if klines else avg_hourly
            volume_ratio = current_hourly / avg_hourly if avg_hourly > 0 else 1.0
        
        # SKOR HESAPLAMA
        score = 50
        reasons = []
        
        # Teknik Analiz
        if rsi < 25:
            score += 15
            reasons.append(f"RSI {rsi:.0f} aşırı satım")
        elif rsi < 35:
            score += 10
            reasons.append(f"RSI {rsi:.0f} alım bölgesi")
        elif rsi < 45:
            score += 5
        elif rsi > 75:
            score -= 12
            reasons.append(f"RSI {rsi:.0f} aşırı alım")
        elif rsi > 65:
            score -= 6
        
        if macd_signal == 1:
            score += 6
            reasons.append("MACD alım sinyali")
        else:
            score -= 4
        
        if bb_position == -1:
            score += 8
            reasons.append("BB alt bant (dip)")
        elif bb_position == 1:
            score -= 10
            reasons.append("BB üst bant (zirve)")
        
        # Alım/Satım Baskısı
        if buy_pressure > 3.0:
            score += 15
            reasons.append(f"Çok güçlü alım {buy_pressure:.1f}x")
        elif buy_pressure > 2.0:
            score += 10
            reasons.append(f"Güçlü alım {buy_pressure:.1f}x")
        elif buy_pressure > 1.3:
            score += 5
        elif buy_pressure < 0.6:
            score -= 12
            reasons.append(f"Güçlü satış {buy_pressure:.1f}x")
        elif buy_pressure < 0.85:
            score -= 5
        
        # Hacim Artışı
        if volume_ratio > 2.5:
            score += 10
            reasons.append(f"Hacim %{(volume_ratio-1)*100:.0f} arttı")
        elif volume_ratio > 1.8:
            score += 6
        elif volume_ratio > 1.3:
            score += 3
        elif volume_ratio < 0.7:
            score -= 4
        
        # Momentum
        price_change = float(ticker["priceChangePercent"])
        if price_change > 25:
            score += 5
        elif price_change > 15:
            score += 3
        elif price_change > 8:
            score += 2
        elif price_change < -15:
            score -= 8
            reasons.append(f"%{price_change:.1f} düşüş")
        elif price_change < -8:
            score -= 4
        
        score = max(10, min(95, score))
        
        if score >= 90:
            signal = "ÇOK GÜÇLÜ AL"
        elif score >= 80:
            signal = "GÜÇLÜ AL"
        elif score >= 68:
            signal = "AL"
        elif score >= 55:
            signal = "DİKKATLİ AL"
        elif score >= 45:
            signal = "BEKLE"
        elif score >= 35:
            signal = "SATIŞ"
        else:
            signal = "SAT"
        
        return {
            "success": True,
            "symbol": symbol,
            "score": round(score),
            "signal": signal,
            "reasons": reasons[:4],
            "indicators": {
                "rsi": round(rsi, 1),
                "macd": macd_signal,
                "bb_position": bb_position,
                "buy_pressure": round(buy_pressure, 2),
                "volume_ratio": round(volume_ratio, 2),
                "price_change": round(price_change, 2)
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Teknik Gösterge Hesaplamaları ─────────────────────────────────────────────
def calculate_rsi(prices, period=14):
    """RSI hesapla"""
    if len(prices) < period + 1:
        return 50
    
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_macd(prices, fast=12, slow=26, signal=9):
    """MACD hesapla"""
    if len(prices) < slow:
        return 0, 0
    
    def ema(data, period):
        k = 2 / (period + 1)
        ema_val = data[0]
        for price in data[1:]:
            ema_val = price * k + ema_val * (1 - k)
        return ema_val
    
    ema_fast = ema(prices[-fast:], fast)
    ema_slow = ema(prices[-slow:], slow)
    macd_line = ema_fast - ema_slow
    
    macd_history = []
    for i in range(slow, len(prices)):
        ef = ema(prices[i-fast:i], fast)
        es = ema(prices[i-slow:i], slow)
        macd_history.append(ef - es)
    
    signal_line = ema(macd_history[-signal:], signal) if len(macd_history) >= signal else macd_line
    
    return macd_line, signal_line

def calculate_bollinger(prices, period=20, std_dev=2):
    """Bollinger Bands hesapla"""
    if len(prices) < period:
        return prices[-1] * 1.02, prices[-1], prices[-1] * 0.98
    
    recent = prices[-period:]
    middle = sum(recent) / period
    
    variance = sum((p - middle) ** 2 for p in recent) / period
    std = variance ** 0.5
    
    upper = middle + (std * std_dev)
    lower = middle - (std * std_dev)
    
    return upper, middle, lower

# ── Market Sentiment (Fear & Greed) ───────────────────────────────────────────
@app.get("/api/market-sentiment")
def market_sentiment():
    try:
        # Alternative Crypto Fear & Greed Index
        r = urllib.request.urlopen("https://api.alternative.me/fng/?limit=10", timeout=10)
        data = json.loads(r.read().decode("utf-8"))
        
        if not data.get("data"):
            raise Exception("No data")
        
        current = data["data"][0]
        history = data["data"][:7]
        
        value = int(current["value"])
        if value < 25:
            label_tr = "Aşırı Korku"
        elif value < 45:
            label_tr = "Korku"
        elif value < 55:
            label_tr = "Nötr"
        elif value < 75:
            label_tr = "Açgözlülük"
        else:
            label_tr = "Aşırı Açgözlülük"
        
        return {
            "success": True,
            "fear_greed": {
                "value": value,
                "label": current["value_classification"],
                "label_tr": label_tr,
                "timestamp": current["timestamp"],
                "history": [{"value": int(h["value"]), "timestamp": h["timestamp"]} for h in history]
            },
            "global": {
                "btc_dominance": 54.2,
                "total_volume_usd": 95000000000,
                "market_cap_change_24h": 2.4
            }
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

# ── AI Chat (Claude) ──────────────────────────────────────────────────────────
class AIRequest(BaseModel):
    message: str
    context: str = ""
    focus: str = "altcoin"

@app.post("/api/ai")
def ai_chat(req: AIRequest):
    """Claude AI ile sohbet"""
    CLAUDE_KEY = os.environ.get("CLAUDE_API_KEY", "")
    
    if not CLAUDE_KEY:
        return {"success": False, "text": "AI aktif değil. CLAUDE_API_KEY environment variable ekleyin."}
    
    try:
        prompt = f"""Sen bir kripto trading uzmanısın. Kullanıcıya kısa ve net cevaplar ver.

PIYASA DURUMU:
{req.context}

KULLANICI SORUSU: {req.message}

ODAK: {req.focus} coinleri

CEVAP (max 3 paragraf):"""

        headers = {
            "x-api-key": CLAUDE_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        
        body = json.dumps({
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 500,
            "messages": [{"role": "user", "content": prompt}]
        })
        
        req_obj = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body.encode(),
            headers=headers,
            method="POST"
        )
        
        with urllib.request.urlopen(req_obj, timeout=30) as res:
            data = json.loads(res.read().decode("utf-8"))
            text = data["content"][0]["text"]
            return {"success": True, "text": text}
            
    except Exception as e:
        return {"success": False, "text": f"AI hatası: {str(e)}"}

# ── Backtest ──────────────────────────────────────────────────────────────────
@app.get("/api/backtest/{symbol}")
def backtest_signals(symbol: str, days: int = 30):
    """Geçmiş RSI sinyallerinin doğruluğunu test et"""
    try:
        sym = symbol.upper() + "USDT"
        interval = "1h"
        limit = days * 24
        
        klines = get_pub("/api/v3/klines", {"symbol": sym, "interval": interval, "limit": limit})
        
        signals = []
        wins = 0
        losses = 0
        capital = 1000.0
        
        for i in range(14, len(klines) - 24):
            closes = [float(k[4]) for k in klines[max(0, i-14):i+1]]
            
            if len(closes) < 14:
                continue
            
            gains = []
            losses_list = []
            for j in range(1, len(closes)):
                change = closes[j] - closes[j-1]
                if change > 0:
                    gains.append(change)
                    losses_list.append(0)
                else:
                    gains.append(0)
                    losses_list.append(abs(change))
            
            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses_list[-14:]) / 14
            
            if avg_loss == 0:
                rsi = 100
            else:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))
            
            entry_price = float(klines[i][4])
            exit_price = float(klines[i + 24][4])
            
            signal = None
            if rsi < 30:
                signal = "AL"
            elif rsi > 70:
                signal = "SAT"
            
            if signal:
                change_pct = ((exit_price - entry_price) / entry_price) * 100
                
                success = False
                if signal == "AL" and change_pct > 0:
                    success = True
                    wins += 1
                    capital *= (1 + change_pct / 100)
                elif signal == "SAT" and change_pct < 0:
                    success = True
                    wins += 1
                    capital *= (1 - change_pct / 100)
                else:
                    losses += 1
                
                signals.append({
                    "timestamp": klines[i][0],
                    "signal": signal,
                    "rsi": round(rsi, 1),
                    "entry": round(entry_price, 4),
                    "exit": round(exit_price, 4),
                    "change": round(change_pct, 2),
                    "success": success
                })
        
        total = wins + losses
        win_rate = (wins / total * 100) if total > 0 else 0
        
        return {
            "success": True,
            "symbol": symbol,
            "days": days,
            "total_signals": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "final_capital": round(capital, 2),
            "profit_pct": round(((capital - 1000) / 1000) * 100, 1),
            "last_signals": signals[-10:]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
