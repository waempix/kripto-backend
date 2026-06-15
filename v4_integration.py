# -*- coding: utf-8 -*-
"""
KriptoAI — v4 Entegrasyon Köprüsü (LONG + SHORT)
=================================================
main.py'a bağlanır. v3 tracker'ı BOZMAZ; v4 long + v4-short skorlarını paralel üretir.
⚠️ SHORT = PAPER TRADING — gerçek emir YOK, gölge tablo, ölçüm amaçlı.
"""
import time, urllib.request, json
from score_v4 import (
    compute_smart_score_v4, compute_dynamic_targets, build_data_from_klines,
    calc_rsi, calc_atr, calc_ma, calc_bollinger, mtf_trend_health,
)

# ════════════════════════════════════════════════════════════════════════════
#  FUNDING RATE — Binance Futures (auth'suz, fapi)
# ════════════════════════════════════════════════════════════════════════════
_funding_cache = {"ts": 0, "data": {}}
_oi_cache = {}

def refresh_funding_all(get_pub, futures_base):
    """Tüm USDT-perp funding oranlarını tek çağrıda çeker, 5 dk cache."""
    now = time.time()
    if _funding_cache["data"] and now - _funding_cache["ts"] < 300:
        return _funding_cache["data"]
    out = {}
    try:
        data = get_pub("/fapi/v1/premiumIndex", base=futures_base, timeout=15)
        if isinstance(data, list):
            for item in data:
                s = item.get("symbol", "")
                if s.endswith("USDT"):
                    out[s[:-4]] = float(item.get("lastFundingRate", 0))
        _funding_cache["data"] = out
        _funding_cache["ts"] = now
    except Exception as e:
        print(f"[V4] funding fetch hata: {e}", flush=True)
    return out


def get_funding_rate(symbol, get_pub, futures_base):
    allf = refresh_funding_all(get_pub, futures_base)
    return allf.get(symbol)


def get_oi_change(symbol, get_pub, futures_base):
    """Son 1h OI değişim %. Coin başına 1 çağrı, 10 dk cache."""
    c = _oi_cache.get(symbol)
    now = time.time()
    if c and now - c["ts"] < 600:
        return c["value"]
    val = 0.0
    try:
        oi = get_pub("/futures/data/openInterestHist",
                     {"symbol": symbol + "USDT", "period": "1h", "limit": 2},
                     base=futures_base, timeout=10)
        if isinstance(oi, list) and len(oi) >= 2:
            a = float(oi[-2].get("sumOpenInterest", 0))
            b = float(oi[-1].get("sumOpenInterest", 0))
            if a > 0:
                val = round((b - a) / a * 100, 2)
    except Exception:
        pass
    _oi_cache[symbol] = {"value": val, "ts": now}
    return val


# ════════════════════════════════════════════════════════════════════════════
#  TOKEN UNLOCK — STATİK TAKVİM (manuel güncelleme, bedava)
# ════════════════════════════════════════════════════════════════════════════
# Format: "SEMBOL": [("YYYY-MM-DD", supply_yuzdesi), ...]
# Sadece >%1.5 olanlar veto tetikler. Ayda bir CoinGecko unlock sayfasından güncelle.
KNOWN_UNLOCKS = {
    "STRK": [("2026-06-15", 4.05)],
    "ARB":  [("2026-06-16", 2.1)],
    "APT":  [("2026-06-12", 1.9)],
    "SUI":  [("2026-06-01", 1.3)],
    "OP":   [("2026-06-30", 2.3)],
    "EIGEN":[("2026-06-20", 3.5)],
    "TIA":  [("2026-06-10", 5.2)],
    "ZK":   [("2026-06-17", 6.1)],
    "TRUMP":[("2026-06-18", 2.8)],
    "HYPE": [("2026-05-28", 2.0)],
}


def unlock_pct_next_48h(symbol, now_ts=None):
    """Önümüzdeki 48 saatte symbol için açılacak supply yüzdesi (en büyüğü)."""
    if now_ts is None:
        now_ts = time.time()
    events = KNOWN_UNLOCKS.get(symbol.upper())
    if not events:
        return 0.0
    horizon = now_ts + 48 * 3600
    best = 0.0
    for date_str, pct in events:
        try:
            t = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
        except Exception:
            continue
        if now_ts <= t <= horizon:
            best = max(best, pct)
    return best


