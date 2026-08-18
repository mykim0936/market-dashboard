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
import subprocess
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


def git_commit_and_push():
    """스냅샷 파일만(git add -A 아님) 커밋해서 origin/main 에 바로 푸시한다 —
    이래야 Streamlit Cloud 배포본이 다음 접속 때 최신 스냅샷을 받는다. 내용이
    이전과 똑같으면(장 휴장일 등) 빈 커밋을 만들지 않고 조용히 넘어간다.
    실패해도(네트워크, 인증 등) 예외를 다시 던지지 않는다 — 스냅샷 파일 자체는
    이미 로컬에 저장됐으니 이 단계가 실패해도 스케줄러 작업 전체를 실패로
    끝내지 않고, 다음 실행 때 다시 시도되게 한다."""
    repo_dir = os.path.dirname(os.path.abspath(__file__)) or '.'
    try:
        status = subprocess.run(
            ['git', 'status', '--porcelain', '--', SNAPSHOT_PATH],
            cwd=repo_dir, capture_output=True, text=True, check=True,
        )
        if not status.stdout.strip():
            print('스냅샷 내용이 이전과 같아 git push를 건너뜁니다.')
            return

        subprocess.run(['git', 'add', '--', SNAPSHOT_PATH], cwd=repo_dir, check=True)
        commit_msg = f"Auto-update DART/pykrx snapshot ({datetime.now().strftime('%Y-%m-%d')})"
        subprocess.run(['git', 'commit', '-m', commit_msg], cwd=repo_dir, check=True)
        subprocess.run(['git', 'push'], cwd=repo_dir, check=True)
        print('-> git push 완료')
    except subprocess.CalledProcessError as e:
        print(f'[WARN] git 자동 커밋/푸시 실패(다음 실행 때 재시도됨): {e}')
    except Exception as e:
        print(f'[WARN] git 자동 커밋/푸시 중 예외(다음 실행 때 재시도됨): {e}')


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

    git_commit_and_push()


if __name__ == '__main__':
    main()
