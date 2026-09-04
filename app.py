# app.py — 시장 대시보드
#
# 로컬(Windows 작업 스케줄러)과 클라우드(Streamlit Community Cloud) 양쪽에서 동작하도록
# "CSV가 있으면 그걸 읽고, 없으면 그 자리에서 직접 받아온다" 하이브리드 방식을 쓴다.
# - 로컬: collect.py가 10분마다 CSV를 써두므로 디스크만 읽어 빠르다.
# - 클라우드: 스케줄러가 없으므로 data/*.csv 가 커밋되어 있지 않다 -> 매번 직접 조회.
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
    'ECOS_API_KEY', 'FRED_API_KEY', 'DASHBOARD_PASSWORD', 'OPENDART_API_KEY',
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
# 패널 제목이 "24시간 이내"라고 못 박아 두는데, 로컬 캐시 파일이 있으면 나이를
# 따지지 않고 무조건 믿어버리면(스케줄러가 멈췄을 때) 며칠 지난 뉴스를 "24시간
# 이내"로 잘못 표시하게 된다. 캐시가 이보다 오래됐으면 라이브 조회로 대체한다.
NEWS_STALE_THRESHOLD_SEC = 6 * 60 * 60
PORTFOLIO_CSV = os.path.join(DATA_DIR, 'portfolio_status.csv')
# collect_dart.py가 로컬(주기적 스케줄러)에서 만들어 git에 커밋해두는 PER/재무
# 스냅샷 — Streamlit Cloud에서 opendart.fss.or.kr/KRX 벌크 조회로의 접속이
# 구조적으로 막혀 있어(2026-08 확인), 라이브 조회 대신 이 파일을 우선 쓴다.
DART_SNAPSHOT_PATH = os.path.join(DATA_DIR, 'dart_snapshot.json')
INVESTOR_FLOW_CSV = os.path.join(DATA_DIR, 'investor_flow.csv')
INVESTOR_FLOW_KOSDAQ_CSV = os.path.join(DATA_DIR, 'investor_flow_kosdaq.csv')
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

    /* 스위스 스타일 헤드라인: 굵게, 자간 좁게, 그리드 규칙선으로 구획.
    본문(13px)과 확실히 구분되도록 단계별로 크기 차이를 벌려둔다 — 이전에는 h3가
    1.05rem(≈13.7px)이라 본문과 거의 같아서 구획 제목 구실을 못 했다. */
    h1, h2, h3 {{
        font-weight: 700 !important;
        letter-spacing: -0.02em;
        color: {SWISS_WHITE} !important;
    }}
    h1 {{
        font-size: 1.7rem !important;
        border-bottom: 3px solid {SWISS_WHITE};
        padding-bottom: 0.5rem;
    }}
    h2 {{
        font-size: 1.45rem !important;
        border-bottom: 2px solid {SWISS_WHITE};
        padding-bottom: 0.3rem;
    }}
    /* 각 패널 제목(st.subheader) — 초록 세로 규칙선을 붙여 스크롤 중에도 "여기서
    새 구획이 시작된다"가 한눈에 잡히게 한다. */
    h3 {{
        font-size: 1.3rem !important;
        margin-top: 1.6rem !important;
        padding-left: 0.55rem;
        border-left: 3px solid {SWISS_GREEN};
        line-height: 1.35;
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
    /* 카드 라벨은 이전에 0.7rem(≈9px) + uppercase + 자간 확대였는데, 한글은 대문자
    개념이 없어 uppercase가 무의미하고 9px에 자간까지 벌리면 오히려 읽기 나빠진다.
    크기를 키우고 자간을 정상으로 되돌린다. */
    [data-testid="stMetricLabel"] {{
        color: {SWISS_GRAY};
        font-size: 0.92rem !important;
        letter-spacing: normal;
        font-weight: 600;
    }}
    [data-testid="stMetricLabel"] p {{
        font-size: 0.92rem !important;
    }}
    [data-testid="stMetricValue"] {{
        color: {SWISS_WHITE};
        font-weight: 700;
        font-size: 1.5rem !important;
    }}
    [data-testid="stMetricDelta"] {{
        font-size: 0.95rem !important;
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

    /* 출처·갱신시각 캡션 — 보조 정보라 회색이지만, 기본 0.875rem(≈11px)은 한글에서
    너무 작아 살짝 키우고 줄간격을 벌려 두세 줄짜리 캡션도 읽히게 한다. */
    [data-testid="stCaptionContainer"], .stCaption {{
        color: {SWISS_GRAY} !important;
        font-size: 0.92rem !important;
        line-height: 1.6 !important;
    }}
    [data-testid="stCaptionContainer"] p {{
        font-size: 0.92rem !important;
    }}

    /* 표(glide-data-grid)는 캔버스로 그려져서 일반 CSS font-size가 안 먹고, 아래
    커스텀 속성만 반영된다. 인라인으로 박혀 있어 !important 로 덮어써야 이긴다. */
    .stDataFrameGlideDataEditor {{
        --gdg-base-font-style: 500 12.5px !important;
        --gdg-header-font-style: 600 12.5px !important;
        --gdg-cell-vertical-padding: 6px !important;
    }}

    /* 라디오(기간 선택 등) 항목이 너무 붙어 있어 잘못 누르기 쉬웠던 부분 완화 */
    [data-testid="stRadio"] label {{
        font-size: 0.95rem !important;
    }}
    [role="radiogroup"] {{
        gap: 0.35rem;
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


@st.cache_data(ttl=LIVE_FETCH_TTL_SEC)
def fetch_investor_flow_live(market='KOSPI'):
    """최근 3개월 투자자별(기관/외국인/개인) 순매수 대금(원). collect.py의
    collect_investor_flow()와 같은 조회를 그 자리에서 직접 한다."""
    end_dt = datetime.now()
    start_dt = end_dt - pd.DateOffset(months=3)
    df = pykrx_stock.get_market_trading_value_by_date(
        start_dt.strftime('%Y%m%d'), end_dt.strftime('%Y%m%d'), market)
    return df


def get_investor_flow_df(market='KOSPI'):
    snapshot_rows = load_dart_snapshot_investor_flow().get(market)
    if snapshot_rows:
        df = pd.DataFrame(snapshot_rows)
        df['날짜'] = pd.to_datetime(df['날짜'])
        return df
    csv_path = INVESTOR_FLOW_CSV if market == 'KOSPI' else INVESTOR_FLOW_KOSDAQ_CSV
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path, encoding='utf-8-sig')
        df['날짜'] = pd.to_datetime(df['날짜'])
        return df
    try:
        df = fetch_investor_flow_live(market)
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    df = df.reset_index().rename(columns={df.index.name or 'index': '날짜'})
    return df


# 포트폴리오 성과 추이에서 고를 수 있는 기간 — RS_PERIODS와 같은 규칙.
PORTFOLIO_PERF_PERIODS = {
    '1개월': lambda now: now - pd.DateOffset(months=1),
    '3개월': lambda now: now - pd.DateOffset(months=3),
    '6개월': lambda now: now - pd.DateOffset(months=6),
    'YTD': lambda now: pd.Timestamp(year=now.year, month=1, day=1),
    '1년': lambda now: now - pd.DateOffset(years=1),
}


@st.cache_data(ttl=LIVE_FETCH_TTL_SEC)
def fetch_portfolio_value_series(holdings_key, start_dt):
    """보유 종목들의 일별 평가금액 합계 시계열을 만든다. holdings_key는
    ((ticker, quantity), ...) 튜플 — st.cache_data가 해시할 수 있어야 해서
    DataFrame 대신 튜플로 받는다.

    주의: 지금 보유 중인 수량을 과거에도 그대로 들고 있었다고 가정한 역산이다
    (실제 매매 이력이 없어서 진짜 계좌 잔고 추이가 아니다). 상장이 늦은 종목이
    섞여 있으면 그 종목의 데이터가 없는 구간은 통째로 빼서(dropna) 합계가 갑자기
    뛰는 착시를 막는다 — 그래서 실제 시작일이 요청한 기간보다 늦을 수 있고,
    호출부에서 그 시작일을 캡션에 밝힌다."""
    series = {}
    for ticker, qty in holdings_key:
        try:
            df = fetch_stock_series(ticker, start_dt)
        except Exception:
            continue
        if df is not None and not df.empty and 'Close' in df.columns:
            series[ticker] = df['Close'] * qty
    if not series:
        return pd.Series(dtype=float)
    # ffill은 종목별 휴장/결측일을 메우고, dropna는 아직 상장 전이라 값 자체가
    # 없는 앞 구간을 잘라낸다.
    combined = pd.DataFrame(series).ffill().dropna()
    if combined.empty:
        return pd.Series(dtype=float)
    return combined.sum(axis=1)


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
    for col in ('target_price', 'stop_price'):
        if col not in df.columns:
            df[col] = pd.NA
        df[col] = pd.to_numeric(df[col], errors='coerce')
    return df


# PER은 하루 안에 거의 안 바뀌고, 시장 전체 종목의 PER/업종을 한 번에 받아오는
# 무거운 호출이라 지수/시세보다 길게 캐시한다. 다만 실패(빈 결과)도 그대로 캐시
# 되는 게 st.cache_data의 기본 동작이라, 로그인/네트워크 문제가 막 해결된 직후처럼
# "예전의 빈 실패 결과"가 한동안 눌러앉는 상황을 줄이려고 1시간보다는 짧게 잡는다.
PER_TTL_SEC = 600
PER_LOOKBACK_DAYS = 5

# DART(전자공시) 데이터는 분기/연 단위로만 바뀌므로 하루 단위로 길게 캐시한다.
DART_TTL_SEC = 24 * 60 * 60

# 업종별 등락 패널 기본 기간(일). 시장 전체 조회라 PER과 같은 캐시 주기를 쓴다.
SECTOR_PERF_DAYS = 7


@st.cache_data(ttl=PER_TTL_SEC)
def fetch_sector_performance(market, days):
    """market('KOSPI'/'KOSDAQ') 업종 지수의 기간 등락률(%)을 내림차순 Series로
    반환한다(업종명 -> 등락률). pykrx가 같은 응답에 시장 전체("코스피")와
    "코스피 200 정보기술" 같은 파생 지수까지 섞어 주므로, 시장 이름으로 시작하는
    항목을 빼서 순수 업종 지수만 남긴다. 조회 실패 시 빈 Series.

    클라우드에서는 pykrx 라이브 조회가 막혀 있어, collect_dart.py가 커밋해둔
    스냅샷(SECTOR_PERF_DAYS=7일 고정)이 있으면 그걸 우선 쓴다 — days가 그 값과
    다르면(현재 호출부는 항상 SECTOR_PERF_DAYS를 넘기므로 실질적으로 항상 일치)
    스냅샷을 건너뛰고 라이브 조회로 대체한다."""
    if days == SECTOR_PERF_DAYS:
        snapshot = load_dart_snapshot_sector_performance().get(market)
        if snapshot:
            return pd.Series(snapshot, dtype=float).sort_values(ascending=False)

    end_dt = datetime.now()
    start_dt = end_dt - pd.Timedelta(days=days)
    try:
        df = pykrx_stock.get_index_price_change(
            start_dt.strftime('%Y%m%d'), end_dt.strftime('%Y%m%d'), market)
    except Exception:
        return pd.Series(dtype=float)
    if df.empty or '등락률' not in df.columns:
        return pd.Series(dtype=float)
    prefix = '코스피' if market == 'KOSPI' else '코스닥'
    sectors = df[~df.index.str.startswith(prefix)]
    return sectors['등락률'].sort_values(ascending=False)


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


@st.cache_data(ttl=CACHE_TTL_SEC)
def load_dart_snapshot_investor_flow():
    """collect_dart.py가 커밋해둔 투자자별 수급 스냅샷 — {market: [{날짜, 외국인합계,
    기관합계, 개인}, ...]}. data/investor_flow*.csv는 .gitignore돼 있어 클라우드
    배포본에 안 실리므로, 라이브 조회가 막힌 클라우드에서는 이 스냅샷이 유일한
    데이터 소스다. 없거나 비었으면 {}."""
    if not os.path.exists(DART_SNAPSHOT_PATH):
        return {}
    try:
        with open(DART_SNAPSHOT_PATH, encoding='utf-8') as f:
            return json.load(f).get('investor_flow', {})
    except Exception:
        return {}


@st.cache_data(ttl=CACHE_TTL_SEC)
def load_dart_snapshot_sector_performance():
    """collect_dart.py가 커밋해둔 업종별 등락 스냅샷 — {market: {업종명: 등락률}}.
    없거나 비었으면 {}."""
    if not os.path.exists(DART_SNAPSHOT_PATH):
        return {}
    try:
        with open(DART_SNAPSHOT_PATH, encoding='utf-8') as f:
            return json.load(f).get('sector_performance', {})
    except Exception:
        return {}


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
def fetch_dart_financial_ratios_cached(ticker):
    """정량 스코어카드의 재무 안정성·수익성 카드용 — 부채비율/유동비율/이자보상배율/
    ROE/순이익률. 올해 사업보고서가 아직 안 나왔을 수 있어 최근 연도부터 2개까지 시도."""
    corp_code = fetch_dart_corp_map().get(ticker)
    if not corp_code:
        return {}
    this_year = datetime.now().year
    for bsns_year in (this_year - 1, this_year - 2):
        ratios = fetch_dart.fetch_financial_ratios(corp_code, str(bsns_year))
        if ratios:
            return ratios
    return {}


@st.cache_data(ttl=DART_TTL_SEC)
def fetch_dart_financial_ratios_5y_cached(ticker):
    """정량 스코어카드의 "밸류에이션·재무 안정성" 5개년 추이 표용 — 최근 5개
    사업연도까지 각각 부채비율/유동비율/이자보상배율/ROE/순이익률/총자산회전율을
    조회한다(연도당 fnlttSinglIndx.json 3회). 반환: {연도: ratios_dict} — 아직
    사업보고서가 안 나온 연도는 빠진다."""
    corp_code = fetch_dart_corp_map().get(ticker)
    if not corp_code:
        return {}
    this_year = datetime.now().year
    result = {}
    for bsns_year in range(this_year - 1, this_year - 7, -1):
        ratios = fetch_dart.fetch_financial_ratios(corp_code, str(bsns_year))
        if ratios:
            result[bsns_year] = ratios
        if len(result) >= 5:
            break
    return result


@st.cache_data(ttl=DART_TTL_SEC)
def fetch_dart_balance_ratios_5y_cached(ticker):
    """정량 스코어카드 "5개년 추이" 그래프용 — 부채비율/유동비율/ROE/ROA/
    총자산회전율을 원본 재무제표(BS/IS)에서 직접 계산해 최근 5개년까지 받는다.
    fetch_dart_financial_ratios_5y_cached(fnlttSinglIndx 기반)는 이 지표들을
    최근 1~2개년치만 주는 회사가 많아(2026-08 확인) 대신 만들었다 — 원본 계정은
    더 오래전까지 있어서 fetch_dart.fetch_balance_sheet_ratios()가 앵커 연도
    2개(최근·4년 전)를 합쳐 최대 6개년을 커버한다."""
    corp_code = fetch_dart_corp_map().get(ticker)
    if not corp_code:
        return {}
    this_year = datetime.now().year
    by_year = {}
    for anchor in (this_year - 1, this_year - 4):
        by_year.update(fetch_dart.fetch_balance_sheet_ratios(corp_code, str(anchor)))
    years = sorted(by_year.keys())[-5:]
    return {y: by_year[y] for y in years}


@st.cache_data(ttl=DART_TTL_SEC)
def fetch_dart_cashflow_cached(ticker):
    """정량 스코어카드의 "이익의 질" 차트용 — 최근 5개년 영업활동현금흐름(억원).
    fetch_dart.fetch_operating_cashflow()는 한 번에 3개년(당기/전기/전전기)만
    주므로, 앵커 연도를 두 번(최근·3년 전) 걸쳐 불러 합친다."""
    corp_code = fetch_dart_corp_map().get(ticker)
    if not corp_code:
        return []
    this_year = datetime.now().year
    by_year = {}
    for anchor in (this_year - 1, this_year - 2, this_year - 4, this_year - 5):
        items = fetch_dart.fetch_operating_cashflow(corp_code, str(anchor))
        for it in items:
            by_year.setdefault(it['year'], it['cfo'])
        if len(by_year) >= 5:
            break
    years = sorted(by_year.keys())[-5:]
    return [{'year': y, 'cfo': by_year[y]} for y in years]


# 표시는 최근 60거래일이지만 주말·공휴일을 감안해 넉넉히 받아온 뒤 뒤에서 자른다.
INVESTOR_FLOW_LOOKBACK_DAYS = 90
STOCK_INVESTOR_FLOW_DAYS = 60


@st.cache_data(ttl=PER_TTL_SEC)
def fetch_stock_investor_flow(ticker):
    """정량 스코어카드용 — 개별 종목의 최근 60거래일 투자자별(외국인/기관/개인)
    순매수 대금(원)을 pykrx에서 직접 받는다. 기존 "투자자별 수급" 패널은 코스피
    시장 전체 기준이라, 이건 그 함수를 종목 티커로 그대로 호출하는 별개 경로다.
    실패하거나 자료가 없으면 빈 DataFrame."""
    end_dt = datetime.now()
    start_dt = end_dt - pd.Timedelta(days=INVESTOR_FLOW_LOOKBACK_DAYS)
    try:
        df = pykrx_stock.get_market_trading_value_by_date(
            start_dt.strftime('%Y%m%d'), end_dt.strftime('%Y%m%d'), ticker,
        )
    except Exception:
        return pd.DataFrame()
    return df.tail(STOCK_INVESTOR_FLOW_DAYS)


@st.cache_data(ttl=PER_TTL_SEC)
def fetch_stock_shorting(ticker):
    """정량 스코어카드용 — 개별 종목의 최근 공매도 잔고 비중(%) 추이(pykrx)."""
    end_dt = datetime.now()
    start_dt = end_dt - pd.Timedelta(days=INVESTOR_FLOW_LOOKBACK_DAYS)
    try:
        df = pykrx_stock.get_shorting_balance_by_date(
            start_dt.strftime('%Y%m%d'), end_dt.strftime('%Y%m%d'), ticker,
        )
    except Exception:
        return pd.DataFrame()
    return df.tail(STOCK_INVESTOR_FLOW_DAYS)


@st.cache_data(ttl=DART_TTL_SEC)
def fetch_dart_capital_changes_cached(ticker):
    """정량 스코어카드의 희석위험 판정용 — 최근 사업연도의 자본금 변동 이력."""
    corp_code = fetch_dart_corp_map().get(ticker)
    if not corp_code:
        return []
    this_year = datetime.now().year
    for bsns_year in (this_year - 1, this_year - 2):
        changes = fetch_dart.fetch_capital_changes(corp_code, str(bsns_year))
        if changes:
            return changes
    return []


@st.cache_data(ttl=DART_TTL_SEC)
def fetch_dart_largest_shareholder_cached(ticker):
    """정량 스코어카드의 지배구조 카드용 — 최대주주(+특수관계인) 최신 지분율."""
    corp_code = fetch_dart_corp_map().get(ticker)
    if not corp_code:
        return {}
    this_year = datetime.now().year
    for bsns_year in (this_year - 1, this_year - 2):
        info = fetch_dart.fetch_largest_shareholder(corp_code, str(bsns_year))
        if info:
            return info
    return {}


@st.cache_data(ttl=DART_TTL_SEC)
def fetch_dart_debt_trend_cached(ticker):
    """보유 종목 집중점검의 "재무 건전성 악화" 판정용 — 최근 확인 가능한 2개
    연도의 부채비율을 {연도: 부채비율} 형태로 모은다(사업보고서가 아직 안 나온
    연도는 자동으로 건너뛴다)."""
    corp_code = fetch_dart_corp_map().get(ticker)
    if not corp_code:
        return {}
    this_year = datetime.now().year
    result = {}
    for bsns_year in (this_year - 1, this_year - 2, this_year - 3):
        ratios = fetch_dart.fetch_financial_ratios(corp_code, str(bsns_year))
        if ratios.get('debt_ratio') is not None:
            result[bsns_year] = ratios['debt_ratio']
        if len(result) >= 2:
            break
    return result


@st.cache_data(ttl=DART_TTL_SEC)
def fetch_dart_inventory_cached(ticker):
    """보유 종목 집중점검의 "재고 급증" 판정용 — 최근 3개년 재고자산(억원)."""
    corp_code = fetch_dart_corp_map().get(ticker)
    if not corp_code:
        return []
    this_year = datetime.now().year
    for bsns_year in (this_year - 1, this_year - 2):
        items = fetch_dart.fetch_inventory(corp_code, str(bsns_year))
        if items:
            return items
    return []


@st.cache_data(ttl=DART_TTL_SEC)
def fetch_dart_major_trades_cached(ticker):
    """보유 종목 집중점검의 "대주주 지분 매도" 판정용 — 5% 이상 대량보유자 신고 이력."""
    corp_code = fetch_dart_corp_map().get(ticker)
    if not corp_code:
        return []
    return fetch_dart.fetch_major_shareholder_trades(corp_code)


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


@st.cache_data(ttl=DART_TTL_SEC)
def fetch_dart_quarterly_financials_5y_cached(ticker):
    """"종목 분석" 2단계(추이 분석)용 — 최근 5년(최대 20개 분기) 매출액·영업이익
    (억원). 6개년치를 요청해서 올해가 아직 1분기뿐이어도 20개 분기를 채운다."""
    corp_code = fetch_dart_corp_map().get(ticker)
    if not corp_code:
        return [], None
    this_year = datetime.now().year
    bsns_years = [this_year - i for i in range(6)]
    items, fs_div = fetch_dart.fetch_quarterly_financials(corp_code, bsns_years)
    return items[-20:], fs_div


@st.cache_data(ttl=PER_TTL_SEC)
def fetch_per_universe(market):
    """market('KOSPI'/'KOSDAQ') 전체 종목의 PER·PBR·업종명을 한 번에 받아온다 — 종목별로
    따로 부르지 않고 시장 전체를 한 번에 받아 "업계 PER"(같은 업종 종목들의 PER
    중앙값) 계산과 정량 스코어카드의 PBR 조회에 함께 쓴다(PBR은 A3에서 제거했던
    보유종목 표에는 다시 노출하지 않고, 정량 스코어카드에서만 이 함수를 직접 불러
    쓴다). 티커를 인덱스로 하는 DataFrame(PER, PBR, 업종명)을 반환하고, 최근 며칠
    안에 거래일 데이터가 없으면(휴장 등) 빈 DataFrame을 반환한다."""
    for delta in range(PER_LOOKBACK_DAYS):
        d = (datetime.now() - pd.Timedelta(days=delta)).strftime('%Y%m%d')
        try:
            fundamentals = pykrx_stock.get_market_fundamental(d, market=market)
            sectors = pykrx_stock.get_market_sector_classifications(d, market)
        except Exception:
            continue
        if not fundamentals.empty and not sectors.empty:
            return fundamentals[['PER', 'PBR']].join(sectors[['업종명']], how='left')
    return pd.DataFrame(columns=['PER', 'PBR', '업종명'])


@st.cache_data(ttl=24 * 60 * 60)
def load_krx_listing():
    """"종목 분석" 검색창용 — 코스피·코스닥 전 종목의 이름·시장 목록(하루 캐시).
    fetch_portfolio.py의 fetch_krx_listing()(같은 fdr.StockListing 소스, 실패 시
    None)을 그대로 재사용한다. 실패하면 빈 DataFrame을 반환해 호출부에서 종목코드
    직접 입력으로 대체할 수 있게 한다."""
    try:
        listing = fetch_portfolio.fetch_krx_listing()
    except Exception:
        listing = None
    if listing is None or listing.empty or 'Name' not in listing.columns:
        return pd.DataFrame(columns=['Name', 'Market'])
    return listing[['Name', 'Market']]


def attach_per_columns(stocks):
    """보유 종목 각각에 자기 PER과 "업계 PER"를 붙인다.
    - 자기 PER: collect_dart.py가 만들어둔 로컬 스냅샷의 EPS가 있으면 그걸 오늘
      현재가와 결합해 계산한다(스냅샷 EPS는 분기/연 단위라 오래돼도 되지만, 가격은
      항상 최신을 쓴다). 스냅샷에 없으면 라이브 DART 조회, 그것도 안 되면 pykrx
      자체 PER 순으로 대체한다.
    - 업계 PER: 같은 시장·같은 업종 내 다른 종목들의 PER 중앙값(적자로 PER이 의미
      없는 0 이하 값은 제외) — 스냅샷에 미리 계산돼 있으면 그걸 쓰고, 없으면 pykrx
      시장 전체 데이터를 라이브로 받아 계산한다.
    ETF·상장 정보가 없는 종목·시장 구분이 없는 종목은 조용히 "-"(None)로 남긴다 —
    표에서 이 함수 호출 자체가 실패해도(pykrx 장애 등) 호출부에서 try/except로
    감싸 나머지 표는 그대로 보여준다."""
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


# MDD·52주 위치·거래량 배율 계산에 넉넉한 버퍼를 두고 최근 400일치를 받는다
# (52주=약 252거래일 + 거래량 20일 평균 계산 여유분).
SECTOR_CONCENTRATION_WARN_PCT = 50  # 한 업종이 이 비중을 넘으면 업종 쏠림으로 경고
RISK_LOOKBACK_DAYS = 400
VOLUME_SURGE_RATIO = 2.0  # 20일 평균 대비 이 배수 이상이면 "급증"으로 표시


def attach_risk_columns(stocks):
    """보유 종목 각각에 리스크 지표 3개를 붙인다 — 전부 이미 받아오는 주가 시계열
    (FDR)로 계산하고 새 API 호출은 없다.
    - MDD(고점대비): 최근 52주 최고가 대비 현재가 낙폭(%). 음수만 나온다(현재가가
      고점을 갱신 중이면 0%).
    - 52주 위치(%): 최근 52주 최고~최저 구간에서 현재가가 몇 % 지점인지
      (0%=52주 최저, 100%=52주 최고).
    - 거래량 배율: 당일 거래량 / 직전 20거래일 평균 거래량. VOLUME_SURGE_RATIO
      이상이면 "급증"으로 본다.
    개별 종목 조회가 실패해도 그 종목만 "-"로 남고 나머지는 계속 계산한다."""
    stocks = stocks.copy()
    mdd_vals, pos52_vals, vol_ratio_vals = [], [], []

    for _, row in stocks.iterrows():
        ticker = row['ticker']
        current_price = row.get('current_price')
        try:
            start_dt = datetime.now() - pd.Timedelta(days=RISK_LOOKBACK_DAYS)
            price_df = fetch_stock_series(ticker, start_dt)
        except Exception:
            price_df = pd.DataFrame()

        if price_df.empty or 'Close' not in price_df.columns or not current_price:
            mdd_vals.append(None)
            pos52_vals.append(None)
            vol_ratio_vals.append(None)
            continue

        window = price_df.tail(252)  # 최근 약 52주 거래일
        high_52w = window['Close'].max()
        low_52w = window['Close'].min()

        mdd_vals.append((current_price / high_52w - 1) * 100 if high_52w else None)
        pos52_vals.append(
            (current_price - low_52w) / (high_52w - low_52w) * 100
            if high_52w and high_52w != low_52w else None
        )

        if 'Volume' in price_df.columns and len(price_df) >= 21:
            recent_vol = price_df['Volume'].iloc[-1]
            avg_vol = price_df['Volume'].iloc[-21:-1].mean()
            vol_ratio_vals.append(recent_vol / avg_vol if avg_vol else None)
        else:
            vol_ratio_vals.append(None)

    stocks['mdd'] = mdd_vals
    stocks['pos_52w'] = pos52_vals
    stocks['vol_ratio'] = vol_ratio_vals
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
        df = load_csv(PORTFOLIO_CSV)
    else:
        try:
            df = fetch_portfolio_live()
        except FileNotFoundError:
            return None  # Secrets도 없고 로컬 portfolio.csv도 없는 경우
    # target_price/stop_price는 로컬 portfolio_status.csv가 이 컬럼이 생기기 전에
    # 만들어졌을 수도 있으니(다음 스케줄러 실행 전까지), 없으면 만들어 채워둔다.
    for col in ('target_price', 'stop_price'):
        if col not in df.columns:
            df[col] = pd.NA
        else:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    return df


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


def _news_cache_is_fresh():
    if not os.path.exists(NEWS_CSV):
        return False
    age_sec = datetime.now().timestamp() - os.path.getmtime(NEWS_CSV)
    return age_sec <= NEWS_STALE_THRESHOLD_SEC


def get_news_df():
    """로컬 캐시가 있어도 너무 오래됐으면(스케줄러가 멈춘 경우 등) 쓰지 않는다 —
    패널이 "24시간 이내"라고 표시하는데 실제로는 며칠 지난 뉴스를 보여주는 걸
    막기 위해서다. render_news_panel()의 캡션도 이 판단과 같은 기준을 써야
    "갱신 시각"이 실제로 보여준 데이터와 어긋나지 않는다."""
    if _news_cache_is_fresh():
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


INVESTOR_FLOW_SUBJECT_COLORS = alt.Scale(
    domain=['외국인합계', '기관합계', '개인'], range=[SWISS_GREEN, SWISS_GRAY, SWISS_WHITE],
)


def render_investor_flow_panel():
    """투자자별(외국인/기관/개인) 순매수 — 코스피·코스닥을 나란히 비교할 수 있게
    2x2로 배치한다. 위쪽 행은 누적 순매수(선, "며칠째 사고/팔고 있는지" 추세용),
    아래쪽 행은 일별 순매수(막대, 특정 날짜의 급변동 확인용)."""
    st.subheader('투자자별 수급 (최근 3개월)')
    data = {market: get_investor_flow_df(market) for market in ('KOSPI', 'KOSDAQ')}

    col_kospi, col_kosdaq = st.columns(2)
    with col_kospi:
        _render_investor_flow_cumulative(data['KOSPI'], 'KOSPI', '코스피')
    with col_kosdaq:
        _render_investor_flow_cumulative(data['KOSDAQ'], 'KOSDAQ', '코스닥')

    col_kospi2, col_kosdaq2 = st.columns(2)
    with col_kospi2:
        _render_investor_flow_daily(data['KOSPI'], '코스피')
    with col_kosdaq2:
        _render_investor_flow_daily(data['KOSDAQ'], '코스닥')


def _render_investor_flow_cumulative(df, market, market_label):
    """2x2 위쪽 행 한 칸 — 누적 순매수 선 차트 + 당일 순매수 3종 + 출처 캡션."""
    st.markdown(f'###### {market_label} · 누적')
    if df.empty:
        st.info('투자자별 수급 데이터를 불러오지 못했습니다.')
        return

    chart_df = df[['날짜', '외국인합계', '기관합계', '개인']].copy()
    for col in ('외국인합계', '기관합계', '개인'):
        chart_df[col] = (chart_df[col] / 1e8).cumsum()  # 원 -> 억원, 누적

    melted = chart_df.melt(id_vars='날짜', var_name='주체', value_name='누적 순매수(억원)')

    chart = (
        alt.Chart(melted)
        .mark_line(point=False)
        .encode(
            x=alt.X('날짜:T', title=None),
            y=alt.Y('누적 순매수(억원):Q', title='누적 순매수(억원)', scale=alt.Scale(zero=False)),
            color=alt.Color('주체:N', title=None, scale=INVESTOR_FLOW_SUBJECT_COLORS),
            tooltip=[alt.Tooltip('날짜:T'), alt.Tooltip('주체:N'), alt.Tooltip('누적 순매수(억원):Q', format=',.0f')],
        )
        .properties(height=260)
        .interactive()
    )
    st.altair_chart(chart, use_container_width=True)

    last = df.iloc[-1]
    cols = st.columns(3)
    cols[0].metric('외국인 당일', f"{last['외국인합계'] / 1e8:+,.0f}억")
    cols[1].metric('기관 당일', f"{last['기관합계'] / 1e8:+,.0f}억")
    cols[2].metric('개인 당일', f"{last['개인'] / 1e8:+,.0f}억")

    if load_dart_snapshot_investor_flow().get(market):
        source = f"스냅샷({load_dart_snapshot_generated_at() or '?'} 기준, collect_dart.py) · 출처: pykrx (KRX 투자자별 거래대금)"
    else:
        csv_path = INVESTOR_FLOW_CSV if market == 'KOSPI' else INVESTOR_FLOW_KOSDAQ_CSV
        source = file_caption(csv_path, 'pykrx (KRX 투자자별 거래대금)')
    st.caption(source + f' · 최근 3개월 누적 · {market_label} 전체 기준(개별 종목 수급 아님)')


def _render_investor_flow_daily(df, market_label):
    """2x2 아래쪽 행 한 칸 — 하루 단위 순매수 막대 차트(누적하지 않은 원값).
    특정 날짜에 유독 크게 사거나 판 날을 바로 찾을 수 있게 누적 차트와 짝을 이룬다."""
    st.markdown(f'###### {market_label} · 일별')
    if df.empty:
        st.info('투자자별 수급 데이터를 불러오지 못했습니다.')
        return

    chart_df = df[['날짜', '외국인합계', '기관합계', '개인']].copy()
    for col in ('외국인합계', '기관합계', '개인'):
        chart_df[col] = chart_df[col] / 1e8  # 원 -> 억원

    melted = chart_df.melt(id_vars='날짜', var_name='주체', value_name='일별 순매수(억원)')

    chart = (
        alt.Chart(melted)
        .mark_bar()
        .encode(
            x=alt.X('날짜:T', title=None),
            xOffset=alt.XOffset('주체:N'),
            y=alt.Y('일별 순매수(억원):Q', title='일별 순매수(억원)'),
            color=alt.Color('주체:N', title=None, scale=INVESTOR_FLOW_SUBJECT_COLORS),
            tooltip=[alt.Tooltip('날짜:T'), alt.Tooltip('주체:N'), alt.Tooltip('일별 순매수(억원):Q', format=',.0f')],
        )
        .properties(height=260)
        .interactive()
    )
    st.altair_chart(chart, use_container_width=True)
    st.caption('막대는 그날 하루치 순매수(누적 아님) · 외국인·기관·개인을 나란히 배치')


def render_sector_panel():
    """업종별 등락 — 최근 한 주 동안 어느 업종이 오르고 내렸는지 가로 막대로 본다.
    지수 카드(코스피/코스닥 전체)만으로는 "시장이 왜 움직였는지"를 알 수 없어서,
    업종 단위로 쪼개 자금이 어디로 몰렸는지 한눈에 보게 하는 패널이다."""
    st.subheader(f'업종별 등락 (최근 {SECTOR_PERF_DAYS}일)')

    market_label = st.radio(
        '시장', ['코스피', '코스닥'], horizontal=True, key='sector_market', label_visibility='collapsed',
    )
    market = 'KOSPI' if market_label == '코스피' else 'KOSDAQ'

    try:
        sectors = fetch_sector_performance(market, SECTOR_PERF_DAYS)
    except Exception as e:
        st.warning(f'업종별 등락을 불러오지 못했습니다: {e}')
        return
    if sectors.empty:
        st.info('업종별 등락 데이터를 불러오지 못했습니다.')
        return

    chart_df = sectors.reset_index()
    chart_df.columns = ['업종', '등락률(%)']
    chart = (
        alt.Chart(chart_df)
        .mark_bar()
        .encode(
            x=alt.X('등락률(%):Q', title='등락률(%)'),
            y=alt.Y('업종:N', title=None, sort=chart_df['업종'].tolist()),
            # 한국 시장 관행대로 상승은 빨강, 하락은 파랑.
            color=alt.condition(alt.datum['등락률(%)'] >= 0, alt.value(UP_COLOR), alt.value(DOWN_COLOR)),
            tooltip=[alt.Tooltip('업종:N'), alt.Tooltip('등락률(%):Q', format='+.2f')],
        )
        .properties(height=max(320, 22 * len(chart_df)))
    )
    st.altair_chart(chart, use_container_width=True)

    top = chart_df.head(3)
    bottom = chart_df.tail(3).iloc[::-1]
    c1, c2 = st.columns(2)
    with c1:
        st.markdown('**가장 많이 오른 업종**')
        for _, r in top.iterrows():
            st.markdown(f"- {r['업종']} `{r['등락률(%)']:+.2f}%`")
    with c2:
        st.markdown('**가장 많이 내린 업종**')
        for _, r in bottom.iterrows():
            st.markdown(f"- {r['업종']} `{r['등락률(%)']:+.2f}%`")

    if load_dart_snapshot_sector_performance().get(market):
        source_note = f"스냅샷({load_dart_snapshot_generated_at() or '?'} 기준, collect_dart.py)"
    else:
        source_note = '라이브 조회'
    st.caption(
        f'{market_label} 업종 지수의 최근 {SECTOR_PERF_DAYS}일 등락률 · 빨강=상승, 파랑=하락 · '
        f'출처: pykrx (KRX 업종 지수) · {source_note}'
    )


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
    st.markdown(
        '- **PER (Price Earnings Ratio, 주가수익비율)** — DART 전자공시(사업보고서) 주당순이익(EPS) 기준 값에 '
        '오늘 현재가를 나눈 값 (순손실 종목은 산정 불가로 "-")\n'
        '- **업계 PER** — pykrx 기준 같은 시장(코스피/코스닥)·같은 업종 내 다른 종목들의 PER 중앙값 (적자 종목 제외)\n'
        '- **ETF (Exchange Traded Fund, 상장지수펀드)**·시장 구분이 없는 종목은 둘 다 "-"로 표시됩니다'
    )

    st.divider()
    render_portfolio_breakdown(stocks)


def render_portfolio_breakdown(stocks):
    """보유 종목 표만으로는 "누가 손익을 만들었나 / 어디에 쏠려 있나"가 한눈에 안
    들어와서, 종목별 손익 기여도와 업종별 비중을 막대로 함께 보여준다.
    stocks는 render_portfolio_panel이 이미 attach_per_columns까지 끝낸 DataFrame —
    업종(industry_name)을 다시 조회하지 않으려고 그대로 넘겨받는다."""
    st.markdown('###### 손익 기여도 · 비중 분석')
    col_pl, col_sector = st.columns(2)

    with col_pl:
        pl_df = pd.DataFrame({
            '종목명': stocks['name'],
            '평가손익': pd.to_numeric(stocks['profit'], errors='coerce'),
        }).dropna().sort_values('평가손익')
        if pl_df.empty:
            st.info('손익 기여도를 계산할 데이터가 없습니다.')
        else:
            pl_chart = (
                alt.Chart(pl_df)
                .mark_bar()
                .encode(
                    x=alt.X('평가손익:Q', title='평가손익(원)'),
                    y=alt.Y('종목명:N', title=None, sort=pl_df['종목명'].tolist()),
                    color=alt.condition(alt.datum['평가손익'] >= 0, alt.value(UP_COLOR), alt.value(DOWN_COLOR)),
                    tooltip=[alt.Tooltip('종목명:N'), alt.Tooltip('평가손익:Q', format=',.0f')],
                )
                .properties(height=max(240, 30 * len(pl_df)))
            )
            st.altair_chart(pl_chart, use_container_width=True)
            worst = pl_df.iloc[0]
            best = pl_df.iloc[-1]
            st.caption(
                f"손익 기여 1위: {best['종목명']} ({best['평가손익']:+,.0f}원) · "
                f"손실 기여 1위: {worst['종목명']} ({worst['평가손익']:+,.0f}원)"
            )

    with col_sector:
        sector_src = stocks.copy()
        # ETF·시장 구분이 없는 종목은 업종이 비어 있어(pykrx 업종 분류 대상이 아님)
        # 그냥 빼면 합계가 100%가 안 되므로 별도 항목으로 묶는다.
        sector_src['업종'] = sector_src['industry_name'].fillna('ETF·기타').replace('', 'ETF·기타')
        sector_df = (
            sector_src.groupby('업종')['eval_amount'].sum().reset_index()
            .sort_values('eval_amount', ascending=False)
        )
        total_eval = sector_df['eval_amount'].sum()
        if not total_eval:
            st.info('업종별 비중을 계산할 데이터가 없습니다.')
        else:
            sector_df['비중(%)'] = sector_df['eval_amount'] / total_eval * 100
            sector_chart = (
                alt.Chart(sector_df)
                .mark_bar(color=SWISS_GREEN)
                .encode(
                    x=alt.X('비중(%):Q', title='비중(%)'),
                    y=alt.Y('업종:N', title=None, sort=sector_df['업종'].tolist()),
                    tooltip=[
                        alt.Tooltip('업종:N'), alt.Tooltip('비중(%):Q', format='.1f'),
                        alt.Tooltip('eval_amount:Q', title='평가금액', format=',.0f'),
                    ],
                )
                .properties(height=max(240, 30 * len(sector_df)))
            )
            st.altair_chart(sector_chart, use_container_width=True)
            top_sector = sector_df.iloc[0]
            st.caption(
                f"최대 비중 업종: {top_sector['업종']} ({top_sector['비중(%)']:.1f}%) · "
                f'업종 수 {len(sector_df)}개 · 현금 제외 주식 평가금액 대비'
            )

    # 업종 쏠림은 종목 쏠림과 별개다 — 서로 다른 종목에 나눠 담았어도 같은 업종이면
    # 악재가 왔을 때 한꺼번에 빠진다. 종목 단위 집중도 경고(보유 종목 현황)가
    # 놓치는 위험이라 여기서 따로 알린다.
    if not stocks.empty:
        sector_weights = (
            stocks.assign(업종=stocks['industry_name'].fillna('ETF·기타').replace('', 'ETF·기타'))
            .groupby('업종')['eval_amount'].sum()
        )
        sector_total = sector_weights.sum()
        if sector_total:
            top_pct = sector_weights.max() / sector_total * 100
            top_name = sector_weights.idxmax()
            if top_pct >= SECTOR_CONCENTRATION_WARN_PCT and top_name != 'ETF·기타':
                st.warning(
                    f'업종 집중도 경고: **{top_name}** 업종이 주식 평가금액의 {top_pct:.1f}%를 차지합니다. '
                    '종목은 나눠 담았어도 같은 업종이면 업황 악재에 함께 하락하는 경향이 있어, '
                    '종목 분산만으로는 위험이 줄지 않습니다.'
                )

    st.markdown(
        '- **손익 기여도** — 종목별 평가손익(평가금액 − 매입금액). 빨강=이익, 파랑=손실\n'
        '- **업종별 비중** — 같은 업종 종목들의 평가금액을 합쳐 계산 (pykrx 업종 분류 기준)\n'
        '- 한 업종 비중이 과하게 크면 그 업종에 악재가 왔을 때 계좌 전체가 함께 흔들립니다'
    )


def render_portfolio_performance():
    """계좌 평가금액이 시간에 따라 어떻게 움직였는지, 그리고 그게 시장(코스피)보다
    나았는지를 함께 본다. 보유 종목 표는 "지금 이 순간"만 보여주기 때문에 추세와
    벤치마크 대비 성과가 빠져 있었다.

    한계: 실제 매매 이력(언제 얼마에 사고팔았는지)이 없어서, 지금 보유 중인 수량을
    과거에도 그대로 들고 있었다고 가정한 역산이다 — 실제 계좌 잔고 추이와는 다르며
    캡션에 그 사실을 밝힌다."""
    st.subheader('포트폴리오 성과 추이')

    portfolio_df = get_portfolio_df()
    if portfolio_df is None or portfolio_df.empty:
        st.info('보유 종목이 없어 성과 추이를 볼 수 없습니다.')
        return
    stocks = portfolio_df[portfolio_df['ticker'] != CASH_TICKER]
    if stocks.empty:
        st.info('보유 종목이 없어 성과 추이를 볼 수 없습니다.')
        return

    col_period, col_bench = st.columns(2)
    with col_period:
        period_label = st.selectbox(
            '기간', list(PORTFOLIO_PERF_PERIODS), index=2, key='perf_period')
    with col_bench:
        bench_label = st.selectbox('비교 지수', list(RS_BENCHMARKS), key='perf_benchmark')

    now = pd.Timestamp.now()
    start_dt = PORTFOLIO_PERF_PERIODS[period_label](now)
    holdings_key = tuple((str(r['ticker']), float(r['quantity'])) for _, r in stocks.iterrows())

    try:
        with st.spinner('포트폴리오 성과 계산 중...'):
            value_series = fetch_portfolio_value_series(holdings_key, start_dt)
    except Exception as e:
        st.warning(f'성과 추이를 계산하지 못했습니다: {e}')
        return
    if value_series.empty or len(value_series) < 2:
        st.info('성과를 계산할 만큼 주가 데이터가 충분하지 않습니다.')
        return

    bench_source, bench_code = RS_BENCHMARKS[bench_label]
    try:
        bench_df, _ = load_series_live(bench_source, bench_code, start_dt)
    except Exception:
        bench_df = pd.DataFrame()

    port_norm = value_series / value_series.iloc[0] * 100
    rows = [{'날짜': d, '구분': '내 포트폴리오', '지수(시작=100)': v} for d, v in port_norm.items()]

    bench_return = None
    if not bench_df.empty and 'Close' in bench_df.columns:
        bench_close = bench_df['Close']
        bench_close = bench_close[bench_close.index >= port_norm.index[0]]
        if len(bench_close) >= 2:
            bench_norm = bench_close / bench_close.iloc[0] * 100
            bench_return = bench_norm.iloc[-1] - 100
            rows += [{'날짜': d, '구분': bench_label, '지수(시작=100)': v} for d, v in bench_norm.items()]

    port_return = port_norm.iloc[-1] - 100

    m1, m2, m3 = st.columns(3)
    m1.metric('내 포트폴리오', f'{port_return:+.2f}%')
    m2.metric(f'{bench_label}', f'{bench_return:+.2f}%' if bench_return is not None else '-')
    if bench_return is not None:
        diff = port_return - bench_return
        m3.metric('상대 성과', f'{diff:+.2f}%p',
                  delta='시장보다 우수' if diff > 0 else ('시장보다 부진' if diff < 0 else '동일'),
                  delta_color='off')
    else:
        m3.metric('상대 성과', '-')

    chart = (
        alt.Chart(pd.DataFrame(rows))
        .mark_line()
        .encode(
            x=alt.X('날짜:T', title=None),
            y=alt.Y('지수(시작=100):Q', title='시작일=100', scale=alt.Scale(zero=False)),
            color=alt.Color(
                '구분:N', title=None,
                scale=alt.Scale(domain=['내 포트폴리오', bench_label], range=[SWISS_GREEN, SWISS_GRAY]),
            ),
            tooltip=[alt.Tooltip('날짜:T'), alt.Tooltip('구분:N'),
                     alt.Tooltip('지수(시작=100):Q', format=',.1f')],
        )
        .properties(height=340)
        .interactive()
    )
    st.altair_chart(chart, use_container_width=True)

    # 계좌 전체 리스크 — 종목별 MDD는 "종목 분석" 탭에 있지만 계좌를 합쳤을 때의
    # 낙폭/변동성은 어디에도 없었다. 위에서 이미 받아온 시계열로만 계산해 추가
    # API 호출이 없다.
    daily_returns = value_series.pct_change().dropna()
    running_max = value_series.cummax()
    drawdown = (value_series / running_max - 1) * 100
    max_drawdown = drawdown.min()
    trough_date = drawdown.idxmin()
    # 거래일 기준 연율화(1년 ≈ 252거래일) — 일간 변동성을 연 단위로 환산한 관행적 표기.
    annual_vol = daily_returns.std() * (252 ** 0.5) * 100 if len(daily_returns) > 1 else None

    r1, r2, r3 = st.columns(3)
    r1.metric('기간 내 최대 낙폭(MDD)', f'{max_drawdown:.2f}%')
    r2.metric('연환산 변동성', f'{annual_vol:.1f}%' if annual_vol is not None else '-')
    r3.metric('현재 고점 대비', f'{drawdown.iloc[-1]:.2f}%')
    trough_str = trough_date.strftime('%Y-%m-%d') if hasattr(trough_date, 'strftime') else str(trough_date)
    st.caption(
        f'최대 낙폭은 {trough_str}에 기록 · '
        'MDD = 기간 중 고점 대비 가장 크게 빠졌던 폭, 연환산 변동성 = 일간 등락폭을 1년 기준으로 환산한 값 '
        '(클수록 가격이 심하게 출렁인다는 뜻)'
    )

    actual_start = port_norm.index[0]
    start_str = actual_start.strftime('%Y-%m-%d') if hasattr(actual_start, 'strftime') else str(actual_start)
    st.markdown(
        f'- 두 선 모두 **{start_str}을 100으로 맞춰** 같은 출발선에서 비교합니다 — 위로 더 간 쪽이 더 많이 오른 것\n'
        '- **⚠️ 지금 보유 중인 수량을 과거에도 그대로 들고 있었다고 가정한 역산입니다** — '
        '실제 매매 이력(추가매수·매도 시점)이 없어서 진짜 계좌 잔고 추이와는 다릅니다\n'
        '- 상장이 늦은 종목이 있으면 그 종목 데이터가 생긴 날부터 계산해서, 선택한 기간보다 시작일이 늦을 수 있습니다\n'
        '- 출처: FinanceDataReader(보유 종목) · pykrx/yfinance(비교 지수)'
    )


def _diagnose_holding(ticker):
    """종목 하나에 5가지 이상 신호가 있는지 DART 데이터로 점검한다.
    반환: [(신호 라벨, 쉬운 설명 문자열)] — 이상 없으면 빈 리스트."""
    findings = []

    try:
        annual_years, _ = fetch_dart_financials_cached(ticker)
    except Exception:
        annual_years = []
    revenue_yoy = oi_yoy = latest_oi = None
    if len(annual_years) >= 2:
        latest, prev = annual_years[-1], annual_years[-2]
        revenue_yoy = _growth_pct(latest.get('revenue'), prev.get('revenue'))
        oi_yoy = _growth_pct(latest.get('operating_income'), prev.get('operating_income'))
        latest_oi = latest.get('operating_income')

    # 1. 마진 악화: 매출은 늘었는데 영업이익은 줄었다.
    if revenue_yoy is not None and oi_yoy is not None and revenue_yoy > 0 and oi_yoy < 0:
        findings.append((
            '마진 악화',
            f'매출은 {revenue_yoy:+.1f}% 늘었는데 영업이익은 {oi_yoy:+.1f}% 줄었습니다. '
            '팔리기는 하는데 남는 돈이 줄고 있다는 뜻 — 원가 상승이나 경쟁 심화로 수익성이 나빠지고 있을 수 있습니다.'
        ))

    # 2. 이익의 질: 영업이익은 있는데 실제 현금흐름은 그에 못 미친다.
    try:
        cfo_list = fetch_dart_cashflow_cached(ticker)
    except Exception:
        cfo_list = []
    if cfo_list and latest_oi is not None and latest_oi > 0:
        latest_cfo = cfo_list[-1]['cfo']
        if latest_cfo is not None and latest_cfo < latest_oi * 0.5:
            findings.append((
                '이익의 질 문제',
                f'영업이익은 {latest_oi:,.0f}억원인데 실제 영업활동현금흐름은 {latest_cfo:,.0f}억원에 그칩니다. '
                '장부상 이익만큼 현금이 안 들어온다는 뜻 — 외상매출(매출채권)이 쌓이고 있거나 재고가 안 팔리고 있을 가능성이 있습니다.'
            ))

    # 3. 재무 건전성 악화: 부채비율이 짧은 기간에 크게 뛰었다.
    try:
        debt_trend = fetch_dart_debt_trend_cached(ticker)
    except Exception:
        debt_trend = {}
    if len(debt_trend) >= 2:
        years_sorted = sorted(debt_trend.keys())
        old_debt, new_debt = debt_trend[years_sorted[0]], debt_trend[years_sorted[-1]]
        if old_debt is not None and new_debt - old_debt >= 30:
            findings.append((
                '재무 건전성 악화',
                f'부채비율이 {years_sorted[0]}년 {old_debt:.0f}%에서 {years_sorted[-1]}년 {new_debt:.0f}%로 '
                f'{new_debt - old_debt:+.0f}%p 뛰었습니다. 빚을 늘려 사업을 유지하고 있다는 신호일 수 있습니다.'
            ))

    # 4. 판매 부진: 매출 증가율보다 재고가 훨씬 빠르게 늘었다.
    try:
        inventory_list = fetch_dart_inventory_cached(ticker)
    except Exception:
        inventory_list = []
    if len(inventory_list) >= 2:
        latest_inv, prev_inv = inventory_list[-1]['inventory'], inventory_list[-2]['inventory']
        inv_yoy = _growth_pct(latest_inv, prev_inv)
        if inv_yoy is not None and inv_yoy >= 30 and (revenue_yoy is None or inv_yoy - revenue_yoy >= 20):
            revenue_note = f'매출 {revenue_yoy:+.1f}%' if revenue_yoy is not None else '매출 증가율 확인 불가'
            findings.append((
                '재고 급증',
                f'재고자산이 최근 1년 새 {inv_yoy:+.1f}% 늘었는데 {revenue_note}에 그쳤습니다. '
                '만든 건 늘었는데 안 팔리고 쌓이고 있다는 뜻 — 판매 부진 신호일 수 있습니다.'
            ))

    # 5. 대주주(5% 이상 대량보유자) 지분 매도 — 최근 90일 내 "처분" 사유 공시.
    try:
        trades = fetch_dart_major_trades_cached(ticker)
    except Exception:
        trades = []
    cutoff = (datetime.now() - pd.Timedelta(days=90)).strftime('%Y%m%d')
    recent_sells = [t for t in trades if (t.get('date') or '') >= cutoff and '처분' in (t.get('reason') or '')]
    if recent_sells:
        latest_sell = recent_sells[0]
        findings.append((
            '대주주 지분 매도',
            f'최근 90일 내 {latest_sell.get("holder") or "대주주"}의 지분 처분 공시가 있었습니다'
            f'(접수일 {latest_sell.get("date")}, 사유: {latest_sell.get("reason")}). '
            '회사 내부자·대주주가 주식을 파는 데는 여러 이유가 있을 수 있지만, '
            '규모가 크면 향후 전망에 대한 부정적 신호로 해석되기도 합니다.'
        ))

    return findings


def render_concentration_check():
    """보유 종목 집중점검 — 마진 악화·이익의 질·재무건전성·재고급증·대주주매도
    5가지 이상 신호를 DART 데이터로 자동 스캔한다. 종목 하나당 여러 번의 DART
    조회가 필요해 무겁다(하루 캐시로 완화). 예전 "리스크 지표"(MDD/52주위치/
    거래량배율/목표가·손절가) 패널을 대체한다 — 그 지표들은 정량 스코어카드
    (종목 분석 탭)에서 검색한 종목별로 그대로 볼 수 있다."""
    st.subheader('보유 종목 집중점검')

    portfolio_df = get_portfolio_df()
    if portfolio_df is None or portfolio_df.empty:
        st.info('보유 종목이 없어 집중점검을 할 수 없습니다.')
        return
    stocks = portfolio_df[portfolio_df['ticker'] != CASH_TICKER]
    if stocks.empty:
        st.info('보유 종목이 없어 집중점검을 할 수 없습니다.')
        return

    with st.spinner('보유 종목의 재무 데이터를 점검하는 중... (DART 조회, 시간이 걸릴 수 있습니다)'):
        results = [(row['name'], _diagnose_holding(str(row['ticker']))) for _, row in stocks.iterrows()]

    flagged = [(n, f) for n, f in results if f]
    clean = [n for n, f in results if not f]

    if not flagged:
        st.success('✅ 보유 종목 전체에서 5가지 이상 신호가 감지되지 않았습니다.')
    else:
        st.error(f'🚨 {len(flagged)}개 종목에서 이상 신호가 감지되었습니다 — ' + ', '.join(n for n, _ in flagged))
        for name, findings in flagged:
            with st.expander(f'⚠️ {name} — {len(findings)}건', expanded=True):
                for label, explanation in findings:
                    st.markdown(f'**{label}**  \n{explanation}')

    if clean:
        st.caption('이상 신호 없음: ' + ', '.join(clean))

    st.markdown(
        '- **① 마진 악화** — 매출 증가 + 영업이익 감소\n'
        '- **② 이익의 질** — 영업이익 대비 영업활동현금흐름 부족\n'
        '- **③ 재무 건전성** — 부채비율 급등\n'
        '- **④ 판매 부진** — 매출 증가율 대비 재고 급증\n'
        '- **⑤ 대주주 지분 매도** — 최근 90일 내 5% 이상 대량보유자의 지분 처분 공시 (소규모 임원 개인 거래는 포함되지 않을 수 있음)\n'
        '- 전부 DART 공시 데이터 기반 자동 판정이며 투자 조언이 아닙니다\n'
        '- 출처: DART, 하루 캐시'
    )


def _build_revenue_oi_chart(items, x_labels):
    """매출액·영업이익은 그룹 막대, 영업이익률은 꺾은선(보조축)으로 겹쳐 그린다.
    셋 다 같은 색상 스케일을 공유해서 범례 하나에 다 같이 뜬다. items는
    x_labels와 순서가 1:1로 대응하는 {'revenue':, 'operating_income':} 리스트."""
    bar_rows, margin_rows = [], []
    for label, it in zip(x_labels, items):
        revenue = it.get('revenue')
        op_income = it.get('operating_income')
        if revenue is not None:
            bar_rows.append({'구간': label, '항목': '매출액', '금액(억원)': revenue})
        if op_income is not None:
            bar_rows.append({'구간': label, '항목': '영업이익', '금액(억원)': op_income})
        if revenue and op_income is not None:
            margin_rows.append({'구간': label, '항목': '영업이익률(%)', '영업이익률(%)': op_income / revenue * 100})

    color_scale = alt.Scale(domain=['매출액', '영업이익', '영업이익률(%)'], range=[SWISS_GRAY, SWISS_GREEN, SWISS_WHITE])
    legend = alt.Legend(title=None)

    bars = (
        alt.Chart(pd.DataFrame(bar_rows))
        .mark_bar()
        .encode(
            x=alt.X('구간:N', title=None, sort=x_labels),
            xOffset=alt.XOffset('항목:N'),
            y=alt.Y('금액(억원):Q', title='억원'),
            color=alt.Color('항목:N', scale=color_scale, legend=legend),
            tooltip=[alt.Tooltip('구간:N'), alt.Tooltip('항목:N'), alt.Tooltip('금액(억원):Q', format=',.1f')],
        )
    )
    line = (
        alt.Chart(pd.DataFrame(margin_rows))
        .mark_line(point=True)
        .encode(
            x=alt.X('구간:N', title=None, sort=x_labels),
            y=alt.Y('영업이익률(%):Q', title='영업이익률(%)'),
            color=alt.Color('항목:N', scale=color_scale, legend=legend),
            tooltip=[alt.Tooltip('구간:N'), alt.Tooltip('영업이익률(%):Q', format='+.1f')],
        )
    )
    return alt.layer(bars, line).resolve_scale(y='independent').properties(height=350)


def _growth_pct(current, previous):
    """전기 대비 증감률(%) — 전기가 0/None이거나 현재값이 None이면 계산 불가."""
    if current is None or previous is None or previous == 0:
        return None
    return (current - previous) / abs(previous) * 100


def _render_growth_metrics(items, period_type):
    """최신 구간의 매출·영업이익 증감률을 카드로 보여준다. 연도별은 전년대비(YoY)만,
    분기별은 전분기대비(QoQ)와 전년동기대비(YoY) 둘 다 — 분기 실적은 계절성이 커서
    바로 전분기와 비교하면 오해하기 쉽고(예: 4분기가 항상 성수기인 업종), 같은 분기
    끼리 비교하는 전년동기대비가 추세 판단에 더 흔히 쓰인다."""
    if len(items) < 2:
        return
    latest = items[-1]

    if period_type == '연도별':
        prev = items[-2]
        rows = [('매출액 YoY', _growth_pct(latest.get('revenue'), prev.get('revenue'))),
                ('영업이익 YoY', _growth_pct(latest.get('operating_income'), prev.get('operating_income')))]
    else:
        qoq_prev = items[-2]
        yoy_prev = next(
            (it for it in items if it.get('year') == latest.get('year', 0) - 1
             and it.get('quarter') == latest.get('quarter')),
            None,
        )
        rows = [
            ('매출액 QoQ', _growth_pct(latest.get('revenue'), qoq_prev.get('revenue'))),
            ('매출액 YoY', _growth_pct(latest.get('revenue'), yoy_prev.get('revenue')) if yoy_prev else None),
            ('영업이익 QoQ', _growth_pct(latest.get('operating_income'), qoq_prev.get('operating_income'))),
            ('영업이익 YoY', _growth_pct(latest.get('operating_income'), yoy_prev.get('operating_income')) if yoy_prev else None),
        ]

    cols = st.columns(len(rows))
    for col, (label, value) in zip(cols, rows):
        col.metric(label, f'{value:+.1f}%' if value is not None else '-', delta_color='off')
    st.markdown(
        '- **YoY (Year over Year, 전년동기대비)** — 작년 같은 기간과 비교한 증감률\n'
        '- **QoQ (Quarter over Quarter, 전분기대비)** — 바로 직전 분기와 비교한 증감률 (계절성이 큰 업종에서는 왜곡될 수 있어 YoY와 함께 봅니다)\n'
        '- 증감률은 위 차트와 같은 구간(최신 항목) 기준입니다'
    )


PRICE_5Y_LOOKBACK_DAYS = 5 * 365  # 정량 스코어카드 "개요" 탭 맨 아래 5년 주가 흐름 차트용

MA_WINDOWS = (20, 60, 120)
MA_DISPLAY_DAYS = 3 * 365  # 이동평균·이격도·RSI·MACD 통합 그래프 표시 기간 — 최근 3년
MA_LOOKBACK_DAYS = MA_DISPLAY_DAYS + 150  # 120일 이동평균이 표시 구간 초반부터 안정되도록 여유를 더 받는다


BB_WINDOW = 20
BB_STD_MULT = 2
RSI_WINDOW = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9


def _render_moving_average_section(ticker, selected_name):
    """20·60·120일 이동평균선(+볼린저밴드)을 주가에 겹쳐 그리고, 이격도·RSI·MACD로
    추세 과열/과매도와 방향 전환 신호를 함께 본다. 전부 이미 받아오는 주가
    시계열(FDR)만으로 계산하고 새 API 호출은 없다."""
    st.subheader('이동평균선 · 이격도 · RSI · MACD')

    try:
        start_dt = datetime.now() - pd.Timedelta(days=MA_LOOKBACK_DAYS)
        with st.spinner('이동평균 계산 중...'):
            price_df = fetch_stock_series(ticker, start_dt)
    except Exception as e:
        st.warning(f'주가 데이터를 불러오지 못했습니다: {e}')
        return

    if price_df.empty or len(price_df) < MA_WINDOWS[0]:
        st.info('이동평균을 계산할 만큼 주가 데이터가 충분하지 않습니다.')
        return

    ma_df = price_df[['Close']].copy()
    for window in MA_WINDOWS:
        ma_df[f'MA{window}'] = ma_df['Close'].rolling(window=window).mean()

    # 볼린저밴드 — 20일 이동평균 ± 2표준편차. 주가가 상단 밴드 위/하단 밴드 아래로
    # 벗어나면 통계적으로 드문(과열/과매도) 구간이라는 뜻으로 흔히 쓰인다.
    bb_std = ma_df['Close'].rolling(window=BB_WINDOW).std()
    ma_df['BB상단'] = ma_df['MA20'] + BB_STD_MULT * bb_std
    ma_df['BB하단'] = ma_df['MA20'] - BB_STD_MULT * bb_std

    # RSI(14) — Wilder의 지수평활 방식. 70 이상 과매수, 30 이하 과매도로 흔히 해석한다.
    delta = ma_df['Close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / RSI_WINDOW, min_periods=RSI_WINDOW, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_WINDOW, min_periods=RSI_WINDOW, adjust=False).mean()
    rs = avg_gain / avg_loss
    ma_df['RSI'] = 100 - (100 / (1 + rs))
    ma_df.loc[avg_loss == 0, 'RSI'] = 100  # 손실이 아예 없으면 RS가 무한대 -> RSI 100

    # MACD(12,26,9) — 단기/장기 지수이동평균 차이(추세)와 그 신호선(9일 EMA).
    # MACD가 신호선을 상향/하향 돌파하면 흔히 골든/데드크로스로 해석한다.
    ema_fast = ma_df['Close'].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = ma_df['Close'].ewm(span=MACD_SLOW, adjust=False).mean()
    ma_df['MACD'] = ema_fast - ema_slow
    ma_df['MACD_signal'] = ma_df['MACD'].ewm(span=MACD_SIGNAL, adjust=False).mean()
    ma_df['MACD_hist'] = ma_df['MACD'] - ma_df['MACD_signal']

    display_df = ma_df.tail(MA_DISPLAY_DAYS).reset_index()
    date_col = display_df.columns[0]
    display_df = display_df.rename(columns={date_col: 'Date', 'Close': '주가'})

    ma_dash = {'MA20': [1, 0], 'MA60': [1, 0], 'MA120': [5, 3]}
    ma_labels = {'MA20': '20일선', 'MA60': '60일선', 'MA120': '120일선'}
    series_order = ['주가', '20일선', '60일선', '120일선', '볼린저 상단', '볼린저 하단']
    series_colors = [SWISS_WHITE, SWISS_GREEN, SWISS_GRAY, SWISS_GRAY, SWISS_GRAY, SWISS_GRAY]
    color_scale = alt.Scale(domain=series_order, range=series_colors)
    legend = alt.Legend(title=None)

    price_line = (
        alt.Chart(display_df.assign(구분='주가'))
        .mark_line()
        .encode(
            x=alt.X('Date:T', title=None),
            y=alt.Y('주가:Q', title='주가(원)', scale=alt.Scale(zero=False)),
            color=alt.Color('구분:N', scale=color_scale, legend=legend),
            tooltip=[alt.Tooltip('Date:T'), alt.Tooltip('주가:Q', title='주가', format=',.0f')],
        )
    )
    ma_lines = [
        alt.Chart(display_df.assign(구분=ma_labels[f'MA{w}']))
        .mark_line(strokeDash=ma_dash[f'MA{w}'])
        .encode(
            x=alt.X('Date:T', title=None),
            y=alt.Y(f'MA{w}:Q', title=None),
            color=alt.Color('구분:N', scale=color_scale, legend=legend),
            tooltip=[alt.Tooltip('Date:T'), alt.Tooltip(f'MA{w}:Q', title=f'{w}일선', format=',.0f')],
        )
        for w in MA_WINDOWS
    ]
    bb_labels = {'BB상단': '볼린저 상단', 'BB하단': '볼린저 하단'}
    bb_lines = [
        alt.Chart(display_df.assign(구분=bb_labels[label]))
        .mark_line(strokeDash=[2, 2], opacity=0.6)
        .encode(
            x=alt.X('Date:T', title=None),
            y=alt.Y(f'{label}:Q', title=None),
            color=alt.Color('구분:N', scale=color_scale, legend=legend),
            tooltip=[alt.Tooltip('Date:T'), alt.Tooltip(f'{label}:Q', title=f'볼린저밴드 {label}', format=',.0f')],
        )
        for label in ('BB상단', 'BB하단')
    ]
    chart = alt.layer(price_line, *ma_lines, *bb_lines).properties(height=380).interactive()

    st.altair_chart(chart, use_container_width=True)
    st.caption(
        f'{selected_name} 주가와 이동평균선·볼린저밴드(20일선 ± 2표준편차). 최근 3년치를 '
        '표시하되, 지표 자체는 그 이전 데이터까지 포함해 계산합니다.'
    )

    current = display_df['주가'].iloc[-1]
    cols = st.columns(len(MA_WINDOWS))
    for col, window in zip(cols, MA_WINDOWS):
        ma_value = display_df[f'MA{window}'].iloc[-1]
        gap = (current - ma_value) / ma_value * 100 if pd.notna(ma_value) and ma_value else None
        col.metric(f'{window}일선 이격도', f'{gap:+.1f}%' if gap is not None else '-', delta_color='off')
    st.caption('이격도 = (현재가 − 이동평균) / 이동평균 × 100. 양수면 이동평균 위, 음수면 아래에 있다는 뜻입니다.')

    st.divider()
    st.markdown('###### RSI(14, Relative Strength Index, 상대강도지수)')
    st.caption('최근 가격 상승압력과 하락압력의 상대적 크기를 0~100 사이 숫자로 나타낸 지표입니다.')
    rsi_chart_df = display_df[['Date', 'RSI']].dropna()
    if rsi_chart_df.empty:
        st.info('RSI를 계산할 만큼 데이터가 충분하지 않습니다.')
    else:
        rsi_line = (
            alt.Chart(rsi_chart_df)
            .mark_line(color=SWISS_GREEN)
            .encode(
                x=alt.X('Date:T', title=None),
                y=alt.Y('RSI:Q', title=None, scale=alt.Scale(domain=[0, 100])),
                tooltip=[alt.Tooltip('Date:T'), alt.Tooltip('RSI:Q', format='.1f')],
            )
        )
        rsi_bands = (
            alt.Chart(pd.DataFrame({'y': [30, 70]}))
            .mark_rule(strokeDash=[2, 2], color=SWISS_GRAY)
            .encode(y='y:Q')
        )
        st.altair_chart((rsi_bands + rsi_line).properties(height=220).interactive(), use_container_width=True)
        latest_rsi = rsi_chart_df['RSI'].iloc[-1]
        rsi_label = '과매수' if latest_rsi >= 70 else ('과매도' if latest_rsi <= 30 else '중립')
        st.metric('현재 RSI', f'{latest_rsi:.1f} ({rsi_label})', delta_color='off')
        st.markdown(
            '- **70 이상 (과매수)** — 최근 많이 올라 단기 조정 가능성을 흔히 경계하는 구간\n'
            '- **30 이하 (과매도)** — 최근 많이 내려 단기 반등 가능성을 흔히 기대하는 구간\n'
            '- **50 부근** — 상승압력과 하락압력이 비슷한 중립 구간, 50 위/아래로 추세 방향을 가늠하기도 함\n'
            '- 주가는 신고가를 갱신하는데 RSI는 이전 고점을 못 넘으면("다이버전스") 상승 동력이 약해지고 있다는 신호로 보기도 합니다'
        )

    st.divider()
    st.markdown('###### MACD(12, 26, 9, Moving Average Convergence Divergence, 이동평균수렴확산지수)')
    st.caption('단기 이동평균과 장기 이동평균의 차이로 추세 전환을 포착하는 지표입니다.')
    macd_chart_df = display_df[['Date', 'MACD', 'MACD_signal', 'MACD_hist']].dropna()
    if macd_chart_df.empty:
        st.info('MACD를 계산할 만큼 데이터가 충분하지 않습니다.')
    else:
        macd_scale = alt.Scale(domain=['MACD', '시그널', '히스토그램'], range=[SWISS_GREEN, SWISS_WHITE, SWISS_GRAY])
        macd_legend = alt.Legend(title=None)
        macd_bar = (
            alt.Chart(macd_chart_df.assign(구분='히스토그램'))
            .mark_bar(opacity=0.5)
            .encode(
                x=alt.X('Date:T', title=None),
                y=alt.Y('MACD_hist:Q', title=None),
                color=alt.Color('구분:N', scale=macd_scale, legend=macd_legend),
                tooltip=[alt.Tooltip('Date:T'), alt.Tooltip('MACD_hist:Q', title='히스토그램', format=',.0f')],
            )
        )
        macd_line = (
            alt.Chart(macd_chart_df.assign(구분='MACD'))
            .mark_line()
            .encode(x=alt.X('Date:T', title=None), y=alt.Y('MACD:Q', title=None),
                    color=alt.Color('구분:N', scale=macd_scale, legend=macd_legend),
                    tooltip=[alt.Tooltip('Date:T'), alt.Tooltip('MACD:Q', format=',.0f')])
        )
        signal_line = (
            alt.Chart(macd_chart_df.assign(구분='시그널'))
            .mark_line(strokeDash=[3, 2])
            .encode(x=alt.X('Date:T', title=None), y=alt.Y('MACD_signal:Q', title=None),
                    color=alt.Color('구분:N', scale=macd_scale, legend=macd_legend),
                    tooltip=[alt.Tooltip('Date:T'), alt.Tooltip('MACD_signal:Q', title='시그널', format=',.0f')])
        )
        st.altair_chart(
            (macd_bar + macd_line + signal_line).properties(height=220).interactive(), use_container_width=True
        )
        latest = macd_chart_df.iloc[-1]
        cross_label = 'MACD가 시그널선 위 (상승 모멘텀)' if latest['MACD'] >= latest['MACD_signal'] else 'MACD가 시그널선 아래 (하락 모멘텀)'
        st.metric('MACD − 시그널', f"{latest['MACD_hist']:+,.0f}", delta_color='off')
        st.caption(f'현재: {cross_label}')
        st.markdown(
            '- **골든크로스** — MACD가 시그널선을 아래에서 위로 뚫고 올라가면 매수 신호로 흔히 해석\n'
            '- **데드크로스** — MACD가 시그널선을 위에서 아래로 뚫고 내려가면 매도 신호로 흔히 해석\n'
            '- **0선 위/아래** — MACD가 0보다 위면 단기 이동평균이 장기 이동평균 위(상승추세), 아래면 그 반대\n'
            '- **히스토그램(막대)** — MACD와 시그널선의 차이. 막대가 커지면 모멘텀이 강해지고, 작아지면 모멘텀이 약해진다는 뜻'
        )


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

    col_left, col_right = st.columns(2)

    with col_left:
        st.altair_chart(_build_revenue_oi_chart(items, x_labels), use_container_width=True)

        partial_notes = [f"{it['year']}년: {it['partial']}" for it in items if it.get('partial')]
        caption = f'출처: {source_note} · {fs_div or ""} · 매출액·영업이익은 왼쪽 축, 영업이익률은 오른쪽 축입니다.'
        if partial_notes:
            caption += ' · 부분 실적 ' + ', '.join(partial_notes)
        st.caption(caption)

        _render_growth_metrics(items, period_type)

    with col_right:
        # 주가 vs 연간 영업이익 — 기간 단위 선택과 무관하게 항상 연도별.
        # 부분 실적 연도(설립 첫해 등)는 12개월치가 아니라 추이·비교를 왜곡하므로
        # 그래프에서는 제외하고, 정상 연도만으로 최근 7개년을 그린다.
        full_years = [y for y in annual_years if not y.get('partial')]
        if len(full_years) < 2:
            st.info('정상 실적 연도가 2개 미만이라 추이 그래프를 그릴 수 없습니다.')
        else:
            chart_years = full_years[-7:]
            start_dt = pd.Timestamp(year=chart_years[0]['year'], month=1, day=1)

            try:
                with st.spinner('주가 데이터 불러오는 중...'):
                    price_df = fetch_stock_series(ticker, start_dt)
                if price_df.empty:
                    st.warning('주가 데이터를 불러오지 못했습니다.')
                else:
                    price_line = price_df[['Close']].reset_index()
                    oi_df = pd.DataFrame([
                        {'Date': pd.Timestamp(year=y['year'], month=12, day=31), '영업이익': y['operating_income']}
                        for y in chart_years
                    ])

                    combo_scale = alt.Scale(domain=['주가', '영업이익'], range=[NEUTRAL_CHART_COLOR, SWISS_GREEN])
                    combo_legend = alt.Legend(title=None)
                    price_chart = (
                        alt.Chart(price_line.assign(구분='주가'))
                        .mark_line()
                        .encode(
                            x=alt.X('Date:T', title=None),
                            y=alt.Y('Close:Q', title='주가(원)', scale=alt.Scale(zero=False)),
                            color=alt.Color('구분:N', scale=combo_scale, legend=combo_legend),
                            tooltip=[alt.Tooltip('Date:T'), alt.Tooltip('Close:Q', title='주가', format=',.0f')],
                        )
                    )
                    oi_chart = (
                        alt.Chart(oi_df.assign(구분='영업이익'))
                        .mark_line(point=True)
                        .encode(
                            x=alt.X('Date:T', title=None),
                            y=alt.Y('영업이익:Q', title='영업이익(억원)', scale=alt.Scale(zero=False)),
                            color=alt.Color('구분:N', scale=combo_scale, legend=combo_legend),
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
                        f'{selected_name} 주가(왼쪽 축)와 연간 영업이익(오른쪽 축 · 각 연말 시점에 표시) '
                        f'— {chart_years[0]["year"]}~{chart_years[-1]["year"]}년. 두 값의 단위가 달라(원 vs 억원) '
                        '지수화 대신 서로 다른 축으로 함께 표시했습니다.'
                    )
            except Exception as e:
                st.warning(f'그래프를 그리지 못했습니다: {e}')

    _render_moving_average_section(ticker, selected_name)


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




def _stability_signal(debt_ratio, interest_coverage):
    if debt_ratio is None and interest_coverage is None:
        return 0, '➖ 안정성: 데이터가 부족해 판단할 수 없습니다.'
    warnings = []
    if interest_coverage is not None and interest_coverage < 1:
        warnings.append(f'이자보상배율 {interest_coverage:.1f}배(1 미만 — 영업이익으로 이자비용도 못 감당)')
    if debt_ratio is not None and debt_ratio > 200:
        warnings.append(f'부채비율 {debt_ratio:.0f}%(200% 초과)')
    if warnings:
        return -1, '⚠️ 안정성: ' + ', '.join(warnings) + ' — 재무 부담이 큽니다.'
    if debt_ratio is not None and debt_ratio < 100:
        return 1, f'✅ 안정성: 부채비율 {debt_ratio:.0f}%로 자기자본보다 부채가 적고, 특별한 위험 신호가 없습니다.'
    return 0, '➖ 안정성: 뚜렷한 위험 신호는 없으나 우량하다고 보기도 애매한 수준입니다.'


# 최근 1년 내 발생하면 지분 희석으로 볼 수 있는 자본변동 유형.
DILUTIVE_CHANGE_TYPES = ('유상증자', '전환권행사', '신주인수권행사')


def _dilution_signal(capital_changes):
    if not capital_changes:
        return 0, '➖ 희석위험: 최근 자본변동 이력을 찾지 못했습니다.'
    cutoff = (datetime.now() - pd.Timedelta(days=365)).strftime('%Y.%m.%d')
    recent = [c for c in capital_changes if c['type'] in DILUTIVE_CHANGE_TYPES and (c['date'] or '') >= cutoff]
    if recent:
        types_found = ', '.join(sorted({c['type'] for c in recent}))
        return -1, f'⚠️ 희석위험: 최근 1년 내 {types_found} 이력이 있습니다 — 지분 희석 가능성을 확인하세요.'
    return 1, '✅ 희석위험: 최근 1년 내 유상증자·전환사채 전환 등 희석 이벤트가 없습니다.'


def _tier_and_icon(value, low_cut, high_cut, higher_is_better):
    """값 하나를 (상/중/하 수준, 신호 아이콘)으로 바꾼다. low_cut/high_cut은
    "낮다/보통/높다" 구간의 경계값이고, higher_is_better는 그 지표가 높을수록
    좋은지(예: ROE) 낮을수록 좋은지(예: 부채비율)를 뜻한다 — 같은 "상"이라도
    지표에 따라 좋은 신호(✅)일 수도 나쁜 신호(❌)일 수도 있어 방향을 따로 받는다."""
    if value is None:
        return None, '-'
    level = '하' if value < low_cut else ('상' if value > high_cut else '중')
    if higher_is_better:
        icon = '✅' if value > high_cut else ('❌' if value < low_cut else '⚠️')
    else:
        icon = '✅' if value < low_cut else ('❌' if value > high_cut else '⚠️')
    return level, icon


def _peer_ratio_tier(value, peer_value, higher_is_better=False):
    """업종 평균 대비 실제 동종업계 중앙값과 비교한 상/중/하 — PER·PBR처럼 pykrx
    시장 전체 데이터로 진짜 업종 평균을 계산할 수 있는 지표에만 쓴다(비율 0.8/1.2
    를 경계로 삼는 건 기존 _valuation_signal과 같은 기준)."""
    if value is None or not peer_value or peer_value <= 0:
        return None, '-'
    ratio = value / peer_value
    return _tier_and_icon(ratio, 0.8, 1.2, higher_is_better)


def _build_indicator_table(rows):
    """rows: [(지표명, 값, 서식문자열, 수준, 아이콘)] -> st.dataframe에 바로 넣을 DataFrame."""
    return pd.DataFrame([
        {
            '지표': label,
            '값': (fmt.format(value) if value is not None else '-'),
            '업종 평균 대비': level or '-',
            '신호': icon,
        }
        for label, value, fmt, level, icon in rows
    ]).set_index('지표')


def _build_ratio_trend_chart(balance_ratios_by_year, interest_ratios_by_year):
    """부채비율·유동비율·이자보상배율·ROE·ROA·총자산회전율의 5개년 추이를
    지표별 꺾은선 그래프 6개(3열 그리드, 각자 독립된 y축)로 만든다. 단위가
    서로 달라(%, 배) 한 차트에 같이 그리면 비교가 안 되므로 나눴다. 5개 지표는
    fetch_dart_balance_ratios_5y_cached(원본 재무제표 계산, 이력이 김)를 쓰고
    이자보상배율만 fetch_dart_financial_ratios_5y_cached(fnlttSinglIndx, 이력이
    짧음)를 쓴다 — 이자비용 계정명이 회사마다 달라 원본에서 직접 계산하지 않는다."""
    balance_years = set(balance_ratios_by_year.keys())
    interest_years = set(interest_ratios_by_year.keys())
    if not balance_years and not interest_years:
        return None

    specs = [
        ('부채비율 (%)', balance_ratios_by_year, 'debt_ratio'),
        ('유동비율 (%)', balance_ratios_by_year, 'current_ratio'),
        ('이자보상배율 (배)', interest_ratios_by_year, 'interest_coverage'),
        ('ROE (%)', balance_ratios_by_year, 'roe'),
        ('ROA (%)', balance_ratios_by_year, 'roa'),
        ('총자산회전율 (배)', balance_ratios_by_year, 'asset_turnover_ratio'),
    ]
    rows = []
    for label, source, key in specs:
        for year, values in source.items():
            v = values.get(key)
            if key == 'asset_turnover_ratio':
                v = values.get('asset_turnover')
                v = v / 100 if v is not None else None
            if v is not None:
                rows.append({'연도': str(year), '지표': label, '값': v})
    if not rows:
        return None

    chart = (
        alt.Chart(pd.DataFrame(rows))
        .mark_line(point=True, color=SWISS_GREEN, strokeWidth=2.5)
        .encode(
            x=alt.X('연도:N', title=None),
            y=alt.Y('값:Q', title=None),
            tooltip=[alt.Tooltip('연도:N'), alt.Tooltip('지표:N'), alt.Tooltip('값:Q', format=',.2f')],
        )
        .properties(height=220, width=260)
        .facet(facet=alt.Facet('지표:N', title=None), columns=3)
        .resolve_scale(y='independent')
    )
    return chart


def _build_cfo_oi_chart(cfo_list, oi_by_year):
    """영업활동현금흐름과 영업이익을 연도별 그룹 막대로 겹쳐 그린다("이익의 질"
    확인용) — 둘 다 억원 단위라 축을 하나만 쓴다."""
    rows = []
    for item in cfo_list:
        year = str(item['year'])
        if item.get('cfo') is not None:
            rows.append({'연도': year, '항목': '영업활동현금흐름', '금액(억원)': item['cfo']})
        oi = oi_by_year.get(item['year'])
        if oi is not None:
            rows.append({'연도': year, '항목': '영업이익', '금액(억원)': oi})
    if not rows:
        return None

    years_order = [str(item['year']) for item in cfo_list]
    return (
        alt.Chart(pd.DataFrame(rows))
        .mark_bar()
        .encode(
            x=alt.X('연도:N', title=None, sort=years_order),
            xOffset=alt.XOffset('항목:N'),
            y=alt.Y('금액(억원):Q', title='억원'),
            color=alt.Color(
                '항목:N', title=None,
                scale=alt.Scale(domain=['영업활동현금흐름', '영업이익'], range=[SWISS_GREEN, SWISS_GRAY]),
            ),
            tooltip=[alt.Tooltip('연도:N'), alt.Tooltip('항목:N'), alt.Tooltip('금액(억원):Q', format=',.0f')],
        )
        .properties(height=280)
    )


def _build_trend_commentary(annual_years, quarterly_items):
    """2단계(추이 분석)용 — 5개년·20분기 숫자를 문장으로 풀어준다. 표·차트만
    보고 방향을 스스로 읽어야 했던 걸, "그래서 늘었다는 거야 줄었다는 거야"를
    바로 알 수 있게 한다."""
    lines = []
    full_years = [y for y in annual_years if not y.get('partial')]
    if len(full_years) >= 2:
        first, last = full_years[0], full_years[-1]
        rev_first, rev_last = first.get('revenue'), last.get('revenue')
        oi_first, oi_last = first.get('operating_income'), last.get('operating_income')
        if rev_first and rev_last is not None:
            chg = (rev_last - rev_first) / abs(rev_first) * 100
            direction = '증가' if chg > 0 else ('감소' if chg < 0 else '보합')
            lines.append(
                f"매출액은 {first['year']}년 {rev_first:,.0f}억원에서 {last['year']}년 {rev_last:,.0f}억원으로 "
                f'{direction}했습니다 ({chg:+.1f}%, {len(full_years)}개년 기준).'
            )
        if oi_first is not None and oi_last is not None and oi_first != 0:
            chg = (oi_last - oi_first) / abs(oi_first) * 100
            direction = '증가' if chg > 0 else ('감소' if chg < 0 else '보합')
            lines.append(
                f"영업이익은 {first['year']}년 {oi_first:,.0f}억원에서 {last['year']}년 {oi_last:,.0f}억원으로 "
                f'{direction}했습니다 ({chg:+.1f}%).'
            )
        if rev_first and rev_last and oi_first is not None and oi_last is not None and rev_first > 0 and rev_last > 0:
            m_first, m_last = oi_first / rev_first * 100, oi_last / rev_last * 100
            verb = '개선' if m_last > m_first else ('악화' if m_last < m_first else '유지')
            lines.append(f"영업이익률은 {first['year']}년 {m_first:.1f}%에서 {last['year']}년 {m_last:.1f}%로 {verb}됐습니다.")

    if len(quarterly_items) >= 8:
        recent4, first4 = quarterly_items[-4:], quarterly_items[:4]

        def _avg(items, key):
            vals = [it[key] for it in items if it.get(key) is not None]
            return sum(vals) / len(vals) if vals else None

        rev_recent, rev_first = _avg(recent4, 'revenue'), _avg(first4, 'revenue')
        if rev_recent is not None and rev_first:
            chg = (rev_recent - rev_first) / abs(rev_first) * 100
            lines.append(
                f'최근 4개 분기 평균 매출({rev_recent:,.0f}억원)은 {len(quarterly_items)}분기 전 4개 분기 평균'
                f"({rev_first:,.0f}억원) 대비 {chg:+.1f}% {'증가' if chg > 0 else '감소'}했습니다."
            )

    if not lines:
        return None
    return '\n'.join(f'- {line}' for line in lines)


def _sector_rank_text(label, value, universe_col, universe, industry_name, ticker):
    """3단계(경쟁사 비교)용 — "업종 N개 종목 중 값이 낮은 순으로 몇 등(하위 몇%)"
    형태로 순위를 매긴다. 값이 낮을수록 저평가로 보는 PER/PBR용 — 순위가 낮을수록
    저평가 쪽이라는 뜻이라, 그대로 "낮은 순 등수"를 보여주는 게 제일 직관적이다."""
    if value is None or universe is None or universe.empty or not industry_name:
        return None
    if universe_col not in universe.columns or '업종명' not in universe.columns:
        return None
    peers = universe[(universe['업종명'] == industry_name) & (universe[universe_col] > 0)]
    if peers.empty:
        return None
    peer_vals = peers[universe_col]
    rank = int((peer_vals < value).sum()) + 1
    total = len(peer_vals) + (0 if ticker in peer_vals.index else 1)
    pct = rank / total * 100
    return f'{label} 기준 {industry_name} 업종 {total}개 종목 중 낮은 순으로 {rank}위(하위 {pct:.0f}%) — 낮을수록 저평가 쪽입니다.'


def _derive_investment_signals(ctx):
    """4단계(투자 신호)용 — 이미 계산된 값들만으로 최대 3개 긍정 + 3개 부정 신호를
    우선순위(밸류에이션 → 재무건전성 → 수익성 → 성장성 → 모멘텀 → 지배구조) 순으로
    뽑는다. 조건에 안 맞으면 그 신호는 그냥 빠진다 — 억지로 3개씩 채우지 않는다."""
    positives, negatives = [], []

    per, industry_per = ctx.get('per'), ctx.get('industry_per')
    if per is not None and industry_per and industry_per > 0:
        ratio = per / industry_per
        if ratio < 0.8:
            positives.append(f'PER {per:.1f}배로 업종 평균({industry_per:.1f}배) 대비 저평가 상태입니다.')
        elif ratio > 1.2:
            negatives.append(f'PER {per:.1f}배로 업종 평균({industry_per:.1f}배) 대비 고평가 상태입니다.')

    debt_ratio = ctx.get('debt_ratio')
    if debt_ratio is not None:
        if debt_ratio < 100:
            positives.append(f'부채비율 {debt_ratio:.0f}%로 재무구조가 안정적입니다.')
        elif debt_ratio > 200:
            negatives.append(f'부채비율 {debt_ratio:.0f}%로 재무 부담이 큽니다.')

    interest_coverage = ctx.get('interest_coverage')
    if interest_coverage is not None and interest_coverage < 1:
        negatives.append(f'이자보상배율 {interest_coverage:.1f}배로 영업이익으로 이자비용도 못 감당합니다.')

    roe = ctx.get('roe')
    if roe is not None:
        if roe >= 15:
            positives.append(f'ROE {roe:.1f}%로 자기자본 대비 수익성이 우수합니다.')
        elif roe < 5:
            negatives.append(f'ROE {roe:.1f}%로 자기자본 대비 수익성이 저조합니다.')

    revenue_yoy, oi_yoy = ctx.get('revenue_yoy'), ctx.get('oi_yoy')
    if revenue_yoy is not None and oi_yoy is not None:
        if revenue_yoy > 0 and oi_yoy > 0:
            positives.append(f'매출 {revenue_yoy:+.1f}%, 영업이익 {oi_yoy:+.1f}%로 동반 성장 중입니다.')
        elif revenue_yoy < 0 and oi_yoy < 0:
            negatives.append(f'매출 {revenue_yoy:+.1f}%, 영업이익 {oi_yoy:+.1f}%로 동반 역성장 중입니다.')
        elif revenue_yoy > 0 and oi_yoy < 0:
            negatives.append(f'매출은 {revenue_yoy:+.1f}% 늘었지만 영업이익은 {oi_yoy:+.1f}% 줄어 마진이 악화되고 있습니다.')

    pos_52w = ctx.get('pos_52w')
    if pos_52w is not None:
        if pos_52w <= 20:
            positives.append(f'52주 구간의 하위 {pos_52w:.0f}% 지점(저점권)에 있습니다.')
        elif pos_52w >= 80:
            negatives.append(f'52주 구간의 상위 {100 - pos_52w:.0f}% 지점(고점권)에 있어 추격 매수에 주의가 필요합니다.')

    if ctx.get('dilution_score') == 1:
        positives.append('최근 1년 내 유상증자·전환사채 전환 등 지분 희석 이벤트가 없습니다.')
    elif ctx.get('dilution_score') == -1:
        negatives.append('최근 1년 내 지분 희석 이벤트가 있었습니다 — 지분 희석 가능성을 확인해보세요.')

    if ctx.get('cfo_gap_warning'):
        negatives.append('최근 영업활동현금흐름이 영업이익보다 지속적으로 적어 "이익의 질"이 우려됩니다.')

    return positives[:3], negatives[:3]


def _build_overall_assessment(ctx):
    """5단계(종합 평가)용 — 점수를 다시 매기지 않고("종합판정"은 앞서 없앴다),
    긍정/부정 신호 개수만으로 문장 하나짜리 결론을 만들고, 이번 조회에서
    데이터가 비어 있던 항목들을 "추가로 확인이 필요한 사항"으로 모은다."""
    positives, negatives = ctx['positives'], ctx['negatives']
    if len(positives) > len(negatives):
        lean = '전반적으로 긍정적 요인이 더 많이 관찰됩니다.'
    elif len(negatives) > len(positives):
        lean = '전반적으로 주의가 필요한 요인이 더 많이 관찰됩니다.'
    else:
        lean = '긍정적 요인과 주의 요인이 비슷하게 섞여 있습니다.'

    follow_ups = []
    if ctx.get('interest_coverage') is None:
        follow_ups.append('이자보상배율 데이터가 없어 이자비용 감당 능력을 별도로 확인해볼 필요가 있습니다.')
    if not ctx.get('related_news_found'):
        follow_ups.append('최근 관련 뉴스가 확인되지 않아, 공시·이벤트 탭이나 증권사 리포트로 최신 동향을 따로 확인해보세요.')
    if ctx.get('shareholder_pct') is None:
        follow_ups.append('최대주주 지분율 데이터를 찾지 못해 지배구조 안정성을 별도로 확인해볼 필요가 있습니다.')
    if ctx.get('per') is None:
        follow_ups.append('PER을 계산할 EPS 데이터가 없어 밸류에이션 판단에 참고가 제한적입니다.')
    if not follow_ups:
        follow_ups.append('이번 조회에서는 크게 비어 있는 데이터가 없었습니다 — 그래도 아래 각 단계 탭의 세부 수치를 직접 한 번씩 확인해보세요.')

    return lean, follow_ups


def render_quant_scorecard():
    """OpenAI API 없이, PER/업계PER·재무 안정성·수급·기술·공시 데이터를 종목별로
    모아 보여준다. 보유 종목 여부와 무관하게 코스피·코스닥 상장 종목이면
    무엇이든 검색해서 볼 수 있다."""
    listing = load_krx_listing()

    if listing.empty:
        st.warning('종목 목록을 불러오지 못했습니다. 종목코드를 직접 입력해 주세요.')
        ticker = st.text_input('종목코드 (6자리)', key='quant_ticker_manual').strip()
        if not ticker:
            return
        name, market = ticker, None
    else:
        listing_sorted = listing.sort_values('Name')
        labels = [f'{row.Name} ({code})' for code, row in listing_sorted.iterrows()]
        code_by_label = dict(zip(labels, listing_sorted.index))
        selected_label = st.selectbox(
            '종목 검색', labels, index=None, placeholder='종목명 또는 코드로 검색 (예: 삼성전자, 005930)',
            key='quant_search',
        )
        if not selected_label:
            st.info('분석할 종목을 선택하세요. 코스피·코스닥 전 종목이 검색 대상입니다.')
            return
        ticker = code_by_label[selected_label]
        name = listing_sorted.loc[ticker, 'Name']
        market = listing_sorted.loc[ticker, 'Market']

    try:
        start_dt = datetime.now() - pd.Timedelta(days=MA_LOOKBACK_DAYS)
        with st.spinner(f'{name} 데이터를 불러오는 중...'):
            price_df = fetch_stock_series(ticker, start_dt)
    except Exception as e:
        st.warning(f'주가 데이터를 불러오지 못했습니다: {e}')
        return
    if price_df.empty:
        st.warning(f'{name}({ticker})의 주가 데이터를 찾지 못했습니다.')
        return
    current_price = float(price_df['Close'].iloc[-1])

    try:
        price_5y_df = fetch_stock_series(ticker, datetime.now() - pd.Timedelta(days=PRICE_5Y_LOOKBACK_DAYS))
    except Exception:
        price_5y_df = pd.DataFrame()

    stocks = pd.DataFrame([{'ticker': ticker, 'market': market, 'current_price': current_price}])
    try:
        stocks = attach_per_columns(stocks)
    except Exception:
        stocks['per'], stocks['industry_name'], stocks['industry_per'] = None, None, None
    try:
        stocks = attach_risk_columns(stocks)
    except Exception:
        stocks['mdd'], stocks['pos_52w'], stocks['vol_ratio'] = None, None, None
    row = stocks.iloc[0]
    per, industry_per = row.get('per'), row.get('industry_per')
    mdd, pos_52w, vol_ratio = row.get('mdd'), row.get('pos_52w'), row.get('vol_ratio')

    try:
        annual_years, _ = fetch_dart_financials_cached(ticker)
    except Exception:
        annual_years = []
    revenue_yoy = oi_yoy = None
    if len(annual_years) >= 2:
        latest, prev = annual_years[-1], annual_years[-2]
        revenue_yoy = _growth_pct(latest.get('revenue'), prev.get('revenue'))
        oi_yoy = _growth_pct(latest.get('operating_income'), prev.get('operating_income'))

    industry_name = row.get('industry_name')
    pbr = industry_pbr = None
    universe = None  # 3단계(경쟁사 비교)의 업종 내 순위 계산에도 재사용 — market이
    # KOSPI/KOSDAQ가 아니거나 조회가 실패해도 항상 정의돼 있도록 미리 None으로 둔다.
    if market in ('KOSPI', 'KOSDAQ'):
        try:
            universe = fetch_per_universe(market)
            if not universe.empty and ticker in universe.index:
                raw_pbr = universe.loc[ticker, 'PBR']
                pbr = raw_pbr if raw_pbr and raw_pbr > 0 else None
                if industry_name and '업종명' in universe.columns:
                    peers = universe[
                        (universe['업종명'] == industry_name) & (universe.index != ticker) & (universe['PBR'] > 0)
                    ]
                    industry_pbr = peers['PBR'].median() if not peers.empty else None
        except Exception:
            pbr = industry_pbr = None
            universe = None

    try:
        ratios = fetch_dart_financial_ratios_cached(ticker)
    except Exception:
        ratios = {}
    debt_ratio = ratios.get('debt_ratio')
    current_ratio = ratios.get('current_ratio')
    interest_coverage = ratios.get('interest_coverage')
    roe = ratios.get('roe')
    net_margin = ratios.get('net_margin')
    asset_turnover_pct = ratios.get('asset_turnover')  # DART가 %로 줌(예: 30.245 = 0.30배)
    asset_turnover = asset_turnover_pct / 100 if asset_turnover_pct is not None else None
    roa = net_margin * asset_turnover_pct / 100 if net_margin is not None and asset_turnover_pct is not None else None
    psr = per * net_margin / 100 if per is not None and net_margin is not None and net_margin > 0 else None
    peg = per / oi_yoy if per is not None and oi_yoy is not None and oi_yoy > 0 else None

    try:
        quarterly_items, _ = fetch_dart_quarterly_financials_5y_cached(ticker)
    except Exception:
        quarterly_items = []

    try:
        cfo_list = fetch_dart_cashflow_cached(ticker)
    except Exception:
        cfo_list = []
    try:
        capital_changes = fetch_dart_capital_changes_cached(ticker)
    except Exception:
        capital_changes = []
    try:
        shareholder = fetch_dart_largest_shareholder_cached(ticker)
    except Exception:
        shareholder = {}
    try:
        interest_ratios_by_year = fetch_dart_financial_ratios_5y_cached(ticker)
    except Exception:
        interest_ratios_by_year = {}
    try:
        balance_ratios_by_year = fetch_dart_balance_ratios_5y_cached(ticker)
    except Exception:
        balance_ratios_by_year = {}
    net_income = None
    if balance_ratios_by_year:
        latest_bs_year = max(balance_ratios_by_year.keys())
        net_income = balance_ratios_by_year[latest_bs_year].get('net_income')
    try:
        news_df = get_news_df()
    except Exception:
        news_df = pd.DataFrame()
    related_news = pd.DataFrame()
    if not news_df.empty and 'title' in news_df.columns:
        related_news = news_df[news_df['title'].str.contains(name, case=False, na=False, regex=False)]
        related_news = related_news.sort_values('published_at', ascending=False)

    stability_score, stability_text = _stability_signal(debt_ratio, interest_coverage)
    dilution_score, dilution_text = _dilution_signal(capital_changes)

    oi_by_year = {y['year']: y.get('operating_income') for y in annual_years}
    cfo_gap_years = [
        item for item in cfo_list
        if item.get('cfo') is not None and oi_by_year.get(item['year']) is not None
        and item['cfo'] < oi_by_year[item['year']]
    ]
    cfo_gap_warning = len(cfo_list) >= 1 and len(cfo_gap_years) >= max(2, (len(cfo_list) + 1) // 2)

    signal_ctx = {
        'per': per, 'industry_per': industry_per, 'debt_ratio': debt_ratio,
        'interest_coverage': interest_coverage, 'roe': roe,
        'revenue_yoy': revenue_yoy, 'oi_yoy': oi_yoy, 'pos_52w': pos_52w,
        'dilution_score': dilution_score, 'cfo_gap_warning': cfo_gap_warning,
    }
    positives, negatives = _derive_investment_signals(signal_ctx)
    assessment_ctx = dict(signal_ctx)
    assessment_ctx.update({
        'positives': positives, 'negatives': negatives,
        'related_news_found': not related_news.empty,
        'shareholder_pct': shareholder.get('total_pct'),
    })
    overall_lean, follow_ups = _build_overall_assessment(assessment_ctx)

    current_rows = [
        ('PER (배)', per, '{:.1f}', *_peer_ratio_tier(per, industry_per, higher_is_better=False)),
        ('PBR (배)', pbr, '{:.2f}', *_peer_ratio_tier(pbr, industry_pbr, higher_is_better=False)),
        ('부채비율 (%)', debt_ratio, '{:.0f}', *_tier_and_icon(debt_ratio, 100, 200, higher_is_better=False)),
        ('유동비율 (%)', current_ratio, '{:.0f}', *_tier_and_icon(current_ratio, 100, 200, higher_is_better=True)),
        (
            '이자보상배율 (배)', interest_coverage, '{:.1f}',
            *_tier_and_icon(interest_coverage, 1, 3, higher_is_better=True),
        ),
        ('ROE (%)', roe, '{:.1f}', *_tier_and_icon(roe, 5, 15, higher_is_better=True)),
        ('ROA (%)', roa, '{:.1f}', *_tier_and_icon(roa, 3, 8, higher_is_better=True)),
        ('총자산회전율 (배)', asset_turnover, '{:.2f}', *_tier_and_icon(asset_turnover, 0.5, 1.5, higher_is_better=True)),
    ]

    st.markdown(f'##### {name} ({ticker}) · {market or "-"}')
    if stability_score == -1 or dilution_score == -1:
        risk_lines = [t for t, s in zip((stability_text, dilution_text), (stability_score, dilution_score)) if s == -1]
        st.error('🚨 **주의: 재무·지배구조 위험 신호가 있습니다** — ' + ' / '.join(risk_lines))

    tab_step1, tab_step2, tab_step3, tab_step4, tab_step5, tab_flow, tab_events = st.tabs([
        '1단계 · 핵심 지표', '2단계 · 추이 분석', '3단계 · 경쟁사 비교',
        '4단계 · 투자 신호', '5단계 · 종합 평가', '수급·기술', '공시·이벤트',
    ])

    with tab_step1:
        st.markdown('###### 요약')
        c1, c2, c3, c4 = st.columns(4)
        c1.metric('현재가', f'{current_price:,.0f}원')
        c2.metric('PER', f'{per:.1f}배' if per is not None else '-')
        c3.metric('업계 PER', f'{industry_per:.1f}배' if industry_per is not None else '-')
        c4.metric('52주 위치', f'{pos_52w:.0f}%' if pos_52w is not None else '-')
        c5, c6, c7, c8 = st.columns(4)
        c5.metric('고점대비(MDD)', f'{mdd:.1f}%' if mdd is not None else '-')
        c6.metric('거래량배율', f'{vol_ratio:.1f}배' if vol_ratio is not None else '-')
        c7.metric('매출 YoY', f'{revenue_yoy:+.1f}%' if revenue_yoy is not None else '-')
        c8.metric('영업이익 YoY', f'{oi_yoy:+.1f}%' if oi_yoy is not None else '-')
        if mdd is not None and mdd >= -0.5:
            st.caption('🔔 52주 신고가 갱신 중입니다.')
        elif pos_52w is not None and pos_52w <= 0.5:
            st.caption('🔔 52주 신저가 갱신 중입니다.')
        if vol_ratio is not None and vol_ratio >= VOLUME_SURGE_RATIO:
            st.caption(f'🔔 거래량: 20일 평균 대비 {vol_ratio:.1f}배로 급증했습니다.')

        st.divider()
        st.markdown('###### 핵심 재무 지표 (매출 · 영업이익 · 순이익 · 부채비율 · ROE)')
        latest_annual = annual_years[-1] if annual_years else {}
        f1, f2, f3, f4, f5 = st.columns(5)
        f1.metric('매출액', f"{latest_annual['revenue']:,.0f}억원" if latest_annual.get('revenue') is not None else '-')
        f2.metric(
            '영업이익',
            f"{latest_annual['operating_income']:,.0f}억원" if latest_annual.get('operating_income') is not None else '-',
        )
        f3.metric('순이익', f'{net_income:,.0f}억원' if net_income is not None else '-')
        f4.metric('부채비율', f'{debt_ratio:.0f}%' if debt_ratio is not None else '-')
        f5.metric('ROE', f'{roe:.1f}%' if roe is not None else '-')
        if latest_annual.get('year') is not None:
            st.caption(f"{latest_annual['year']}년 사업보고서 기준(순이익·부채비율·ROE는 원본 재무제표에서 직접 계산) · 출처: DART")

        st.divider()
        st.markdown('###### 전체 지표 표')
        st.dataframe(_build_indicator_table(current_rows), use_container_width=True)
        st.caption(f'업종: {industry_name or "-"}')
        st.markdown(
            '- **PER (Price Earnings Ratio, 주가수익비율)** — 주가 ÷ 주당순이익(EPS). 낮을수록 이익 대비 주가가 저렴 '
            '(0.8배 미만=하/저평가, 1.2배 초과=상/고평가, 업종 중앙값과 비교)\n'
            '- **PBR (Price Book-value Ratio, 주가순자산비율)** — 주가 ÷ 주당순자산. 1배 미만이면 지금 회사를 청산했을 때 '
            '받는 돈보다 주가가 싸다는 뜻 (기준은 PER과 동일)\n'
            '- **부채비율** — 총부채 ÷ 자기자본(%). 100% 미만 안정, 200% 초과 위험\n'
            '- **유동비율** — 유동자산 ÷ 유동부채(%). 1년 내 갚을 빚을 감당할 여력 — 100% 미만 위험, 200% 초과 안정\n'
            '- **이자보상배율** — 영업이익 ÷ 이자비용(배). 1배 미만이면 번 돈으로 이자도 못 갚는다는 뜻, 3배 초과면 안전\n'
            '- **ROE (Return on Equity, 자기자본이익률)** — 자기자본 대비 순이익 비율(%). 5% 미만 낮음, 15% 초과 우수\n'
            '- **ROA (Return on Assets, 총자산이익률)** — 전체 자산 대비 순이익 비율(%). 3% 미만 낮음, 8% 초과 우수 '
            '(DART가 따로 안 줘서 순이익률×총자산회전율로 근사 계산)\n'
            '- **총자산회전율** — 매출액 ÷ 총자산(배). 자산을 얼마나 효율적으로 매출로 굴렸는지 — 업종별 편차가 커서 '
            '0.5배/1.5배 기준의 의미가 제한적\n'
            '- 부채비율~총자산회전율 6개는 pykrx가 시장 전체로 제공하지 않아 실제 업종 평균이 아니라 재무분석에서 흔히 쓰는 일반적 기준선입니다\n'
            '- ✅=우호적 신호, ⚠️=평균 수준, ❌=주의 신호 — 투자 조언이 아닙니다'
        )

        st.divider()
        p1, p2 = st.columns(2)
        p1.metric('PSR', f'{psr:.2f}배' if psr is not None else '-')
        p2.metric('PEG', f'{peg:.2f}' if peg is not None else '-')
        st.markdown(
            '- **PSR (Price Sales Ratio, 주가매출비율)** — 시가총액 ÷ 매출액. 적자라 PER을 못 구할 때 대안으로 씀 '
            '(이 페이지에서는 PER × 순이익률로 계산)\n'
            '- **PEG (Price Earnings to Growth Ratio, 주가수익성장비율)** — PER ÷ 영업이익 성장률(YoY). 성장성 대비 '
            'PER이 비싼지 판단 — 1 미만이면 저평가, 1 초과면 고평가로 보는 게 일반적\n'
            '- **YoY (Year over Year, 전년동기대비)** — 작년 같은 기간과 비교한 증감률\n'
            '- **MDD (Maximum Drawdown, 최대낙폭)** — 52주 최고가 대비 현재가가 얼마나 떨어졌는지'
        )

    with tab_step2:
        st.markdown('###### 추이 해석')
        commentary = _build_trend_commentary(annual_years, quarterly_items)
        if commentary:
            st.markdown(commentary)
        else:
            st.caption('추이를 해석할 만큼 연도별·분기별 데이터가 충분하지 않습니다.')

        if len(annual_years) >= 2:
            st.divider()
            chart_items = annual_years[-5:]
            x_labels = [str(y['year']) for y in chart_items]
            st.altair_chart(_build_revenue_oi_chart(chart_items, x_labels), use_container_width=True)
            st.caption('연도별 매출액·영업이익·영업이익률 · 출처: DART 사업보고서 (최근 5개년)')

        if len(quarterly_items) >= 2:
            st.divider()
            q_x_labels = [f"{it['year']}년 {it['quarter']}분기" for it in quarterly_items]
            st.altair_chart(_build_revenue_oi_chart(quarterly_items, q_x_labels), use_container_width=True)
            st.caption(f'분기별 매출액·영업이익·영업이익률 · 최근 {len(quarterly_items)}개 분기 · 출처: DART 분기/반기/사업보고서')

        st.divider()
        st.markdown('###### 재무비율 5개년 추이')
        ratio_trend_chart = _build_ratio_trend_chart(balance_ratios_by_year, interest_ratios_by_year)
        if ratio_trend_chart is None:
            st.caption('5개년 추이를 계산할 데이터를 찾지 못했습니다.')
        else:
            st.altair_chart(ratio_trend_chart, use_container_width=True)
            st.markdown(
                '- 부채비율·유동비율·ROE·ROA·총자산회전율의 최근 5개 사업연도 추이입니다 (원본 재무제표에서 직접 계산)\n'
                '- 이자보상배율은 이자비용 계정명이 회사마다 달라 DART가 계산해주는 값을 그대로 쓰는데, 최근 1~2개년치만 '
                '있는 경우가 많아 다른 지표보다 점이 적을 수 있습니다\n'
                '- PER·PBR은 시가 기준이라 연도별 이력을 안정적으로 구할 무료 API가 없어 이 그래프에서는 빠졌습니다 '
                '(1단계 "전체 지표 표"의 오늘 기준 값만 제공)\n'
                '- 출처: DART 사업보고서 재무제표·재무지표(하루 캐시)'
            )

        if len(cfo_list) >= 1:
            st.divider()
            st.markdown('###### 이익의 질 — 영업활동현금흐름 vs 영업이익 (최근 5개년)')
            cfo_oi_chart = _build_cfo_oi_chart(cfo_list, oi_by_year)
            if cfo_oi_chart is None:
                st.caption('이익의 질을 계산할 데이터를 찾지 못했습니다.')
            else:
                st.altair_chart(cfo_oi_chart, use_container_width=True)
                if cfo_gap_warning:
                    st.warning(
                        '⚠️ 최근 몇 개년 중 절반 이상에서 영업활동현금흐름이 영업이익보다 적습니다 — '
                        '회계상 이익만큼 실제 현금이 들어오지 않고 있다는 뜻일 수 있어(매출채권 누적, 재고 증가 등) '
                        '"이익의 질"을 한번 확인해볼 필요가 있습니다.'
                    )
                st.markdown(
                    '- **영업활동현금흐름(CFO, Cash Flow from Operations)** — 실제로 영업에서 들어오고 나간 현금\n'
                    '- **영업이익** — 회계상 장부에 기록된 이익 (실제 현금 유출입과는 다를 수 있음)\n'
                    '- 두 값이 비슷하게 움직이면 "이익의 질"이 좋다고 보고, 영업이익은 느는데 현금흐름이 안 따라오면 '
                    '주의 신호로 봅니다\n'
                    '- 출처: DART 사업보고서 전체 재무제표(현금흐름표), 하루 캐시'
                )

        if not price_5y_df.empty:
            st.divider()
            price_5y_chart = (
                alt.Chart(price_5y_df[['Close']].reset_index())
                .mark_line(color=NEUTRAL_CHART_COLOR)
                .encode(
                    x=alt.X('Date:T', title=None),
                    y=alt.Y('Close:Q', title='주가(원)', scale=alt.Scale(zero=False)),
                    tooltip=[alt.Tooltip('Date:T'), alt.Tooltip('Close:Q', title='주가', format=',.0f')],
                )
                .properties(height=280)
                .interactive()
            )
            st.altair_chart(price_5y_chart, use_container_width=True)
            st.caption(f'{name} 최근 5년 주가 흐름 · 출처: FinanceDataReader')

    with tab_step3:
        st.markdown('###### 업종 내 상대 위치')
        per_rank = _sector_rank_text('PER', per, 'PER', universe, industry_name, ticker)
        pbr_rank = _sector_rank_text('PBR', pbr, 'PBR', universe, industry_name, ticker)
        if per_rank:
            st.markdown(f'- {per_rank}')
        if pbr_rank:
            st.markdown(f'- {pbr_rank}')
        if not per_rank and not pbr_rank:
            st.caption('업종 내 순위를 계산할 데이터가 부족합니다 (ETF이거나 업종 분류가 없는 종목일 수 있습니다).')
        st.caption(f'업종: {industry_name or "-"} · 부채비율·ROE 등 나머지 지표는 실제 업종 평균이 아니라 일반적 기준선이라 순위를 매기지 않습니다 (1단계 참고).')

    with tab_step4:
        st.markdown('###### 긍정적 신호')
        if positives:
            for p in positives:
                st.markdown(f'- ✅ {p}')
        else:
            st.caption('뚜렷한 긍정적 신호가 감지되지 않았습니다.')
        st.divider()
        st.markdown('###### 부정적 신호')
        if negatives:
            for n in negatives:
                st.markdown(f'- ⚠️ {n}')
        else:
            st.caption('뚜렷한 부정적 신호가 감지되지 않았습니다.')
        st.caption('밸류에이션·재무건전성·수익성·성장성·모멘텀·지배구조 순으로 우선순위를 매겨 최대 3개씩 뽑은 규칙 기반 신호이며, 투자 조언이 아닙니다.')

    with tab_step5:
        st.markdown('###### 종합 평가')
        st.info(overall_lean)
        st.caption(f'긍정 신호 {len(positives)}건 · 부정 신호 {len(negatives)}건 (4단계 기준)')
        st.divider()
        st.markdown('###### 추가로 확인이 필요한 사항')
        for f in follow_ups:
            st.markdown(f'- {f}')
        st.caption('규칙 기반 요약이며 투자 조언이 아닙니다. 최종 투자 판단과 책임은 본인에게 있습니다.')

    with tab_flow:
        if 'Volume' in price_df.columns and len(price_df) >= 20:
            avg_trading_value = (price_df['Close'] * price_df['Volume']).tail(20).mean() / 1e8
            st.metric('일평균 거래대금(최근 20거래일)', f'{avg_trading_value:,.0f}억원')
            st.caption('거래대금 = 종가 × 거래량(근사치) · 값이 작을수록 사고팔기 부담스러운(유동성 낮은) 종목입니다.')
            st.divider()

        try:
            flow_df = fetch_stock_investor_flow(ticker)
        except Exception:
            flow_df = pd.DataFrame()
        if not flow_df.empty and {'외국인합계', '기관합계', '개인'}.issubset(flow_df.columns):
            chart_df = flow_df[['외국인합계', '기관합계', '개인']].div(1e8).cumsum().reset_index()
            chart_df.columns = ['날짜', '외국인합계', '기관합계', '개인']
            melted = chart_df.melt(id_vars='날짜', var_name='주체', value_name='누적 순매수(억원)')
            flow_chart = (
                alt.Chart(melted)
                .mark_line(point=True)
                .encode(
                    x=alt.X('날짜:T', title=None),
                    y=alt.Y('누적 순매수(억원):Q', title='누적 순매수(억원)', scale=alt.Scale(zero=False)),
                    color=alt.Color(
                        '주체:N', title=None,
                        scale=alt.Scale(domain=['외국인합계', '기관합계', '개인'], range=[SWISS_GREEN, SWISS_GRAY, SWISS_WHITE]),
                    ),
                    tooltip=[alt.Tooltip('날짜:T'), alt.Tooltip('주체:N'), alt.Tooltip('누적 순매수(억원):Q', format=',.0f')],
                )
                .properties(height=260)
                .interactive()
            )
            st.altair_chart(flow_chart, use_container_width=True)
            st.caption(f'{name} 개별 종목 기준 최근 {len(flow_df)}거래일 누적 순매수 · 출처: pykrx (KRX 투자자별 거래대금)')
        else:
            st.info('종목별 수급 데이터를 불러오지 못했습니다.')

        try:
            short_df = fetch_stock_shorting(ticker)
        except Exception:
            short_df = pd.DataFrame()
        if not short_df.empty and '비중' in short_df.columns:
            latest_date = short_df.index[-1]
            latest_date_str = latest_date.strftime('%Y-%m-%d') if hasattr(latest_date, 'strftime') else str(latest_date)
            st.metric('공매도 비중(최신)', f"{short_df['비중'].iloc[-1]:.2f}%")
            st.caption(f'{latest_date_str} 기준 · 상장주식수 대비 공매도 잔고 비중 · 출처: pykrx (공매도 잔고 현황)')

        st.divider()
        _render_moving_average_section(ticker, name)

    with tab_events:
        st.markdown('###### 지배구조 · 희석위험')
        if shareholder.get('total_pct') is not None:
            st.metric(f"최대주주(+특수관계인) 지분율 — {shareholder.get('name') or '-'}", f"{shareholder['total_pct']:.2f}%")
        st.markdown(f'- {dilution_text}')
        if capital_changes:
            with st.expander(f'최근 자본변동 이력 ({len(capital_changes)}건)'):
                for c in capital_changes[:15]:
                    qty_str = f"{c['qty']:,.0f}주" if c.get('qty') is not None else '-'
                    st.markdown(f"- `{c.get('date') or '-'}` {c.get('type') or '-'} ({qty_str})")
        st.caption('출처: DART 증자(감자) 현황 · 최대주주 현황, 하루 캐시.')

        st.divider()
        st.markdown(f'###### {name} 관련 주요 뉴스')
        if related_news.empty:
            st.caption(f'{name} 관련 뉴스를 찾지 못했습니다.')
        else:
            render_news_list(related_news)
        st.markdown(
            '- "환율 및 뉴스" 탭이 모으는 RSS 피드 기사 제목에 종목명이 들어간 것만 걸러 보여줍니다 '
            '(대략 최근 24시간 이내 · 국내외 뉴스 통합)\n'
            '- 종목명이 기사 제목에 그대로 안 들어가면(축약어, 영문 표기 등) 실제로 관련 있어도 안 뜰 수 있습니다\n'
            '- 전문 뉴스 검색이 아니라 이미 수집 중인 일반 경제 RSS를 재사용한 결과라 보도량이 적은 종목은 뜨는 기사가 거의 없을 수 있습니다\n'
            '- 출처: RSS 피드 (fetch_news.py)'
        )


def render_stock_opinion_tab():
    """"종목 분석" 탭 — 정량 스코어카드(규칙 기반, API 키 불필요) 단일 모드.
    예전엔 OpenAI 기반 "월스트리트 DCF/Comps"·"테마·모트 분석" 모드도 라디오로
    골라 쓸 수 있었으나, OPENAI_API_KEY 없이는 계속 "설정 필요" 안내만 뜨는 채로
    남아 있어 완전히 제거했다(stock_opinion.py 자체도 삭제)."""
    st.subheader('종목 분석')
    st.caption(
        '밸류에이션·재무 안정성·수급·기술·공시 데이터를 종목별로 모아 보여줍니다. '
        '코스피·코스닥 전 종목을 검색해 볼 수 있습니다.'
    )
    render_quant_scorecard()


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

    if _news_cache_is_fresh():
        st.caption(file_caption(NEWS_CSV, 'RSS 피드 (fetch_news.py)'))
    else:
        st.caption(
            f'갱신 시각: 방금 (라이브 조회 — 로컬 캐시가 {NEWS_STALE_THRESHOLD_SEC // 3600}시간 '
            '넘게 오래돼 대체함) · 출처: RSS 피드 (fetch_news.py)'
        )


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
    """"전체 시장현황" 탭의 자동 새로고침 대상 — 지수 카드·거시 지표·투자자별 수급."""
    st.caption(f"화면 갱신 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    render_index_cards(MARKET_SERIES, '지수 현황')
    st.divider()
    render_macro_panel()
    st.divider()
    render_investor_flow_panel()
    st.divider()
    render_sector_panel()


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
        # 기간/지수를 고르는 상호작용 패널이라 자동 새로고침 대상이 아니다.
        render_portfolio_performance()
        st.divider()
        # 종목마다 여러 DART 조회가 필요해 무거워 자동 새로고침 대상이 아니다.
        render_concentration_check()
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
