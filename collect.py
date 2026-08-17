# collect.py — 시세 수집 스크립트 골격
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import FinanceDataReader as fdr
from pykrx import stock

# fdr/pykrx 내부의 urllib 호출 상당수가 자체 타임아웃을 안 걸어서, KRX 쪽이 응답을
# 멈추면 프로세스가 무한 대기한다(2026-08-17 fetch_portfolio.py가 180초 넘게 걸린 원인).
# 소켓 기본 타임아웃을 걸어 무한 대기 대신 명확한 에러로 끝나게 한다.
socket.setdefaulttimeout(30)

SYMBOLS = {
    'kospi':  'KS11',     # 코스피
    'kosdaq': 'KQ11',     # 코스닥
    'nasdaq': 'IXIC',     # 나스닥 종합
    'sp500':  'US500',    # S&P500
    'usdkrw': 'USD/KRW',  # 원달러 환율
}
START = '2001-01-01'
DATA_DIR = 'data'

# 장중 스케줄러(10분마다)와 대시보드의 '지금 데이터 갱신' 버튼이 겹쳐 눌리면
# 같은 CSV에 동시에 쓰다가 깨질 수 있어 파일 락으로 막는다.
LOCK_PATH = os.path.join(DATA_DIR, 'collect.lock')
LOCK_STALE_SEC = 600  # 정상 실행은 수 분 내 끝나므로, 이보다 오래된 락은 죽은 프로세스로 간주해 무시한다.


@contextmanager
def collect_lock():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(LOCK_PATH) and time.time() - os.path.getmtime(LOCK_PATH) < LOCK_STALE_SEC:
        print(f"[SKIP] 다른 수집 작업이 이미 실행 중입니다 ({LOCK_PATH}).")
        sys.exit(0)

    with open(LOCK_PATH, 'w') as f:
        f.write(str(os.getpid()))
    try:
        yield
    finally:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)

# 새로 받은 행 수가 기존 파일의 이 비율보다 적으면 소스 쪽 일시 장애(빈 응답 등)로 보고
# 덮어쓰지 않는다. 2026-08-17에 KRX 접속이 막히며 kospi/kosdaq 6,320행이 0행으로
# 덮어써져 6년치 히스토리가 날아간 사고가 있었다.
MIN_ROW_RATIO = 0.9


def collect():
    os.makedirs(DATA_DIR, exist_ok=True)   # data 폴더 없으면 생성
    for name, symbol in SYMBOLS.items():
        path = os.path.join(DATA_DIR, f"{name}.csv")
        try:
            df = fdr.DataReader(symbol, START)

            if os.path.exists(path):
                existing_rows = sum(1 for _ in open(path, encoding='utf-8-sig')) - 1  # 헤더 제외
                if len(df) < existing_rows * MIN_ROW_RATIO:
                    print(f"[SKIP] {name}: 새로 받은 {len(df)}행이 기존 {existing_rows}행보다 크게 적어 "
                          f"소스 장애로 보고 덮어쓰지 않습니다.")
                    continue

            df['fetched_at'] = datetime.now().isoformat()  # 조회 시각 기록
            df.to_csv(path, encoding='utf-8-sig')
            print(f"[OK] {name}: {len(df)}행 -> {path}")
        except Exception as e:
            print(f"[FAIL] {name}: {e}")


def find_latest_business_day(max_back_days=30):
    """오늘부터 하루씩 거슬러 올라가며 시가총액 데이터가 있는 최근 거래일을 찾는다."""
    day = datetime.now()
    for _ in range(max_back_days):
        date_str = day.strftime('%Y%m%d')
        df = stock.get_market_cap_by_ticker(date_str)
        time.sleep(1)
        if not df.empty and (df['시가총액'] > 0).any():
            return date_str, df
        day -= timedelta(days=1)
    raise RuntimeError("최근 거래일을 찾지 못했습니다.")


