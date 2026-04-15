import os, time, hmac, hashlib, json, math
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import urllib.request, urllib.parse

READ_KEY     = os.environ.get("BINANCE_API_KEY", "")
READ_SECRET  = os.environ.get("BINANCE_API_SECRET", "")
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
    q = sign(params, secret)
    if method == "POST":
        r = urllib.request.Request(base+path, data=q.encode(),
            headers={"X-MBX-APIKEY":key,"Content-Type":"application/x-www-form-urlencoded"})
    else:
        r = urllib.request.Request(base+path+"?"+q, headers={"X-MBX-APIKEY":key})
    try:
        with urllib.request.urlopen(r, timeout=20) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode("utf-8"))
        raise HTTPException(status_code=e.code, detail=body.get("msg", str(e)))

def get_pub(path, params=None):
    q = urllib.parse.urlencode(params or {})
    url = BASE + path + ("?"+q if q else "")
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))

def round_step(qty, step):
    if step <= 0: return qty
    precision = max(0, round(-math.log10(step)))
    return round(math.floor(qty/step)*step, precision)

def get_spot_filters(symbol):
    try:
        info = get_pub("/api/v3/exchangeInfo", {"symbol": symbol})
        step, min_qty, min_notional = 0.001, 0.0, 5.0
        for f in info["symbols"][0].get("filters",[]):
            if f["filterType"]=="LOT_SIZE": step=float(f["stepSize"]); min_qty=float(f["minQty"])
            elif f["filterType"] in ("MIN_NOTIONAL","NOTIONAL"): min_notional=float(f.get("minNotional",f.get("minVal",5.0)))
        return step, min_qty, min_notional
    except: return 0.001, 0.0, 5.0

FUTURES_CACHE = {}
def get_futures_filters(symbol):
    if symbol in FUTURES_CACHE: return FUTURES_CACHE[symbol]
    try:
        info = json.loads(urllib.request.urlopen(FUTURES_BASE+"/fapi/v1/exchangeInfo",timeout=10).read().decode())
        for s in info["symbols"]:
            step, min_n = 0.001, 5.0
            for f in s.get("filters",[]):
                if f["filterType"]=="LOT_SIZE": step=float(f["stepSize"])
                elif f["filterType"]=="MIN_NOTIONAL": min_n=float(f.get("notional",5.0))
            FUTURES_CACHE[s["symbol"]] = (step, min_n)
        return FUTURES_CACHE.get(symbol,(0.001,5.0))
    except: return (0.001,5.0)

# ── Teknik Analiz ─────────────────────────────────────────────────────────────
def get_klines(symbol, interval="1h", limit=100):
    url = f"{BASE}/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit={limit}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())

