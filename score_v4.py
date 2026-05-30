# -*- coding: utf-8 -*-
"""
KriptoAI — Skor Motoru v4  (mean-reversion + dip teyidi tabanlı)
================================================================
Felsefe: YÜKSEK SKOR = YÜKSEK KAZANMA OLASILIĞI = AL  (g.g'nin orijinal niyeti)
Eski v3'ün hatası: skor "momentum/tepe gücü" ölçüyordu. v4 "kazanma olasılığı" ölçer.

Mimari:  GATE (veto kapıları)  ×  CORE (0-100 olasılık skoru)
  - Herhangi bir veto kapısı kapalıysa  -> skor 0, sinyal yok
  - Geçenler için 5 bileşenli ağırlıklı skor:
        Dip kalitesi      35
        Akış teyidi       25
        Trend & gör.güç   20
        Katalizör&sektör  12
        Kalite&likidite    8
  - AL eşiği: skor >= 72  (veriyle kalibre edilecek)

KATMAN AYRIMI:
  - compute_smart_score_v4(data)  -> SAF hesaplama (veri çekmez, test edilebilir)
  - fetch_coin_data_v4(symbol)    -> veri toplama (Binance/fapi), data dict üretir
Bu ayrım sayesinde geçmiş kline'larla backtest yapılabilir.
"""

# ============================================================================
#  BÖLÜM 1 — TEKNİK GÖSTERGE YARDIMCILARI (saf Python, bağımlılık yok)
# ============================================================================

def calc_rsi(closes, period=14):
    """Wilder RSI. closes: eskiden yeniye sıralı kapanış listesi."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period-1) + gains[i]) / period
        avg_loss = (avg_loss * (period-1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def calc_atr(highs, lows, closes, period=14):
    """Wilder ATR — dinamik SL/TP için."""
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i] - closes[i-1]))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period-1) + trs[i]) / period
    return atr


def calc_ma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calc_ema(closes, period):
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema


def calc_bollinger(closes, period=20, mult=2.0):
    if len(closes) < period:
        return None
    w = closes[-period:]
    mean = sum(w) / period
    var = sum((x - mean) ** 2 for x in w) / period
    std = var ** 0.5
    return {"upper": mean + mult*std, "lower": mean - mult*std, "mid": mean, "std": std}


def detect_higher_low(lows, lookback=6):
    """
    Dip teyidi: son <lookback> mumda dip OLUŞMUŞ ve GEÇMİŞ mi?
    - En düşük dip son mumda DEĞİL (yani düşüş durdu)
    - Son mum, dip mumundan yukarıda (yani toparlıyor)
    True dönerse 'düşen bıçak' değil, gerçek dönüş adayı.
    """
    if len(lows) < lookback:
        return False
    recent = lows[-lookback:]
    min_idx = recent.index(min(recent))
    if min_idx >= lookback - 1:        # dip hâlâ devam ediyor -> bıçak düşüyor
        return False
    return recent[-1] > recent[min_idx]  # son mum dipten yukarıda


def detect_rejection_wick(o, h, l, c):
    """Son mumda uzun alt fitil + güçlü kapanış = satıcı reddedildi (dönüş işareti)."""
    rng = h - l
    if rng <= 0:
        return False
    lower_wick = min(o, c) - l
    body = abs(c - o)
    return lower_wick > body * 1.5 and c >= o


def calc_drawdown_pct(closes_window):
    """Oversold derinliği: pencere tepesinden mevcut fiyata düşüş % (negatif)."""
    if not closes_window:
        return 0.0
    peak = max(closes_window)
    if peak <= 0:
        return 0.0
    return (closes_window[-1] - peak) / peak * 100.0


def mtf_trend_health(daily_closes):
    """
    Üst zaman dilimi (günlük) trend sağlığı: 0=serbest düşüş(tuzak), 0.5=zayıf, 1=sağlıklı
    Saatlik dip + günlük serbest düşüş = falling knife -> 0 döner (çekirdek skoru kırar)
    """
    ema50 = calc_ema(daily_closes, 50)
    if ema50 is None or ema50 <= 0:
        return 0.5
    cur = daily_closes[-1]
    ema50_prev = calc_ema(daily_closes[:-3], 50) if len(daily_closes) > 53 else None
    falling = (ema50_prev is not None) and (ema50 < ema50_prev)
    dist = (cur - ema50) / ema50
    if dist < -0.15 and falling:
        return 0.0          # EMA'nın çok altında + EMA düşüyor = serbest düşüş
    if dist > 0:
        return 1.0          # EMA üstünde = sağlıklı zemin
    return 0.5              # EMA altında ama yakın = nötr


def relative_strength(coin_chg_24h, btc_chg_24h):
    """Coin BTC'den ne kadar güçlü? -%5 fark=0, +%5 fark=1. BTC yatay/düşerken coin yükseliyorsa yüksek."""
    diff = coin_chg_24h - btc_chg_24h
    return max(0.0, min(1.0, (diff + 5.0) / 10.0))


