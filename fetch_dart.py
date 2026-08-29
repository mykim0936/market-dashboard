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

# Streamlit Community Cloud에서 opendart.fss.or.kr로의 연결이 (인증 실패가 아니라)
# TCP 연결 자체가 느려서 30초 만에 타임아웃난 사례가 있었다 — 진짜 느린 건지 아예
# 막힌 건지 구분하려고 넉넉하게 늘려둔다.
DART_API_BASE = 'https://opendart.fss.or.kr/api'
DART_TIMEOUT_SEC = 60


def _api_key():
    return os.environ.get('OPENDART_API_KEY')


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


# {report_code: 그 보고서가 커버하는 분기 번호(1~4)}. 처음에는 반기(11012)/3분기
# (11014)/사업보고서(11011)의 thstrm_amount가 전부 "연초부터의 누적"이라고 가정하고
# 앞 분기 누적을 빼는 방식으로 짰었는데, 실제 DART 응답을 까보니 틀렸다:
#
#   - 1분기보고서(11013): thstrm_amount = 1분기 단독 (thstrm_add_amount와 동일)
#   - 반기보고서(11012):   thstrm_amount = "2분기 단독"(3개월)!  thstrm_add_amount = 반기 누적(6개월)
#   - 3분기보고서(11014):  thstrm_amount = "3분기 단독"(3개월)!  thstrm_add_amount = 9개월 누적
#   - 사업보고서(11011):   thstrm_amount = 연간 누적(4분기 단독 아님), thstrm_add_amount 없음
#
# 즉 반기·3분기 보고서의 thstrm_amount는 이미 "그 분기 하나"의 실적이라 빼기가 필요
# 없고(오히려 빼면 틀린다 — 이전 버전의 실제 버그였다), 4분기만 연간 누적에서 9개월
# 누적(3분기보고서의 thstrm_add_amount)을 빼서 만들어야 한다. 여러 종목의 실제
# 응답으로 이 패턴을 확인했다(아이쓰리시스템/쏠리드/엔켐, OFS·CFS 둘 다 동일).
QUARTER_REPORT_CODES = {'11013': 1, '11012': 2, '11014': 3, '11011': 4}


def _fetch_is_rows(corp_code, bsns_year, reprt_code):
    """해당 보고서의 매출액/영업이익 손익계산서 행을 (rows, fs_div)로 반환 —
    연결(CFS) 우선, 없으면 개별(OFS). 못 찾으면 (None, None)."""
    for fs_div in ('CFS', 'OFS'):
        candidate_rows = _get('fnlttSinglAcnt.json', {
            'corp_code': corp_code, 'bsns_year': bsns_year, 'reprt_code': reprt_code,
        })
        if not candidate_rows:
            continue
        filtered = [
            r for r in candidate_rows
            if r.get('fs_div') == fs_div and r.get('sj_div') == 'IS'
            and r.get('account_nm') in ('매출액', '영업이익')
        ]
        if filtered:
            return filtered, fs_div
    return None, None


def fetch_quarterly_financials(corp_code, bsns_years):
    """여러 bsns_year에 대해 분기별 매출액·영업이익(억원)을 계산한다. 1~3분기는
    각 보고서의 thstrm_amount를 그대로 쓰고(이미 단일 분기 값), 4분기만 사업보고서의
    연간 누적에서 3분기보고서의 9개월 누적(thstrm_add_amount)을 빼서 만든다.
    아직 안 나온 분기는 건너뛴다. 반환: ((연도, 분기) 오름차순 리스트, 실제 쓰인 fs_div)."""
    by_key = {}
    fs_div_used = None

    for bsns_year in bsns_years:
        nine_month = {'revenue': None, 'operating_income': None}

        for reprt_code, qnum in QUARTER_REPORT_CODES.items():
            rows, fs_div = _fetch_is_rows(corp_code, bsns_year, reprt_code)
            if not rows:
                continue
            fs_div_used = fs_div

            revenue_row = next((r for r in rows if r.get('account_nm') == '매출액'), None)
            oi_row = next((r for r in rows if r.get('account_nm') == '영업이익'), None)
            date_str = (revenue_row or oi_row or {}).get('thstrm_dt', '')
            _, end = _parse_period(date_str)
            if end is None:
                continue

            if qnum == 4:
                # 사업보고서 thstrm_amount는 연간 누적 — 3분기까지의 누적을 빼야
                # 4분기 단독이 나온다. 3분기보고서를 못 받았으면 계산 불가.
                fy_revenue = _to_amount(revenue_row, 'thstrm_amount')
                fy_oi = _to_amount(oi_row, 'thstrm_amount')
                q_revenue = (
                    None if fy_revenue is None or nine_month['revenue'] is None
                    else fy_revenue - nine_month['revenue']
                )
                q_op_income = (
                    None if fy_oi is None or nine_month['operating_income'] is None
                    else fy_oi - nine_month['operating_income']
                )
            else:
                q_revenue = _to_amount(revenue_row, 'thstrm_amount')
                q_op_income = _to_amount(oi_row, 'thstrm_amount')
                if qnum == 3:
                    nine_month['revenue'] = _to_amount(revenue_row, 'thstrm_add_amount')
                    nine_month['operating_income'] = _to_amount(oi_row, 'thstrm_add_amount')

            if q_revenue is None and q_op_income is None:
                continue
            by_key[(bsns_year, qnum)] = {
                'year': bsns_year, 'quarter': qnum, 'revenue': q_revenue, 'operating_income': q_op_income,
            }

    return sorted(by_key.values(), key=lambda x: (x['year'], x['quarter'])), fs_div_used