def calc_rsi(closes, period=14):
    if len(closes) < period+1: return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff,0)); losses.append(max(-diff,0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0: return 100
    rs = avg_g / avg_l
    return round(100 - 100/(1+rs), 1)

def calc_ema(values, period):
    k = 2/(period+1)
    ema = values[0]
    for v in values[1:]: ema = v*k + ema*(1-k)
    return ema

def calc_macd(closes):
    if len(closes) < 26: return 0, 0
    ema12 = calc_ema(closes[-12:], 12)
    ema26 = calc_ema(closes[-26:], 26)
    macd  = ema12 - ema26
    return round(macd, 6), 1 if macd > 0 else -1

def calc_bollinger(closes, period=20):
    if len(closes) < period: return 0, 0, 0
    window = closes[-period:]
    mid = sum(window)/period
    std = (sum((x-mid)**2 for x in window)/period)**0.5
    upper = mid + 2*std
    lower = mid - 2*std
    return round(upper,4), round(mid,4), round(lower,4)

def analyze_coin(symbol):
    try:
        klines_1h = get_klines(symbol, "1h", 100)
        klines_4h = get_klines(symbol, "4h", 50)
        closes_1h = [float(k[4]) for k in klines_1h]
        closes_4h = [float(k[4]) for k in klines_4h]
        volumes   = [float(k[5]) for k in klines_1h[-20:]]
        
        rsi       = calc_rsi(closes_1h)
        _, macd_sig = calc_macd(closes_4h)
        bb_upper, bb_mid, bb_lower = calc_bollinger(closes_1h)
        
        price     = closes_1h[-1]
        avg_vol   = sum(volumes[:-1])/(len(volumes)-1) if len(volumes)>1 else 0
        vol_ratio = round(volumes[-1]/avg_vol, 2) if avg_vol > 0 else 1
        
        # BB pozisyon: -1 (alt bant), 0 (orta), 1 (üst bant)
        bb_pos = 0
        if bb_upper > bb_lower:
            pct = (price - bb_lower) / (bb_upper - bb_lower)
            if pct < 0.2: bb_pos = -1   # Alt bant — aşırı satım
            elif pct > 0.8: bb_pos = 1  # Üst bant — aşırı alım
        
        # Sinyal skoru
        score = 50
        if rsi < 30: score += 20
        elif rsi < 40: score += 12
        elif rsi < 50: score += 6
        elif rsi > 75: score -= 12
        elif rsi > 65: score -= 4
        
        score += macd_sig * 15
        
        if bb_pos == -1: score += 12   # Alt bantta — al fırsatı
        elif bb_pos == 1: score -= 10  # Üst bantta — dikkat
        
        if vol_ratio > 2: score += 10   # Yüksek hacim
        elif vol_ratio > 1.5: score += 5
        
        score = min(99, max(5, score))
        
        # Öneri
        if score >= 80:   rec = "GÜÇLÜ AL"
        elif score >= 68: rec = "AL"
        elif score >= 55: rec = "DİKKATLİ AL"
        elif score >= 45: rec = "BEKLE"
        elif score >= 35: rec = "SATIŞ BÖLGESİ"
        else:             rec = "SAT"
        
        # Nedenler
        reasons = []
        if rsi < 40: reasons.append(f"RSI {rsi} aşırı satım")
        elif rsi < 50: reasons.append(f"RSI {rsi} alım bölgesi")
        elif rsi > 65: reasons.append(f"RSI {rsi} güçlü trend")
        if macd_sig == 1: reasons.append("MACD alım sinyali")
        elif macd_sig == -1: reasons.append("MACD satış sinyali")
        if bb_pos == -1: reasons.append("Bollinger alt bant — dip")
        elif bb_pos == 1: reasons.append("Bollinger üst bant — zirve")
        if vol_ratio > 1.5: reasons.append(f"Hacim {vol_ratio}x ortalamanın üstünde")
        
        return {
            "symbol":    symbol,
            "price":     round(price, 8),
            "rsi":       rsi,
            "macd":      macd_sig,
            "bb_upper":  bb_upper,
            "bb_mid":    bb_mid,
            "bb_lower":  bb_lower,
            "bb_pos":    bb_pos,
            "vol_ratio": vol_ratio,
            "score":     score,
            "rec":       rec,
            "reasons":   reasons[:3],
        }
    except Exception as e:
        return {"symbol": symbol, "error": str(e), "score": 50, "rec": "BEKLE", "reasons": []}

# ── Endpoints ─────────────────────────────────────────────────────────────────
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
    except Exception as e: raise HTTPException(status_code=500,detail=str(e))

@app.get("/api/trades/{symbol}")
def trades(symbol: str):
    try:
        ts = req(BASE,"/api/v3/myTrades",{"symbol":symbol.upper()+"USDT","limit":20},READ_KEY,READ_SECRET)
        return {"success":True,"trades":[{"time":t["time"],"side":"AL" if t["isBuyer"] else "SAT","price":float(t["price"]),"qty":float(t["qty"]),"total":round(float(t["price"])*float(t["qty"]),2),"fee":float(t["commission"]),"feeCoin":t["commissionAsset"]} for t in ts]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500,detail=str(e))

@app.get("/api/open-orders")
def open_orders():
    try:
        orders = req(BASE,"/api/v3/openOrders",{},READ_KEY,READ_SECRET)
        return {"success":True,"orders":[{"symbol":o["symbol"],"side":"AL" if o["side"]=="BUY" else "SAT","price":float(o["price"]),"qty":float(o["origQty"]),"status":o["status"]} for o in orders]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500,detail=str(e))

@app.get("/api/futures/balance")
def futures_balance():
    try:
        balances = req(FUTURES_BASE,"/fapi/v2/balance",{},TRADE_KEY,TRADE_SECRET)
        usdt = next((b for b in balances if b["asset"]=="USDT"),None)
        if not usdt: return {"success":True,"balance":0,"availableBalance":0}
        return {"success":True,"balance":round(float(usdt["balance"]),2),"availableBalance":round(float(usdt["availableBalance"]),2)}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500,detail=str(e))

@app.get("/api/futures/positions")
def futures_positions():
    try:
        positions = req(FUTURES_BASE,"/fapi/v2/positionRisk",{},TRADE_KEY,TRADE_SECRET)
        result = []
        for p in positions:
            if float(p["positionAmt"])==0: continue
            amt=float(p["positionAmt"]); entry=float(p["entryPrice"]); mark=float(p["markPrice"])
            pnl=float(p["unRealizedProfit"]); lev=int(p["leverage"])
            pct=(pnl/(abs(amt)*entry/lev))*100 if entry>0 else 0
            result.append({"symbol":p["symbol"],"side":"LONG" if amt>0 else "SHORT","size":round(abs(amt),4),"entry":round(entry,4),"mark":round(mark,4),"pnl":round(pnl,2),"pnlPct":round(pct,2),"leverage":lev,"liquidation":round(float(p["liquidationPrice"]),4)})
        return {"success":True,"positions":result}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500,detail=str(e))

