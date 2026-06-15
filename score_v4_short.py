# -*- coding: utf-8 -*-
"""
KriptoAI — Skor Motoru v4-SHORT  (bear piyasa, SHORT paper trading)
====================================================================
⚠️ PAPER TRADING — gerçek emir YOK. Gölge tabloya kaydedilir, isabet ölçülür.
⚠️ SHORT RİSKLERİ: yukarı sınırsız zarar, likidasyon, funding maliyeti, squeeze.

Felsefe: YÜKSEK SKOR = YÜKSEK DÜŞÜŞ OLASILIĞI = SHORT
Long v4'ün AYNASI ama gate'ler bear-dostu (long bear'da kapalı, short bear'da açık).

Mimari (long ile aynı iskelet, ters yorumlama):
  GATE (short açma vetoları) × CORE (0-100 düşüş olasılığı)
  Bileşenler: zirve kalitesi 35, akış 25, trend 20, katalizör 12, kalite 8
  SHORT eşiği: skor >= 72
"""
from score_v4 import (
    calc_rsi, calc_atr, calc_ma, calc_bollinger, calc_ema,
    mtf_trend_health, relative_strength, _clamp01,
)

# ════════════════════════════════════════════════════════════════════════════
#  SHORT'A ÖZGÜ GÖSTERGELER (long'un tersi)
# ════════════════════════════════════════════════════════════════════════════

def detect_lower_high(highs, lookback=6):
    """Zirve teyidi: son <lookback> mumda tepe OLUŞMUŞ ve GEÇMİŞ mi? (higher_low'un tersi)"""
    if len(highs) < lookback:
        return False
    recent = highs[-lookback:]
    max_idx = recent.index(max(recent))
    if max_idx >= lookback - 1:        # tepe hâlâ devam ediyor → yukarı momentum sürüyor
        return False
    return recent[-1] < recent[max_idx]  # son mum tepeden aşağıda → düşüş başladı


def detect_upper_rejection(o, h, l, c):
    """Üst fitil reddi: mum yukarı uzandı ama kırmızı kapandı → alıcı reddedildi."""
    rng = h - l
    if rng <= 0:
        return False
    upper_wick = h - max(o, c)
    body = abs(c - o)
    return upper_wick > body * 1.5 and c <= o


def calc_rally_pct(closes_window):
    """Rally derinliği: pencere dibinden mevcut fiyata yükseliş % (drawdown'un tersi, pozitif)."""
    if not closes_window:
        return 0.0
    trough = min(closes_window)
    if trough <= 0:
        return 0.0
    return (closes_window[-1] - trough) / trough * 100.0


def mtf_trend_health_short(daily_closes):
    """Short için MTF: 1.0 = günlük AŞAĞI (short'u destekler), 0.0 = güçlü yukarı (short tehlikeli)."""
    h = mtf_trend_health(daily_closes)   # long: 1=sağlıklı yukarı, 0=serbest düşüş
    return 1.0 - h                       # short için ters çevir


# ════════════════════════════════════════════════════════════════════════════
#  VETO KAPILARI — short AÇMA (biri kapalıysa skor=0)
# ════════════════════════════════════════════════════════════════════════════