# "단일회사 주요 재무지표"(fnlttSinglIndx.json) 응답의 idx_nm -> 우리가 쓰는 키.
# DART가 idx_cl_code=M220000(안정성)/M210000(수익성)/M240000(활동성)로 나눠주는
# 지표 중 일부만 쓴다. ROA는 DART가 별도 항목으로 주지 않아, 호출부에서
# 순이익률 × 총자산회전율로 듀폰 방식 근사치를 계산한다(asset_turnover는
# DART가 %로 주는 값 — 예: 30.245는 회전율 0.30배라는 뜻).
_RATIO_IDX_MAP = {
    '부채비율': 'debt_ratio', '유동비율': 'current_ratio',
    '이자보상배율': 'interest_coverage', 'ROE': 'roe', '순이익률': 'net_margin',
    '총자산회전율': 'asset_turnover',
}


def fetch_financial_ratios(corp_code, bsns_year):
    """부채비율·유동비율·이자보상배율·ROE·순이익률·총자산회전율을 한 번에 조회한다
    (fnlttSinglIndx.json, 안정성/수익성/활동성 지표군 각 1회 호출). DART가 해당
    종목·연도에 대해 값을 계산하지 못한 지표는 idx_val 자체가 없을 수 있어(예:
    이자비용을 구분 공시하지 않는 회사의 이자보상배율) 그런 항목은 조용히 빠진다 —
    호출부에서 없는 키는 "-"로 표시하면 된다.
    반환: {'debt_ratio':, 'current_ratio':, 'interest_coverage':, 'roe':, 'net_margin':,
    'asset_turnover':} (일부 또는 전부 없을 수 있음, 완전 실패 시 빈 dict)."""
    result = {}
    for idx_cl_code in ('M220000', 'M210000', 'M240000'):
        rows = _get('fnlttSinglIndx.json', {
            'corp_code': corp_code, 'bsns_year': bsns_year, 'reprt_code': '11011',
            'idx_cl_code': idx_cl_code,
        })
        if not rows:
            continue
        for row in rows:
            key = _RATIO_IDX_MAP.get(row.get('idx_nm'))
            raw = row.get('idx_val')
            if key is None or not raw:
                continue
            try:
                result[key] = float(str(raw).replace(',', ''))
            except ValueError:
                continue
    return result


def fetch_operating_cashflow(corp_code, bsns_year):
    """최근 3개년 영업활동현금흐름(억원)을 사업보고서 1회 조회로 받는다(전체
    재무제표 API가 당기/전기/전전기를 한 번에 주는 걸 그대로 활용 — fetch_eps와
    같은 fnlttSinglAcntAll.json 호출 패턴). 영업이익과 함께 보면 "이익의 질"
    (영업이익은 있는데 실제 현금은 못 버는 경우)을 가늠할 수 있다. 반환: 연도
    오름차순 [{'year':, 'cfo':}] — 계정을 못 찾으면 []."""
    for fs_div in ('CFS', 'OFS'):
        rows = _get('fnlttSinglAcntAll.json', {
            'corp_code': corp_code, 'bsns_year': bsns_year, 'reprt_code': '11011', 'fs_div': fs_div,
        })
        if not rows:
            continue
        cf_row = next(
            (r for r in rows if r.get('sj_div') == 'CF'
             and '영업활동' in r.get('account_nm', '') and '현금흐름' in r.get('account_nm', '')),
            None,
        )
        if cf_row is None:
            continue
        by_year = {}
        for amount_key, year_offset in (('thstrm_amount', 0), ('frmtrm_amount', 1), ('bfefrmtrm_amount', 2)):
            value = _to_amount(cf_row, amount_key)
            if value is not None:
                by_year[int(bsns_year) - year_offset] = value
        if by_year:
            return sorted(({'year': y, 'cfo': v} for y, v in by_year.items()), key=lambda x: x['year'])
    return []


