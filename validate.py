# validate.py — 수집 데이터 검증 스크립트
import json
import os
from datetime import datetime, timezone

import pandas as pd
import FinanceDataReader as fdr
from pykrx import stock

DATA_DIR = 'data'
KOSPI_CSV = os.path.join(DATA_DIR, 'kospi.csv')
STATUS_JSON = os.path.join(DATA_DIR, 'status.json')

MAX_ERROR_RATE = 0.001      # 0.1%
EXTREME_CHANGE = 0.30       # ±30%
RECENT_N = 30                # 최근 거래일 수


def cross_check_kospi():
    """FDR과 pykrx의 최근 30거래일 코스피 종가를 비교해 최대 오차율을 확인한다."""
    fdr_df = pd.read_csv(KOSPI_CSV, encoding='utf-8-sig', parse_dates=['Date'])
    fdr_recent = fdr_df.sort_values('Date').tail(RECENT_N)

    fromdate = fdr_recent['Date'].min().strftime('%Y%m%d')
    todate = fdr_recent['Date'].max().strftime('%Y%m%d')

    pykrx_df = stock.get_index_ohlcv_by_date(fromdate, todate, "1001")
    pykrx_df = pykrx_df.reset_index().rename(columns={'날짜': 'Date', '종가': 'Close_pykrx'})
    pykrx_df['Date'] = pd.to_datetime(pykrx_df['Date'])

    merged = fdr_recent[['Date', 'Close']].merge(
        pykrx_df[['Date', 'Close_pykrx']], on='Date', how='inner'
    )

    merged['error_rate'] = (merged['Close'] - merged['Close_pykrx']).abs() / merged['Close_pykrx']
    max_error_rate = merged['error_rate'].max() if not merged.empty else None
    ok = bool(max_error_rate is not None and max_error_rate < MAX_ERROR_RATE)

    return {
        'ok': ok,
        'max_error_rate': None if max_error_rate is None else round(float(max_error_rate), 6),
        'compared_rows': len(merged),
        'merged': merged,
        'pykrx_dates': set(pykrx_df['Date']),
        'fdr_recent_dates': set(fdr_recent['Date']),
    }


def flag_extreme():
    """전일 대비 등락률이 ±15%를 초과하는 행을 센다."""
    df = pd.read_csv(KOSPI_CSV, encoding='utf-8-sig')
    extreme = df[df['Change'].abs() > EXTREME_CHANGE]
    return {
        'count': int(len(extreme)),
        'rows': extreme[['Date', 'Change']].to_dict(orient='records'),
    }


def find_missing_dates(pykrx_dates, fdr_recent_dates):
    """pykrx 날짜 목록을 기준 달력으로 fdr 데이터의 누락 날짜를 찾는다."""
    missing = sorted(pykrx_dates - fdr_recent_dates)
    return [d.strftime('%Y-%m-%d') for d in missing]


def load_status():
    if os.path.exists(STATUS_JSON):
        with open(STATUS_JSON, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_status(status):
    with open(STATUS_JSON, 'w', encoding='utf-8') as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def validate():
    cross = cross_check_kospi()
    extreme = flag_extreme()
    missing = find_missing_dates(cross['pykrx_dates'], cross['fdr_recent_dates'])

    status = load_status()  # 기존 파일(예: tasks 구획)은 그대로 보존
    status['checks'] = {
        'cross_check_ok': cross['ok'],
        'range_alert': extreme['count'] > 0,
        'missing_dates': len(missing),
    }
    status['last_run'] = datetime.now(timezone.utc).isoformat()
    save_status(status)

    print(f"[cross_check] {'PASS' if cross['ok'] else 'FAIL'} "
          f"(max_error_rate={cross['max_error_rate']}, compared_rows={cross['compared_rows']})")
    print(f"[flag_extreme] {'PASS' if extreme['count'] == 0 else 'FAIL'} "
          f"(count={extreme['count']})")
    print(f"[missing_dates] {'PASS' if len(missing) == 0 else 'FAIL'} "
          f"(count={len(missing)})")
    if missing:
        print(f"  missing: {missing}")
    if extreme['count'] > 0:
        print(f"  extreme rows: {extreme['rows']}")

    print(f"-> {STATUS_JSON} 저장 완료")


if __name__ == '__main__':
    validate()
