# app.py — 시장 대시보드
#
# 로컬(Windows 작업 스케줄러)과 클라우드(Streamlit Community Cloud) 양쪽에서 동작하도록
# "CSV가 있으면 그걸 읽고, 없으면 그 자리에서 직접 받아온다" 하이브리드 방식을 쓴다.
# - 로컬: collect.py가 10분마다 CSV를 써두므로 디스크만 읽어 빠르다.
# - 클라우드: 스케줄러가 없으므로 data/*.csv 가 커밋되어 있지 않다 -> 매번 직접 조회.
import html
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone

import altair as alt
import pandas as pd
import streamlit as st

# 라이브 조회 경로(fdr/fetch_* 모듈)가 이제 Streamlit 요청 처리 스레드 안에서 직접
# 실행되므로, 하나라도 무한 대기하면 대시보드 전체가 멈춘다. 소켓 기본 타임아웃으로
# 상한을 걸어 "느림"으로 끝나지 "먹통"으로 끝나지 않게 한다.
socket.setdefaulttimeout(30)


def _load_secret(name):
    try:
        return st.secrets.get(name)
    except Exception:
        return None


# fetch_indicators 는 import 시점에 os.getenv 로 키를 읽어 모듈 상수에 저장하므로,
# import 하기 전에 st.secrets 값을 os.environ 에 심어둬야 한다.
# KRX_ID/KRX_PW 는 pykrx가 import 시점에 자동으로 로그인 시도하며 읽는 값이다 —
# 로컬은 Windows 사용자 환경변수로 이미 있지만 클라우드는 여기로 브리지해야
# pykrx가 인증된 상태로 동작한다(없으면 비로그인 상태라 일부 대량 조회 —
# 업계 PER 계산용 시장 전체 PER/업종 데이터 등 — 가 막힐 수 있다).
for _key in (
    'ECOS_API_KEY', 'FRED_API_KEY', 'DASHBOARD_PASSWORD', 'OPENAI_API_KEY', 'OPENDART_API_KEY',
    'KRX_ID', 'KRX_PW',
):
    _val = _load_secret(_key)
    if _val and not os.environ.get(_key):
        os.environ[_key] = _val

import pykrx.stock as pykrx_stock
import yfinance as yf

import fetch_dart
import fetch_indicators
import fetch_news
import fetch_portfolio
import stock_opinion

DATA_DIR = 'data'

# (label, 파일명, 소스, 코드) — 국내 지수는 pykrx, 해외 지수·환율은 yfinance를 쓴다
# (collect.py 와 동일한 소스 구성. 2026-08-18 FinanceDataReader에서 교체).
# 탭 구성: "전체 시장현황" 탭은 지수(MARKET_SERIES), "환율 및 뉴스" 탭은 환율(FX_SERIES).
# 카드와 차트가 같은 목록을 쓰므로(환율만 별도 목록) 하나로 통일해 중복을 없앴다.
MARKET_SERIES = [
    ('코스피', 'kospi.csv', 'pykrx', '1001'),
    ('코스닥', 'kosdaq.csv', 'pykrx', '2001'),
    ('나스닥', 'nasdaq.csv', 'yfinance', '^IXIC'),
    ('S&P500', 'sp500.csv', 'yfinance', '^GSPC'),
]

FX_SERIES = [
    ('원/달러', 'usdkrw.csv', 'yfinance', 'KRW=X'),
]

# RS(상대강도) 비교 탭에서 고를 수 있는 기초지수 — (소스, 코드)는 MARKET_SERIES와 같은 규칙.
RS_BENCHMARKS = {
    '코스피': ('pykrx', '1001'),
    '코스닥': ('pykrx', '2001'),
    'S&P500': ('yfinance', '^GSPC'),
    '나스닥100': ('yfinance', '^NDX'),
}

# RS 비교 기간 — 각각 "지금부터 이만큼 전"의 시작일을 계산하는 함수.
RS_PERIODS = {
    '1주일': lambda now: now - pd.DateOffset(weeks=1),
    '1개월': lambda now: now - pd.DateOffset(months=1),
    '3개월': lambda now: now - pd.DateOffset(months=3),
    '6개월': lambda now: now - pd.DateOffset(months=6),
    'YTD': lambda now: pd.Timestamp(year=now.year, month=1, day=1),
    '1년': lambda now: now - pd.DateOffset(years=1),
}

# 지수/환율 차트에서 고를 수 있는 기간 — None은 "전체 기간"(필터링 없음)을 뜻한다.
CHART_PERIODS = {
    '1주일': lambda now: now - pd.DateOffset(weeks=1),
    '1개월': lambda now: now - pd.DateOffset(months=1),
    '3개월': lambda now: now - pd.DateOffset(months=3),
    '6개월': lambda now: now - pd.DateOffset(months=6),
    '1년': lambda now: now - pd.DateOffset(years=1),
    '5년': lambda now: now - pd.DateOffset(years=5),
    '전체': None,
}

# 카드는 최근 종가/전일 대비만 필요하므로 라이브 조회 시 짧게 받아 가볍게 유지한다.
CARD_LOOKBACK_YEARS = 1
# 차트는 2000년 근처부터 전체 기간을 넘기고, 화면에서는 마우스 스크롤/드래그로
# 확대·축소하며 보게 한다(로컬 CSV는 collect.py가 2001-01-01부터 받아두므로 이미 전체 보유).
CHART_YEARS = datetime.now().year - 2000

# 스위스 인터내셔널 스타일 팔레트 — 흰색/초록/회색/검정 네 가지만 쓴다.
# 배경이 검정이라 텍스트/보조색은 그 위에서 잘 읽히도록 조정한 값이다
# (SWISS_BLACK은 순검정이 아니라 화면 배경용 짙은 무채색, SWISS_GRAY_LIGHT는
# 검정 배경 위의 옅은 구분선/카드용 회색).
SWISS_BLACK = '#0D0D0D'
SWISS_WHITE = '#FFFFFF'
SWISS_GRAY = '#9CA3AF'
SWISS_GRAY_LIGHT = '#2A2A2A'
SWISS_GREEN = '#22C55E'

# 차트는 상승/하락/경고 신호가 아닌 단순 추세선이므로 팔레트의 흰색 하나만 고정해서 쓴다
# (검정 배경 위라 검정 대신 흰색을 써야 보인다).
NEUTRAL_CHART_COLOR = SWISS_WHITE

# 국내 관행에 맞춰 상승은 빨강, 하락은 파랑으로 표기한다(테마 팔레트와 별개로 유지 —
# 등락 표시는 장식이 아니라 국내 투자자에게 익숙한 기능적 관례라 사용자가 유지를 택함).
UP_COLOR = '#D32F2F'
DOWN_COLOR = '#1565C0'

INDICATORS_CSV = os.path.join(DATA_DIR, 'indicators.csv')
NEWS_CSV = os.path.join(DATA_DIR, 'news.csv')
PORTFOLIO_CSV = os.path.join(DATA_DIR, 'portfolio_status.csv')
# collect_dart.py가 로컬(주기적 스케줄러)에서 만들어 git에 커밋해두는 PER/재무
# 스냅샷 — Streamlit Cloud에서 opendart.fss.or.kr/KRX 벌크 조회로의 접속이
# 구조적으로 막혀 있어(2026-08 확인), 라이브 조회 대신 이 파일을 우선 쓴다.
DART_SNAPSHOT_PATH = os.path.join(DATA_DIR, 'dart_snapshot.json')
NEWS_LIMIT = 15

