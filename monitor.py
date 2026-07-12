"""
BTC Regime Monitor
- Binance 공개 API로 BTC/USDT 일봉 수집 (인증 불필요)
- ADX(14), 볼린저밴드 폭, 200일 이평 기울기, Fear & Greed Index 계산
- 시장 레짐(횡보 vs 추세) 판단 후 텔레그램 알림
- GitHub Actions에서 주기 실행

환경변수:
  TELEGRAM_BOT_TOKEN : 텔레그램 봇 토큰
  TELEGRAM_CHAT_ID   : 수신 chat id
"""

import os
import json
import requests
import pandas as pd
from datetime import datetime, timezone

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
FNG_URL = "https://api.alternative.me/fng/?limit=2"
STATE_FILE = "last_state.json"  # 레짐 상태 저장 (전환 감지용)

# ── 판단 기준 (필요시 조정) ──────────────────────────────
ADX_RANGING_MAX = 22    # ADX가 이 값 미만이면 횡보
ADX_TRENDING_MIN = 28   # ADX가 이 값 초과면 추세
BBW_PCTL_WINDOW = 120   # 볼린저밴드 폭 백분위 계산 기간(일)
# ────────────────────────────────────────────────────────


def fetch_btc_daily(limit=300):
    """Binance에서 BTC/USDT 일봉 데이터 수집 (무료, 키 불필요)"""
    params = {"symbol": "BTCUSDT", "interval": "1d", "limit": limit}
    r = requests.get(BINANCE_KLINES_URL, params=params, timeout=30)
    r.raise_for_status()
    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "trades", "tbbav", "tbqav", "ignore"]
    df = pd.DataFrame(r.json(), columns=cols)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["date"] = pd.to_datetime(df["open_time"], unit="ms")
    return df[["date", "open", "high", "low", "close", "volume"]]


def compute_adx(df, period=14):
    """ADX 직접 계산 (Wilder smoothing)"""
    h, l, c = df["high"], df["low"], df["close"]
    plus_dm = (h.diff()).clip(lower=0)
    minus_dm = (-l.diff()).clip(lower=0)
    # +DM / -DM 중 큰 쪽만 유효
    plus_dm[plus_dm < minus_dm] = 0.0
    minus_dm[minus_dm <= plus_dm] = 0.0

    tr = pd.concat([
        h - l,
        (h - c.shift()).abs(),
        (l - c.shift()).abs()
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx, plus_di, minus_di


def compute_bb_width(df, period=20, k=2):
    """볼린저밴드 폭 (상단-하단)/중심선"""
    mid = df["close"].rolling(period).mean()
    std = df["close"].rolling(period).std()
    upper = mid + k * std
    lower = mid - k * std
    return (upper - lower) / mid


def fetch_fear_greed():
    """Fear & Greed Index (alternative.me, 무료)"""
    try:
        r = requests.get(FNG_URL, timeout=15)
        r.raise_for_status()
        data = r.json()["data"]
        today = data[0]
        return int(today["value"]), today["value_classification"]
    except Exception:
        return None, None


def classify_regime(adx_now, bbw_pctl, ma200_slope_pct):
    """레짐 판단: RANGING(횡보) / TRENDING_UP / TRENDING_DOWN / TRANSITION"""
    if adx_now < ADX_RANGING_MAX:
        return "RANGING"
    if adx_now > ADX_TRENDING_MIN:
        return "TRENDING_UP" if ma200_slope_pct > 0 else "TRENDING_DOWN"
    # 중간 구간: 밴드폭 확장 여부로 보조 판단
    if bbw_pctl > 0.8:
        return "TRENDING_UP" if ma200_slope_pct > 0 else "TRENDING_DOWN"
    return "TRANSITION"


REGIME_KR = {
    "RANGING": "🟢 횡보장 (그리드 적합)",
    "TRENDING_UP": "📈 상승 추세 (그리드 중단 고려, 보유 유리)",
    "TRENDING_DOWN": "📉 하락 추세 (그리드 위험, 신규매수 중단 고려)",
    "TRANSITION": "🟡 전환 구간 (관망)",
}


def load_last_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f).get("regime")
    return None


def save_state(regime):
    with open(STATE_FILE, "w") as f:
        json.dump({"regime": regime,
                   "updated": datetime.now(timezone.utc).isoformat()}, f)


def send_telegram(msg):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[WARN] 텔레그램 환경변수 미설정. 메시지 출력만 합니다.\n")
        print(msg)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": msg,
                                 "parse_mode": "HTML"}, timeout=15)
    r.raise_for_status()


def main():
    df = fetch_btc_daily()
    price_now = df["close"].iloc[-1]

    adx, plus_di, minus_di = compute_adx(df)
    adx_now = adx.iloc[-1]

    bbw = compute_bb_width(df)
    bbw_now = bbw.iloc[-1]
    bbw_recent = bbw.tail(BBW_PCTL_WINDOW).dropna()
    bbw_pctl = (bbw_recent < bbw_now).mean()  # 최근 구간 내 백분위

    ma200 = df["close"].rolling(200).mean()
    # 200일선 최근 10일 기울기 (%)
    ma200_slope_pct = (ma200.iloc[-1] / ma200.iloc[-11] - 1) * 100

    fng_value, fng_label = fetch_fear_greed()

    regime = classify_regime(adx_now, bbw_pctl, ma200_slope_pct)
    last_regime = load_last_state()
    regime_changed = (last_regime is not None and last_regime != regime)

    lines = [
        f"<b>₿ BTC 레짐 모니터</b>  ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC)",
        "",
        f"현재가: ${price_now:,.0f}",
        f"레짐: <b>{REGIME_KR[regime]}</b>",
    ]
    if regime_changed:
        lines.insert(2, f"⚠️ <b>레짐 전환 감지!</b> {REGIME_KR.get(last_regime, last_regime)} → {REGIME_KR[regime]}")
        lines.insert(3, "")
    lines += [
        "",
        f"· ADX(14): {adx_now:.1f}  (횡보<{ADX_RANGING_MAX} / 추세>{ADX_TRENDING_MIN})",
        f"· +DI/-DI: {plus_di.iloc[-1]:.1f} / {minus_di.iloc[-1]:.1f}",
        f"· BB밴드폭 백분위: {bbw_pctl*100:.0f}% (높을수록 변동성 확장)",
        f"· 200일선 기울기(10일): {ma200_slope_pct:+.2f}%",
    ]
    if fng_value is not None:
        lines.append(f"· Fear & Greed: {fng_value} ({fng_label})")

    msg = "\n".join(lines)

    # ── 알림 정책 ──────────────────────────────────────
    # 1) 레짐 전환 감지 시: 즉시 발송
    # 2) 정기 리포트: KST 09시(UTC 00시) 실행분만 발송
    # 그 외 시간대: 발송 안 함 (체크만 하고 조용히 종료)
    now_utc = datetime.now(timezone.utc)
    is_daily_report_hour = (now_utc.hour == 0)  # UTC 00시 = KST 09시

    if regime_changed:
        send_telegram(msg)
        print("sent: regime changed ->", regime)
    elif is_daily_report_hour:
        send_telegram("📋 [일일 정기 리포트]\n\n" + msg)
        print("sent: daily report ->", regime)
    else:
        print("no alert (unchanged):", regime)

    save_state(regime)


if __name__ == "__main__":
    main()
