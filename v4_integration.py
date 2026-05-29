# -*- coding: utf-8 -*-
"""
KriptoAI — v4 Entegrasyon Köprüsü
==================================
main.py'a tek satırla bağlanır. v3 tracker'ı BOZMAZ; v4 skorunu paralel üretir.

main.py'da yapılacak:
  from v4_integration import compute_v4_for_symbol, get_funding_rate, KNOWN_UNLOCKS

Bu modül main.py'daki şu fonksiyonları parametre olarak alır (import değil, geçiş):
  get_pub, FUTURES_BASE, get_market_regime, get_btc_dominance,
  get_btc_correlation, get_volume_profile, get_whale_activity, get_news_sentiment

Böylece main.py'daki cache'li fonksiyonlar tekrar yazılmaz, aynısı kullanılır.
"""
import time, urllib.request, json
from score_v4 import (
    compute_smart_score_v4, compute_dynamic_targets, build_data_from_klines,
    calc_rsi, calc_atr, calc_ma, calc_bollinger, mtf_trend_health,
)

# ════════════════════════════════════════════════════════════════════════════
#  FUNDING RATE — Binance Futures (auth'suz, fapi). main.py'daki get_pub kullanır.
# ════════════════════════════════════════════════════════════════════════════
_funding_cache = {"ts": 0, "data": {}}          # tüm coinler tek çağrıda
_oi_cache = {}                                   # symbol -> {"value":, "ts":}

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
                    out[s[:-4]] = float(item.get("lastFundingRate", 0))  # ondalık (0.0001=%0.01)
        _funding_cache["data"] = out
        _funding_cache["ts"] = now
    except Exception as e:
        print(f"[V4] funding fetch hata: {e}", flush=True)
    return out