CASH_TICKER = 'CASH'

# 로컬 CSV 캐시(디스크 읽기)는 짧게, 라이브 조회(네트워크 호출)는 길게 잡아
# 클라우드에서 여러 사용자가 동시에 열어도 KRX/RSS 요청이 과도하게 나가지 않게 한다.
CACHE_TTL_SEC = 20
LIVE_FETCH_TTL_SEC = 60

REFRESH_OPTIONS = {'끔': None, '30초': 30, '1분': 60, '5분': 300}
QUICK_REFRESH_TIMEOUT_SEC = 300


# --- 테마 (Pretendard + 스위스 인터내셔널 스타일) --------------------------

def inject_theme_css():
    """폰트는 Pretendard, 스타일은 스위스 인터내셔널(그리드형 레이아웃, 각진 모서리,
    그림자 없음, 좌측 정렬, 헤어라인 규칙선)을 따르고 흰색/초록/회색/검정 네 색만 쓴다.
    Streamlit 위젯은 커스텀 CSS 클래스가 없어 data-testid/data-baseweb 속성을 건다."""
    st.markdown(f"""
    <style>
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.css');

    /* Streamlit이 h1~h6/버튼 등에 "Source Sans"를 직접 지정해두므로 전체 요소에
    !important 로 걸어야 실제로 이긴다(상속만으로는 개별 태그 규칙에 밀림).
    단, [data-testid="stIconMaterial"](사이드바 접기 화살표 등 material 아이콘)는 제외해야
    한다 — 아이콘이 리거처 글꼴(Material Symbols)의 특수 글자를 그림으로 그리는 방식이라
    Pretendard로 바꾸면 "keyboard_double_arrow_right" 같은 원본 텍스트가 그대로 보인다. */
    html, body, *:not([data-testid="stIconMaterial"]) {{
        font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, sans-serif !important;
    }}

    /* 전반적으로 글씨를 작게 — rem/em 단위로 잡힌 대부분의 크기가 이 기준값을 따라
    같이 줄어든다(기본 16px -> 13px, 약 80%). */
    html {{
        font-size: 13px;
    }}

    .stApp {{
        background-color: {SWISS_BLACK};
        color: {SWISS_WHITE};
    }}

    /* 스위스 스타일 헤드라인: 굵게, 자간 좁게, 그리드 규칙선으로 구획 */
    h1, h2, h3 {{
        font-weight: 700 !important;
        letter-spacing: -0.02em;
        color: {SWISS_WHITE} !important;
    }}
    h1 {{
        font-size: 1.6rem !important;
        border-bottom: 3px solid {SWISS_WHITE};
        padding-bottom: 0.5rem;
    }}
    h2 {{
        font-size: 1.25rem !important;
        border-bottom: 2px solid {SWISS_WHITE};
        padding-bottom: 0.3rem;
    }}
    h3 {{
        font-size: 1.05rem !important;
    }}

    hr {{
        border: none;
        border-top: 1px solid {SWISS_GRAY_LIGHT};
        margin: 1.5rem 0;
    }}

    /* 그림자·둥근 모서리 제거 — 스위스 스타일은 평면적이고 각져 있다 */
    [data-testid="stMetric"], [data-testid="stExpander"], .stAlert,
    [data-testid="stDataFrame"], div[data-baseweb="select"] > div,
    div[data-baseweb="input"] > div {{
        border-radius: 0 !important;
        box-shadow: none !important;
    }}

    [data-testid="stMetric"] {{
        background-color: {SWISS_BLACK};
        border: 1px solid {SWISS_GRAY_LIGHT};
        padding: 0.75rem 1rem;
    }}
    [data-testid="stMetricLabel"] {{
        color: {SWISS_GRAY};
        text-transform: uppercase;
        font-size: 0.7rem;
        letter-spacing: 0.05em;
    }}
    [data-testid="stMetricValue"] {{
        color: {SWISS_WHITE};
        font-weight: 700;
        font-size: 1.4rem !important;
    }}

    /* 탭 강조색(선택된 탭 글자/밑줄)도 config.toml의 primaryColor(초록)를 그대로 쓴다. */
    [role="tab"] {{
        font-weight: 600;
        color: {SWISS_GRAY};
        font-size: 1.3rem !important;
    }}
    [role="tab"] p {{
        font-size: 1.3rem !important;
    }}

    /* 버튼 — 흰 바탕에 검정 글씨(검정 배경 위에서 도드라지는 스위스 포스터식 반전 블록),
    호버 시 초록(단일 악센트 컬러)으로 바뀐다. */
    .stButton > button, .stDownloadButton > button {{
        background-color: {SWISS_WHITE};
        color: {SWISS_BLACK};
        border: 1px solid {SWISS_WHITE};
        border-radius: 0;
        font-weight: 600;
    }}
    .stButton > button:hover, .stDownloadButton > button:hover {{
        background-color: {SWISS_GREEN};
        border-color: {SWISS_GREEN};
        color: {SWISS_BLACK};
    }}

    /* 라디오/셀렉트 강조색(체크 표시 등)은 config.toml의 primaryColor(초록)를
    Streamlit이 기본적으로 그대로 써서 별도 CSS 없이도 이미 초록으로 나온다. */

    section[data-testid="stSidebar"] {{
        background-color: {SWISS_GRAY_LIGHT};
        border-right: 1px solid {SWISS_GRAY};
    }}

    [data-testid="stCaptionContainer"], .stCaption {{
        color: {SWISS_GRAY} !important;
    }}
    </style>
    """, unsafe_allow_html=True)


# --- 비밀번호 게이트 -------------------------------------------------------

def check_password():
    """DASHBOARD_PASSWORD 가 설정돼 있을 때만 게이트를 건다.
    로컬 개발처럼 비밀번호를 안 정한 환경에서는 그냥 통과시킨다."""
    configured = os.environ.get('DASHBOARD_PASSWORD')
    if not configured:
        return True
    if st.session_state.get('_pw_ok'):
        return True

    def _check():
        st.session_state['_pw_ok'] = st.session_state.get('_pw_input', '') == configured

    st.title('시장 대시보드')
    st.text_input('비밀번호', type='password', key='_pw_input', on_change=_check)
    if '_pw_input' in st.session_state and not st.session_state.get('_pw_ok'):
        st.error('비밀번호가 틀렸습니다.')
    return False


# --- 데이터 로더 (하이브리드: 로컬 CSV 우선, 없으면 라이브 조회) -----------

@st.cache_data(ttl=CACHE_TTL_SEC)
def load_series(filename):
    path = os.path.join(DATA_DIR, filename)
    df = pd.read_csv(path, encoding='utf-8-sig')

    if 'Date' in df.columns:
        date_col = 'Date'
    else:
        date_col = df.columns[0]  # 헤더 없는 첫 컬럼(날짜 인덱스)

    df[date_col] = pd.to_datetime(df[date_col])
    df = df.sort_values(date_col).set_index(date_col)
    df.index.name = 'Date'  # 헤더 없는 컬럼은 pandas가 "Unnamed: 0" 등으로 붙여 Altair 차트에서 콜론이 오작동함
    return df


# pykrx 국내 지수 코드 -> yfinance 폴백 심볼 (KRX가 pykrx/FDR 같은 비공식 스크래핑을 막을 때 씀)
PYKRX_TO_YFINANCE_FALLBACK = {'1001': '^KS11', '2001': '^KQ11'}