def fetch_capital_changes(corp_code, bsns_year):
    """유상증자·무상증자·전환사채(CB) 전환·주식매수선택권 행사 등 자본금 변동
    이력(증자·감자 현황, irdsSttus.json). 반환: [{'date':, 'type':, 'qty':}]
    (date는 'YYYY.MM.DD' 문자열, DART가 준 순서 그대로) — 실패/없음 시 []."""
    rows = _get('irdsSttus.json', {
        'corp_code': corp_code, 'bsns_year': bsns_year, 'reprt_code': '11011',
    })
    if not rows:
        return []
    items = []
    for r in rows:
        try:
            qty = float((r.get('isu_dcrs_qy') or '').replace(',', ''))
        except ValueError:
            qty = None
        items.append({'date': r.get('isu_dcrs_de'), 'type': r.get('isu_dcrs_stle'), 'qty': qty})
    return items


def fetch_largest_shareholder(corp_code, bsns_year):
    """최대주주(및 특수관계인) 최신 지분율 1개 시점만 뽑는다(최대주주 현황,
    hyslrSttus.json) — 분기별 추이는 호출량이 많아 이번 버전에서는 다루지 않는다.
    반환: {'name':, 'total_pct':} — 특수관계인 합계('계') 행이 있으면 그 지분율을,
    없으면 본인 단독 지분율을 쓴다. 못 찾으면 {}."""
    rows = _get('hyslrSttus.json', {
        'corp_code': corp_code, 'bsns_year': bsns_year, 'reprt_code': '11011',
    })
    if not rows:
        return {}
    total_row = next((r for r in rows if r.get('nm') == '계' and r.get('stock_knd') == '보통주'), None)
    first_row = rows[0]
    pct_row = total_row or first_row
    try:
        total_pct = float(str(pct_row.get('trmend_posesn_stock_qota_rt')).replace(',', ''))
    except (TypeError, ValueError):
        total_pct = None
    return {'name': first_row.get('nm'), 'total_pct': total_pct}


def fetch_inventory(corp_code, bsns_year):
    """최근 3개년 재고자산(억원)을 사업보고서 1회 조회로 받는다(재무상태표 계정,
    fnlttSinglAcntAll.json — fetch_operating_cashflow와 같은 3개년-한번에 패턴).
    재고자산이 사실상 없는 업종(예: 서비스업)은 계정 자체가 없을 수 있다.
    반환: 연도 오름차순 [{'year':, 'inventory':}] — 계정을 못 찾으면 []."""
    for fs_div in ('CFS', 'OFS'):
        rows = _get('fnlttSinglAcntAll.json', {
            'corp_code': corp_code, 'bsns_year': bsns_year, 'reprt_code': '11011', 'fs_div': fs_div,
        })
        if not rows:
            continue
        inv_row = next(
            (r for r in rows if r.get('sj_div') == 'BS' and r.get('account_nm') == '재고자산'), None,
        )
        if inv_row is None:
            continue
        by_year = {}
        for amount_key, year_offset in (('thstrm_amount', 0), ('frmtrm_amount', 1), ('bfefrmtrm_amount', 2)):
            value = _to_amount(inv_row, amount_key)
            if value is not None:
                by_year[int(bsns_year) - year_offset] = value
        if by_year:
            return sorted(({'year': y, 'inventory': v} for y, v in by_year.items()), key=lambda x: x['year'])
    return []