# ── Sinyal endpoint'i — tüm coinler için teknik analiz ────────────────────────
SIGNALS_CACHE = {}
SIGNALS_TIME  = 0

@app.get("/api/signals")
def signals(symbols: str = "BTC,ETH,SOL,BNB,INJ,HYPE,TAO,PEPE,NEAR,SUI,ARB,AVAX,STRK,LINK,PENDLE"):
    global SIGNALS_CACHE, SIGNALS_TIME
    now = time.time()
    
    # 5 dakika cache
    if now - SIGNALS_TIME < 300 and SIGNALS_CACHE:
        return {"success":True,"signals":SIGNALS_CACHE,"cached":True}
    
    sym_list = [s.strip().upper() for s in symbols.split(",")][:20]
    results  = {}
    for sym in sym_list:
        try:
            results[sym] = analyze_coin(sym)
            time.sleep(0.1)  # Rate limit
        except Exception as e:
            results[sym] = {"symbol":sym,"score":50,"rec":"BEKLE","reasons":[],"error":str(e)}
    
    SIGNALS_CACHE = results
    SIGNALS_TIME  = now
    return {"success":True,"signals":results,"cached":False}

@app.get("/api/signals/{symbol}")
def signal_one(symbol: str):
    return {"success":True,"signal":analyze_coin(symbol.upper())}

# ── Haber endpoint'i ──────────────────────────────────────────────────────────
import xml.etree.ElementTree as ET

def fetch_rss(url, source_name, limit=8):
    try:
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            content = r.read().decode("utf-8", errors="ignore")
        root = ET.fromstring(content)
        items = []
        for item in root.findall(".//item")[:limit]:
            title = item.findtext("title","").strip()
            link  = item.findtext("link","").strip()
            pub   = item.findtext("pubDate","").strip()
            if title and link:
                items.append({"title":title,"url":link,"source":source_name,
                              "published":pub,"currencies":[],"positive":0,"negative":0})
        return items
    except Exception as e:
        return []