def fetch_yfinance_df(ticker, start_dt):
    """yfinance는 단일 종목이어도 (Price, Ticker) 멀티인덱스 컬럼을 반환하므로 정리한다."""
    df = yf.download(ticker, start=start_dt.strftime('%Y-%m-%d'), progress=False)
    if not df.empty and isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index.name = 'Date'
    return df


@st.cache_data(ttl=LIVE_FETCH_TTL_SEC)
def load_series_live(source, code, start_dt):
    """반환값은 (df, 실제로 쓰인 출처 라벨) — 국내 지수는 pykrx를 우선 시도하고
    실패하면 yfinance로 자동 폴백하므로, 캡션에 실제 출처를 보여주려면 라벨도 같이 받아야 한다.
    start_dt 는 정확한 시작 시점(datetime/Timestamp) — RS 비교 탭처럼 "1주일 전부터"
    같은 정확한 기간이 필요한 경우도 있어 연 단위가 아니라 날짜를 직접 받는다."""
    if source == 'pykrx':
        try:
            df = pykrx_stock.get_index_ohlcv_by_date(
                start_dt.strftime('%Y%m%d'), datetime.now().strftime('%Y%m%d'), code)
            df = df.rename(columns={'시가': 'Open', '고가': 'High', '저가': 'Low', '종가': 'Close', '거래량': 'Volume'})
            df.index.name = 'Date'
            if not df.empty:
                return df, 'pykrx (KRX)'
        except Exception:
            pass
        fallback_ticker = PYKRX_TO_YFINANCE_FALLBACK[code]
        return fetch_yfinance_df(fallback_ticker, start_dt), 'yfinance (pykrx 실패, 자동 폴백)'

    return fetch_yfinance_df(code, start_dt), 'yfinance'


def get_series(filename, source, code, years):
    """반환값은 (df, 라이브 조회 시 실제로 쓰인 출처 라벨 또는 로컬 CSV면 None)."""
    path = os.path.join(DATA_DIR, filename)
    if os.path.exists(path):
        return load_series(filename), None
    start_dt = datetime.now() - pd.DateOffset(years=years)
    return load_series_live(source, code, start_dt)


@st.cache_data(ttl=CACHE_TTL_SEC)
def load_csv(path):
    return pd.read_csv(path, encoding='utf-8-sig')


@st.cache_data(ttl=LIVE_FETCH_TTL_SEC)
def fetch_indicators_live():
    rows = fetch_indicators.fetch_all_indicators()
    fetched_at = datetime.now(timezone.utc).isoformat()
    for row in rows:
        row['fetched_at'] = fetched_at
    return pd.DataFrame(rows)


def get_indicators_df():
    if os.path.exists(INDICATORS_CSV):
        return load_csv(INDICATORS_CSV)
    return fetch_indicators_live()


def get_holdings_from_secrets():
    """클라우드 배포본용 — portfolio.csv(개인정보라 저장소에 커밋하지 않음) 대신
    Streamlit Secrets 의 [[portfolio]] 배열에서 보유 종목을 읽는다.
    형식은 .streamlit/secrets.toml.example 참고."""
    try:
        rows = st.secrets.get('portfolio')
    except Exception:
        rows = None
    if not rows:
        return None

    df = pd.DataFrame(rows)
    df['ticker'] = df['ticker'].astype(str).str.strip()
    df['quantity'] = pd.to_numeric(df['quantity'])
    df['avg_price'] = pd.to_numeric(df['avg_price'])
    return df


# PER은 하루 안에 거의 안 바뀌고, 시장 전체 종목의 PER/업종을 한 번에 받아오는
# 무거운 호출이라 지수/시세보다 길게 캐시한다. 다만 실패(빈 결과)도 그대로 캐시
# 되는 게 st.cache_data의 기본 동작이라, 로그인/네트워크 문제가 막 해결된 직후처럼
# "예전의 빈 실패 결과"가 한동안 눌러앉는 상황을 줄이려고 1시간보다는 짧게 잡는다.
PER_TTL_SEC = 600
PER_LOOKBACK_DAYS = 5

# DART(전자공시) 데이터는 분기/연 단위로만 바뀌므로 하루 단위로 길게 캐시한다.
DART_TTL_SEC = 24 * 60 * 60


@st.cache_data(ttl=CACHE_TTL_SEC)
def load_dart_snapshot():
    """collect_dart.py가 로컬에서 만들어 커밋해둔 스냅샷 — 있으면 이걸 최우선으로
    쓰고(클라우드에서 DART/pykrx 라이브 조회가 막혀 있어도 동작), 없으면(로컬에서
    아직 한 번도 안 돌렸을 때 등) 호출부가 기존 라이브 조회로 대체한다."""
    if not os.path.exists(DART_SNAPSHOT_PATH):
        return {}
    try:
        with open(DART_SNAPSHOT_PATH, encoding='utf-8') as f:
            return json.load(f).get('stocks', {})
    except Exception:
        return {}


@st.cache_data(ttl=CACHE_TTL_SEC)
def load_dart_snapshot_generated_at():
    """스냅샷 캡션에 쓸 생성 시각 — 없거나 못 읽으면 None."""
    if not os.path.exists(DART_SNAPSHOT_PATH):
        return None
    try:
        with open(DART_SNAPSHOT_PATH, encoding='utf-8') as f:
            return json.load(f).get('generated_at')
    except Exception:
        return None


@st.cache_data(ttl=DART_TTL_SEC)
def fetch_dart_corp_map():
    """종목코드 -> DART corp_code 매핑(전체 상장사, 수 MB짜리 벌크 조회) — 자주 안
    바뀌므로 하루 캐시. OPENDART_API_KEY가 없으면 빈 딕셔너리를 반환한다."""
    return fetch_dart.fetch_corp_code_map()


@st.cache_data(ttl=DART_TTL_SEC)
def fetch_dart_eps_cached(ticker):
    """DART 사업보고서 기준 EPS(원)를 구한다. 반환: (eps, fs_div, bsns_year) —
    못 찾으면 (None, None, None). 올해 사업보고서가 아직 안 나왔을 수 있어 최근
    연도부터 2개까지 시도한다."""
    corp_code = fetch_dart_corp_map().get(ticker)
    if not corp_code:
        return None, None, None
    this_year = datetime.now().year
    for bsns_year in (this_year - 1, this_year - 2):
        eps, fs_div = fetch_dart.fetch_eps(corp_code, str(bsns_year))
        if eps is not None:
            return eps, fs_div, bsns_year
    return None, None, None


@st.cache_data(ttl=DART_TTL_SEC)
def fetch_dart_financials_cached(ticker):
    """종목 상세 패널의 연도별 보기용 — 최근 최대 9개년치 매출액·영업이익(억원)을
    구한다. 반환: (연도 오름차순 리스트, fs_div) — 못 찾으면 ([], None)."""
    corp_code = fetch_dart_corp_map().get(ticker)
    if not corp_code:
        return [], None
    this_year = datetime.now().year
    bsns_years = [str(this_year - 1), str(this_year - 4), str(this_year - 7)]
    return fetch_dart.fetch_annual_financials(corp_code, bsns_years)


@st.cache_data(ttl=DART_TTL_SEC)
def fetch_dart_quarterly_financials_cached(ticker):
    """종목 상세 패널의 분기별 보기용 — 최근 최대 8개 분기 매출액·영업이익(억원)을
    구한다. 반환: (분기 오름차순 리스트, fs_div) — 못 찾으면 ([], None)."""
    corp_code = fetch_dart_corp_map().get(ticker)
    if not corp_code:
        return [], None
    this_year = datetime.now().year
    items, fs_div = fetch_dart.fetch_quarterly_financials(corp_code, [this_year, this_year - 1])
    return items[-8:], fs_div


