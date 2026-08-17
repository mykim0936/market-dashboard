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
SERIES_CONFIG = [
    ('코스피', 'kospi.csv', 'pykrx', '1001'),
    ('코스닥', 'kosdaq.csv', 'pykrx', '2001'),
    ('원/달러', 'usdkrw.csv', 'yfinance', 'KRW=X'),
    ('나스닥', 'nasdaq.csv', 'yfinance', '^IXIC'),
    ('S&P500', 'sp500.csv', 'yfinance', '^GSPC'),
]

CHART_SERIES = [
    ('코스피', 'kospi.csv', 'pykrx', '1001'),
    ('코스닥', 'kosdaq.csv', 'pykrx', '2001'),
    ('나스닥', 'nasdaq.csv', 'yfinance', '^IXIC'),
    ('S&P500', 'sp500.csv', 'yfinance', '^GSPC'),
]
CHART_YEARS = 5

# 차트는 상승/하락/경고 신호가 아닌 단순 추세선이므로 중립색 하나만 고정해서 쓴다.
NEUTRAL_CHART_COLOR = '#6B7280'

# 국내 관행에 맞춰 상승은 빨강, 하락은 파랑으로 표기한다.
UP_COLOR = '#D32F2F'
DOWN_COLOR = '#1565C0'

INDICATORS_CSV = os.path.join(DATA_DIR, 'indicators.csv')
BRIEFING_MD = os.path.join(DATA_DIR, 'briefing.md')
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
def load_series_live(source, code, years):
    """반환값은 (df, 실제로 쓰인 출처 라벨) — 국내 지수는 pykrx를 우선 시도하고
    실패하면 yfinance로 자동 폴백하므로, 캡션에 실제 출처를 보여주려면 라벨도 같이 받아야 한다."""
    start_dt = datetime.now() - pd.DateOffset(years=years)

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
    return load_series_live(source, code, years)


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

def render_index_cards():
    st.subheader('지수 현황')
    cols = st.columns(len(SERIES_CONFIG))
    for col, (label, filename, source, code) in zip(cols, SERIES_CONFIG):
        with col:
            try:
                df, _ = get_series(filename, source, code, CHART_YEARS)
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
    st.caption(file_caption(os.path.join(DATA_DIR, 'kospi.csv'), 'pykrx / yfinance') + ' · 전일 대비')


def render_macro_panel():
    st.subheader('거시 지표')
    df = get_indicators_df()
    if df.empty:
        st.info('표시할 거시 지표가 없습니다. ECOS_API_KEY / FRED_API_KEY 설정을 확인해 주세요.')
        return

    cols = st.columns(len(df))
    for col, (_, row) in zip(cols, df.iterrows()):
        with col:
            st.metric(row['label'], row['value'])
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


def render_briefing_panel():
    st.subheader('시황 브리핑')
    if not os.path.exists(BRIEFING_MD):
        st.info(
            '이 패널은 로컬에서 `claude -p` 로 생성한 브리핑(make_briefing.py)만 보여준다. '
            '클라우드 배포본에는 claude CLI가 없어 이 파일이 갱신되지 않는다.'
        )
        return
    with open(BRIEFING_MD, encoding='utf-8') as f:
        st.markdown(f.read())
    st.caption(file_caption(BRIEFING_MD, 'claude -p 브리핑 생성 (indicators.csv + news.csv 기반)'))


def render_index_charts():
    for label, filename, source, code in CHART_SERIES:
        st.subheader(f'{label} 최근 {CHART_YEARS}년')
        live_label = None
        try:
            df, live_label = get_series(filename, source, code, CHART_YEARS)
            if df.empty:
                raise ValueError('데이터 소스가 빈 결과를 반환했습니다')
            cutoff = df.index.max() - pd.DateOffset(years=CHART_YEARS)
            recent = df.loc[df.index >= cutoff, 'Close']
            st.line_chart(recent, color=NEUTRAL_CHART_COLOR)
        except Exception as e:
            st.warning(f"차트를 불러오지 못했습니다: {e}")
        # live_label 은 라이브 조회 시 실제로 쓰인 출처(폴백 포함)를 반영한다.
        # 로컬 CSV를 읽었을 때는 None 이므로 설정된 기본 출처로 표시한다.
        source_label = live_label or ('pykrx (KRX)' if source == 'pykrx' else 'yfinance')
        st.caption(file_caption(os.path.join(DATA_DIR, filename), source_label))


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


def render_live_panels():
    """자동 새로고침 대상 — 값이 자주 바뀌는 패널만 모아둔다."""
    st.caption(f"화면 갱신 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    render_index_cards()
    st.divider()
    render_macro_panel()
    st.divider()
    render_portfolio_panel()
    st.divider()
    render_news_panel()


def main():
    st.set_page_config(page_title='시장 대시보드', layout='wide')

    if not check_password():
        return

    st.title('시장 대시보드')

    interval = render_sidebar()

    # 정보 설계 순서: 지수 -> 거시 -> 보유 종목 -> 뉴스 (여기까지 자동 갱신)
    #                 -> 브리핑 -> 장기 차트 (수동 갱신으로 충분)
    # run_every 값이 라디오 선택에 따라 달라져야 해서 데코레이터를 런타임에 적용한다.
    st.fragment(run_every=interval)(render_live_panels)()

    st.divider()
    render_briefing_panel()
    st.divider()
    render_index_charts()


if __name__ == '__main__':
    main()
