# fetch_dart.py — DART(전자공시) Open API 직접 호출
#
# opendart.fss.or.kr 공식 REST API를 requests로 직접 부른다(ECOS/FRED와 같은 패턴 —
# SDK 대신 raw HTTP). 종목 상세(PER 계산용 EPS, 최근 매출액/영업이익) 자동 조회에 쓴다.
import io
import os
import re
import zipfile
from datetime import date
import xml.etree.ElementTree as ET

import requests

DART_API_BASE = 'https://opendart.fss.or.kr/api'
DART_TIMEOUT_SEC = 30


def _api_key():
    return os.environ.get('OPENDART_API_KEY')


def diagnose():
    """진단용 — 정상 경로(_get)는 실패 사유를 다 None으로 뭉개버리므로, 실제 원인을
    구분해야 할 때(예: 배포 환경에서 이유 없이 계속 실패할 때) 이걸로 직접 확인한다.
    삼성전자(00126380)로 고정 조회해서 corp_code/매핑 문제와 분리한다."""
    api_key = _api_key()
    result = {'api_key_set': bool(api_key), 'api_key_len': len(api_key) if api_key else 0}
    if not api_key:
        return result
    try:
        resp = requests.get(f'{DART_API_BASE}/fnlttSinglAcnt.json', params={
            'crtfc_key': api_key, 'corp_code': '00126380', 'bsns_year': '2025', 'reprt_code': '11011',
        }, timeout=DART_TIMEOUT_SEC)
        result['http_status'] = resp.status_code
        try:
            data = resp.json()
            result['dart_status'] = data.get('status')
            result['dart_message'] = data.get('message')
            result['row_count'] = len(data.get('list', []))
        except ValueError:
            result['json_parse_failed'] = True
            result['raw_text_head'] = resp.text[:300]
    except requests.exceptions.RequestException as e:
        result['request_exception'] = f'{type(e).__name__}: {e}'
    return result


def _get(endpoint, params):
    """반환값은 status="000"(정상)일 때만 list, 그 외(키 없음/자료 없음/네트워크 오류/
    HTTP 오류/DART 오류코드)는 전부 None — 호출부가 매번 try/except를 안 써도
    되게 여기서 모든 실패를 흡수한다."""
    api_key = _api_key()
    if not api_key:
        return None
    params = dict(params)
    params['crtfc_key'] = api_key
    try:
        resp = requests.get(f'{DART_API_BASE}/{endpoint}', params=params, timeout=DART_TIMEOUT_SEC)
        resp.raise_for_status()
        data = resp.json()
    except (requests.exceptions.RequestException, ValueError):
        return None
    if data.get('status') != '000':
        return None
    return data.get('list', [])


def fetch_corp_code_map():
    """전체 기업의 종목코드(6자리) -> corp_code(DART 고유번호, 8자리) 매핑을 한 번에
    받아온다(수 MB짜리 zip 하나). corp_code는 거의 안 바뀌므로 호출부(app.py)에서
    길게(하루 이상) 캐시해서 쓴다. 키가 없거나 요청이 실패하면 빈 딕셔너리를 반환."""
    api_key = _api_key()
    if not api_key:
        return {}
    try:
        resp = requests.get(f'{DART_API_BASE}/corpCode.xml', params={'crtfc_key': api_key}, timeout=DART_TIMEOUT_SEC)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            xml_bytes = z.read('CORPCODE.xml')
        root = ET.fromstring(xml_bytes)
    except Exception:
        return {}

    mapping = {}
    for item in root.findall('list'):
        stock_code = (item.findtext('stock_code') or '').strip()
        corp_code = item.findtext('corp_code')
        if stock_code and corp_code:
            mapping[stock_code] = corp_code
    return mapping