@app.get("/api/news")
def news():
    all_items = []
    
    # Kaynak 1: CoinTelegraph
    items = fetch_rss("https://cointelegraph.com/rss", "CoinTelegraph", 6)
    all_items.extend(items)
    
    # Kaynak 2: CoinDesk
    if len(all_items) < 8:
        items2 = fetch_rss("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk", 6)
        all_items.extend(items2)
    
    # Kaynak 3: Bitcoin Magazine
    if len(all_items) < 10:
        items3 = fetch_rss("https://bitcoinmagazine.com/feed", "Bitcoin Magazine", 4)
        all_items.extend(items3)

    # Kaynak 4: CryptoPanic (bonus)
    try:
        url = "https://cryptopanic.com/api/v1/posts/?auth_token=free&public=true&kind=news"
        with urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0"}), timeout=8) as r:
            data = json.loads(r.read().decode())
        for p in data.get("results",[])[:5]:
            all_items.append({
                "title":     p.get("title",""),
                "url":       p.get("url",""),
                "source":    p.get("source",{}).get("title","CryptoPanic"),
                "published": p.get("published_at",""),
                "currencies":[c["code"] for c in p.get("currencies",[])],
                "positive":  p.get("votes",{}).get("positive",0),
                "negative":  p.get("votes",{}).get("negative",0),
            })
    except: pass

    return {"success": True, "news": all_items[:15]}

# ── AI Analiz endpoint'i ─────────────────────────────────────────────────────
CLAUDE_KEY = os.environ.get("CLAUDE_KEY", "")

class AIRequest(BaseModel):
    message: str
    context: str = ""

@app.post("/api/ai")
def ai_analyze(req: AIRequest):
    if not CLAUDE_KEY:
        return {"success": False, "text": "CLAUDE_KEY eksik"}
    try:
        payload = json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 500,
            "system": """Sen KriptoAI'ın kişisel kripto trading asistanısın. Görevin: anlık piyasa verilerine bakarak EN İYİ altcoin fırsatını bulmak ve somut giriş/çıkış noktaları vermek.

KURALLAR:
- BTC ve ETH ÖNERME, sadece altcoin öner
- Her zaman 1 veya 2 spesifik coin ismi ver
- Giriş fiyatı, stop-loss ve hedef söyle
- Kısa ve net yaz, maksimum 100 kelime
- Markdown kullanma, düz metin
- "genel olarak" veya "piyasa koşullarına göre" gibi muğlak cümleler KURMA
- Somut ol: "X coini al, giriş Y$, stop Z$, hedef W$"
""" + (f"\n\nŞu anki teknik sinyaller (Binance canlı veri):\n{req.context}" if req.context else ""),
            "messages": [{"role": "user", "content": req.message}]
        }).encode("utf-8")

        r = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": CLAUDE_KEY,
                "anthropic-version": "2023-06-01"
            }
        )
        with urllib.request.urlopen(r, timeout=30) as res:
            data = json.loads(res.read().decode("utf-8"))
        return {"success": True, "text": data["content"][0]["text"]}
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode("utf-8"))
        return {"success": False, "text": "API hatası: " + body.get("error", {}).get("message", str(e))}
    except Exception as e:
        return {"success": False, "text": "Hata: " + str(e)}

# ── Emir endpoint'leri ────────────────────────────────────────────────────────
class SpotOrder(BaseModel):
    symbol:     str
    side:       str
    usdtAmount: float
    price:      float
    orderType:  str = "MARKET"

class FuturesOrder(BaseModel):
    symbol:     str
    side:       str
    quantity:   float
    leverage:   int   = 5
    orderType:  str   = "MARKET"
    stopLoss:   float = 0.0
    takeProfit: float = 0.0

