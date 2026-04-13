import os, time
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from binance.client import Client
from binance.exceptions import BinanceAPIException

API_KEY    = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

def get_client():
    if not API_KEY or not API_SECRET:
        raise HTTPException(status_code=400, detail="API key eksik")
    c = Client(API_KEY, API_SECRET)
    try:
        s = c.get_server_time()["serverTime"]
        c.timestamp_offset = s - int(time.time() * 1000)
    except: pass
    return c

@app.get("/api/ping")
def ping(): return {"status": "ok"}

@app.get("/api/portfolio")
def portfolio():
    try:
        c = get_client()
        acc = c.get_account()
        px  = {p["symbol"]: float(p["price"]) for p in c.get_all_tickers()}
        res, tot = [], 0.0
        for b in acc["balances"]:
            amt = float(b["free"]) + float(b["locked"])
            if amt <= 0: continue
            a = b["asset"]
            u = amt if a=="USDT" else amt*px.get(a+"USDT",0) or amt*px.get(a+"BTC",0)*px.get("BTCUSDT",1)
            tot += u
            res.append({"coin":a,"amount":round(amt,8),"usdtValue":round(u,2),"price":round(px.get(a+"USDT",0),6)})
        res.sort(key=lambda x: x["usdtValue"], reverse=True)
        return {"success":True,"portfolio":res,"totalUsdt":round(tot,2)}
    except BinanceAPIException as e:
        raise HTTPException(status_code=400, detail=e.message)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/trades/{symbol}")
def trades(symbol: str):
    try:
        c = get_client()
        ts = c.get_my_trades(symbol=symbol.upper()+"USDT", limit=20)
        return {"success":True,"trades":[{"time":t["time"],"side":"AL" if t["isBuyer"] else "SAT","price":float(t["price"]),"qty":float(t["qty"]),"total":round(float(t["price"])*float(t["qty"]),2),"fee":float(t["commission"]),"feeCoin":t["commissionAsset"]} for t in ts]}
    except BinanceAPIException as e:
        raise HTTPException(status_code=400, detail=e.message)

@app.get("/api/open-orders")
def open_orders():
    try:
        c = get_client()
        return {"success":True,"orders":[{"symbol":o["symbol"],"side":"AL" if o["side"]=="BUY" else "SAT","price":float(o["price"]),"qty":float(o["origQty"]),"status":o["status"]} for o in c.get_open_orders()]}
    except BinanceAPIException as e:
        raise HTTPException(status_code=400, detail=e.message)
