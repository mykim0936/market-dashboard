# app.py — 시장 대시보드
#
# 로컬(Windows 작업 스케줄러)과 클라우드(Streamlit Community Cloud) 양쪽에서 동작하도록
# "CSV가 있으면 그걸 읽고, 없으면 그 자리에서 직접 받아온다" 하이브리드 방식을 쓴다.
# - 로컬: collect.py가 10분마다 CSV를 써두므로 디스크만 읽어 빠르다.
# - 클라우드: 스케줄러가 없으므로 data/*.csv 가 커밋되어 있지 않다 -> 매번 직접 조회.
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
for _key in ('ECOS_API_KEY', 'FRED_API_KEY', 'DASHBOARD_PASSWORD'):
    _val = _load_secret(_key)
    if _val and not os.environ.get(_key):
        os.environ[_key] = _val

import pykrx.stock as pykrx_stock
import yfinance as yf

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
PORTFOLIO_CSV = os.path.join(DATA_DIR, 'portfolio_status.csv')
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
        })
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
    })[['종목명', '시장', '현재가', '전일대비(%)', '수량', '평단가',
        '매입금액', '평가금액', '평가손익', '수익률(%)', '비중(%)']]

    # 목록 조회가 429로 막혀 개별 조회로 받아오면 시장 구분이 비어 있다.
    table['시장'] = table['시장'].fillna('-').replace('', '-')

    st.dataframe(style_portfolio(table), width='stretch', hide_index=True)

    top = table.iloc[0]
    if top['비중(%)'] >= 40:
        st.warning(
            f"집중도 경고: {top['종목명']} 한 종목이 주식 평가금액의 {top['비중(%)']:.1f}%를 차지합니다. "
            '해당 종목의 변동이 계좌 전체 손익을 좌우하는 구조입니다.'
        )

    st.caption(file_caption(PORTFOLIO_CSV, 'FinanceDataReader (KRX 종가/등락률)') +
               ' · 비중은 현금 제외 주식 평가금액 대비')


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

    tab_market, tab_portfolio, tab_fx_news, tab_rs = st.tabs(
        ['전체 시장현황', '내 계좌 포트폴리오', '환율 및 뉴스', 'RS 비교']
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

    with tab_fx_news:
        st.fragment(run_every=interval)(render_fx_news_live)()
        st.divider()
        render_index_charts(FX_SERIES)

    with tab_rs:
        # 사용자가 종목/기간/지수를 고르는 상호작용 탭이라 자동 새로고침 대상이 아니다
        # (자동 리런이 선택값을 방해하지 않게).
        render_rs_tab()


if __name__ == '__main__':
    main()