def get_funding_rate(symbol, get_pub, futures_base):
    """Tek sembol funding (tüm-liste cache'inden)."""
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
#  TOKEN UNLOCK — STATİK TAKVİM (manuel güncelleme, bedava, 403 yemez)
# ════════════════════════════════════════════════════════════════════════════
# Büyük unlock'lar haftalar önceden bellidir. Ayda bir CoinGecko/Tokenomist
# unlock sayfasından bakıp aşağıyı güncelle. Format:
#   "SEMBOL": [("YYYY-MM-DD", supply_yuzdesi), ...]
# supply_yuzdesi: o tarihte dolaşıma girecek toplam arzın yüzdesi.
# Sadece >%1.5 olanlar veto tetikler (score_v4 check_gates).
#
# ⚠️ GÜNCELLEME NOTU (son: Mayıs 2026 — web verisinden):
#   Aşağıdaki tarihler örnek/yaklaşık. Gerçek tarihleri unlock takviminden teyit et.
KNOWN_UNLOCKS = {
    # sembol : [(tarih, supply%), ...]
    "STRK": [("2026-06-15", 4.05)],     # ~%4 cliff, web verisi
    "ARB":  [("2026-06-16", 2.1)],
    "APT":  [("2026-06-12", 1.9)],
    "SUI":  [("2026-06-01", 1.3)],      # <1.5 → veto tetiklemez, bilgi amaçlı
    "OP":   [("2026-06-30", 2.3)],
    "EIGEN":[("2026-06-20", 3.5)],
    "TIA":  [("2026-06-10", 5.2)],
    "ZK":   [("2026-06-17", 6.1)],
    "TRUMP":[("2026-06-18", 2.8)],
    # HYPE büyük dev cliff — tarih teyidi gerek
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
            # tarih -> epoch (UTC gün başı varsayımı)
            t = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
        except Exception:
            continue
        if now_ts <= t <= horizon:
            best = max(best, pct)
    return best


# ════════════════════════════════════════════════════════════════════════════
#  ANA KÖPRÜ — bir sembol için v4 skoru üretir (main.py'dan çağrılır)
# ════════════════════════════════════════════════════════════════════════════
def compute_v4_for_symbol(symbol, *, get_pub, futures_base,
                          get_market_regime, get_btc_dominance,
                          get_btc_correlation, get_volume_profile,
                          get_whale_activity, get_news_sentiment,
                          get_btc_trends):
    """
    main.py'daki mevcut fonksiyonları kullanarak v4 verisini toplar ve skoru döner.
    Hepsi parametre — main.py'daki cache'li sürümler aynen kullanılır, kod tekrarı yok.
    Hata olursa {"score":0,"error":...} döner, asla exception fırlatmaz (tracker'ı bozmaz).
    """
    try:
        sym = symbol.upper()
        pair = sym + "USDT"

        # 1h ve 1d kline (RSI, ATR, MA, Bollinger, dip teyidi, MTF, drawdown)
        k1h = get_pub("/api/v3/klines", {"symbol": pair, "interval": "1h", "limit": 100})
        k1d = get_pub("/api/v3/klines", {"symbol": pair, "interval": "1d", "limit": 60})
        if not k1h or len(k1h) < 50 or not k1d or len(k1d) < 20:
            return {"symbol": sym, "score": 0, "error": "yetersiz kline"}

        # 24h ticker (değişim, hacim)
        tk = get_pub("/api/v3/ticker/24hr", {"symbol": pair})
        change_24h = float(tk.get("priceChangePercent", 0))
        vol_usd = float(tk.get("quoteVolume", 0))

        # Hacim oranı (son 24h / önceki 24h)
        vols = [float(k[7]) for k in k1h]
        if len(vols) >= 48:
            cur_v = sum(vols[-24:]); prev_v = sum(vols[-48:-24])
            vol_ratio = (cur_v / prev_v) if prev_v > 0 else 1.0
        else:
            vol_ratio = 1.0
        vol_ratio = max(0.1, min(20.0, vol_ratio))

        # Makro (cache'li, main.py'dan)
        regime_info = get_market_regime()
        btc_tr = get_btc_trends()
        dom = get_btc_dominance()
        corr = get_btc_correlation(sym)
        vprof = get_volume_profile(sym)
        whale = get_whale_activity(sym)
        news = get_news_sentiment(sym)

        # Futures (fapi)
        funding = get_funding_rate(sym, get_pub, futures_base)
        oi_change = get_oi_change(sym, get_pub, futures_base)

        # BTC 4h değişim (gate için) — btc_trends 24h veriyor, 4h'i kline'dan al
        btc_4h = 0.0
        try:
            bk = get_pub("/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 5})
            if len(bk) >= 5:
                btc_4h = (float(bk[-1][4]) - float(bk[0][4])) / float(bk[0][4]) * 100
        except Exception:
            pass

        # News sentiment -> v4 etiketi
        ns = news.get("sentiment", "neutral")
        if ns in ("very_bullish", "bullish"):
            news_label = "bullish"
        elif ns in ("very_bearish", "bearish"):
            news_label = "bearish"
        elif ns in ("error", "no_news", "no_recent_news", "no_activity"):
            news_label = "error"   # veri yok → ceza verme (score_catalyst 0.5)
        else:
            news_label = "neutral"

        # Whale net flow
        whale_net = whale.get("net_flow", 0) or 0
        whale_buy = whale.get("whale_buy_volume", 0) or 0
        whale_sell = whale.get("whale_sell_volume", 0) or 0

        # Sektör momentumu — şimdilik nötr (sectors endpoint coin->sektör eşleme gerektirir, faz 2)
        sector_momentum = 0.5
        social_trend = 0.5  # trending-coins entegrasyonu faz 2

        ctx = {
            "regime": regime_info.get("regime", "SIDEWAYS"),
            "fear_greed": regime_info.get("fear_greed", 50),
            "btc_24h_change": btc_tr.get("change_24h", 0),
            "btc_4h_change": round(btc_4h, 2),
            "btc_correlation": corr,
            "funding_rate": funding,            # None olabilir → gate güvenli geçer
            "oi_change_pct": oi_change,
            "unlock_pct_48h": unlock_pct_next_48h(sym),
            "whale_net_flow": whale_net,
            "whale_buy_volume": whale_buy,
            "whale_sell_volume": whale_sell,
            "buy_pct": vprof.get("buy_pct", 50),
            "vol_ratio": round(vol_ratio, 2),
            "news_sentiment": news_label,
            "sector_momentum": sector_momentum,
            "social_trend": social_trend,
            "volume_24h_usd": vol_usd,
            "change_24h": change_24h,
        }

        d = build_data_from_klines(sym, k1h, k1d, ctx)
        result = compute_smart_score_v4(d)

        # ATR tabanlı dinamik hedefler
        result["targets"] = compute_dynamic_targets(d["price"], d.get("atr"))
        result["price"] = d["price"]
        result["atr"] = round(d["atr"], 8) if d.get("atr") else None
        result["funding_rate"] = funding
        result["oi_change_pct"] = oi_change
        result["unlock_pct_48h"] = ctx["unlock_pct_48h"]
        return result

    except Exception as e:
        return {"symbol": symbol.upper(), "score": 0, "error": str(e)[:200]}
