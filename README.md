# BTC Regime Monitor (₿ 레짐 모니터)

비트코인 시장이 **횡보장인지 추세장인지** 자동 판별해서 텔레그램으로 알려주는 모니터.
그리드매매 가동/중단 판단 보조용. 완전 무료 (Binance 공개 API + GitHub Actions + Telegram).

## 판단 로직
| 지표 | 기준 |
|---|---|
| ADX(14) | < 22 → 횡보(RANGING) / > 28 → 추세(TRENDING) |
| 볼린저밴드 폭 백분위 | 최근 120일 내 80% 초과 시 변동성 확장 판정 보조 |
| 200일 이평 기울기 | 추세 방향(상승/하락) 구분 |
| Fear & Greed Index | 참고 지표 (alternative.me) |

레짐이 **전환될 때** 메시지에 ⚠️ 전환 알림 표시.

## 설정 방법 (5분)

### 1. 텔레그램 봇 준비 (기존 인도 규제 모니터 봇 재사용 가능)
- @BotFather → `/newbot` → 토큰 발급
- 봇에게 아무 메시지 전송 후 `https://api.telegram.org/bot<토큰>/getUpdates` 에서 chat_id 확인

### 2. GitHub 저장소 생성 & 파일 업로드
```bash
git init && git add . && git commit -m "init"
git remote add origin https://github.com/<계정>/btc-regime-monitor.git
git push -u origin main
```

### 3. Secrets 등록
저장소 → Settings → Secrets and variables → Actions:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### 4. 실행 확인
Actions 탭 → "BTC Regime Monitor" → Run workflow (수동 실행) → 텔레그램 수신 확인

## 스케줄
기본: 하루 2회 (KST 오전 9시, 오후 9시). `.github/workflows/monitor.yml`의 cron 수정으로 변경 가능.

## 기준값 튜닝
`monitor.py` 상단:
```python
ADX_RANGING_MAX = 22    # 낮출수록 횡보 판정 엄격
ADX_TRENDING_MIN = 28   # 낮출수록 추세 판정 민감
```

## 알림 정책 변경
현재는 매 실행마다 상태 리포트 발송. 전환 시에만 받고 싶으면 `monitor.py`의 `main()`에서:
```python
if regime_changed:
    send_telegram(msg)
```
로 조건 걸면 됨.