def check_gates_short(d):
    failed = []
    detail = {}

    regime = d.get("regime", "SIDEWAYS")
    detail["regime"] = regime
    # Short SADECE bear/sideways'te. Bull'da short açma.
    if regime == "BULL":
        failed.append("BULL piyasa (short tehlikeli)")

    # BTC son 4h sert YUKARI → short açma (momentum yukarı, ezilirsin)
    btc_4h = d.get("btc_4h_change", 0.0)
    detail["btc_4h"] = btc_4h
    if btc_4h >= 2.5:
        failed.append(f"BTC 4h pump +{btc_4h:.1f}%")

    # Funding aşırı NEGATİF → herkes zaten short → yukarı squeeze riski
    funding = d.get("funding_rate", 0.0)
    detail["funding"] = funding
    if funding is not None and funding < -0.0005:
        failed.append(f"funding aşırı negatif {funding*100:.3f}% (squeeze riski)")

    # Coin zaten ÇOK düşmüş → dip bounce riski, short geç kaldı
    rally = calc_rally_pct(d.get("closes_7d", []))
    dd = d.get("_drawdown_7d", 0.0)
    detail["drawdown_7d"] = dd
    if dd <= -30:
        failed.append(f"zaten -%{abs(dd):.0f} düşmüş (bounce riski)")

    # F&G aşırı korku → kapitülasyon, dip yakın, short geç
    fng = d.get("fear_greed", 50)
    detail["fear_greed"] = fng
    if fng <= 12:
        failed.append(f"F&G {fng} aşırı korku (dip bounce riski)")

    # Likidite
    vol_usd = d.get("volume_24h_usd", 0.0)
    detail["vol_24h_usd"] = vol_usd
    if vol_usd < 5_000_000:
        failed.append(f"düşük likidite ${vol_usd/1e6:.1f}M")

    return {"passed": len(failed) == 0, "failed": failed, "detail": detail}


# ════════════════════════════════════════════════════════════════════════════
#  ÇEKİRDEK BİLEŞENLER (long'un ters yorumu)
# ════════════════════════════════════════════════════════════════════════════

def score_peak_quality(d):
    """Zirve kalitesi (35). Long'daki dip_quality'nin aynası."""
    parts = {}
    closes = d.get("closes", [])
    highs = d.get("highs", [])

    # RSI yüksekliği (12) — yüksek RSI = aşırı alım = düşüş adayı
    rsi = d.get("rsi") or calc_rsi(closes)
    if rsi is None:    rsi_s = 0.4
    elif rsi > 75:     rsi_s = 1.0
    elif rsi > 65:     rsi_s = 0.8
    elif rsi > 55:     rsi_s = 0.5
    elif rsi > 45:     rsi_s = 0.25
    elif rsi > 35:     rsi_s = 0.1
    else:              rsi_s = 0.0   # aşırı satım = short için kötü
    parts["rsi"] = rsi_s * 12

    # Zirve teyidi (13) — lower_high (8) + üst fitil reddi (5)
    lh = detect_lower_high(highs) if highs else False
    last = d.get("last_candle")
    ur = detect_upper_rejection(*last) if last else False
    parts["lower_high"]      = (8 if lh else 0)
    parts["upper_rejection"] = (5 if ur else 0)

    # Bollinger üst bant (5) — fiyat üst banda yakın = zirve
    bb = d.get("bollinger") or (calc_bollinger(closes) if closes else None)
    price = d.get("price", closes[-1] if closes else 0)
    if bb and price > 0:
        rng = bb["upper"] - bb["lower"]
        pos = (price - bb["lower"]) / rng if rng > 0 else 0.5
        bb_s = _clamp01((pos - 0.6) / 0.4)   # üste yakın = yüksek puan
    else:
        bb_s = 0.4
    parts["bollinger"] = bb_s * 5

    # Rally derinliği (5) — son 7g çok yükselmiş = geri çekilecek
    rally = calc_rally_pct(d.get("closes_7d", closes[-7:] if len(closes) >= 7 else closes))
    if   rally >= 25: r_s = 1.0
    elif rally >= 15: r_s = 0.8
    elif rally >= 8:  r_s = 0.5
    elif rally >= 3:  r_s = 0.25
    else:             r_s = 0.0
    parts["rally"] = r_s * 5

    return sum(parts.values()), parts