def collect_marketcap_top10():
    try:
        date_str, cap_df = find_latest_business_day()
        top10 = cap_df.sort_values('시가총액', ascending=False).head(10).copy()

        names = []
        for ticker in top10.index:
            names.append(stock.get_market_ticker_name(ticker))
            time.sleep(1)
        top10.insert(0, '종목명', names)
        top10.insert(0, '기준일', date_str)

        path = os.path.join(DATA_DIR, 'marketcap_top10.csv')
        top10.to_csv(path, encoding='utf-8-sig')
        print(f"[OK] marketcap_top10: {len(top10)}행 (기준일 {date_str}) -> {path}")
    except Exception as e:
        print(f"[FAIL] marketcap_top10: {e}")


def collect_investor_flow():
    try:
        latest_str, _ = find_latest_business_day()
        latest_date = datetime.strptime(latest_str, '%Y%m%d')
        from_str = (latest_date - timedelta(days=40)).strftime('%Y%m%d')

        df = stock.get_market_trading_value_by_date(from_str, latest_str, 'KOSPI')
        time.sleep(1)
        df = df.tail(20)

        path = os.path.join(DATA_DIR, 'investor_flow.csv')
        df.to_csv(path, encoding='utf-8-sig')
        print(f"[OK] investor_flow: {len(df)}행 (마지막 날짜 {df.index[-1].date()}) -> {path}")
    except Exception as e:
        print(f"[FAIL] investor_flow: {e}")


STATUS_JSON = os.path.join(DATA_DIR, 'status.json')
PIPELINE_TIMEOUT_SEC = 180

# collect() 이후 순서대로 실행할 후속 스크립트들 (task 이름, 파일명, quick 모드 포함 여부)
# make_briefing 은 claude 호출이라 느려서 quick 모드에서는 건너뛴다.
PIPELINE_STEPS = [
    ('fetch_portfolio', 'fetch_portfolio.py', True),
    ('fetch_indicators', 'fetch_indicators.py', True),
    ('fetch_news', 'fetch_news.py', True),
    ('make_briefing', 'make_briefing.py', False),
]


def load_status():
    if os.path.exists(STATUS_JSON):
        with open(STATUS_JSON, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_status(status):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATUS_JSON, 'w', encoding='utf-8') as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def run_pipeline(quick=False):
    """후속 스크립트들을 순서대로 실행하고 각 단계의 성공 여부와 시각을
    status.json 의 tasks 구획에 기록한다. 하나가 실패해도 다음 단계는 계속 진행한다.
    quick=True 면 오래 걸리는 단계는 건너뛴다."""
    status = load_status()  # checks 등 기존 구획은 그대로 보존
    tasks = status.get('tasks', {})

    steps = [(n, s) for n, s, in_quick in PIPELINE_STEPS if in_quick or not quick]
    for name, script in steps:
        ran_at = datetime.now(timezone.utc).isoformat()
        try:
            child_env = dict(os.environ, PYTHONIOENCODING='utf-8')  # 콘솔 기본 인코딩(cp949)이 아닌 UTF-8로 출력하도록 강제
            result = subprocess.run(
                [sys.executable, script],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                env=child_env,
                timeout=PIPELINE_TIMEOUT_SEC,
            )
            ok = result.returncode == 0
            error = None if ok else (result.stderr.strip() or result.stdout.strip())[:500]
        except subprocess.TimeoutExpired:
            ok = False
            error = f"{PIPELINE_TIMEOUT_SEC}초 초과"
        except Exception as e:
            ok = False
            error = str(e)

        tasks[name] = {'ok': ok, 'ran_at': ran_at}
        if not ok:
            tasks[name]['error'] = error

        print(f"[{'OK' if ok else 'FAIL'}] {name}" + ('' if ok else f": {error}"))

    status['tasks'] = tasks
    save_status(status)
    print(f"-> {STATUS_JSON} 의 tasks 갱신 완료")


if __name__ == '__main__':
    # --quick: 대시보드의 '지금 데이터 갱신' 버튼용. 시총 상위/투자자 수급은
    # 종목마다 1초씩 쉬며 조회해 수 분이 걸리므로 제외한다.
    quick = '--quick' in sys.argv

    with collect_lock():
        collect()
        if not quick:
            collect_marketcap_top10()
            collect_investor_flow()
        run_pipeline(quick=quick)