@st.cache_data(ttl=PER_TTL_SEC)
def fetch_per_universe(market):
    """market('KOSPI'/'KOSDAQ') 전체 종목의 PER·업종명을 한 번에 받아온다 — 종목별로
    따로 부르지 않고 시장 전체를 한 번에 받아 "업계 PER"(같은 업종 종목들의 PER
    중앙값) 계산에 쓴다. 티커를 인덱스로 하는 DataFrame(PER, 업종명)을 반환하고,
    최근 며칠 안에 거래일 데이터가 없으면(휴장 등) 빈 DataFrame을 반환한다."""
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


def attach_per_columns(stocks):
    """보유 종목 각각에 자기 PER과 "업계 PER"를 붙인다.
    - 자기 PER: collect_dart.py가 만들어둔 로컬 스냅샷의 EPS가 있으면 그걸 오늘
      현재가와 결합해 계산한다(스냅샷 EPS는 분기/연 단위라 오래돼도 되지만, 가격은
      항상 최신을 쓴다). 스냅샷에 없으면 라이브 DART 조회, 그것도 안 되면 pykrx
      자체 PER 순으로 대체한다.
    - 업계 PER: 같은 시장·같은 업종 내 다른 종목들의 PER 중앙값(적자로 PER이 의미
      없는 0 이하 값은 제외) — 스냅샷에 미리 계산돼 있으면 그걸 쓰고, 없으면 pykrx
      시장 전체 데이터를 라이브로 받아 계산한다.
    스냅샷과 라이브 조회 둘 다 실패하면 조용히 "-"(None)로 남긴다 — 표에서 이 함수
    호출 자체가 실패해도(pykrx 장애 등) 호출부에서 try/except로 감싸 나머지 표는
    그대로 보여준다."""
    stocks = stocks.copy()
    snapshot = load_dart_snapshot()
    per_vals, industry_names, industry_pers = [], [], []
    universes = {}

    for _, row in stocks.iterrows():
        market = row.get('market')
        ticker = row['ticker']
        current_price = row.get('current_price')
        snap = snapshot.get(ticker)

        eps = snap.get('eps') if snap else None
        if eps is None and (not snap or 'eps' not in snap):
            try:
                eps, _, _ = fetch_dart_eps_cached(ticker)
            except Exception:
                # DART 쪽이 죽어도(네트워크 장애 등) 아래 pykrx 기반 PER·업계 PER
                # 계산까지 같이 죽지 않게 여기서 막는다.
                eps = None

        need_universe = eps is None or not snap or snap.get('industry_per') is None
        universe = None
        if need_universe and market in ('KOSPI', 'KOSDAQ'):
            if market not in universes:
                universes[market] = fetch_per_universe(market)
            universe = universes[market]

        if eps is not None:
            stock_per = current_price / eps if eps > 0 and current_price else None
        elif universe is not None and not universe.empty and ticker in universe.index:
            raw_per = universe.loc[ticker, 'PER']
            stock_per = raw_per if raw_per and raw_per > 0 else None
        else:
            stock_per = None
        per_vals.append(stock_per)

        if snap and snap.get('industry_per') is not None:
            industry_names.append(snap.get('industry_name'))
            industry_pers.append(snap.get('industry_per'))
        elif universe is not None and not universe.empty and ticker in universe.index:
            industry = universe.loc[ticker, '업종명']
            peers = universe[
                (universe['업종명'] == industry) & (universe.index != ticker) & (universe['PER'] > 0)
            ]
            industry_names.append(industry)
            industry_pers.append(peers['PER'].median() if not peers.empty else None)
        else:
            industry_names.append(None)
            industry_pers.append(None)

    stocks['per'] = per_vals
    stocks['industry_name'] = industry_names
    stocks['industry_per'] = industry_pers
    return stocks


@st.cache_data(ttl=LIVE_FETCH_TTL_SEC)
def fetch_portfolio_live():
    holdings = get_holdings_from_secrets()  # 없으면 fetch_portfolio.py가 로컬 portfolio.csv로 폴백
    rows = fetch_portfolio.compute_portfolio_rows(holdings)
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values('eval_amount', ascending=False)
    df['fetched_at'] = datetime.now(timezone.utc).isoformat()
    return df


def get_portfolio_df():
    if os.path.exists(PORTFOLIO_CSV):
        return load_csv(PORTFOLIO_CSV)
    try:
        return fetch_portfolio_live()
    except FileNotFoundError:
        return None  # Secrets도 없고 로컬 portfolio.csv도 없는 경우


@st.cache_data(ttl=LIVE_FETCH_TTL_SEC)
def fetch_stock_series(ticker, start_dt):
    """RS 비교 탭용 — 보유 종목 개별 시계열(FDR, 지수 라이브 조회와 별개 경로)."""
    return fetch_portfolio.fetch_price_history(ticker, start_dt.strftime('%Y-%m-%d'))


@st.cache_data(ttl=LIVE_FETCH_TTL_SEC)
def fetch_news_live():
    articles = fetch_news.fetch_recent_news()
    df = pd.DataFrame(articles)
    if not df.empty:
        df['published_at'] = pd.to_datetime(df['published_at'], utc=True)
    return df


def get_news_df():
    if os.path.exists(NEWS_CSV):
        df = load_csv(NEWS_CSV)
        if not df.empty:
            df['published_at'] = pd.to_datetime(df['published_at'], utc=True)
        return df
    return fetch_news_live()


def file_caption(path, source):
    """데이터 패널 하단에 붙일 '갱신 시각 + 출처' 캡션.
    로컬 CSV가 있으면 파일 수정 시각, 없어서 라이브 조회했으면 '지금'으로 표시한다."""
    if not os.path.exists(path):
        return f"갱신 시각: 방금 (라이브 조회) · 출처: {source}"
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    return f"갱신 시각: {mtime.strftime('%Y-%m-%d %H:%M:%S')} · 출처: {source}"


# --- 패널 ----------------------------------------------------------------

def render_index_cards(series_list, title):
    st.subheader(title)
    cols = st.columns(len(series_list))
    for col, (label, filename, source, code) in zip(cols, series_list):
        with col:
            try:
                df, _ = get_series(filename, source, code, CARD_LOOKBACK_YEARS)
                last_two = df['Close'].tail(2)
                current = last_two.iloc[-1]
                delta = current - last_two.iloc[-2] if len(last_two) == 2 else None
                if delta is None:
                    delta_text = None
                else:
                    pct = delta / (current - delta) * 100 if current != delta else 0
                    delta_text = f"{delta:,.2f} ({pct:+.2f}%)"
                st.metric(label, f"{current:,.2f}", delta=delta_text, delta_color='inverse')
            except Exception as e:
                st.metric(label, '-')
                st.caption(f"로드 실패: {e}")
    ref_filename = series_list[0][1]
    st.caption(file_caption(os.path.join(DATA_DIR, ref_filename), 'pykrx / yfinance') + ' · 전일 대비')