def score_flow_short(d):
    """Akış (25). Satış baskısı + balina dağıtım + funding pozitif (long squeeze)."""
    parts = {}

    # Satış hacmi dönüşü (8) — hacim artışı düşüşü teyit ediyor
    vr = d.get("vol_ratio", 1.0)
    if   vr >= 2.0: v_s = 1.0
    elif vr >= 1.5: v_s = 0.8
    elif vr >= 1.2: v_s = 0.5
    elif vr >= 1.0: v_s = 0.3
    else:           v_s = 0.1
    parts["vol_donus"] = v_s * 8

    # Satıcı baskınlığı (7) — buy_pct DÜŞÜK = satıcı hakim
    buy_pct = d.get("buy_pct", 50.0)
    sell_dom = _clamp01((55 - buy_pct) / 20)   # 55%→0, 35%→1
    parts["sell_pressure"] = sell_dom * 7

    # Balina dağıtımı (6) — net_flow NEGATİF = balina satıyor
    net = d.get("whale_net_flow", 0.0)
    wbuy = d.get("whale_buy_volume", 0.0)
    wsell = d.get("whale_sell_volume", 0.0)
    tot = wbuy + wsell
    if tot > 0:
        w_s = _clamp01((-net / tot + 0.2) / 0.6)   # net negatifse yüksek puan
    else:
        w_s = 0.4
    parts["whale_distribution"] = w_s * 6

    # Funding aşırı POZİTİF (4) — long'lar dolu → aşağı squeeze potansiyeli
    funding = d.get("funding_rate", 0.0) or 0.0
    if   funding > 0.0005: f_s = 1.0
    elif funding > 0.0002: f_s = 0.6
    elif funding > 0.0:    f_s = 0.3
    else:                  f_s = 0.0
    parts["funding"] = f_s * 4

    return sum(parts.values()), parts


def score_trend_short(d):
    """Trend (20). MTF aşağı + coin BTC'den zayıf + MA üstünde (düşecek mesafe)."""
    parts = {}

    # MTF günlük aşağı (8)
    mtf_s = mtf_trend_health_short(d.get("daily_closes", []))
    parts["mtf"] = mtf_s * 8

    # Göreceli zayıflık (7) — coin BTC'den zayıfsa düşüşe daha yatkın
    rs = relative_strength(d.get("change_24h", 0.0), d.get("btc_24h_change", 0.0))
    parts["relative_weakness"] = (1.0 - rs) * 7

    # MA mesafesi (5) — fiyat MA50 ÜSTÜNDE = düşecek alan var
    closes = d.get("closes", [])
    ma50 = d.get("ma50") or (calc_ma(closes, 50) if len(closes) >= 50 else None)
    price = d.get("price", closes[-1] if closes else 0)
    if ma50 and ma50 > 0 and price > 0:
        dist = (price - ma50) / ma50 * 100
        if   4 <= dist <= 15:  ma_s = 1.0   # üstünde, düşecek mesafe ideal
        elif 15 < dist <= 25:  ma_s = 0.6   # çok yukarı, ama bounce riski
        elif -2 <= dist < 4:   ma_s = 0.5
        else:                  ma_s = 0.2   # altında, zaten düşmüş
    else:
        ma_s = 0.4
    parts["ma_mesafe"] = ma_s * 5

    return sum(parts.values()), parts


def score_catalyst_short(d):
    """Katalizör (12). Negatif haber + sektör zayıf + sosyal aşırı (FOMO tepe)."""
    parts = {}

    news = d.get("news_sentiment", "neutral")
    if   news == "bearish": n_s = 1.0
    elif news in ("neutral", "error", None): n_s = 0.5
    else: n_s = 0.0   # bullish haber = short için kötü
    parts["haber"] = n_s * 5

    sec = d.get("sector_momentum", 0.5)
    parts["sektor_zayif"] = _clamp01(1.0 - sec) * 4   # sektör zayıfsa short iyi

    # Sosyal aşırı yüksek = FOMO tepe = short fırsatı
    soc = d.get("social_trend", 0.5)
    parts["sosyal_fomo"] = _clamp01((soc - 0.5) / 0.4) * 3
    return sum(parts.values()), parts


