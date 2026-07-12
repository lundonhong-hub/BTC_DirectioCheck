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

COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
UPBIT_CANDLES_URL = "https://api.upbit.com/v1/candles/days"
FNG_URL = "https://api.alternative.me/fng/?limit=2"
STATE_FILE = "last_state.json"  # 레짐 상태 저장 (전환 감지용)

# ── 판단 기준 (필요시 조정) ──────────────────────────────
ADX_RANGING_MAX = 22    # ADX가 이 값 미만이면 횡보
ADX_TRENDING_MIN = 28   # ADX가 이 값 초과면 추세
BBW_PCTL_WINDOW = 120   # 볼린저밴드 폭 백분위 계산 기간(일)
# ────────────────────────────────────────────────────────


def fetch_btc_daily(limit=300):
    """BTC 일봉 수집. 1순위 Coinbase(USD), 실패 시 업비트(KRW) 백업.
    (바이낸스는 GitHub Actions의 미국 IP를 451로 차단하므로 사용 불가)"""
    try:
        return _fetch_coinbase_daily(limit)
    except Exception as e:
        print(f"[WARN] Coinbase 실패({e}) -> 업비트로 재시도")
        return _fetch_upbit_daily(min(limit, 200))


def _fetch_coinbase_daily(limit=300):
    """Coinbase Exchange 공개 API (무료, 키 불필요, 최대 300개)"""
    params = {"granularity": 86400}  # 1일봉
    r = requests.get(COINBASE_CANDLES_URL, params=params, timeout=30,
                     headers={"User-Agent": "btc-regime-monitor"})
    r.raise_for_status()
    # 응답: [[time, low, high, open, close, volume], ...] 최신순
    rows = r.json()[:limit]
    df = pd.DataFrame(rows, columns=["ts", "low", "high", "open", "close", "volume"])
    df["date"] = pd.to_datetime(df["ts"], unit="s")
    df = df.sort_values("date").reset_index(drop=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df.attrs["currency"] = "USD"
    return df[["date", "open", "high", "low", "close", "volume"]]


def _fetch_upbit_daily(limit=200):
    """업비트 공개 API 백업 (무료, 키 불필요, 최대 200개, KRW 표시)"""
    params = {"market": "KRW-BTC", "count": limit}
    r = requests.get(UPBIT_CANDLES_URL, params=params, timeout=30)
    r.raise_for_status()
    rows = r.json()  # 최신순
    df = pd.DataFrame([{
        "date": row["candle_date_time_utc"],
        "open": row["opening_price"],
        "high": row["high_price"],
        "low": row["low_price"],
        "close": row["trade_price"],
        "volume": row["candle_acc_trade_volume"],
    } for row in rows])
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df.attrs["currency"] = "KRW"
    return df


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


def html_escape(text):
    """텔레그램 HTML parse_mode 안전 처리"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# def generate_ai_commentary(indicators: dict):
#     """Claude Haiku로 초보자용 해설 생성. 실패 시 None 반환 (기본 리포트는 정상 발송)"""
#     api_key = os.environ.get("ANTHROPIC_API_KEY")
#     if not api_key:
#         print("[INFO] ANTHROPIC_API_KEY 미설정 -> AI 해설 생략")
#         return None
 
#     prompt = f"""다음은 비트코인 시장 레짐 모니터의 오늘 지표입니다:

# - 현재가: {indicators['price_str']}
# - 레짐 판정: {indicators['regime_kr']}
# - ADX(14): {indicators['adx']:.1f} (22 미만이면 횡보, 28 초과면 추세)
# - +DI: {indicators['plus_di']:.1f} / -DI: {indicators['minus_di']:.1f} (상승 힘 vs 하락 힘)
# - 볼린저밴드 폭 백분위: {indicators['bbw_pctl']*100:.0f}% (낮으면 변동성 수축, 높으면 확장)
# - 장기 이동평균선 기울기(10일): {indicators['ma_slope']:+.2f}%
# - Fear & Greed Index: {indicators['fng']}

# 이 지표들을 종합해서 투자 초보자도 이해할 수 있게 한국어로 해설해주세요.
# 규칙:
# - 첫 문장은 오늘 시장을 한 줄로 요약
# - 이어서 3~5문장으로 핵심 지표들이 무엇을 의미하는지 쉽게 풀어서 설명
# - 그리드매매(횡보장 전략) 관점에서 지금이 어떤 국면인지 한 문장 코멘트
# - 매수/매도 추천은 하지 말 것. 해석만 제공
# - 전체 250자 이내, 마크다운/HTML 태그 없이 순수 텍스트로만"""

def generate_ai_commentary(indicators: dict):
    """Claude Haiku로 초보자용 해설 생성. 실패 시 None 반환 (기본 리포트는 정상 발송)"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[INFO] ANTHROPIC_API_KEY 미설정 -> AI 해설 생략")
        return None

    # 들여쓰기 에러 방지를 위해 왼쪽 공백을 완벽하게 맞춘 프롬프트
    prompt = (
        f"다음은 비트코인 시장 레짐 모니터의 오늘 지표입니다:\n"
        f"- 현재가: {indicators['price_str']}\n"
        f"- 레짐 판정: {indicators['regime_kr']}\n"
        f"- ADX(14): {indicators['adx']:.1f} (22 미만 횡보, 28 초과 추세)\n"
        f"- +DI: {indicators['plus_di']:.1f} / -DI: {indicators['minus_di']:.1f}\n"
        f"- 볼린저밴드 폭 백분위: {indicators['bbw_pctl']*100:.0f}%\n"
        f"- 장기 이동평균선 기울기(10일): {indicators['ma_slope']:+.2f}%\n"
        f"- Fear & Greed Index: {indicators['fng']}\n\n"
        f"위 지표를 종합하여 트레이더가 한눈에 읽기 편한 '두괄식 요약 리포트'를 작성해주세요.\n\n"
        f"[출력 규칙]\n"
        f"1. 반드시 아래의 [출력 양식] 형식을 그대로 유지하세요.\n"
        f"2. 키워드만 툭툭 던지는 단답형 대신, '~함', '~임', '~ 추천'으로 끝나는 명확하고 완성된 문장으로 작성하세요.\n"
        f"3. 매수/매도 추천은 절대 금지하며, 마크다운이나 HTML 태그 없이 순수 텍스트로만 작성하세요.\n"
        f"4. 각 항목은 1~2문장 이내로 명확하게 인과관계를 설명해야 합니다.\n\n"
        f"[출력 양식]\n"
        f"📢 시장 요약: [오늘의 시장 핵심 상황을 두괄식으로 명확하게 한 문장 요약]\n"
        f"🔍 지표 분석: [주요 수치(ADX, DI, 밴드폭 등)가 현재 어떤 상태를 뜻하는지 쉽게 설명]\n"
        f"📈 추세/심리: [장기 이평선 기울기와 공포탐욕지수를 바탕으로 한 추세 및 투자 심리 진단]\n"
        f"🤖 그리드 전략: [현재 레짐 국면에서 그리드매매/수동매매 시 주의하거나 취해야 할 스탠스]"
    )

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", [])
                       if b.get("type") == "text").strip()
        return html_escape(text) if text else None
    except Exception as e:
        print(f"[WARN] AI 해설 생성 실패({e}) -> 해설 없이 발송")
        return None

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

    # 장기 이평: 데이터 여유 있으면 200일, 부족하면(업비트 백업 등) 120일로 자동 축소
    ma_window = 200 if len(df) >= 220 else 120
    ma200 = df["close"].rolling(ma_window).mean()
    # 장기 이평 최근 10일 기울기 (%)
    ma200_slope_pct = (ma200.iloc[-1] / ma200.iloc[-11] - 1) * 100

    fng_value, fng_label = fetch_fear_greed()

    currency = df.attrs.get("currency", "USD")
    price_str = f"${price_now:,.0f}" if currency == "USD" else f"₩{price_now:,.0f}"

    regime = classify_regime(adx_now, bbw_pctl, ma200_slope_pct)
    last_regime = load_last_state()
    regime_changed = (last_regime is not None and last_regime != regime)

    lines = [
        f"<b>₿ BTC 레짐 모니터</b>  ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC)",
        "",
        f"현재가: {price_str}",
        f"레짐: <b>{REGIME_KR[regime]}</b>",
    ]
    if regime_changed:
        lines.insert(2, f"⚠️ <b>레짐 전환 감지!</b> {REGIME_KR.get(last_regime, last_regime)} → {REGIME_KR[regime]}")
        lines.insert(3, "")
    lines += [
        "",
        f"· ADX(14): {adx_now:.1f}  (횡보 {ADX_RANGING_MAX} 미만 / 추세 {ADX_TRENDING_MIN} 초과)",
        f"· +DI/-DI: {plus_di.iloc[-1]:.1f} / {minus_di.iloc[-1]:.1f}",
        f"· BB밴드폭 백분위: {bbw_pctl*100:.0f}% (높을수록 변동성 확장)",
        f"· {ma_window}일선 기울기(10일): {ma200_slope_pct:+.2f}%",
    ]
    if fng_value is not None:
        lines.append(f"· Fear & Greed: {fng_value} ({fng_label})")

    # ── 알림 정책 ──────────────────────────────────────
    # 1) 레짐 전환 감지 시: 즉시 발송
    # 2) 정기 리포트: KST 09시(UTC 00시) 실행분만 발송
    # 그 외 시간대: 발송 안 함 (체크만 하고 조용히 종료, AI 호출도 없음)
    now_utc = datetime.now(timezone.utc)
    # is_daily_report_hour = (now_utc.hour == 0)  # UTC 00시 = KST 09시
    is_daily_report_hour = True

    if regime_changed or is_daily_report_hour:
        # 발송이 확정된 경우에만 AI 해설 생성 (Haiku 헛호출 방지)
        ai_comment = generate_ai_commentary({
            "price_str": price_str,
            "regime_kr": REGIME_KR[regime],
            "adx": adx_now,
            "plus_di": plus_di.iloc[-1],
            "minus_di": minus_di.iloc[-1],
            "bbw_pctl": bbw_pctl,
            "ma_slope": ma200_slope_pct,
            "fng": f"{fng_value} ({fng_label})" if fng_value is not None else "N/A",
        })
        if ai_comment:
            lines += ["", "💬 <b>AI 해설</b>", ai_comment]

        msg = "\n".join(lines)

        if regime_changed:
            send_telegram(msg)
            print("sent: regime changed ->", regime)
        else:
            send_telegram("📋 [일일 정기 리포트]\n\n" + msg)
            print("sent: daily report ->", regime)
    else:
        print("no alert (unchanged):", regime)

    save_state(regime)


if __name__ == "__main__":
    main()