def render_macro_panel():
    st.subheader('거시 지표')
    df = get_indicators_df()
    if df.empty:
        st.info('표시할 거시 지표가 없습니다. ECOS_API_KEY / FRED_API_KEY 설정을 확인해 주세요.')
        return

    cols = st.columns(len(df))
    for col, (_, row) in zip(cols, df.iterrows()):
        with col:
            try:
                value_text = f"{float(row['value']):.2f}"  # FRED는 소수점을 길게 주는 경우가 있어 2자리로 정리
            except (TypeError, ValueError):
                value_text = row['value']
            st.metric(row['label'], value_text)
            st.caption(f"기준일 {row['as_of']} · {row['source']}")

    fetched_at = df['fetched_at'].iloc[0]
    st.caption(f"갱신 시각: {fetched_at} · 출처: ECOS / FRED")


def style_portfolio(df):
    """손익 관련 컬럼만 상승 빨강 / 하락 파랑으로 칠한다."""
    signed_cols = ['전일대비(%)', '평가손익', '수익률(%)']

    def color(v):
        if pd.isna(v) or v == 0:
            return ''
        return f"color: {UP_COLOR}" if v > 0 else f"color: {DOWN_COLOR}"

    return (
        df.style
        .map(color, subset=signed_cols)
        .format({
            '현재가': '{:,.0f}',
            '전일대비(%)': '{:+.2f}',
            '수량': '{:,.0f}',
            '평단가': '{:,.0f}',
            '매입금액': '{:,.0f}',
            '평가금액': '{:,.0f}',
            '평가손익': '{:+,.0f}',
            '수익률(%)': '{:+.2f}',
            '비중(%)': '{:.1f}',
            'PER': '{:.2f}',
            '업계 PER': '{:.2f}',
        }, na_rep='-')
    )


def render_portfolio_panel():
    st.subheader('보유 종목 현황')
    df = get_portfolio_df()
    if df is None:
        st.warning('portfolio.csv 파일이 없습니다. 보유 종목(티커/수량/평단가)을 채워주세요.')
        return
    if df.empty:
        st.info('보유 종목이 없습니다. portfolio.csv 를 채워주세요.')
        return

    stocks = df[df['ticker'] != CASH_TICKER]
    cash = df[df['ticker'] == CASH_TICKER]['eval_amount'].sum()

    try:
        stocks = attach_per_columns(stocks)
    except Exception:
        # PER 조회(pykrx)가 막혀도 나머지 보유 종목 표는 그대로 보여준다.
        stocks = stocks.assign(per=None, industry_name=None, industry_per=None)

    buy_total = stocks['buy_amount'].sum()
    eval_total = stocks['eval_amount'].sum()
    profit = eval_total - buy_total
    profit_pct = profit / buy_total * 100 if buy_total else 0.0

    # 전일 대비 하루 손익 — 현재가와 등락률로 역산한 전일 종가 기준
    prev_eval = (stocks['eval_amount'] / (1 + stocks['change_pct'] / 100)).sum()
    day_profit = eval_total - prev_eval

    cols = st.columns(5)
    cols[0].metric('총자산(주식+현금)', f"{eval_total + cash:,.0f}원")
    cols[1].metric('주식 평가금액', f"{eval_total:,.0f}원",
                   delta=f"{day_profit:+,.0f}원 (당일)", delta_color='inverse')
    cols[2].metric('주식 매입금액', f"{buy_total:,.0f}원")
    cols[3].metric('평가손익', f"{profit:+,.0f}원", delta=f"{profit_pct:+.2f}%", delta_color='inverse')
    cols[4].metric('현금(CMA/RP)', f"{cash:,.0f}원")

    table = stocks.rename(columns={
        'name': '종목명',
        'market': '시장',
        'current_price': '현재가',
        'change_pct': '전일대비(%)',
        'quantity': '수량',
        'avg_price': '평단가',
        'buy_amount': '매입금액',
        'eval_amount': '평가금액',
        'profit': '평가손익',
        'profit_pct': '수익률(%)',
        'weight_pct': '비중(%)',
        'per': 'PER',
        'industry_name': '업종',
        'industry_per': '업계 PER',
    })[['종목명', '시장', '현재가', '전일대비(%)', '수량', '평단가',
        '매입금액', '평가금액', '평가손익', '수익률(%)', '비중(%)',
        'PER', '업종', '업계 PER']]

    # 목록 조회가 429로 막혀 개별 조회로 받아오면 시장 구분이 비어 있다.
    table['시장'] = table['시장'].fillna('-').replace('', '-')
    table['업종'] = table['업종'].fillna('-')

    st.dataframe(style_portfolio(table), width='stretch', hide_index=True)

    top = table.iloc[0]
    if top['비중(%)'] >= 40:
        st.warning(
            f"집중도 경고: {top['종목명']} 한 종목이 주식 평가금액의 {top['비중(%)']:.1f}%를 차지합니다. "
            '해당 종목의 변동이 계좌 전체 손익을 좌우하는 구조입니다.'
        )

    st.caption(file_caption(PORTFOLIO_CSV, 'FinanceDataReader (KRX 종가/등락률)') +
               ' · 비중은 현금 제외 주식 평가금액 대비')
    st.caption(
        'PER은 DART 전자공시(사업보고서) EPS 기준 직접 확인한 값에 오늘 현재가를 나눈 '
        '것이고(순손실 종목은 PER 산정 불가로 "-"), 업계 PER은 pykrx 기준 같은 시장'
        '(코스피/코스닥)·같은 업종 내 다른 종목들의 PER 중앙값(적자 종목 제외)입니다. '
        'ETF·시장 구분이 없는 종목은 둘 다 "-"로 표시됩니다.'
    )


def _build_revenue_oi_chart(items, x_labels):
    """매출액·영업이익은 그룹 막대(회색/초록), 영업이익률은 흰색 꺾은선(보조축)으로
    겹쳐 그린다. items는 x_labels와 순서가 1:1로 대응하는 {'revenue':, 'operating_income':} 리스트."""
    bar_rows, margin_rows = [], []
    for label, it in zip(x_labels, items):
        revenue = it.get('revenue')
        op_income = it.get('operating_income')
        if revenue is not None:
            bar_rows.append({'구간': label, '항목': '매출액', '금액(억원)': revenue})
        if op_income is not None:
            bar_rows.append({'구간': label, '항목': '영업이익', '금액(억원)': op_income})
        if revenue and op_income is not None:
            margin_rows.append({'구간': label, '영업이익률(%)': op_income / revenue * 100})

    bars = (
        alt.Chart(pd.DataFrame(bar_rows))
        .mark_bar()
        .encode(
            x=alt.X('구간:N', title=None, sort=x_labels),
            xOffset=alt.XOffset('항목:N'),
            y=alt.Y('금액(억원):Q', title='억원'),
            color=alt.Color(
                '항목:N', title=None,
                scale=alt.Scale(domain=['매출액', '영업이익'], range=[SWISS_GRAY, SWISS_GREEN]),
            ),
            tooltip=[alt.Tooltip('구간:N'), alt.Tooltip('항목:N'), alt.Tooltip('금액(억원):Q', format=',.1f')],
        )
    )
    line = (
        alt.Chart(pd.DataFrame(margin_rows))
        .mark_line(color=SWISS_WHITE, point=True)
        .encode(
            x=alt.X('구간:N', title=None, sort=x_labels),
            y=alt.Y('영업이익률(%):Q', title='영업이익률(%)'),
            tooltip=[alt.Tooltip('구간:N'), alt.Tooltip('영업이익률(%):Q', format='+.1f')],
        )
    )
    return alt.layer(bars, line).resolve_scale(y='independent').properties(height=350)