def score_quality_short(d):
    """Kalite (8). Likidite + volatilite — long ile aynı."""
    parts = {}
    vol_usd = d.get("volume_24h_usd", 0.0)
    if   vol_usd >= 100e6: l_s = 1.0
    elif vol_usd >= 30e6:  l_s = 0.7
    elif vol_usd >= 10e6:  l_s = 0.5
    elif vol_usd >= 5e6:   l_s = 0.3
    else:                  l_s = 0.0
    parts["likidite"] = l_s * 4

    atr = d.get("atr"); price = d.get("price", 0)
    if atr and price > 0:
        atr_pct = atr / price * 100
        if   2 <= atr_pct <= 7:   v_s = 1.0
        elif 1 <= atr_pct < 2:    v_s = 0.5
        elif 7 < atr_pct <= 12:   v_s = 0.5
        else:                     v_s = 0.2
    else:
        v_s = 0.4
    parts["volatilite"] = v_s * 4
    return sum(parts.values()), parts


# ════════════════════════════════════════════════════════════════════════════
#  ANA SHORT SKOR FONKSİYONU
# ════════════════════════════════════════════════════════════════════════════

def compute_smart_score_v4_short(d):
    """SHORT skoru. Yüksek skor = yüksek düşüş olasılığı. ⚠️ PAPER TRADING."""
    # drawdown'u gate için hesapla
    from score_v4 import calc_drawdown_pct
    d["_drawdown_7d"] = calc_drawdown_pct(d.get("closes_7d", []))

    gates = check_gates_short(d)
    if not gates["passed"]:
        return {
            "symbol": d.get("symbol", "?"), "score": 0, "rec": "BEKLE",
            "vetoed": True, "veto_reasons": gates["failed"],
            "gate_detail": gates["detail"], "components": {}, "side": "SHORT",
        }

    peak, peak_p = score_peak_quality(d)
    flow, flow_p = score_flow_short(d)
    trnd, trnd_p = score_trend_short(d)
    cat,  cat_p  = score_catalyst_short(d)
    qual, qual_p = score_quality_short(d)

    score = max(0, min(100, round(peak + flow + trnd + cat + qual)))
    if   score >= 72: rec = "SHORT"
    elif score >= 60: rec = "İZLE"
    else:             rec = "BEKLE"

    return {
        "symbol": d.get("symbol", "?"), "score": score, "rec": rec,
        "vetoed": False, "veto_reasons": [], "side": "SHORT",
        "components": {
            "peak_quality": round(peak, 1), "flow": round(flow, 1),
            "trend": round(trnd, 1), "catalyst": round(cat, 1), "quality": round(qual, 1),
        },
        "breakdown": {"peak": peak_p, "flow": flow_p, "trend": trnd_p,
                      "catalyst": cat_p, "quality": qual_p},
        "rsi": d.get("rsi") or calc_rsi(d.get("closes", [])),
    }


def compute_short_targets(price, atr):
    """SHORT için ATR tabanlı SL/TP. SL YUKARIDA (fiyat yükselirse zarar), TP AŞAĞIDA."""
    if not atr or atr <= 0 or price <= 0:
        return {"sl": price*1.06, "tp1": price*0.92, "tp2": price*0.84,
                "sl_pct": 6.0, "tp1_pct": 8.0, "tp2_pct": 16.0, "atr_used": False}
    sl  = price + 1.5 * atr     # yukarıda — short'ta zarar yukarı
    tp1 = price - 2.0 * atr     # aşağıda — short'ta kâr aşağı
    tp2 = price - 3.5 * atr
    if (sl - price)  / price * 100 < 1.5: sl  = price * 1.015
    if (price - tp1) / price * 100 < 2.5: tp1 = price * 0.975
    if (price - tp2) / price * 100 < 5.0: tp2 = price * 0.950
    return {
        "sl": sl, "tp1": tp1, "tp2": tp2,
        "sl_pct":  round((sl - price)  / price * 100, 2),
        "tp1_pct": round((price - tp1) / price * 100, 2),
        "tp2_pct": round((price - tp2) / price * 100, 2),
        "atr_used": True,
    }
