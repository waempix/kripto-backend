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
    q   = urllib.parse.urlencode(params or {})
    url = BASE + path + ("?"+q if q else "")
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))

def round_step(qty, step):
    if step <= 0: return qty
    precision = max(0, round(-math.log10(step)))
    return round(math.floor(qty / step) * step, precision)

# Sembol filtrelerini al
def get_spot_filters(symbol):
    try:
        info = get_pub("/api/v3/exchangeInfo", {"symbol": symbol})
        filters = info["symbols"][0]["filters"]
        step = 0.001
        min_notional = 5.0
        min_qty = 0.0
        for f in filters:
            if f["filterType"] == "LOT_SIZE":
                step    = float(f["stepSize"])
                min_qty = float(f["minQty"])
            elif f["filterType"] in ("MIN_NOTIONAL","NOTIONAL"):
                min_notional = float(f.get("minNotional", f.get("minVal", 5.0)))
        return step, min_qty, min_notional
    except:
        return 0.001, 0.0, 5.0

FUTURES_CACHE = {}
def get_futures_filters(symbol):
    if symbol in FUTURES_CACHE:
        return FUTURES_CACHE[symbol]
    try:
        info = json.loads(urllib.request.urlopen(
            FUTURES_BASE+"/fapi/v1/exchangeInfo", timeout=10
        ).read().decode("utf-8"))
        for s in info["symbols"]:
            step = 0.001
            min_notional = 5.0
            for f in s.get("filters",[]):
                if f["filterType"] == "LOT_SIZE":
                    step = float(f["stepSize"])
                elif f["filterType"] == "MIN_NOTIONAL":
                    min_notional = float(f.get("notional", 5.0))
            FUTURES_CACHE[s["symbol"]] = (step, min_notional)
        return FUTURES_CACHE.get(symbol, (0.001, 5.0))
    except:
        return (0.001, 5.0)

# ── Ping ──────────────────────────────────────────────────────────────────────
@app.get("/api/ping")
def ping(): return {"status": "ok"}

# ── Portföy ───────────────────────────────────────────────────────────────────
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

