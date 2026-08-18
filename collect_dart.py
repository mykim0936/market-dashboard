# collect_dart.py — DART/pykrx PER·재무 스냅샷 생성 (로컬 전용)
#
# Streamlit Community Cloud에서 opendart.fss.or.kr 로의 연결이 구조적으로 막혀 있고
# (ConnectTimeout, 2026-08-19 확인), pykrx 벌크 조회도 클라우드에서 신뢰하기 어렵다.
# 대신 이 스크립트를 로컬(Windows 작업 스케줄러)에서 주기적으로 돌려 결과를
# data/dart_snapshot.json 에 저장하고, 이 파일을 git에 커밋해서 배포본에 실어 보낸다.
# app.py 는 이 파일이 있으면 그걸 우선 쓰고, 없으면(로컬에서 아직 한 번도 안 돌렸을 때)
# 기존처럼 라이브 조회를 시도한다. PER/재무는 하루~일주일에 한 번만 갱신돼도 충분하다.
import json
import os
from datetime import datetime, timezone

import pandas as pd
import pykrx.stock as pykrx_stock

import fetch_dart
import fetch_portfolio

DATA_DIR = 'data'
SNAPSHOT_PATH = os.path.join(DATA_DIR, 'dart_snapshot.json')
PER_LOOKBACK_DAYS = 5


def fetch_per_universe(market):
    """app.py의 동명 함수와 같은 로직 — 최근 며칠 안에서 데이터 있는 날을 찾는다."""
    for delta in range(PER_LOOKBACK_DAYS):
        d = (datetime.now() - pd.Timedelta(days=delta)).strftime('%Y%m%d')
        try:
            fundamentals = pykrx_stock.get_market_fundamental(d, market=market)
            sectors = pykrx_stock.get_market_sector_classifications(d, market)
        except Exception:
            continue
        if not fundamentals.empty and not sectors.empty:
            return fundamentals[['PER']].join(sectors[['업종명']], how='left')
    return pd.DataFrame(columns=['PER', '업종명'])


def build_snapshot():
    holdings = fetch_portfolio.load_holdings()
    corp_map = fetch_dart.fetch_corp_code_map()
    universes = {}
    this_year = datetime.now().year

    stocks = {}
    for _, h in holdings.iterrows():
        ticker = h['ticker']
        if ticker == fetch_portfolio.CASH_TICKER:
            continue

        entry = {}
        corp_code = corp_map.get(ticker)
        if corp_code:
            for bsns_year in (this_year - 1, this_year - 2):
                eps, fs_div = fetch_dart.fetch_eps(corp_code, str(bsns_year))
                if eps is not None:
                    entry['eps'] = eps
                    entry['eps_fs_div'] = fs_div
                    entry['eps_bsns_year'] = bsns_year
                    break

            fin_years, fin_fs_div = fetch_dart.fetch_annual_financials(
                corp_code, [str(this_year - 1), str(this_year - 4), str(this_year - 7)])
            if fin_years:
                entry['financials'] = fin_years
                entry['financials_fs_div'] = fin_fs_div

        # 시장을 모르는 상태로 시작하므로 KOSPI/KOSDAQ 순서대로 찾아본다.
        for market in ('KOSPI', 'KOSDAQ'):
            if market not in universes:
                universes[market] = fetch_per_universe(market)
            universe = universes[market]
            if not universe.empty and ticker in universe.index:
                industry = universe.loc[ticker, '업종명']
                peers = universe[
                    (universe['업종명'] == industry) & (universe.index != ticker) & (universe['PER'] > 0)
                ]
                entry['market'] = market
                entry['industry_name'] = industry
                entry['industry_per'] = float(peers['PER'].median()) if not peers.empty else None
                break

        if entry:
            stocks[ticker] = entry
            print(f"[OK] {h['name']}({ticker}): {entry}")
        else:
            print(f"[WARN] {h['name']}({ticker}): DART/pykrx 데이터를 찾지 못했습니다.")

    return stocks


def main():
    stocks = build_snapshot()
    if not stocks:
        print('스냅샷에 담을 데이터가 없어 저장을 건너뜁니다.')
        return

    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'stocks': stocks,
    }
    with open(SNAPSHOT_PATH, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f'-> {len(stocks)}개 종목을 {SNAPSHOT_PATH} 에 저장했습니다.')


if __name__ == '__main__':
    main()