def render_stock_detail_panel():
    """보유 종목 하나를 골라 매출·영업이익(막대)·영업이익률(꺾은선)을 연도별/분기별로
    골라보고, 아래에서 주가·연간 영업이익 추이도 함께 본다. collect_dart.py가
    로컬에서 만들어 커밋해둔 스냅샷을 최우선으로 쓰고, 없으면 DART Open API
    (OPENDART_API_KEY)로 실시간 조회한다(단, 이 호스팅 환경에서는 DART 접속이
    막혀 있을 수 있음)."""
    st.subheader('종목 상세')

    portfolio_df = get_portfolio_df()
    if portfolio_df is None or portfolio_df.empty:
        st.info('보유 종목이 없어 상세 재무를 볼 수 없습니다.')
        return

    holdings = portfolio_df[portfolio_df['ticker'] != CASH_TICKER]
    if holdings.empty:
        st.info('보유 종목이 없어 상세 재무를 볼 수 없습니다.')
        return

    selected_name = st.selectbox('상세히 볼 종목', holdings['name'].tolist(), key='detail_stock')
    ticker = str(holdings.loc[holdings['name'] == selected_name, 'ticker'].iloc[0])

    # collect_dart.py가 로컬에서 만들어 커밋해둔 스냅샷을 최우선으로 쓴다 — 클라우드
    # 에서 DART 라이브 조회가 막혀 있어도(2026-08 확인) 이 파일만 있으면 동작한다.
    snapshot = load_dart_snapshot()
    snap = snapshot.get(ticker)
    snapshot_label = f"스냅샷({load_dart_snapshot_generated_at() or '?'} 기준, collect_dart.py)"

    # 연도별 데이터는 아래 "주가 vs 연간 영업이익" 그래프에도 그대로 쓰이므로
    # 기간 단위 선택과 무관하게 항상 먼저 받아둔다.
    if snap and snap.get('financials'):
        annual_years, annual_fs_div = snap['financials'], snap.get('financials_fs_div')
        annual_source = snapshot_label
    elif os.environ.get('OPENDART_API_KEY'):
        with st.spinner('DART에서 재무 데이터를 불러오는 중...'):
            annual_years, annual_fs_div = fetch_dart_financials_cached(ticker)
        annual_source = 'DART 사업보고서 (자동 조회, 하루 캐시)'
    else:
        annual_years, annual_fs_div, annual_source = [], None, None

    if not annual_years:
        st.info(
            f'{selected_name}의 DART 재무 데이터를 찾지 못했습니다. '
            'ETF·비상장·최근 상장 종목이라 데이터가 없거나, 로컬 스냅샷에 없고 '
            '이 서버에서 DART로의 접속 자체가 막혀 있을 수 있습니다(호스팅 환경에 '
            '따라 opendart.fss.or.kr 접속이 차단되는 경우가 있습니다).'
        )
        return

    period_type = st.radio('기간 단위', ['연도별', '분기별'], horizontal=True, key='detail_period_type')

    if period_type == '연도별':
        items = annual_years[-5:]
        x_labels = [str(y['year']) for y in items]
        source_note, fs_div = annual_source, annual_fs_div
    else:
        if snap and snap.get('quarterly'):
            items = snap['quarterly']
            source_note, fs_div = snapshot_label, snap.get('quarterly_fs_div')
        elif os.environ.get('OPENDART_API_KEY'):
            with st.spinner('DART에서 분기 재무 데이터를 불러오는 중...'):
                items, fs_div = fetch_dart_quarterly_financials_cached(ticker)
            source_note = 'DART 분기/반기/사업보고서 (자동 조회, 하루 캐시)'
        else:
            items, fs_div, source_note = [], None, None
        x_labels = [f"{it['year']}년 {it['quarter']}분기" for it in items]

    if not items:
        st.info(f'{selected_name}의 {period_type} 재무 데이터를 찾지 못했습니다.')
        return

    st.altair_chart(_build_revenue_oi_chart(items, x_labels), use_container_width=True)

    partial_notes = [f"{it['year']}년: {it['partial']}" for it in items if it.get('partial')]
    caption = (
        f'출처: {source_note} · {fs_div or ""} · '
        '매출액·영업이익(막대, 왼쪽 축) / 영업이익률(흰 선, 오른쪽 축)'
    )
    if partial_notes:
        caption += ' · 부분 실적 ' + ', '.join(partial_notes)
    if period_type == '분기별':
        caption += (
            ' · 분기 실적은 누적 보고서(반기/3분기/사업보고서)에서 앞 분기 누적치를 '
            '뺀 값이라, 비교기간 재무 정정 등으로 드물게 음수가 나올 수 있습니다.'
        )
    st.caption(caption)

    # --- 아래: 주가 vs 연간 영업이익 (기간 단위 선택과 무관하게 항상 연도별) ---
    # 부분 실적 연도(설립 첫해 등)는 12개월치가 아니라 추이·비교를 왜곡하므로
    # 그래프에서는 제외하고, 정상 연도만으로 최근 7개년을 그린다.
    full_years = [y for y in annual_years if not y.get('partial')]
    if len(full_years) < 2:
        st.info('정상 실적 연도가 2개 미만이라 추이 그래프를 그릴 수 없습니다.')
        return
    chart_years = full_years[-7:]
    start_dt = pd.Timestamp(year=chart_years[0]['year'], month=1, day=1)

    try:
        with st.spinner('주가 데이터 불러오는 중...'):
            price_df = fetch_stock_series(ticker, start_dt)
        if price_df.empty:
            st.warning('주가 데이터를 불러오지 못했습니다.')
            return

        price_line = price_df[['Close']].reset_index()
        oi_df = pd.DataFrame([
            {'Date': pd.Timestamp(year=y['year'], month=12, day=31), '영업이익': y['operating_income']}
            for y in chart_years
        ])

        price_chart = (
            alt.Chart(price_line)
            .mark_line(color=NEUTRAL_CHART_COLOR)
            .encode(
                x=alt.X('Date:T', title=None),
                y=alt.Y('Close:Q', title='주가(원)', scale=alt.Scale(zero=False)),
                tooltip=[alt.Tooltip('Date:T'), alt.Tooltip('Close:Q', title='주가', format=',.0f')],
            )
        )
        oi_chart = (
            alt.Chart(oi_df)
            .mark_line(color=SWISS_GREEN, point=True)
            .encode(
                x=alt.X('Date:T', title=None),
                y=alt.Y('영업이익:Q', title='영업이익(억원)', scale=alt.Scale(zero=False)),
                tooltip=[alt.Tooltip('Date:T', title='연도'), alt.Tooltip('영업이익:Q', format=',.1f')],
            )
        )
        combo = (
            alt.layer(price_chart, oi_chart)
            .resolve_scale(y='independent')
            .properties(height=320)
            .interactive()
        )
        st.altair_chart(combo, use_container_width=True)
        st.caption(
            f'{selected_name} 주가(흰색, 왼쪽 축)와 연간 영업이익(초록, 오른쪽 축 · 각 연말 시점에 표시) '
            f'— {chart_years[0]["year"]}~{chart_years[-1]["year"]}년. 두 값의 단위가 달라(원 vs 억원) '
            '지수화 대신 서로 다른 축으로 함께 표시했습니다.'
        )
    except Exception as e:
        st.warning(f'그래프를 그리지 못했습니다: {e}')


# 차트를 세로로 쌓지 않고 2열 그리드로 배치할 때 한 칸에 넣을 높이(px) — 가로 폭이
# 절반으로 줄어드는 만큼 세로도 같이 줄여야 차트 비율이 과하게 길쭉해지지 않는다.
CHART_GRID_HEIGHT = 260


