"""종목추천 탭 데이터 수집 — 코스피·코스닥 전 종목의 일별 OHLCV를 pykrx 벌크
API(하루치 전종목 한 번에)로 받아 recommendation_signals.py의 4개 전략을 평가하고
data/recommendations.json에 저장한다.

무거운 배치라 collect.py의 PIPELINE_STEPS(180초 타임아웃, run_pipeline())에는 넣지
않고 별도 스케줄러(run_recommendations.bat)로 돌린다. 가격 이력만 받으면 종목당
개별 호출이 아니라 "날짜당 전종목 한 번"이라 ~170거래일 x ~1.2초 = 수 분이면
끝나지만, 종목명 캐시가 없는 첫 실행은 종목당 개별 호출(pykrx에 벌크 조회가
없음)이라 2,700여 개 x 0.3~0.4초 = 15~20분 추가로 걸린다. 이후로는 새로 상장된
종목만 채우면 되므로 빨라진다.
"""

import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime

import pandas as pd
from pykrx import stock

from recommendation_signals import evaluate_all

DATA_DIR = "data"
KOSPI_CSV_PATH = os.path.join(DATA_DIR, "kospi.csv")
TICKER_NAME_CACHE_PATH = os.path.join(DATA_DIR, "ticker_names.json")
# collect.py의 collect_lock()과 같은 패턴 — 수동 실행과 스케줄러 실행이 겹치면
# pykrx 세션/파일 쓰기가 부딪힐 수 있어 막는다.
LOCK_PATH = os.path.join(DATA_DIR, "recommendations.lock")
LOCK_STALE_SEC = 3600  # 정상 실행은 전체 스캔이라도 30분 내 끝나므로, 이보다 오래된 락은 죽은 프로세스로 간주


@contextmanager
def recommendations_lock():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(LOCK_PATH) and time.time() - os.path.getmtime(LOCK_PATH) < LOCK_STALE_SEC:
        print(f"[SKIP] 다른 종목추천 수집이 이미 실행 중입니다 ({LOCK_PATH}).")
        sys.exit(0)
    with open(LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))
    try:
        yield
    finally:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
RECOMMENDATIONS_PATH = os.path.join(DATA_DIR, "recommendations.json")

# MA120이 안정화되려면 120거래일, 다이버전스 윈도우가 60거래일 필요 — 여유를 좀
# 더 둬서 지표 초반 왜곡을 피한다.
LOOKBACK_TRADING_DAYS = 170
REQUEST_DELAY_SEC = 0.1

OHLCV_COLUMN_MAP = {"시가": "Open", "고가": "High", "저가": "Low", "종가": "Close", "거래량": "Volume"}


def _trading_dates(n: int) -> list[str]:
    """data/kospi.csv(collect.py가 매일 갱신하는 지수 이력)의 최근 n개 거래일을
    'YYYYMMDD' 형식으로 반환한다 — 주말·공휴일 계산을 새로 짤 필요 없이 기존
    파일을 달력으로 재활용."""
    df = pd.read_csv(KOSPI_CSV_PATH, encoding="utf-8-sig")
    dates = df["Date"].tail(n).tolist()
    return [d.replace("-", "") for d in dates]


def _load_universe_tickers(latest_date: str) -> set[str]:
    """코스피+코스닥만(코넥스 제외) — "종목 분석" 탭 검색 대상과 동일한 범위."""
    kospi = set(stock.get_market_ticker_list(latest_date, market="KOSPI"))
    kosdaq = set(stock.get_market_ticker_list(latest_date, market="KOSDAQ"))
    return kospi | kosdaq


def fetch_price_history(dates: list[str], universe: set[str]) -> dict[str, pd.DataFrame]:
    """dates(오래된 순) x universe로 필터링한 장기 OHLCV.
    반환: {티커: DataFrame(오름차순, Open/High/Low/Close/Volume)}"""
    frames = []
    for date_str in dates:
        try:
            df = stock.get_market_ohlcv_by_ticker(date_str, market="ALL")
        except Exception as e:
            print(f"[WARN] {date_str} 조회 실패: {e}")
            continue
        if df.empty:
            continue
        df = df[df.index.isin(universe)]
        df = df.rename(columns=OHLCV_COLUMN_MAP)[list(OHLCV_COLUMN_MAP.values())]
        df = df.reset_index().rename(columns={"티커": "Ticker"})
        df["Date"] = date_str
        frames.append(df)
        time.sleep(REQUEST_DELAY_SEC)

    if not frames:
        return {}

    long_df = pd.concat(frames, ignore_index=True)
    return {
        ticker: group.sort_values("Date").reset_index(drop=True)
        for ticker, group in long_df.groupby("Ticker")
    }


def _load_name_cache() -> dict[str, str]:
    if not os.path.exists(TICKER_NAME_CACHE_PATH):
        return {}
    with open(TICKER_NAME_CACHE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_names(tickers) -> dict[str, str]:
    """캐시에 없는 티커만 개별 조회해서 채운다(신규 상장 등) — 전체를 매번
    다시 받지 않는다. 종목명은 사실상 안 바뀌므로 TTL 없이 계속 누적."""
    cache = _load_name_cache()
    updated = False
    for tk in tickers:
        if tk in cache:
            continue
        try:
            cache[tk] = stock.get_market_ticker_name(tk)
        except Exception:
            cache[tk] = tk
        updated = True
        time.sleep(REQUEST_DELAY_SEC)

    if updated:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(TICKER_NAME_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    return cache


def run() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)

    dates = _trading_dates(LOOKBACK_TRADING_DAYS)
    if not dates:
        print("[FAIL] data/kospi.csv에서 거래일을 찾지 못했습니다.")
        return

    universe = _load_universe_tickers(dates[-1])
    print(f"대상 종목: {len(universe)}개, 조회 거래일: {len(dates)}일 ({dates[0]} ~ {dates[-1]})")

    price_history = fetch_price_history(dates, universe)
    print(f"가격 이력 확보: {len(price_history)}개 종목")

    names = resolve_names(price_history.keys())

    results: dict[str, list] = {"trend": [], "reversion": [], "breakout": [], "divergence": []}
    for ticker, df in price_history.items():
        signals = evaluate_all(df)
        name = names.get(ticker, ticker)
        for key, detail in signals.items():
            if detail:
                results[key].append({"symbol": ticker, "name": name, **detail})

    for key, items in results.items():
        items.sort(key=lambda x: x.get("volume_ratio", 0), reverse=True)

    payload = {
        "generated_at": datetime.now().isoformat(),
        "universe_count": len(price_history),
        "signals": results,
    }
    with open(RECOMMENDATIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"-> {RECOMMENDATIONS_PATH} 저장 완료")
    for key, items in results.items():
        print(f"  {key}: {len(items)}개")


if __name__ == "__main__":
    with recommendations_lock():
        run()
