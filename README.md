# 시장 대시보드

코스피·코스닥·나스닥·S&P500 지수, 거시 지표, 보유 종목 현황, 24시간 이내 국내외 뉴스를
한 화면에서 보는 Streamlit 대시보드.

## 실행

```bash
python -m streamlit run app.py
```

`.claude/launch.json` 에 `streamlit-dashboard` 설정이 있어 preview 로도 바로 띄울 수 있다.

## 동작 방식 — 로컬 캐시 우선, 없으면 라이브 조회

`app.py`는 패널마다 **"로컬에 CSV가 있으면 그걸 읽고, 없으면 그 자리에서 직접 받아온다"**
하이브리드 방식으로 동작한다.

- **로컬(이 PC)**: 아래 Windows 작업 스케줄러가 `data/*.csv` 를 주기적으로 써두므로,
  대시보드는 디스크만 읽어 빠르다.
- **클라우드 배포본**: 스케줄러가 없고 `data/*.csv` 도 커밋하지 않으므로, 패널을 열 때마다
  그 자리에서 직접 조회한다(결과는 최대 60초 캐시).

즉 **같은 `app.py` 하나로 로컬/클라우드 둘 다 동작**한다 — 코드를 분기할 필요가 없다.

## 로컬 자동 갱신 (Windows 작업 스케줄러)

컴퓨터가 켜져 있고 로그인된 상태라면, 대시보드를 열어두지 않아도 아래 두 작업이
백그라운드에서 CSV를 계속 최신으로 유지한다.

| 작업 이름 | 스케줄 | 실행 파일 | 하는 일 |
| --- | --- | --- | --- |
| `MarketDashboardQuickRefresh` | 평일 08:50~15:40, 10분마다 | `run_quick_refresh.bat` | 지수·보유종목·거시지표·뉴스 (`collect.py --quick`) |
| `MarketDashboardFullCollect` | 평일 15:50 (장 마감 후 1회) | `run_full_collect.bat` | 전체 — 시총 상위·수급·브리핑까지 (`collect.py`) |

확인/수정은 Windows **작업 스케줄러** 앱에서 `MarketDashboard`로 검색하면 된다.
`collect.py`에는 두 작업(또는 대시보드의 갱신 버튼)이 동시에 실행돼도 CSV가 깨지지
않도록 파일 락(`data/collect.lock`)이 걸려 있다.

수동 갱신 명령:

| 명령 | 하는 일 | 걸리는 시간 |
| --- | --- | --- |
| `python collect.py` | 전체 — 지수 시세 + 시총 상위/투자자 수급 + 보유종목·지표·뉴스 + 브리핑 | 수 분 |
| `python collect.py --quick` | 지수 시세 + 보유종목·지표·뉴스 (시총/수급/브리핑 제외) | 1분 내외 |

대시보드 사이드바의 **지금 데이터 갱신** 버튼이 `--quick`을 실행한다(로컬 CSV가 있을 때만
버튼이 보인다 — 클라우드 배포본은 어차피 매번 라이브로 받아오므로 버튼이 필요 없다).

## 클라우드 배포 (핸드폰·태블릿에서 접속)

로컬 PC가 꺼져 있어도 접속하려면 **Streamlit Community Cloud**(무료)에 올린다.
아래 단계는 계정 생성·GitHub 연결처럼 개인 인증이 필요한 부분이라 직접 진행해야 한다.