# ════════════════════════════════════════════════════════════════════════════
#  ORTAK VERİ TOPLAMA — long ve short aynı ctx'i kullanır
# ════════════════════════════════════════════════════════════════════════════
def _build_ctx(symbol, *, get_pub, futures_base, get_market_regime,
               get_btc_correlation, get_volume_profile, get_whale_activity,
               get_news_sentiment, get_btc_trends):
    """Bir sembol için tüm veriyi toplayıp (k1h, k1d, ctx) döner. Hata olursa None."""
    sym = symbol.upper()
    pair = sym + "USDT"

    k1h = get_pub("/api/v3/klines", {"symbol": pair, "interval": "1h", "limit": 100})
    k1d = get_pub("/api/v3/klines", {"symbol": pair, "interval": "1d", "limit": 60})
    if not k1h or len(k1h) < 50 or not k1d or len(k1d) < 20:
        return None

    tk = get_pub("/api/v3/ticker/24hr", {"symbol": pair})
    change_24h = float(tk.get("priceChangePercent", 0))
    vol_usd = float(tk.get("quoteVolume", 0))

    vols = [float(k[7]) for k in k1h]
    if len(vols) >= 48:
        cur_v = sum(vols[-24:]); prev_v = sum(vols[-48:-24])
        vol_ratio = (cur_v / prev_v) if prev_v > 0 else 1.0
    else:
        vol_ratio = 1.0
    vol_ratio = max(0.1, min(20.0, vol_ratio))

    regime_info = get_market_regime()
    btc_tr = get_btc_trends()
    corr = get_btc_correlation(sym)
    vprof = get_volume_profile(sym)
    whale = get_whale_activity(sym)
    news = get_news_sentiment(sym)
    funding = get_funding_rate(sym, get_pub, futures_base)
    oi_change = get_oi_change(sym, get_pub, futures_base)

    btc_4h = 0.0
    try:
        bk = get_pub("/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 5})
        if len(bk) >= 5:
            btc_4h = (float(bk[-1][4]) - float(bk[0][4])) / float(bk[0][4]) * 100
    except Exception:
        pass

    ns = news.get("sentiment", "neutral")
    if ns in ("very_bullish", "bullish"):       news_label = "bullish"
    elif ns in ("very_bearish", "bearish"):     news_label = "bearish"
    elif ns in ("error", "no_news", "no_recent_news", "no_activity"): news_label = "error"
    else:                                       news_label = "neutral"

    ctx = {
        "regime": regime_info.get("regime", "SIDEWAYS"),
        "fear_greed": regime_info.get("fear_greed", 50),
        "btc_24h_change": btc_tr.get("change_24h", 0),
        "btc_4h_change": round(btc_4h, 2),
        "btc_correlation": corr,
        "funding_rate": funding,
        "oi_change_pct": oi_change,
        "unlock_pct_48h": unlock_pct_next_48h(sym),
        "whale_net_flow": whale.get("net_flow", 0) or 0,
        "whale_buy_volume": whale.get("whale_buy_volume", 0) or 0,
        "whale_sell_volume": whale.get("whale_sell_volume", 0) or 0,
        "buy_pct": vprof.get("buy_pct", 50),
        "vol_ratio": round(vol_ratio, 2),
        "news_sentiment": news_label,
        "sector_momentum": 0.5,
        "social_trend": 0.5,
        "volume_24h_usd": vol_usd,
        "change_24h": change_24h,
    }
    return k1h, k1d, ctx, funding, oi_change


# ════════════════════════════════════════════════════════════════════════════
#  LONG KÖPRÜSÜ
# ════════════════════════════════════════════════════════════════════════════
def compute_v4_for_symbol(symbol, *, get_pub, futures_base,
                          get_market_regime, get_btc_dominance,
                          get_btc_correlation, get_volume_profile,
                          get_whale_activity, get_news_sentiment,
                          get_btc_trends):
    """v4 LONG skoru. Hata olursa {"score":0,"error":...} döner, exception fırlatmaz."""
    try:
        sym = symbol.upper()
        built = _build_ctx(sym, get_pub=get_pub, futures_base=futures_base,
                           get_market_regime=get_market_regime,
                           get_btc_correlation=get_btc_correlation,
                           get_volume_profile=get_volume_profile,
                           get_whale_activity=get_whale_activity,
                           get_news_sentiment=get_news_sentiment,
                           get_btc_trends=get_btc_trends)
        if built is None:
            return {"symbol": sym, "score": 0, "error": "yetersiz kline"}
        k1h, k1d, ctx, funding, oi_change = built

        d = build_data_from_klines(sym, k1h, k1d, ctx)
        result = compute_smart_score_v4(d)
        result["targets"] = compute_dynamic_targets(d["price"], d.get("atr"))
        result["price"] = d["price"]
        result["atr"] = round(d["atr"], 8) if d.get("atr") else None
        result["funding_rate"] = funding
        result["oi_change_pct"] = oi_change
        result["unlock_pct_48h"] = ctx["unlock_pct_48h"]
        return result
    except Exception as e:
        return {"symbol": symbol.upper(), "score": 0, "error": str(e)[:200]}


# ════════════════════════════════════════════════════════════════════════════
#  SHORT KÖPRÜSÜ — ⚠️ PAPER TRADING (bear piyasa)
# ════════════════════════════════════════════════════════════════════════════
def compute_v4_short_for_symbol(symbol, *, get_pub, futures_base,
                                get_market_regime, get_btc_dominance,
                                get_btc_correlation, get_volume_profile,
                                get_whale_activity, get_news_sentiment,
                                get_btc_trends):
    """v4-SHORT skoru. Aynı veriyi toplar, short motoruna verir. ⚠️ PAPER."""
    try:
        from score_v4_short import compute_smart_score_v4_short, compute_short_targets
    except Exception as e:
        return {"symbol": symbol.upper(), "score": 0, "error": f"short motor yok: {e}"}
    try:
        sym = symbol.upper()
        built = _build_ctx(sym, get_pub=get_pub, futures_base=futures_base,
                           get_market_regime=get_market_regime,
                           get_btc_correlation=get_btc_correlation,
                           get_volume_profile=get_volume_profile,
                           get_whale_activity=get_whale_activity,
                           get_news_sentiment=get_news_sentiment,
                           get_btc_trends=get_btc_trends)
        if built is None:
            return {"symbol": sym, "score": 0, "error": "yetersiz kline"}
        k1h, k1d, ctx, funding, oi_change = built

        d = build_data_from_klines(sym, k1h, k1d, ctx)
        result = compute_smart_score_v4_short(d)
        result["targets"] = compute_short_targets(d["price"], d.get("atr"))
        result["price"] = d["price"]
        result["atr"] = round(d["atr"], 8) if d.get("atr") else None
        result["funding_rate"] = funding
        return result
    except Exception as e:
        return {"symbol": symbol.upper(), "score": 0, "error": str(e)[:200]}
