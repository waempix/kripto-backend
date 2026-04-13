import os, time
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from binance.client import Client
from binance.exceptions import BinanceAPIException

API_KEY    = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

app = FastAPI(title="KriptoAI Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

def get_client():
    if not API_KEY or not API_SECRET:
        raise HTTPException(status_code=400, detail="API key eksik")
    c = Client(API_KEY, API_SECRET)
    try:
        server_time = c.get_server_time()["serverTime"]
        local_time  = int(time.time() * 1000)
        c.timestamp_offset = server_time - local_time
    except:
        pass
    return c

@app.get("/api/ping")
def ping():
    return {"status": "ok"}

@app.get("/api/portfolio")
def portfolio():
    try:
        c = get_client()
        account = c.get_account()
        prices  = {p["symbol"]: float(p["price"]) for p in c.get_all_tickers()}
        result, total = [], 0.0
        for b in account["balances"]:
            amt = float(b["free"]) + float(b["locked"])
            if amt <= 0: continue
            asset = b["asset"]
            usdt = 0.0
            if asset == "USDT": usdt = amt
            elif asset + "USDT" in prices: usdt = amt * prices[asset + "USDT"]
            elif asset + "BTC" in prices and "BTCUSDT" in prices:
                usdt = amt * prices[asset + "BTC"] * prices["BTCUSDT"]
            total += usdt
            result.append({"coin":asset,"amount":round(amt,8),"usdtValue":round(usdt,2),"price":round(prices.get(asset+"USDT",0),6)})
        result.sort(key=lambda x: x["usdtValue"], reverse=True)
        return {"success":True,"portfolio":result,"totalUsdt":round(total,2)}
    except BinanceAPIException as e:
        raise HTTPException(status_code=400, detail=f"Binance: {e.message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/trades/{symbol}")
def trades(symbol: str, limit: int = 20):
    try:
        c  = get_client()
        ts = c.get_my_trades(symbol=symbol.upper()+"USDT", limit=limit)
        return {"success":True,"trades":[{"id":t["id"],"time":t["time"],"side":"AL" if t["isBuyer"] else "SAT","price":float(t["price"]),"qty":float(t["qty"]),"total":round(float(t["price"])*float(t["qty"]),2),"fee":float(t["commission"]),"feeCoin":t["commissionAsset"]} for t in ts]}
    except BinanceAPIException as e:
        raise HTTPException(status_code=400, detail=f"Binance: {e.message}")

@app.get("/api/open-orders")
def open_orders():
    try:
        c = get_client()
        orders = c.get_open_orders()
        return {"success":True,"orders":[{"symbol":o["symbol"],"side":"AL" if o["side"]=="BUY" else "SAT","price":float(o["price"]),"qty":float(o["origQty"]),"status":o["status"]} for o in orders]}
    except BinanceAPIException as e:
        raise HTTPException(status_code=400, detail=f"Binance: {e.message}")