# ── İşlem Geçmişi ─────────────────────────────────────────────────────────────
@app.get("/api/trades/{symbol}")
def trades(symbol: str):
    try:
        ts = req(BASE, "/api/v3/myTrades", {"symbol":symbol.upper()+"USDT","limit":20}, READ_KEY, READ_SECRET)
        return {"success":True,"trades":[{"time":t["time"],"side":"AL" if t["isBuyer"] else "SAT","price":float(t["price"]),"qty":float(t["qty"]),"total":round(float(t["price"])*float(t["qty"]),2),"fee":float(t["commission"]),"feeCoin":t["commissionAsset"]} for t in ts]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ── Açık Emirler ──────────────────────────────────────────────────────────────
@app.get("/api/open-orders")
def open_orders():
    try:
        orders = req(BASE, "/api/v3/openOrders", {}, READ_KEY, READ_SECRET)
        return {"success":True,"orders":[{"symbol":o["symbol"],"side":"AL" if o["side"]=="BUY" else "SAT","price":float(o["price"]),"qty":float(o["origQty"]),"status":o["status"]} for o in orders]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ── Futures Bakiye ────────────────────────────────────────────────────────────
@app.get("/api/futures/balance")
def futures_balance():
    try:
        balances = req(FUTURES_BASE, "/fapi/v2/balance", {}, TRADE_KEY, TRADE_SECRET)
        usdt = next((b for b in balances if b["asset"]=="USDT"), None)
        if not usdt: return {"success":True,"balance":0,"availableBalance":0}
        return {"success":True,"balance":round(float(usdt["balance"]),2),"availableBalance":round(float(usdt["availableBalance"]),2)}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ── Futures Pozisyonlar ───────────────────────────────────────────────────────
@app.get("/api/futures/positions")
def futures_positions():
    try:
        positions = req(FUTURES_BASE, "/fapi/v2/positionRisk", {}, TRADE_KEY, TRADE_SECRET)
        result = []
        for p in positions:
            if float(p["positionAmt"]) == 0: continue
            amt   = float(p["positionAmt"])
            entry = float(p["entryPrice"])
            mark  = float(p["markPrice"])
            pnl   = float(p["unRealizedProfit"])
            lev   = int(p["leverage"])
            pct   = (pnl/(abs(amt)*entry/lev))*100 if entry>0 else 0
            result.append({"symbol":p["symbol"],"side":"LONG" if amt>0 else "SHORT","size":round(abs(amt),4),"entry":round(entry,4),"mark":round(mark,4),"pnl":round(pnl,2),"pnlPct":round(pct,2),"leverage":lev,"liquidation":round(float(p["liquidationPrice"]),4)})
        return {"success":True,"positions":result}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ── Spot Emir ─────────────────────────────────────────────────────────────────
class SpotOrder(BaseModel):
    symbol:     str
    side:       str
    usdtAmount: float          # USDT cinsinden miktar
    price:      float          # Mevcut fiyat
    orderType:  str = "MARKET"

@app.post("/api/order/spot")
def spot_order(order: SpotOrder):
    if not TRADE_KEY: raise HTTPException(status_code=400, detail="Trading key eksik")
    try:
        sym = order.symbol.upper() + "USDT"
        step, min_qty, min_notional = get_spot_filters(sym)

        # Miktar hesapla ve yuvarla
        raw_qty = order.usdtAmount / order.price if order.price > 0 else 0
        qty = round_step(raw_qty, step)

        # Minimum kontroller
        if qty < min_qty:
            raise HTTPException(status_code=400, detail=f"Minimum miktar: {min_qty} {order.symbol}")
        if qty * order.price < min_notional:
            raise HTTPException(status_code=400, detail=f"Minimum işlem değeri: ${min_notional} USDT")

        params = {"symbol":sym,"side":order.side.upper(),"type":"MARKET","quantity":qty}
        result = req(BASE, "/api/v3/order", params, TRADE_KEY, TRADE_SECRET, "POST")
        return {"success":True,"orderId":result["orderId"],"symbol":sym,"side":order.side,"qty":qty,"status":result["status"]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ── Futures Emir ──────────────────────────────────────────────────────────────
class FuturesOrder(BaseModel):
    symbol:     str
    side:       str
    quantity:   float
    leverage:   int   = 5
    orderType:  str   = "MARKET"
    stopLoss:   float = 0.0
    takeProfit: float = 0.0

@app.post("/api/futures/order")
def futures_order(order: FuturesOrder):
    if not TRADE_KEY: raise HTTPException(status_code=400, detail="Trading key eksik")
    try:
        sym = order.symbol.upper() + "USDT"
        step, min_notional = get_futures_filters(sym)
        qty = round_step(order.quantity, step)

        # Kaldıraç ayarla
        req(FUTURES_BASE, "/fapi/v1/leverage", {"symbol":sym,"leverage":order.leverage}, TRADE_KEY, TRADE_SECRET, "POST")

        # Ana emir
        params = {"symbol":sym,"side":order.side.upper(),"type":"MARKET","quantity":qty}
        result = req(FUTURES_BASE, "/fapi/v1/order", params, TRADE_KEY, TRADE_SECRET, "POST")
        orders = [{"orderId":result["orderId"],"type":"Ana Emir","status":result["status"]}]

        # Stop-Loss
        if order.stopLoss > 0:
            sl_side = "SELL" if order.side.upper()=="BUY" else "BUY"
            try:
                sl = req(FUTURES_BASE, "/fapi/v1/order", {"symbol":sym,"side":sl_side,"type":"STOP_MARKET","stopPrice":round(order.stopLoss,4),"closePosition":"true"}, TRADE_KEY, TRADE_SECRET, "POST")
                orders.append({"orderId":sl["orderId"],"type":"Stop-Loss","status":sl["status"]})
            except Exception as e:
                orders.append({"type":"Stop-Loss","status":"HATA: "+str(e)})

        # Take-Profit
        if order.takeProfit > 0:
            tp_side = "SELL" if order.side.upper()=="BUY" else "BUY"
            try:
                tp = req(FUTURES_BASE, "/fapi/v1/order", {"symbol":sym,"side":tp_side,"type":"TAKE_PROFIT_MARKET","stopPrice":round(order.takeProfit,4),"closePosition":"true"}, TRADE_KEY, TRADE_SECRET, "POST")
                orders.append({"orderId":tp["orderId"],"type":"Take-Profit","status":tp["status"]})
            except Exception as e:
                orders.append({"type":"Take-Profit","status":"HATA: "+str(e)})

        return {"success":True,"symbol":sym,"side":order.side,"leverage":order.leverage,"qty":qty,"orders":orders}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ── Pozisyon Kapat ────────────────────────────────────────────────────────────
@app.post("/api/futures/close/{symbol}")
def close_position(symbol: str):
    if not TRADE_KEY: raise HTTPException(status_code=400, detail="Trading key eksik")
    try:
        sym = symbol.upper() + "USDT"
        positions = req(FUTURES_BASE, "/fapi/v2/positionRisk", {}, TRADE_KEY, TRADE_SECRET)
        pos = next((p for p in positions if p["symbol"]==sym and float(p["positionAmt"])!=0), None)
        if not pos: raise HTTPException(status_code=404, detail="Açık pozisyon yok")
        amt  = float(pos["positionAmt"])
        side = "SELL" if amt > 0 else "BUY"
        step, _ = get_futures_filters(sym)
        qty  = round_step(abs(amt), step)
        result = req(FUTURES_BASE, "/fapi/v1/order", {"symbol":sym,"side":side,"type":"MARKET","quantity":qty,"reduceOnly":"true"}, TRADE_KEY, TRADE_SECRET, "POST")
        return {"success":True,"message":sym+" kapatıldı","orderId":result["orderId"]}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