def _clamp01(x):
    return max(0.0, min(1.0, x))


# ============================================================================
#  BÖLÜM 2 — VETO KAPILARI  (biri kapalıysa skor = 0)
# ============================================================================

def check_gates(d):
    """
    d: coin veri sözlüğü. Her kapı için (geçti_mi, sebep) döner.
    Dönüş: {"passed": bool, "failed": [sebep,...], "detail": {...}}
    """
    failed = []
    detail = {}

    # 1) Piyasa rejimi BEAR/EXTREME_BEAR olmamalı
    regime = d.get("regime", "SIDEWAYS")
    detail["regime"] = regime
    if regime in ("BEAR", "EXTREME_BEAR"):
        failed.append(f"rejim={regime}")

    # 2) BTC son 4h sert dump yok
    btc_4h = d.get("btc_4h_change", 0.0)
    detail["btc_4h"] = btc_4h
    if btc_4h <= -2.5:
        failed.append(f"BTC 4h dump {btc_4h:.1f}%")

    # 3) Yüksek korelasyon + BTC düşüyor
    corr = d.get("btc_correlation", 0.0)
    btc_24h = d.get("btc_24h_change", 0.0)
    detail["corr"] = corr
    if corr > 0.7 and btc_24h < -1.0:
        failed.append(f"korelasyon {corr:.2f} + BTC düşüş")

    # 4) Aşırı açgözlülük (tepe riski)
    fng = d.get("fear_greed", 50)
    detail["fear_greed"] = fng
    if fng >= 80:
        failed.append(f"F&G aşırı hırs {fng}")

    # 5) Funding aşırı pozitif değil (squeeze riski)
    funding = d.get("funding_rate", 0.0)   # 8 saatlik oran, ondalık (0.001 = %0.1)
    detail["funding"] = funding
    if funding is not None and funding > 0.0008:
        failed.append(f"funding aşırı +{funding*100:.3f}%")

    # 6) Token unlock kapısı — 48h içinde >%1.5 supply unlock yok
    unlock = d.get("unlock_pct_48h", 0.0)  # önümüzdeki 48h'te açılacak supply yüzdesi
    detail["unlock_48h"] = unlock
    if unlock is not None and unlock > 1.5:
        failed.append(f"unlock {unlock:.1f}% (48h)")

    # 7) MTF günlük trend serbest düşüşte değil
    mtf = d.get("mtf_health")
    if mtf is None:
        mtf = mtf_trend_health(d.get("daily_closes", []))
    detail["mtf_health"] = mtf
    if mtf == 0.0:
        failed.append("günlük serbest düşüş")

    # 8) Likidite eşiği
    vol_usd = d.get("volume_24h_usd", 0.0)
    detail["vol_24h_usd"] = vol_usd
    if vol_usd < 5_000_000:
        failed.append(f"düşük likidite ${vol_usd/1e6:.1f}M")

    return {"passed": len(failed) == 0, "failed": failed, "detail": detail}


# ============================================================================
#  BÖLÜM 3 — ÇEKİRDEK SKOR BİLEŞENLERİ  (her biri 0-1 normalize)
# ============================================================================