def render_index_charts(series_list):
    """지수/환율 차트를 가로 2개씩 2xN 그리드로 배치한다."""
    for i in range(0, len(series_list), 2):
        cols = st.columns(2)
        for col, series in zip(cols, series_list[i:i + 2]):
            with col:
                _render_one_index_chart(series)


def _render_one_index_chart(series):
    label, filename, source, code = series
    st.subheader(label)
    selected_period = st.radio(
        f'{label} 기간', list(CHART_PERIODS),
        index=len(CHART_PERIODS) - 1,  # 기본값 '전체' — 이전까지의 동작을 그대로 유지
        horizontal=True, key=f'chart_period_{filename}', label_visibility='collapsed',
    )
    live_label = None
    try:
        df, live_label = get_series(filename, source, code, CHART_YEARS)
        if df.empty:
            raise ValueError('데이터 소스가 빈 결과를 반환했습니다')
        period_start = CHART_PERIODS[selected_period]
        if period_start is not None:
            df = df[df.index >= period_start(pd.Timestamp.now()).normalize()]
            if df.empty:
                raise ValueError('선택한 기간에 해당하는 데이터가 없습니다')
        chart_df = df[['Close']].reset_index()
        chart = (
            alt.Chart(chart_df)
            .mark_line(color=NEUTRAL_CHART_COLOR)
            .encode(
                x=alt.X('Date:T', title=None),
                y=alt.Y('Close:Q', title=None, scale=alt.Scale(zero=False)),
                tooltip=[alt.Tooltip('Date:T'), alt.Tooltip('Close:Q', format=',.2f')],
            )
            .properties(height=CHART_GRID_HEIGHT)
            .interactive()  # 스크롤 확대/축소 + 드래그 이동을 활성화한다
        )
        st.altair_chart(chart, use_container_width=True)
    except Exception as e:
        st.warning(f"차트를 불러오지 못했습니다: {e}")
    # live_label 은 라이브 조회 시 실제로 쓰인 출처(폴백 포함)를 반영한다.
    # 로컬 CSV를 읽었을 때는 None 이므로 설정된 기본 출처로 표시한다.
    source_label = live_label or ('pykrx (KRX)' if source == 'pykrx' else 'yfinance')
    st.caption(file_caption(os.path.join(DATA_DIR, filename), source_label))


def render_rs_tab():
    """보유 종목이 선택한 기간·기초지수 대비 상대적으로 잘했는지(RS, 상대강도)를 본다.
    RS = (종목 정규화 지수 / 벤치마크 정규화 지수) x 100 — 시작일을 100으로 맞추고,
    RS 선이 100보다 위면 벤치마크 대비 아웃퍼폼, 아래면 언더퍼폼이라는 뜻이다."""
    st.subheader('RS 비교 (보유 종목 vs 지수)')

    portfolio_df = get_portfolio_df()
    if portfolio_df is None or portfolio_df.empty:
        st.info('보유 종목이 없어 RS 비교를 할 수 없습니다. portfolio.csv 를 채워주세요.')
        return
    holdings = portfolio_df[portfolio_df['ticker'] != CASH_TICKER]
    if holdings.empty:
        st.info('보유 종목이 없어 RS 비교를 할 수 없습니다.')
        return

    col1, col2, col3 = st.columns(3)
    with col1:
        selected_name = st.selectbox('보유 종목', holdings['name'].tolist(), key='rs_stock')
    with col2:
        selected_period = st.selectbox('기간', list(RS_PERIODS), index=3, key='rs_period')
    with col3:
        selected_benchmark = st.selectbox('기초지수', list(RS_BENCHMARKS), key='rs_benchmark')

    ticker = str(holdings.loc[holdings['name'] == selected_name, 'ticker'].iloc[0])
    now = pd.Timestamp.now()
    start_dt = RS_PERIODS[selected_period](now)
    bench_source, bench_code = RS_BENCHMARKS[selected_benchmark]

    try:
        with st.spinner('RS 계산 중...'):
            stock_df = fetch_stock_series(ticker, start_dt)
            bench_df, _ = load_series_live(bench_source, bench_code, start_dt)

        if stock_df.empty or bench_df.empty:
            st.warning('선택한 종목 또는 지수의 데이터를 불러오지 못했습니다.')
            return

        merged = pd.DataFrame({'stock': stock_df['Close'], 'bench': bench_df['Close']}).dropna()
        merged = merged[merged.index >= start_dt.normalize()]
        if len(merged) < 2:
            st.warning('선택한 기간에 비교할 데이터가 부족합니다. 더 긴 기간을 선택해 보세요.')
            return

        stock_norm = merged['stock'] / merged['stock'].iloc[0] * 100
        bench_norm = merged['bench'] / merged['bench'].iloc[0] * 100
        rs = (stock_norm / bench_norm * 100).rename('RS')

        stock_return = stock_norm.iloc[-1] - 100
        bench_return = bench_norm.iloc[-1] - 100
        outperformance = stock_return - bench_return

        c1, c2, c3 = st.columns(3)
        c1.metric(f'{selected_name} 수익률', f'{stock_return:+.2f}%')
        c2.metric(f'{selected_benchmark} 수익률', f'{bench_return:+.2f}%')
        c3.metric(
            '상대 성과',
            f'{outperformance:+.2f}%p',
            delta='아웃퍼폼' if outperformance > 0 else ('언더퍼폼' if outperformance < 0 else '동일'),
            delta_color='off',
        )

        chart_df = rs.reset_index()
        chart_df.columns = ['Date', 'RS']
        rs_color = UP_COLOR if outperformance >= 0 else DOWN_COLOR
        baseline = (
            alt.Chart(pd.DataFrame({'y': [100]}))
            .mark_rule(strokeDash=[4, 4], color=SWISS_GRAY)
            .encode(y='y:Q')
        )
        rs_line = (
            alt.Chart(chart_df)
            .mark_line(color=rs_color)
            .encode(
                x=alt.X('Date:T', title=None),
                y=alt.Y('RS:Q', title=None, scale=alt.Scale(zero=False)),
                tooltip=[alt.Tooltip('Date:T'), alt.Tooltip('RS:Q', format=',.2f')],
            )
            .interactive()
        )
        st.altair_chart(baseline + rs_line, use_container_width=True)
        st.caption(
            f'{selected_name} vs {selected_benchmark} · 시작일 = 100 기준 RS. '
            '100보다 위에 있으면 지수보다 잘한 것, 아래면 못한 것이다.'
        )
    except Exception as e:
        st.warning(f'RS를 계산하지 못했습니다: {e}')


# 같은 종목을 다시 입력했을 때 매번 유료 API를 다시 호출하지 않도록 길게 캐시한다.
STOCK_OPINION_TTL_SEC = 6 * 60 * 60


@st.cache_data(ttl=STOCK_OPINION_TTL_SEC, show_spinner=False)
def fetch_stock_opinion(company_name):
    return stock_opinion.generate_opinion(company_name)


