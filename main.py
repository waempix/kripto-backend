import os, time, hmac, hashlib
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import urllib.request, urllib.parse, json

API_KEY    = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
BASE       = "https://api.binance.com"

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

def sign(params):
    ts  = int(time.time() * 1000)
    params["timestamp"]  = ts
    params["recvWindow"] = 10000
    q   = urllib.parse.urlencode(params)
    sig = hmac.new(API_SECRET.encode(), q.encode(), hashlib.sha256).hexdigest()
    return q + "&signature=" + sig

def get(path, params=None, signed=False):
    if not API_KEY:
        raise HTTPException(status_code=400, detail="API key eksik")
    p = params or {}
    if signed:
        q = sign(p)
    else:
        q = urllib.parse.urlencode(p)
    url = BASE + path + ("?" + q if q else "")
    req = urllib.request.Request(url, headers={"X-MBX-APIKEY": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode("utf-8"))
        raise HTTPException(status_code=e.code, detail=body.get("msg", str(e)))

@app.get("/api/ping")
def ping():
    return {"status": "ok"}

@app.get("/api/portfolio")
def portfolio():
    try:
        account = get("/api/v3/account", signed=True)
        tickers = get("/api/v3/ticker/price")
        px = {t["symbol"]: float(t["price"]) for t in tickers}
        res, tot = [], 0.0
        for b in account["balances"]:
            amt = float(b["free"]) + float(b["locked"])
            if amt <= 0:
                continue
            a = b["asset"]
            u = 0.0
            if a == "USDT":
                u = amt
            elif a + "USDT" in px:
                u = amt * px[a + "USDT"]
            elif a + "BTC" in px and "BTCUSDT" in px:
                u = amt * px[a + "BTC"] * px["BTCUSDT"]
            tot += u
            res.append({
                "coin":      a,
                "amount":    round(amt, 8),
                "usdtValue": round(u, 2),
                "price":     round(px.get(a + "USDT", 0), 6)
            })
        res.sort(key=lambda x: x["usdtValue"], reverse=True)
        return {"success": True, "portfolio": res, "totalUsdt": round(tot, 2)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/trades/{symbol}")
def trades(symbol: str):
    try:
        ts = get("/api/v3/myTrades", {"symbol": symbol.upper() + "USDT", "limit": 20}, signed=True)
        return {
            "success": True,
            "trades": [
                {
                    "time":    t["time"],
                    "side":    "AL" if t["isBuyer"] else "SAT",
                    "price":   float(t["price"]),
                    "qty":     float(t["qty"]),
                    "total":   round(float(t["price"]) * float(t["qty"]), 2),
                    "fee":     float(t["commission"]),
                    "feeCoin": t["commissionAsset"]
                }
                for t in ts
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/open-orders")
def open_orders():
    try:
        orders = get("/api/v3/openOrders", signed=True)
        return {
            "success": True,
            "orders": [
                {
                    "symbol": o["symbol"],
                    "side":   "AL" if o["side"] == "BUY" else "SAT",
                    "price":  float(o["price"]),
                    "qty":    float(o["origQty"]),
                    "status": o["status"]
                }
                for o in orders
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