def score_dip_quality(d):
    """Dip kalitesi (ağırlık 35). RSI derinliği + dip teyidi + Bollinger + drawdown."""
    parts = {}
    closes = d.get("closes", [])
    lows   = d.get("lows", [])

    # RSI derinliği (max 12p) — düşük RSI = dip fırsatı (TERS çevrilmiş, doğru mantık)
    rsi = d.get("rsi")
    if rsi is None:
        rsi = calc_rsi(closes)
    if rsi is None:
        rsi_s = 0.4
    elif rsi < 30:   rsi_s = 1.0
    elif rsi < 40:   rsi_s = 0.8
    elif rsi < 50:   rsi_s = 0.5
    elif rsi < 60:   rsi_s = 0.25
    elif rsi < 70:   rsi_s = 0.1
    else:            rsi_s = 0.0     # aşırı alım = tepe riski, puan yok
    parts["rsi"] = rsi_s * 12

    # Dip teyidi (max 13p) — higher_low (8p) + rejection_wick (5p)  *** EN KRİTİK ***
    hl = detect_higher_low(lows) if lows else False
    last = d.get("last_candle")  # (o,h,l,c)
    rw = detect_rejection_wick(*last) if last else False
    parts["higher_low"]     = (8 if hl else 0)
    parts["rejection_wick"] = (5 if rw else 0)

    # Bollinger alt bant teması (max 5p)
    bb = d.get("bollinger") or (calc_bollinger(closes) if closes else None)
    price = d.get("price", closes[-1] if closes else 0)
    if bb and price > 0:
        rng = bb["upper"] - bb["lower"]
        pos = (price - bb["lower"]) / rng if rng > 0 else 0.5  # 0=alt bant, 1=üst bant
        bb_s = _clamp01(1.0 - pos * 1.4)  # alta yakın = yüksek puan
    else:
        bb_s = 0.4
    parts["bollinger"] = bb_s * 5

    # Oversold derinliği (max 5p) — son 7g drawdown
    dd = calc_drawdown_pct(d.get("closes_7d", closes[-7:] if len(closes) >= 7 else closes))
    if   dd <= -25: dd_s = 1.0
    elif dd <= -15: dd_s = 0.8
    elif dd <= -8:  dd_s = 0.5
    elif dd <= -3:  dd_s = 0.25
    else:           dd_s = 0.0     # düşmemiş = "dip" değil
    parts["drawdown"] = dd_s * 5

    total = sum(parts.values())   # 0-35
    return total, parts


def score_flow(d):
    """Akış teyidi (ağırlık 25). Hacim dönüşü + volume profile + balina + funding/OI."""
    parts = {}

    # Hacim dönüşü (max 8p)
    vr = d.get("vol_ratio", 1.0)
    if   vr >= 2.0: v_s = 1.0
    elif vr >= 1.5: v_s = 0.8
    elif vr >= 1.2: v_s = 0.5
    elif vr >= 1.0: v_s = 0.3
    else:           v_s = 0.1
    parts["vol_donus"] = v_s * 8

    # Volume profile alıcı baskınlığı (max 7p)
    buy_pct = d.get("buy_pct", 50.0)
    bp_s = _clamp01((buy_pct - 45) / 20)  # 45%=0, 65%=1
    parts["volume_profile"] = bp_s * 7

    # Balina net akışı (max 6p)
    net = d.get("whale_net_flow", 0.0)
    wbuy = d.get("whale_buy_volume", 0.0)
    wsell = d.get("whale_sell_volume", 0.0)
    tot = wbuy + wsell
    if tot > 0:
        w_s = _clamp01((net / tot + 0.2) / 0.6)  # net/tot: -0.2->0, +0.4->1
    else:
        w_s = 0.4
    parts["whale"] = w_s * 6

    # Funding/OI birikim (max 4p): funding nötr/hafif negatif + OI artıyor = birikim
    funding = d.get("funding_rate", 0.0) or 0.0
    oi_change = d.get("oi_change_pct", 0.0)  # son 24h OI değişimi %
    f_s = 0.0
    if funding <= 0.0001:        # nötr veya negatif funding = kontra fırsat
        f_s += 0.6
    if oi_change > 2:            # OI artıyor (yeni para giriyor)
        f_s += 0.4
    parts["funding_oi"] = _clamp01(f_s) * 4

    total = sum(parts.values())   # 0-25
    return total, parts