@app.post("/api/order/spot")
def spot_order(order: SpotOrder):
    if not TRADE_KEY: raise HTTPException(status_code=400,detail="Trading key eksik")
    try:
        sym = order.symbol.upper()+"USDT"
        step,min_qty,min_notional = get_spot_filters(sym)
        raw_qty = order.usdtAmount/order.price if order.price>0 else 0
        qty = round_step(raw_qty, step)
        if qty<min_qty: raise HTTPException(status_code=400,detail=f"Min miktar: {min_qty} {order.symbol}")
        if qty*order.price<min_notional: raise HTTPException(status_code=400,detail=f"Min işlem: ${min_notional}")
        result = req(BASE,"/api/v3/order",{"symbol":sym,"side":order.side.upper(),"type":"MARKET","quantity":qty},TRADE_KEY,TRADE_SECRET,"POST")
        return {"success":True,"orderId":result["orderId"],"symbol":sym,"side":order.side,"qty":qty,"status":result["status"]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500,detail=str(e))

@app.post("/api/futures/order")
def futures_order(order: FuturesOrder):
    if not TRADE_KEY: raise HTTPException(status_code=400,detail="Trading key eksik")
    try:
        sym = order.symbol.upper()+"USDT"
        step,_ = get_futures_filters(sym)
        qty = round_step(order.quantity, step)
        req(FUTURES_BASE,"/fapi/v1/leverage",{"symbol":sym,"leverage":order.leverage},TRADE_KEY,TRADE_SECRET,"POST")
        result = req(FUTURES_BASE,"/fapi/v1/order",{"symbol":sym,"side":order.side.upper(),"type":"MARKET","quantity":qty},TRADE_KEY,TRADE_SECRET,"POST")
        orders = [{"orderId":result["orderId"],"type":"Ana Emir","status":result["status"]}]
        if order.stopLoss>0:
            sl_side="SELL" if order.side.upper()=="BUY" else "BUY"
            try:
                sl=req(FUTURES_BASE,"/fapi/v1/order",{"symbol":sym,"side":sl_side,"type":"STOP_MARKET","stopPrice":round(order.stopLoss,4),"closePosition":"true"},TRADE_KEY,TRADE_SECRET,"POST")
                orders.append({"orderId":sl["orderId"],"type":"Stop-Loss","status":sl["status"]})
            except: pass
        if order.takeProfit>0:
            tp_side="SELL" if order.side.upper()=="BUY" else "BUY"
            try:
                tp=req(FUTURES_BASE,"/fapi/v1/order",{"symbol":sym,"side":tp_side,"type":"TAKE_PROFIT_MARKET","stopPrice":round(order.takeProfit,4),"closePosition":"true"},TRADE_KEY,TRADE_SECRET,"POST")
                orders.append({"orderId":tp["orderId"],"type":"Take-Profit","status":tp["status"]})
            except: pass
        return {"success":True,"symbol":sym,"side":order.side,"leverage":order.leverage,"qty":qty,"orders":orders}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500,detail=str(e))

@app.post("/api/futures/close/{symbol}")
def close_position(symbol: str):
    if not TRADE_KEY: raise HTTPException(status_code=400,detail="Trading key eksik")
    try:
        sym = symbol.upper()+"USDT"
        positions = req(FUTURES_BASE,"/fapi/v2/positionRisk",{},TRADE_KEY,TRADE_SECRET)
        pos = next((p for p in positions if p["symbol"]==sym and float(p["positionAmt"])!=0),None)
        if not pos: raise HTTPException(status_code=404,detail="Açık pozisyon yok")
        amt=float(pos["positionAmt"]); side="SELL" if amt>0 else "BUY"
        step,_=get_futures_filters(sym); qty=round_step(abs(amt),step)
        result=req(FUTURES_BASE,"/fapi/v1/order",{"symbol":sym,"side":side,"type":"MARKET","quantity":qty,"reduceOnly":"true"},TRADE_KEY,TRADE_SECRET,"POST")
        return {"success":True,"message":sym+" kapatıldı","orderId":result["orderId"]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500,detail=str(e))
