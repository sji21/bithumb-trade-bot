# trade_bot

일봉 3신호로 목표 비중을 계산해 텔레그램으로 알리는 개인용 봇.
신호는 바이낸스(USDT) 캔들로 계산하고, 주문 경로는 빗썸 Open API 2.0(KRW)으로 낸다.

> **현재 알림 전용으로 동작한다.** 판단이 연 6~14회뿐이라 자동매매가 필요하지 않아
> 목표 비중만 전송하고 주문은 수동으로 집행한다.

## 문서

| | |
|---|---|
| **[전략 명세](docs/strategy.md)** | 지금 무엇을 하는지 — 신호·배분·운영·파라미터 |
| **[검증 기록](docs/research-log.md)** | 왜 그렇게 됐는지 — 400개 조합 검증 결과와 인사이트 |

## 전략 요약

```
점수 = 0.45×(MA20 > MA60) + 0.35×(슈퍼트렌드 10,3) + 0.20×(MA50 > MA200)

점수 < 0.5  →  비중 0%
점수 ≥ 0.5  →  비중 = 점수 (55 ~ 100%)
```

BTC 70% / ETH 30% 배분, SOL 은 차트만 관찰. 리밸런싱 밴드 10%p.

**수익을 늘리는 전략이 아니라 낙폭을 줄이는 전략이다.**
6년 기준 541% 대 단순보유 430%, 최대 낙폭은 −77% → −40%.
재현: `python3 backtest.py --portfolio`

## 구조

| 모듈 | 책임 |
|---|---|
| `config.py` | .env 로딩, 경로, 로깅, 실행 모드, 전략 파라미터 |
| `market.py` | 바이낸스 OHLCV 조회, 미완성 캔들 제거 |
| `strategy.py` | EMA · RSI · ATR · 슈퍼트렌드, 일봉 신호 판정 |
| `bithumb.py` | 빗썸 API 2.0 클라이언트 (JWT 인증 · 시세 · 잔고 · 주문) |
| `state.py` | 재시작을 견디는 중복 전송 · 중복 주문 방지 |
| `charting.py` | 4시간봉 차트, 일봉 5단 리포트 차트 |
| `report.py` | 텔레그램 리포트 문구 |
| `notify.py` | 텔레그램 전송 |
| `bot.py` | 메인 루프 |
| `backtest.py` | 과거 데이터로 전략 검증 |
| `set_keys.py` | API 키 교체 + 즉시 검증 |

## 설정

```bash
pip3 install -r requirements.txt
cp .env.example .env   # 값을 채운다
```

실행 모드는 두 플래그로 결정된다.

| `ENABLE_LIVE_TRADING` | `ENABLE_LIVE_SIMULATION` | 동작 |
|---|---|---|
| `false` | — | **현재 설정.** 알림만. 주문을 만들지 않음 |
| `true` | `true` | 라이브 경로를 그대로 밟되 mock 영수증만 기록 |
| `true` | `false` | **실주문** |

## 실행

```bash
python3 bot.py
```

macOS 에서는 launchd 로 상시 실행한다 (`~/Library/LaunchAgents/ai.tradebot.bot.plist`).

```bash
launchctl kickstart -k gui/$(id -u)/ai.tradebot.bot   # 재시작
```

## 알림

| 주기 | 내용 |
|---|---|
| 매일 09:01 KST | **일봉 리포트.** 종목별 5단 차트 + 점수 · 목표 비중 · 리밸런싱 금액 |
| 4시간마다 | 시세 확인용. **매매 판단에 쓰지 않는다** |

## 테스트

```bash
python3 -m unittest discover -s tests
```

표준 `unittest` 라 추가 의존성이 없고 네트워크도 쓰지 않는다. 31개 전부
실제로 터졌던 버그에서 나왔다 — RSI 가 손실 0 구간에서 `pd.NA` 를 반환해
리포트가 죽은 것, 워밍업 부족이 조용히 신호 꺼짐으로 섞인 것, 리밸런싱
밴드가 구현되지 않았던 것. 새 버그를 찾는 게 아니라 고친 것이 다시
무너지지 않게 하는 용도다.

## 백테스트

```bash
python3 backtest.py --portfolio     # 채택 전략, docs 표를 재현한다
python3 backtest.py                 # 종목별
python3 backtest.py --legacy        # 폐기된 4시간봉 EMA 크로스
```

`--portfolio` 출력은 [strategy.md](docs/strategy.md) 의 성과 표와 일치해야
한다. 한동안 재현 수단이 없어 SOL 이 섞인 숫자가 문서에 실려 있었다
([경위](docs/research-log.md#문서-숫자-정정)).

봇이 실제로 쓰는 `strategy` 모듈을 그대로 불러 쓴다. 신호 로직을 따로 구현하면
실매매와 어긋난 결과를 보게 되므로 의도적으로 재사용한다.

체결가를 캔들 종가로 가정하고 환율 변동을 무시하므로,
실제 성적은 백테스트 숫자보다 나쁠 가능성이 높다.

## 빗썸 API 2.0 메모

구 1.0(`/public`, `/info`, HMAC-SHA512 + `Api-Sign`)은 종료 예정이라 쓰지 않는다.

- 인증: JWT(HS256). 쿼리가 있으면 `query_hash`(SHA512) 포함
- 마켓 코드: `KRW-BTC` 형식. `BTC-KRW` 는 **존재하지 않는 마켓**이라 400 으로 거부된다
- 시장가 주문 규격이 방향에 따라 다르다
  - 매수 `ord_type='price'` + `price`(총 KRW)
  - 매도 `ord_type='market'` + `volume`(코인 수량)
- **IP 화이트리스트 필수.** 미등록 IP 는 `403 NotAllowIP`. 봇은 이 상태를 감지하면
  현재 공인 IP 를 담아 텔레그램으로 알린다 (조용히 실패해 몇 달간 방치된 적이 있다)

## 주의

가상자산 자동매매는 원금 손실 위험이 있다. API 키에 **출금 권한을 부여하지 말 것**.