> **저장소는 Public.** Streamlit Cloud의 private 저장소 연결에 알려진 버그가 있어
> ([streamlit/streamlit#13007](https://github.com/streamlit/streamlit/issues/13007) — 권한
> 문제를 "branch does not exist"로 잘못 표시), Public 저장소 + `portfolio.csv`를 저장소에서
> 빼고 Streamlit Secrets로 옮기는 방식으로 우회한다. 코드는 공개되지만 보유 종목(수량·평단가)
> 같은 개인 정보는 Secrets에만 있어 GitHub에는 절대 올라가지 않는다.

1. **GitHub 저장소 생성 (Public).** `portfolio.csv`는 `.gitignore`에 등록돼 있어 자동으로
   제외된다.
   ```bash
   git init
   git add app.py collect.py fetch_portfolio.py fetch_indicators.py fetch_news.py \
           make_briefing.py validate.py requirements.txt \
           README.md .gitignore .streamlit/secrets.toml.example \
           run_quick_refresh.bat run_full_collect.bat
   git commit -m "Initial commit"
   git remote add origin <GitHub에서 만든 저장소 URL>
   git push -u origin main
   ```
2. **share.streamlit.io** 에서 GitHub 계정으로 로그인 → New app → 방금 만든 저장소 선택,
   Main file path에 `app.py` 입력 → Deploy.
3. 배포된 앱의 **Settings → Secrets** 에 `.streamlit/secrets.toml`(로컬에만 있는, 실제 값이
   채워진 파일 — git에는 안 올라간다)의 내용을 그대로 복사해서 붙여넣는다. 형식 참고용으로
   `.streamlit/secrets.toml.example`도 저장소에 있다. **DASHBOARD_PASSWORD를 반드시 설정할
   것** — 안 하면 보유 종목·평가손익이 누구나 볼 수 있는 URL로 공개된다.
4. 배포가 끝나면 나오는 `https://xxxx.streamlit.app` 주소를 핸드폰/태블릿 브라우저에서 열면 된다.
   홈 화면에 아이콘으로 추가해두면 앱처럼 쓸 수 있다(iOS: 공유 → 홈 화면에 추가 /
   Android: 브라우저 메뉴 → 홈 화면에 추가).

**클라우드에서 안 되는 것**: `시황 브리핑` 패널은 로컬 `claude` CLI로 생성하므로 클라우드에는
표시되지 않는다(로컬에서 생성된 `data/briefing.md`를 커밋해두면 그 시점 내용이 고정 표시는
가능하나 자동 갱신은 안 된다). 나머지 패널(지수/거시/보유종목/뉴스)은 라이브 조회로 정상 동작한다.

## 파일 구성

| 파일 | 역할 |
| --- | --- |
| `app.py` | Streamlit 대시보드 (로컬 CSV 우선, 없으면 라이브 조회) |
| `collect.py` | 지수 시세 수집 + 후속 스크립트 파이프라인 실행 (로컬 전용) |
| `fetch_portfolio.py` | `portfolio.csv` 기준 보유 종목 시세·평가손익 계산 |
| `fetch_indicators.py` | ECOS(기준금리·환율) / FRED(미 10년물·달러인덱스) |
| `fetch_news.py` | 국내외 RSS 10종에서 24시간 이내 기사 수집 |
| `make_briefing.py` | 지표·뉴스 기반 시황 브리핑 생성 (로컬 claude CLI 필요) |
| `validate.py` | 수집 데이터 교차 검증 |
| `requirements.txt` | Streamlit Cloud 배포용 의존성 목록 |
| `run_quick_refresh.bat` / `run_full_collect.bat` | Windows 작업 스케줄러가 호출하는 래퍼 |
| `portfolio_to_secrets.py` | `portfolio.csv` → Streamlit Secrets용 TOML 변환, 클립보드 복사 |
| `.streamlit/secrets.toml.example` | 클라우드 Secrets 설정 템플릿(실제 값은 커밋 안 함) |

## portfolio.csv (직접 관리)

```csv
name,ticker,quantity,avg_price
아이쓰리시스템,214430,795,88486
CMA RP,CASH,12010000,1
```

- `ticker` 는 6자리 KRX 종목코드. 앞자리 0을 유지해야 한다.
- 현금성 자산은 `ticker` 를 `CASH` 로 두고 `quantity` 에 원화 금액, `avg_price` 에 1을 넣는다.
- **무상증자·액면분할이 있으면 `quantity` 와 `avg_price` 를 직접 조정해야 한다.**
  현재가는 권리락 후 가격이라 조정하지 않으면 수익률이 실제보다 나쁘게 나온다.
  (예: 티앤엘 2026-08-05 무상증자 1:1 → 80주/66,100원을 160주/33,050원으로 정정)
- **`.gitignore`에 등록돼 있어 커밋되지 않는다.** 저장소가 Public이라 개인정보(수량·평단가)를
  코드와 분리해뒀다. 클라우드 배포본은 대신 Streamlit Secrets의 `[[portfolio]]` 배열을 쓴다
  (`.streamlit/secrets.toml.example` 참고). 종목이 바뀌면 로컬 `portfolio.csv`와 클라우드
  Secrets 양쪽을 똑같이 수정해야 한다 — 자동 동기화되지 않는다.

### 보유 종목이 바뀌었을 때

1. `portfolio.csv` 를 직접 수정한다(종목 추가/삭제, 수량·평단가 변경).
2. 아래 스크립트를 실행하면 `.streamlit/secrets.toml` 에 저장된 API 키·비밀번호는 그대로 두고
   `[[portfolio]]` 부분만 `portfolio.csv` 최신 내용으로 다시 만들어 **클립보드에 복사**해준다.
   ```bash
   python portfolio_to_secrets.py
   ```
3. Streamlit Cloud 앱의 **Manage app → Settings → Secrets** 편집창에 그대로 붙여넣고 **Save**.
   앱이 자동 재시작되며 반영된다.

## 문제 해결

**`Python was not found; run without arguments to install from the Microsoft Store...`**

`C:\Users\mykim\AppData\Local\Microsoft\WindowsApps\python.exe` 는 0바이트짜리 Microsoft Store
유도용 스텁이다. 이게 PATH 앞쪽에 있으면 진짜 Python보다 먼저 잡힌다.
사용자 PATH에서 `C:\Users\mykim\AppData\Local\Python\bin` 을 WindowsApps 앞으로 옮기면 해결된다
(2026-08-17 적용 완료). 확인:

```bash
python -c "import sys; print(sys.executable)"
```

**`HTTP Error 429: Too Many Requests` / KRX 응답이 몇 분씩 안 옴**

`fdr.StockListing('KRX')`, `fdr.DataReader('KS11', ...)` 등은 짧은 간격으로 반복 호출하면
막히거나(429), 응답 자체가 멎어버리는(무한 대기) 경우가 있다(2026-08-17에 실제로 겪음 — 아래 참고).
대응:
- `fetch_portfolio.py`는 KRX 목록 조회 3회 재시도 후 종목별 개별 조회로 자동 전환한다.
  이 경로에서는 시장 구분(KOSPI/KOSDAQ)이 `-`로 표시된다.
- 모든 네트워크 호출에 소켓 기본 타임아웃(30초)을 걸어뒀다. 다만 FDR 라이브러리가 내부에서
  자체 재시도를 하는 경우 개별 소켓 타임아웃을 우회해 수 분씩 걸릴 수 있다(실측 2분 44초) —
  완전한 상한선은 아니고 "영원히 안 끝나는 것"만 막아주는 정도로 이해할 것.
- `collect.py`는 새로 받은 행 수가 기존 CSV보다 90% 미만이면 **덮어쓰지 않고 건너뛴다**
  (아래 사고 이후 추가한 안전장치 — 이게 실질적인 방어선이다. 실제로 이 시나리오를
  재현해 정상 동작을 확인했다).
- 갱신을 연달아 누르지 말 것. 스케줄러와 버튼이 동시에 돌아도 파일 락으로 막힌다.

> **사고 기록 (2026-08-17)**: KRX 접속이 막힌 상태에서 `collect.py`가 코스피/코스닥에 대해
> 빈 응답(0행)을 받고도 그대로 기존 CSV(6,320행, 2001~2026년 히스토리)를 덮어써 데이터가
> 날아갔다. `collect.py`는 매번 2001-01-01부터 전체 기간을 다시 받는 구조라 KRX 접속이
> 복구되면 다음 실행에서 전체 히스토리가 자동으로 다시 채워진다 — 별도 백업 복구는
> 필요 없었지만, 재발 방지로 위 "90% 미만이면 건너뛴다" 안전장치를 추가했다.

## 데이터 출처

- 지수·환율·보유종목 시세: FinanceDataReader (KRX/야후)
- 거시 지표: 한국은행 ECOS, 세인트루이스 연은 FRED
  (로컬은 `.env`, 클라우드는 Streamlit Secrets에 API 키 필요)
- 뉴스: 연합뉴스·한국경제·매일경제 / Yahoo Finance·MarketWatch·CNBC·FT·Investing.com RSS