def render_stock_opinion_tab():
    st.subheader('종목 분석')
    st.caption(
        '월스트리트 시니어 애널리스트 프레임워크(Narrative → Reverse DCF → DCF → Comps → '
        '민감도)로 자동 분석합니다. 웹 검색을 포함해 1~5분 정도 걸릴 수 있습니다.'
    )

    if not os.environ.get('OPENAI_API_KEY'):
        st.info('OPENAI_API_KEY 가 설정되어 있지 않아 이 탭을 쓸 수 없습니다. Secrets에 키를 추가해 주세요.')
        return

    company_name = st.text_input(
        '종목명을 입력하세요', key='stock_opinion_input', placeholder='예: 삼성전자, Apple, 카카오',
    )
    run = st.button('분석하기', key='stock_opinion_run')

    if run:
        name = company_name.strip()
        if not name:
            st.warning('종목명을 입력해 주세요.')
        else:
            with st.spinner(f'{name} 분석 중입니다... (웹 검색 포함, 1~5분 소요될 수 있습니다)'):
                text, error = fetch_stock_opinion(name)
            if error:
                st.warning(f'분석에 실패했습니다: {error}')
            else:
                st.session_state['stock_opinion_result'] = (name, text)

    # 버튼을 누른 리런이 아니어도(다른 탭 조작 등으로 전체가 다시 그려질 때) 마지막
    # 분석 결과가 사라지지 않도록 세션 상태에 저장해두고 매번 다시 그린다.
    result = st.session_state.get('stock_opinion_result')
    if result:
        name, text = result
        st.markdown(f'##### {name} 분석 결과')
        st.markdown(
            f'<div style="white-space: pre-wrap; line-height: 1.7;">{html.escape(text)}</div>',
            unsafe_allow_html=True,
        )
        st.caption(
            f'생성 시각: 방금 (같은 종목명은 최대 {STOCK_OPINION_TTL_SEC // 3600}시간 캐시) · '
            f'출처: OpenAI API ({stock_opinion.OPENAI_MODEL} + 웹 검색) · '
            '투자 판단 참고용이며 투자 책임은 본인에게 있습니다.'
        )


def humanize_age(published_at, now):
    delta = now - published_at
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)}분 전"
    return f"{minutes // 60}시간 전"


def render_news_list(df):
    if df.empty:
        st.info('24시간 이내 수집된 뉴스가 없습니다.')
        return
    now = datetime.now(timezone.utc)
    lines = []
    for _, row in df.head(NEWS_LIMIT).iterrows():
        age = humanize_age(row['published_at'], now)
        lines.append(f"- [{row['title']}]({row['link']}) — `{age}` · {row['source']}")
    st.markdown('\n'.join(lines))


def render_news_panel():
    st.subheader('주요 뉴스 (24시간 이내)')
    df = get_news_df()
    if df.empty:
        st.info('표시할 뉴스가 없습니다.')
        return

    df = df.sort_values('published_at', ascending=False)

    # region 컬럼이 없는 옛 news.csv 도 깨지지 않게 기본값을 채운다.
    if 'region' not in df.columns:
        df['region'] = '국내'

    domestic = df[df['region'] == '국내']
    overseas = df[df['region'] == '해외']

    tab_kr, tab_us = st.tabs([f'국내 ({len(domestic)})', f'해외 ({len(overseas)})'])
    with tab_kr:
        render_news_list(domestic)
    with tab_us:
        render_news_list(overseas)

    st.caption(file_caption(NEWS_CSV, 'RSS 피드 (fetch_news.py)'))


# --- 갱신 제어 -----------------------------------------------------------

def run_quick_refresh():
    """지수·보유종목·거시지표·뉴스만 다시 받아온다(무거운 시총/수급/브리핑은 제외).
    로컬 CSV가 있는 환경에서만 의미가 있다 — CSV가 없는 클라우드 배포본은 어차피
    매 요청마다 라이브로 받아오므로 이 버튼 없이도 항상 최신이다."""
    child_env = dict(os.environ, PYTHONIOENCODING='utf-8')
    result = subprocess.run(
        [sys.executable, 'collect.py', '--quick'],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        env=child_env, timeout=QUICK_REFRESH_TIMEOUT_SEC,
    )
    return result.returncode == 0, (result.stdout or '') + (result.stderr or '')


def render_sidebar():
    with st.sidebar:
        st.header('갱신')
        choice = st.radio('자동 새로고침', list(REFRESH_OPTIONS), index=2, horizontal=False)

        if os.path.exists(os.path.join(DATA_DIR, 'kospi.csv')):
            if st.button('지금 데이터 갱신', width='stretch', type='primary'):
                with st.spinner('시세·지표·뉴스를 다시 받아오는 중...'):
                    try:
                        ok, log = run_quick_refresh()
                    except subprocess.TimeoutExpired:
                        ok, log = False, f'{QUICK_REFRESH_TIMEOUT_SEC}초를 초과했습니다.'
                st.cache_data.clear()
                st.success('갱신 완료') if ok else st.error('갱신 실패')
                with st.expander('실행 로그'):
                    st.code(log or '(출력 없음)')
            st.caption(
                '자동 새로고침은 화면만 다시 그립니다. 실제 시세를 새로 받으려면 '
                '위 버튼을 누르거나 `python collect.py` 를 실행하세요.'
            )
        else:
            st.caption(
                f'라이브 조회 모드 — 패널마다 최대 {LIVE_FETCH_TTL_SEC}초 캐시로 '
                '자동으로 최신 데이터를 받아옵니다.'
            )
        return REFRESH_OPTIONS[choice]


def render_market_live():
    """"전체 시장현황" 탭의 자동 새로고침 대상 — 지수 카드·거시 지표."""
    st.caption(f"화면 갱신 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    render_index_cards(MARKET_SERIES, '지수 현황')
    st.divider()
    render_macro_panel()


def render_portfolio_live():
    """"내 계좌 포트폴리오" 탭의 자동 새로고침 대상."""
    st.caption(f"화면 갱신 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    render_portfolio_panel()


def render_fx_news_live():
    """"환율 및 뉴스" 탭의 자동 새로고침 대상 — 환율 카드·뉴스."""
    st.caption(f"화면 갱신 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    render_index_cards(FX_SERIES, '환율')
    st.divider()
    render_news_panel()


def main():
    st.set_page_config(page_title='시장 대시보드', layout='wide')
    inject_theme_css()

    if not check_password():
        return

    st.title('시장 대시보드')

    interval = render_sidebar()

    tab_market, tab_portfolio, tab_fx_news, tab_rs, tab_opinion = st.tabs(
        ['전체 시장현황', '내 계좌 포트폴리오', '환율 및 뉴스', 'RS 비교', '종목 분석']
    )

    # 탭별로 "값이 자주 바뀌는 패널"(카드/지표/뉴스)만 자동 새로고침하고, 브리핑·장기
    # 차트는 무겁거나(26년치) 유료 호출이라 수동 갱신(새로고침 버튼/페이지 새로고침)으로
    # 충분하다는 원래 설계를 탭 안에서도 그대로 유지한다.
    # run_every 값이 라디오 선택에 따라 달라져야 해서 데코레이터를 런타임에 적용한다.
    with tab_market:
        st.fragment(run_every=interval)(render_market_live)()
        st.divider()
        render_index_charts(MARKET_SERIES)

    with tab_portfolio:
        st.fragment(run_every=interval)(render_portfolio_live)()
        st.divider()
        # 종목 선택 상호작용 패널이라 자동 새로고침 대상이 아니다(RS 탭과 같은 이유).
        render_stock_detail_panel()

    with tab_fx_news:
        st.fragment(run_every=interval)(render_fx_news_live)()
        st.divider()
        render_index_charts(FX_SERIES)

    with tab_rs:
        # 사용자가 종목/기간/지수를 고르는 상호작용 탭이라 자동 새로고침 대상이 아니다
        # (자동 리런이 선택값을 방해하지 않게).
        render_rs_tab()

    with tab_opinion:
        render_stock_opinion_tab()


if __name__ == '__main__':
    main()