def fetch_eps(corp_code, bsns_year, reprt_code='11011'):
    """기본주당이익(EPS, 원)을 연결(CFS) 우선으로 찾고, 연결 재무제표가 없는 회사는
    개별(OFS)로 대체한다. 반환: (eps, fs_div) — 못 찾으면 (None, None)."""
    for fs_div in ('CFS', 'OFS'):
        rows = _get('fnlttSinglAcntAll.json', {
            'corp_code': corp_code, 'bsns_year': bsns_year, 'reprt_code': reprt_code, 'fs_div': fs_div,
        })
        if not rows:
            continue
        # "기본주당이익(손실)" — 희석주당이익 말고 기본(basic)을 쓴다. 회사마다 표기가
        # "기본주당이익"/"기본 주당순이익"/"기본주당순손실"처럼 조금씩 다르고, 사업
        # 부문별 "계속영업"/"중단영업" 세부 항목도 "주당"을 포함하지만 "기본"으로
        # 시작하지 않으므로(예: "계속영업기본주당손실") 접두어만 봐도 걸러진다.
        candidates = [
            r for r in rows
            if r.get('account_nm', '').startswith('기본') and '주당' in r.get('account_nm', '')
        ]
        if not candidates:
            continue
        try:
            eps = float(candidates[0]['thstrm_amount'].replace(',', ''))
        except (KeyError, ValueError, AttributeError):
            continue
        return eps, fs_div
    return None, None


def _parse_period(date_str):
    """손익계산서 기간은 'YYYY.MM.DD ~ YYYY.MM.DD' 형태로 온다 — 이걸 파싱해두면
    설립/분할 첫해처럼 1/1~12/31 전체가 아닌 부분 회계연도를 자동으로 알아챌 수 있다.
    실패하면 (None, None)."""
    m = re.match(r'(\d{4})\.(\d{2})\.(\d{2})\s*~\s*(\d{4})\.(\d{2})\.(\d{2})', date_str or '')
    if not m:
        return None, None
    y1, m1, d1, y2, m2, d2 = map(int, m.groups())
    try:
        return date(y1, m1, d1), date(y2, m2, d2)
    except ValueError:
        return None, None


def _to_amount(row, amount_key):
    if row is None:
        return None
    raw = row.get(amount_key)
    if not raw:
        return None
    try:
        return float(raw.replace(',', '')) / 1e8  # 원 -> 억원
    except ValueError:
        return None


def fetch_annual_financials(corp_code, bsns_years):
    """여러 bsns_year에 대해 매출액·영업이익(억원)을 연도별로 모은다. DART의
    "주요계정" API는 한 번 호출로 당기/전기/전전기 3개년 비교치를 주므로, bsns_years
    를 몇 년 간격으로만 넘겨도 여러 해를 커버할 수 있다(예: [2025, 2022] -> 최대
    2023~2025, 2020~2022 총 6개년). 회계기간이 1/1~12/31 전체가 아니면(설립/분할
    첫해 등) 'partial'에 실제 기간·개월 수를 채운다. 반환: (연도 오름차순 리스트,
    실제 쓰인 fs_div)."""
    by_year = {}
    fs_div_used = None

    for bsns_year in bsns_years:
        rows = None
        for fs_div in ('CFS', 'OFS'):
            candidate_rows = _get('fnlttSinglAcnt.json', {
                'corp_code': corp_code, 'bsns_year': bsns_year, 'reprt_code': '11011',
            })
            if not candidate_rows:
                continue
            filtered = [
                r for r in candidate_rows
                if r.get('fs_div') == fs_div and r.get('sj_div') == 'IS'
                and r.get('account_nm') in ('매출액', '영업이익')
            ]
            if filtered:
                rows = filtered
                fs_div_used = fs_div
                break
        if not rows:
            continue

        revenue_row = next((r for r in rows if r.get('account_nm') == '매출액'), None)
        oi_row = next((r for r in rows if r.get('account_nm') == '영업이익'), None)

        for amount_key, date_key in [
            ('thstrm_amount', 'thstrm_dt'), ('frmtrm_amount', 'frmtrm_dt'), ('bfefrmtrm_amount', 'bfefrmtrm_dt'),
        ]:
            date_str = (revenue_row or oi_row or {}).get(date_key, '')
            start, end = _parse_period(date_str)
            if end is None:
                continue
            revenue = _to_amount(revenue_row, amount_key)
            op_income = _to_amount(oi_row, amount_key)
            if revenue is None and op_income is None:
                continue

            partial = None
            if start is not None and (start.month, start.day) != (1, 1):
                months = (end.year * 12 + end.month) - (start.year * 12 + start.month) + 1
                partial = f'{start.month}~{end.month}월({months}개월) — 정상 12개월 실적 아님'

            by_year[end.year] = {
                'year': end.year, 'revenue': revenue, 'operating_income': op_income, 'partial': partial,
            }

    return sorted(by_year.values(), key=lambda x: x['year']), fs_div_used