def fetch_major_shareholder_trades(corp_code):
    """5% 이상 대량보유자(주요주주)의 지분 변동 보고 이력을 전부 받는다(대량보유
    상황보고, majorstock.json — bsns_year 없이 회사 단위로 전체 이력을 준다).
    report_resn(보고사유)에 "처분"이 들어있으면 매도를 뜻한다. 반환:
    [{'date':, 'holder':, 'reason':, 'stake_change_pct':}] (DART가 주는 순서 그대로,
    보통 최신순) — 실패/없음 시 []."""
    rows = _get('majorstock.json', {'corp_code': corp_code})
    if not rows:
        return []
    items = []
    for r in rows:
        try:
            change = float(str(r.get('stkrt_irds')).replace(',', ''))
        except (TypeError, ValueError):
            change = None
        items.append({
            'date': r.get('rcept_dt'), 'holder': r.get('repror'),
            'reason': r.get('report_resn'), 'stake_change_pct': change,
        })
    return items


# 재무상태표(BS)·손익계산서(IS) 표준계정 -> 우리가 쓰는 키. fnlttSinglIndx.json
# ("단일회사 주요 재무지표")은 idx_cl_code별로 계산해서 주지만 회사에 따라 최근
# 1~2개년만 있는 경우가 많다(2026-08 확인, 카카오는 2023~2025년만 있고 그 이전은
# "조회된 데이터가 없습니다"). 반면 원본 재무제표(BS/IS 표준계정)는 훨씬 더
# 오래전까지 있어서, 부채비율/유동비율/ROE/ROA/총자산회전율은 이 원본 계정에서
# 직접 계산하는 쪽이 더 긴 이력을 준다. 이자보상배율만 예외 — 이자비용 계정명이
# 회사마다 제각각이라 fnlttSinglIndx 값을 그대로 쓴다(fetch_financial_ratios).
_BS_IS_ACCOUNT_MAP = {
    '부채총계': 'debt', '자본총계': 'equity', '유동자산': 'current_assets',
    '유동부채': 'current_liab', '자산총계': 'total_assets',
    '당기순이익(손실)': 'net_income', '매출액': 'revenue',
}


def fetch_balance_sheet_ratios(corp_code, bsns_year):
    """부채비율·유동비율·ROE·ROA·총자산회전율 계산에 필요한 원시 재무제표 계정을
    사업보고서 1회 조회로 최근 3개년치 받는다(전체 재무제표 API,
    fetch_operating_cashflow와 같은 "당기/전기/전전기 한 번에" 패턴). 반환:
    {연도: {'debt_ratio':, 'current_ratio':, 'roe':, 'roa':, 'asset_turnover':}}
    (asset_turnover는 fetch_financial_ratios의 total_asset_turnover와 단위를
    맞추려고 %로 반환 — 예: 30.245는 회전율 0.30배) — 계정을 못 찾으면 {}."""
    for fs_div in ('CFS', 'OFS'):
        rows = _get('fnlttSinglAcntAll.json', {
            'corp_code': corp_code, 'bsns_year': bsns_year, 'reprt_code': '11011', 'fs_div': fs_div,
        })
        if not rows:
            continue
        found = {}
        for r in rows:
            key = _BS_IS_ACCOUNT_MAP.get(r.get('account_nm'))
            if key is None or key in found or r.get('sj_div') not in ('BS', 'IS'):
                continue
            found[key] = r
        if not all(k in found for k in ('debt', 'equity', 'current_assets', 'current_liab', 'total_assets')):
            continue

        by_year = {}
        for amount_key, year_offset in (('thstrm_amount', 0), ('frmtrm_amount', 1), ('bfefrmtrm_amount', 2)):
            year = int(bsns_year) - year_offset
            vals = {field: _to_amount(row, amount_key) for field, row in found.items()}
            debt, equity = vals.get('debt'), vals.get('equity')
            cur_assets, cur_liab = vals.get('current_assets'), vals.get('current_liab')
            total_assets, net_income, revenue = vals.get('total_assets'), vals.get('net_income'), vals.get('revenue')

            ratios = {}
            if debt is not None and equity:
                ratios['debt_ratio'] = debt / equity * 100
            if cur_assets is not None and cur_liab:
                ratios['current_ratio'] = cur_assets / cur_liab * 100
            if net_income is not None and equity:
                ratios['roe'] = net_income / equity * 100
            if net_income is not None and total_assets:
                ratios['roa'] = net_income / total_assets * 100
            if revenue is not None and total_assets:
                ratios['asset_turnover'] = revenue / total_assets * 100
            if ratios:
                by_year[year] = ratios
        if by_year:
            return by_year
    return {}
