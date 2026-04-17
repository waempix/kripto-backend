"""
KriptoAI - Market Analysis Module
Hacim spike, order book derinliği, whale alert entegrasyonu
"""

import urllib.request, json, time
from datetime import datetime, timedelta

# Binance API base
BINANCE_API = "https://api.binance.com"

# ── Hacim Analizi ─────────────────────────────────────────────────────────────
def get_volume_analysis(symbol):
    """
    24 saat hacmi / 7 gün ortalama hacim oranı
    > 2.0 = Hacim spike (güçlü sinyal)
    """
    try:
        # 24 saat ticker
        url = f"{BINANCE_API}/api/v3/ticker/24hr?symbol={symbol}USDT"
        with urllib.request.urlopen(url, timeout=5) as response:
            data = json.loads(response.read().decode())
        
        current_volume = float(data.get("quoteVolume", 0)) / 1e6  # Milyon USD
        
        # 7 günlük klines (günlük mumlar)
        end_time = int(time.time() * 1000)
        start_time = end_time - (7 * 24 * 60 * 60 * 1000)
        url = f"{BINANCE_API}/api/v3/klines?symbol={symbol}USDT&interval=1d&startTime={start_time}&endTime={end_time}"
        
        with urllib.request.urlopen(url, timeout=5) as response:
            klines = json.loads(response.read().decode())
        
        # Ortalama hacim (son 7 gün)
        volumes = [float(k[7]) / 1e6 for k in klines]  # quoteAssetVolume
        avg_volume = sum(volumes) / len(volumes) if volumes else 1
        
        # Hacim oranı
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0
        
        # Skor hesapla
        if volume_ratio > 3.0:
            score = 20  # 3x hacim = çok güçlü
        elif volume_ratio > 2.0:
            score = 15  # 2x hacim = güçlü
        elif volume_ratio > 1.5:
            score = 10  # 1.5x hacim = orta
        elif volume_ratio < 0.5:
            score = -10  # Düşük hacim = zayıf
        else:
            score = 0
        
        return {
            "current_volume_usd": round(current_volume, 2),
            "avg_volume_7d_usd": round(avg_volume, 2),
            "volume_ratio": round(volume_ratio, 2),
            "volume_score": score,
            "signal": "GÜÇLÜ HACIM" if volume_ratio > 2.0 else "NORMAL" if volume_ratio > 0.8 else "DÜŞÜK HACIM"
        }
    
    except Exception as e:
        return {
            "current_volume_usd": 0,
            "avg_volume_7d_usd": 0,
            "volume_ratio": 1.0,
            "volume_score": 0,
            "signal": "HATA",
            "error": str(e)
        }

# ── Order Book Derinliği ──────────────────────────────────────────────────────
def get_orderbook_depth(symbol):
    """
    Order book alış/satış derinliği analizi
    Bid > Ask = Alıcı baskısı (pozitif)
    Ask > Bid = Satıcı baskısı (negatif)
    """
    try:
        # Order book depth (ilk 100 seviye)
        url = f"{BINANCE_API}/api/v3/depth?symbol={symbol}USDT&limit=100"
        with urllib.request.urlopen(url, timeout=5) as response:
            data = json.loads(response.read().decode())
        
        # İlk 50 seviyenin toplam değeri
        bids = data.get("bids", [])[:50]
        asks = data.get("asks", [])[:50]
        
        bid_depth = sum(float(b[0]) * float(b[1]) for b in bids)  # fiyat * miktar
        ask_depth = sum(float(a[0]) * float(a[1]) for a in asks)
        
        # Spread (en iyi alış - en iyi satış)
        best_bid = float(bids[0][0]) if bids else 0
        best_ask = float(asks[0][0]) if asks else 0
        spread_pct = ((best_ask - best_bid) / best_bid * 100) if best_bid > 0 else 0
        
        # Alıcı/satıcı oranı
        bid_ask_ratio = bid_depth / ask_depth if ask_depth > 0 else 1.0
        
        # Skor hesapla
        if bid_ask_ratio > 1.5:
            score = 15  # Güçlü alıcı baskısı
        elif bid_ask_ratio > 1.2:
            score = 10  # Orta alıcı baskısı
        elif bid_ask_ratio < 0.8:
            score = -10  # Satıcı baskısı
        elif bid_ask_ratio < 0.7:
            score = -15  # Güçlü satıcı baskısı
        else:
            score = 0  # Dengeli
        
        # Spread kontrolü (dar spread = likidite yüksek)
        if spread_pct < 0.05:  # %0.05'ten dar
            score += 5  # Bonus: yüksek likidite
        
        return {
            "bid_depth_usd": round(bid_depth, 2),
            "ask_depth_usd": round(ask_depth, 2),
            "bid_ask_ratio": round(bid_ask_ratio, 2),
            "spread_pct": round(spread_pct, 4),
            "orderbook_score": score,
            "signal": "ALICI BASKISI" if bid_ask_ratio > 1.2 else "DENGELI" if bid_ask_ratio > 0.8 else "SATICI BASKISI"
        }
    
    except Exception as e:
        return {
            "bid_depth_usd": 0,
            "ask_depth_usd": 0,
            "bid_ask_ratio": 1.0,
            "spread_pct": 0,
            "orderbook_score": 0,
            "signal": "HATA",
            "error": str(e)
        }

