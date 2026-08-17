# fetch_indicators.py — ECOS / FRED 거시 지표 수집 스크립트
import csv
import os
import socket
import sys
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

# requests 호출에는 이미 timeout=10 을 넘기고 있지만, 다른 모듈과 동일하게
# 소켓 기본 타임아웃도 방어적으로 걸어둔다.
socket.setdefaulttimeout(15)

load_dotenv()

ECOS_API_KEY = os.getenv('ECOS_API_KEY')
FRED_API_KEY = os.getenv('FRED_API_KEY')

DATA_DIR = 'data'
OUTPUT_CSV = os.path.join(DATA_DIR, 'indicators.csv')

# --- ECOS(한국은행) 통계표 코드 -----------------------------------------
# ecos.bok.or.kr > 100대 통계지표 / 통계검색 에서 실제 코드를 확인해
# 값이 다르면 아래 상수만 바꿔 넣으면 된다.
ECOS_BASE_URL = 'https://ecos.bok.or.kr/api/StatisticSearch'
ECOS_STATS = {
    'base_rate': {
        'label': '한국 기준금리',
        'stat_code': '722Y001',    # 한국은행 기준금리 및 여수신금리
        'item_code1': '0101000',   # 한국은행 기준금리
        'cycle': 'D',
    },
    'usdkrw': {
        'label': '원/달러 환율',
        'stat_code': '731Y001',    # 시장평균환율
        'item_code1': '0000001',   # 원/미국달러(매매기준율)
        'cycle': 'D',
    },
}

# --- FRED(세인트루이스 연은) 시리즈 ID -----------------------------------
FRED_BASE_URL = 'https://api.stlouisfed.org/fred/series/observations'
FRED_SERIES = {
    'DGS10': '미국 10년물 국채금리',
    'DTWEXBGS': '달러인덱스(Broad)',
}


def check_api_keys():
    """CLI 실행용 — 키가 없으면 안내를 출력하고 프로세스를 종료한다.
    app.py 에서 import 해 쓸 때는 이 함수 대신 fetch_all_indicators() 를 직접 불러
    키가 없는 지표만 건너뛰고 나머지는 계속 진행한다(Streamlit 프로세스가 죽으면 안 되므로)."""
    missing = [name for name, val in (('ECOS_API_KEY', ECOS_API_KEY), ('FRED_API_KEY', FRED_API_KEY)) if not val]
    if missing:
        print('다음 API 키가 .env 에 설정되어 있지 않습니다:', ', '.join(missing))
        print('프로젝트 루트에 .env 파일을 만들고 아래처럼 채워주세요:')
        print('  ECOS_API_KEY=발급받은키')
        print('  FRED_API_KEY=발급받은키')
        sys.exit(1)


def fetch_ecos(key):
    cfg = ECOS_STATS[key]
    end = datetime.now()
    start = end - timedelta(days=10)
    date_fmt = '%Y%m%d' if cfg['cycle'] == 'D' else '%Y%m'
    start_str, end_str = start.strftime(date_fmt), end.strftime(date_fmt)

    url = (
        f"{ECOS_BASE_URL}/{ECOS_API_KEY}/json/kr/1/20/"
        f"{cfg['stat_code']}/{cfg['cycle']}/{start_str}/{end_str}/{cfg['item_code1']}"
    )

    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"[FAIL] {cfg['label']} (ECOS): 네트워크 오류 - {e}")
        return None
    except ValueError as e:
        print(f"[FAIL] {cfg['label']} (ECOS): 응답 파싱 오류 - {e}")
        return None

    if 'RESULT' in payload:
        result = payload['RESULT']
        print(f"[FAIL] {cfg['label']} (ECOS): {result.get('CODE')} - {result.get('MESSAGE')}")
        return None

    rows = payload.get('StatisticSearch', {}).get('row')
    if not rows:
        print(f"[FAIL] {cfg['label']} (ECOS): 조회된 데이터가 없습니다.")
        return None

    latest = rows[-1]
    return {
        'indicator': key,
        'label': cfg['label'],
        'value': latest.get('DATA_VALUE'),
        'as_of': latest.get('TIME'),
        'source': 'ECOS',
    }


def fetch_fred(series_id):
    label = FRED_SERIES[series_id]
    params = {
        'series_id': series_id,
        'api_key': FRED_API_KEY,
        'file_type': 'json',
        'sort_order': 'desc',
        'limit': 10,
    }

    try:
        resp = requests.get(FRED_BASE_URL, params=params, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"[FAIL] {label} (FRED): 네트워크 오류 - {e}")
        return None
    except ValueError as e:
        print(f"[FAIL] {label} (FRED): 응답 파싱 오류 - {e}")
        return None

    if 'error_code' in payload:
        print(f"[FAIL] {label} (FRED): {payload.get('error_code')} - {payload.get('error_message')}")
        return None

    observations = payload.get('observations', [])
    latest = next((o for o in observations if o.get('value') not in (None, '.')), None)
    if latest is None:
        print(f"[FAIL] {label} (FRED): 유효한 관측치가 없습니다.")
        return None

    return {
        'indicator': series_id,
        'label': label,
        'value': latest['value'],
        'as_of': latest['date'],
        'source': 'FRED',
    }


def save_indicators(results):
    os.makedirs(DATA_DIR, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).isoformat()

    with open(OUTPUT_CSV, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['indicator', 'label', 'value', 'as_of', 'source', 'fetched_at'])
        writer.writeheader()
        for row in results:
            row['fetched_at'] = fetched_at
            writer.writerow(row)

    print(f"-> {len(results)}개 지표를 {OUTPUT_CSV} 에 저장했습니다.")


def fetch_all_indicators():
    """app.py 에서 직접 호출하는 라이브러리 진입점.
    키가 없는 소스는 건너뛰고 나머지 지표는 계속 수집한다(sys.exit 하지 않음)."""
    results = []

    if ECOS_API_KEY:
        for key in ECOS_STATS:
            row = fetch_ecos(key)
            if row:
                results.append(row)
    else:
        print('ECOS_API_KEY 가 없어 한국 지표(기준금리/환율)는 건너뜁니다.')

    if FRED_API_KEY:
        for series_id in FRED_SERIES:
            row = fetch_fred(series_id)
            if row:
                results.append(row)
    else:
        print('FRED_API_KEY 가 없어 미국 지표(국채금리/달러인덱스)는 건너뜁니다.')

    return results


def main():
    check_api_keys()
    results = fetch_all_indicators()
    for row in results:
        print(f"[OK] {row['label']}: {row['value']} (기준일 {row['as_of']})")

    if not results:
        print('성공한 지표가 없어 CSV를 저장하지 않았습니다.')
        return

    save_indicators(results)


if __name__ == '__main__':
    main()