def score_trend_rs(d):
    """Trend & göreceli güç (ağırlık 20). MTF + relative strength + MA mesafesi."""
    parts = {}

    # MTF günlük trend sağlığı (max 8p)
    mtf = d.get("mtf_health")
    if mtf is None:
        mtf = mtf_trend_health(d.get("daily_closes", []))
    parts["mtf"] = mtf * 8

    # Relative strength vs BTC (max 7p)
    rs = relative_strength(d.get("change_24h", 0.0), d.get("btc_24h_change", 0.0))
    parts["relative_strength"] = rs * 7

    # MA mesafesi (max 5p): MA50 altında -%5..-%15 = ideal toparlama alanı
    closes = d.get("closes", [])
    ma50 = d.get("ma50") or (calc_ma(closes, 50) if len(closes) >= 50 else None)
    price = d.get("price", closes[-1] if closes else 0)
    if ma50 and ma50 > 0 and price > 0:
        dist = (price - ma50) / ma50 * 100
        if   -15 <= dist <= -4: ma_s = 1.0   # ideal: altında ama dipte değil
        elif -25 <= dist < -15: ma_s = 0.5   # çok altında, riskli
        elif -4 < dist <= 2:    ma_s = 0.6   # MA civarı
        elif dist > 2:          ma_s = 0.2   # üstünde, geç kalınmış
        else:                   ma_s = 0.2   # -%25'ten dipte, trend kırık riski
    else:
        ma_s = 0.4
    parts["ma_mesafe"] = ma_s * 5

    total = sum(parts.values())   # 0-20
    return total, parts


def score_catalyst(d):
    """Katalizör & sektör (ağırlık 12). Haber + sektör + sosyal (aşırı değilse)."""
    parts = {}

    # Haber sentiment (max 5p): pozitif->tam, nötr/error->yarı, negatif->0
    news = d.get("news_sentiment", "neutral")
    if   news == "bullish": n_s = 1.0
    elif news in ("neutral", "error", None): n_s = 0.5  # error=veri yok, ceza verme
    else:  n_s = 0.0   # bearish
    parts["haber"] = n_s * 5

    # Sektör momentumu (max 4p)
    sec = d.get("sector_momentum", 0.5)  # 0-1 normalize sektör gücü
    parts["sektor"] = _clamp01(sec) * 4

    # Sosyal trend (max 3p): yükseliyor AMA aşırı değil (aşırı=FOMO tepe)
    soc = d.get("social_trend", 0.5)  # 0-1; 0.5-0.8 ideal, >0.9 FOMO cezası
    if soc > 0.9:   s_s = 0.3
    elif soc >= 0.5: s_s = soc
    else:            s_s = soc * 0.6
    parts["sosyal"] = _clamp01(s_s) * 3

    total = sum(parts.values())   # 0-12
    return total, parts


def score_quality(d):
    """Kalite & likidite (ağırlık 8). Likidite + volatilite uygunluğu."""
    parts = {}

    # Likidite (max 4p)
    vol_usd = d.get("volume_24h_usd", 0.0)
    if   vol_usd >= 100e6: l_s = 1.0
    elif vol_usd >= 30e6:  l_s = 0.7
    elif vol_usd >= 10e6:  l_s = 0.5
    elif vol_usd >= 5e6:   l_s = 0.3
    else:                  l_s = 0.0
    parts["likidite"] = l_s * 4

    # Volatilite uygunluğu (max 4p): ATR/fiyat çok düşük=hareketsiz, çok yüksek=riskli
    atr = d.get("atr")
    price = d.get("price", 0)
    if atr and price > 0:
        atr_pct = atr / price * 100
        if   2 <= atr_pct <= 7:   v_s = 1.0   # tatlı nokta: yeterli hareket, kontrollü
        elif 1 <= atr_pct < 2:    v_s = 0.5
        elif 7 < atr_pct <= 12:   v_s = 0.5
        else:                     v_s = 0.2
    else:
        v_s = 0.4
    parts["volatilite"] = v_s * 4

    total = sum(parts.values())   # 0-8
    return total, parts


# ============================================================================
#  BÖLÜM 4 — ANA SKOR FONKSİYONU (SAF — veri çekmez)
# ============================================================================