# ── Whale Alert (Opsiyonel - API Key Gerekli) ─────────────────────────────────
def get_whale_transactions(symbol, api_key=None):
    """
    Whale Alert API - Büyük kripto transferleri
    Ücretsiz tier: 10,000 requests/month
    https://whale-alert.io
    """
    if not api_key:
        return {
            "large_transactions": 0,
            "total_value_usd": 0,
            "whale_score": 0,
            "signal": "API KEY YOK"
        }
    
    try:
        # Son 24 saat, minimum $1M transfer
        start_time = int((datetime.now() - timedelta(hours=24)).timestamp())
        url = f"https://api.whale-alert.io/v1/transactions?api_key={api_key}&start={start_time}&currency={symbol.lower()}&min_value=1000000"
        
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
        
        transactions = data.get("transactions", [])
        count = len(transactions)
        total_value = sum(t.get("amount_usd", 0) for t in transactions)
        
        # Exchange'e giriş/çıkış analizi
        exchange_inflow = sum(t.get("amount_usd", 0) for t in transactions if t.get("to", {}).get("owner_type") == "exchange")
        exchange_outflow = sum(t.get("amount_usd", 0) for t in transactions if t.get("from", {}).get("owner_type") == "exchange")
        
        net_flow = exchange_outflow - exchange_inflow  # Pozitif = exchange'den çıkış (birikim)
        
        # Skor hesapla
        if count > 5 and net_flow > 10000000:  # 5+ transfer, $10M+ çıkış
            score = 25  # Güçlü birikim
        elif count > 3 and net_flow > 5000000:
            score = 15  # Orta birikim
        elif net_flow < -10000000:  # Exchange'e giriş
            score = -15  # Satış baskısı
        else:
            score = 0
        
        return {
            "large_transactions": count,
            "total_value_usd": round(total_value, 2),
            "exchange_inflow": round(exchange_inflow, 2),
            "exchange_outflow": round(exchange_outflow, 2),
            "net_flow": round(net_flow, 2),
            "whale_score": score,
            "signal": "BİRİKİM" if net_flow > 5000000 else "NORMAL" if net_flow > -5000000 else "SATIŞ"
        }
    
    except Exception as e:
        return {
            "large_transactions": 0,
            "total_value_usd": 0,
            "whale_score": 0,
            "signal": "HATA",
            "error": str(e)
        }

# ── Kombine Market Analizi ────────────────────────────────────────────────────
def get_market_analysis(symbol, whale_api_key=None):
    """
    Tüm market analizlerini birleştir
    """
    volume = get_volume_analysis(symbol)
    orderbook = get_orderbook_depth(symbol)
    whale = get_whale_transactions(symbol, whale_api_key)
    
    # Toplam skor
    total_score = volume["volume_score"] + orderbook["orderbook_score"] + whale["whale_score"]
    
    # Sinyaller
    signals = []
    if volume["volume_ratio"] > 2.0:
        signals.append(f"{volume['volume_ratio']}x hacim artışı")
    if orderbook["bid_ask_ratio"] > 1.3:
        signals.append("Güçlü alıcı baskısı")
    if whale["net_flow"] > 5000000:
        signals.append("Balina birikimi")
    
    return {
        "symbol": symbol,
        "volume": volume,
        "orderbook": orderbook,
        "whale": whale,
        "total_market_score": total_score,
        "signals": signals,
        "timestamp": datetime.now().isoformat()
    }