def compute_smart_score_v4(d):
    """
    d: coin veri sözlüğü (fetch_coin_data_v4 veya backtest verisi).
    Dönüş: skor + tam dökümü içeren sözlük. success anahtarı YOK (v3 hatası tekrarlanmaz).
    """
    gates = check_gates(d)
    if not gates["passed"]:
        return {
            "symbol": d.get("symbol", "?"),
            "score": 0,
            "rec": "BEKLE",
            "vetoed": True,
            "veto_reasons": gates["failed"],
            "gate_detail": gates["detail"],
            "components": {},
        }

    dip,  dip_parts  = score_dip_quality(d)
    flow, flow_parts = score_flow(d)
    trnd, trnd_parts = score_trend_rs(d)
    cat,  cat_parts  = score_catalyst(d)
    qual, qual_parts = score_quality(d)

    score = round(dip + flow + trnd + cat + qual)
    score = max(0, min(100, score))

    if   score >= 72: rec = "AL"
    elif score >= 60: rec = "İZLE"
    else:             rec = "BEKLE"

    return {
        "symbol": d.get("symbol", "?"),
        "score": score,
        "rec": rec,
        "vetoed": False,
        "veto_reasons": [],
        "components": {
            "dip_quality":  round(dip, 1),
            "flow":         round(flow, 1),
            "trend_rs":     round(trnd, 1),
            "catalyst":     round(cat, 1),
            "quality":      round(qual, 1),
        },
        "breakdown": {
            "dip": dip_parts, "flow": flow_parts, "trend": trnd_parts,
            "catalyst": cat_parts, "quality": qual_parts,
        },
        "rsi": d.get("rsi") or calc_rsi(d.get("closes", [])),
    }


def compute_dynamic_targets(price, atr, side="BUY"):
    """ATR tabanlı dinamik SL/TP. Sabit %3-8 yerine coin volatilitesine göre."""
    if not atr or atr <= 0 or price <= 0:
        # ATR yoksa makul sabit yüzdeye düş
        return {"sl": price*0.94, "tp1": price*1.08, "tp2": price*1.16,
                "sl_pct": 6.0, "tp1_pct": 8.0, "tp2_pct": 16.0, "atr_used": False}
    sl  = price - 1.5 * atr
    tp1 = price + 2.0 * atr
    tp2 = price + 3.5 * atr
    if (price - sl)  / price * 100 < 1.5: sl  = price * 0.985
    if (tp1 - price) / price * 100 < 2.5: tp1 = price * 1.025
    if (tp2 - price) / price * 100 < 5.0: tp2 = price * 1.050
    return {
        "sl": sl, "tp1": tp1, "tp2": tp2,
        "sl_pct":  round((price - sl) / price * 100, 2),
        "tp1_pct": round((tp1 - price) / price * 100, 2),
        "tp2_pct": round((tp2 - price) / price * 100, 2),
        "atr_used": True,
    }


# ============================================================================
#  BÖLÜM 5 — VERİ TOPLAMA (Binance/fapi). main.py'daki get_pub/req ile uyumlu.
#  NOT: Bu katman ağ erişimi gerektirir. main.py'a entegre ederken oradaki
#  fetcher'ları kullanabilir veya bu fonksiyonları çağırabilirsin.
# ============================================================================

def build_data_from_klines(symbol, klines_1h, klines_1d, ctx):
    """
    Ham kline listelerinden v4 veri sözlüğü üretir (saf — ağ yok, test edilebilir).
    klines_1h / klines_1d: Binance kline formatı [[openTime,open,high,low,close,volume,...],...]
    ctx: dış veriler -> {regime, fear_greed, btc_24h_change, btc_4h_change, btc_correlation,
                         funding_rate, oi_change_pct, unlock_pct_48h, whale_*, buy_pct,
                         vol_ratio, news_sentiment, sector_momentum, social_trend, volume_24h_usd}
    """
    o = [float(k[1]) for k in klines_1h]
    h = [float(k[2]) for k in klines_1h]
    l = [float(k[3]) for k in klines_1h]
    c = [float(k[4]) for k in klines_1h]
    dc = [float(k[4]) for k in klines_1d]

    price = c[-1]
    d = {
        "symbol": symbol,
        "price": price,
        "closes": c, "highs": h, "lows": l,
        "closes_7d": dc[-7:] if len(dc) >= 7 else dc,
        "daily_closes": dc,
        "last_candle": (o[-1], h[-1], l[-1], c[-1]),
        "rsi": calc_rsi(c),
        "atr": calc_atr(h, l, c),
        "ma50": calc_ma(c, 50),
        "bollinger": calc_bollinger(c),
        "mtf_health": mtf_trend_health(dc),
    }
    d.update(ctx or {})
    return d
