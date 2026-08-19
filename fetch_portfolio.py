# fetch_portfolio.py — 보유 종목 현황 수집 스크립트
# portfolio.csv(보유 수량/평단가)를 읽어 현재가를 붙이고 평가손익·비중까지 계산해 저장한다.
#
# 주의: 현재가는 무상증자·액면분할이 반영된 권리락 후 가격이다. portfolio.csv 의
# quantity/avg_price 는 직접 관리하는 값이므로, 보유 종목에 그런 이벤트가 생기면
# 수량과 평단가를 함께 조정해야 수익률이 맞는다.
# (예: 티앤엘 2026-08-05 무상증자 1:1 권리락 -> 80주/66,100원을 160주/33,050원으로 정정)
import os
import socket
import time
from datetime import datetime, timedelta, timezone

import FinanceDataReader as fdr
import pandas as pd

# collect.py 와 동일한 이유로 소켓 기본 타임아웃을 건다 — fdr 내부 urllib 호출이
# 자체 타임아웃 없이 무한 대기하는 경우가 있다.
socket.setdefaulttimeout(30)

DATA_DIR = 'data'
PORTFOLIO_CSV = 'portfolio.csv'
OUTPUT_CSV = os.path.join(DATA_DIR, 'portfolio_status.csv')

# 현금성 자산(CMA/RP 등)은 시세 조회 대상이 아니므로 티커를 이 값으로 두고
# quantity 에 원화 금액, avg_price 에 1 을 넣어 평가금액이 곧 잔액이 되게 한다.
CASH_TICKER = 'CASH'

# KRX 목록 조회(fdr.StockListing)는 장애 시 "빠르게 실패"하지 않고 시도당 최대
# 2~3분씩 걸릴 수 있다(2026-08-17 실측, socket.setdefaulttimeout 도 못 막음).
# 반면 종목별 개별 조회(quote_from_history)는 같은 장애 상황에서도 각 1초 이내로
# 빠르고 안정적임을 확인했다 — 그래서 재시도는 1회만 하고 곧장 개별 조회로 넘어간다.
LISTING_RETRIES = 1
LISTING_BACKOFF_SEC = 5


def load_holdings():
    """보유 종목 설정을 읽는다. 티커는 앞자리 0이 잘리지 않도록 문자열로 고정.
    target_price(목표가)·stop_price(손절가)는 선택 항목 — portfolio.csv에 아직
    없거나 특정 종목만 비워뒀어도 되게, 없으면 컬럼 자체를 만들어 NaN으로 채운다."""
    df = pd.read_csv(PORTFOLIO_CSV, encoding='utf-8-sig', dtype={'ticker': str})
    df['ticker'] = df['ticker'].str.strip()
    df['quantity'] = pd.to_numeric(df['quantity'])
    df['avg_price'] = pd.to_numeric(df['avg_price'])
    for col in ('target_price', 'stop_price'):
        if col not in df.columns:
            df[col] = pd.NA
        df[col] = pd.to_numeric(df[col], errors='coerce')
    return df


def fetch_krx_listing():
    """KRX 전 종목 시세를 한 번에 받는다(종목별 조회 8번 대신 1번).
    이 엔드포인트는 짧은 간격으로 부르면 429를 돌려주므로, 몇 번 기다렸다 재시도하고
    그래도 안 되면 None 을 반환해 종목별 개별 조회로 넘긴다."""
    for attempt in range(1, LISTING_RETRIES + 1):
        try:
            return fdr.StockListing('KRX').set_index('Code')
        except Exception as e:
            print(f"[WARN] KRX 목록 조회 실패 ({attempt}/{LISTING_RETRIES}): {e}")
            if attempt < LISTING_RETRIES:
                time.sleep(LISTING_BACKOFF_SEC * attempt)

    print('[WARN] KRX 목록 조회를 포기하고 종목별 개별 조회로 전환합니다.')
    return None


def quote_from_history(ticker):
    """목록 조회가 막혔을 때 쓰는 대체 경로 — 종목별 시세 이력에서 최근 2거래일을 뽑는다."""
    start = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')
    df = fdr.DataReader(ticker, start)
    if df.empty:
        return None

    closes = df['Close'].tail(2)
    current = float(closes.iloc[-1])
    prev = float(closes.iloc[-2]) if len(closes) == 2 else current
    change = current - prev
    return {
        'market': '',  # 개별 조회로는 시장 구분을 알 수 없다
        'current': current,
        'change': change,
        'change_pct': change / prev * 100 if prev else 0.0,
    }


def get_quote(ticker, listing):
    if listing is not None and ticker in listing.index:
        q = listing.loc[ticker]
        return {
            'market': q.get('Market', ''),
            'current': float(q['Close']),
            'change': float(q['Changes']),
            'change_pct': float(q['ChagesRatio']),  # FDR 컬럼명 오타가 원본 그대로임
        }
    return quote_from_history(ticker)


def build_rows(holdings, listing):
    rows = []
    for _, h in holdings.iterrows():
        ticker = h['ticker']
        buy_amount = h['quantity'] * h['avg_price']

        if ticker == CASH_TICKER:
            # 현금은 평가금액 = 매입금액, 등락 없음
            rows.append({
                'name': h['name'],
                'ticker': ticker,
                'market': '현금',
                'quantity': h['quantity'],
                'avg_price': h['avg_price'],
                'current_price': h['avg_price'],
                'change': 0.0,
                'change_pct': 0.0,
                'buy_amount': buy_amount,
                'eval_amount': buy_amount,
                'profit': 0.0,
                'profit_pct': 0.0,
                'target_price': None,
                'stop_price': None,
            })
            continue

        try:
            q = get_quote(ticker, listing)
        except Exception as e:
            print(f"[FAIL] {h['name']}({ticker}): 시세 조회 오류 - {e}")
            continue

        if q is None:
            print(f"[FAIL] {h['name']}({ticker}): 시세를 찾지 못했습니다.")
            continue

        current = q['current']
        eval_amount = h['quantity'] * current
        profit = eval_amount - buy_amount

        rows.append({
            'name': h['name'],
            'ticker': ticker,
            'market': q['market'],
            'quantity': h['quantity'],
            'avg_price': h['avg_price'],
            'current_price': current,
            'change': q['change'],
            'change_pct': q['change_pct'],
            'buy_amount': buy_amount,
            'eval_amount': eval_amount,
            'profit': profit,
            'profit_pct': profit / buy_amount * 100 if buy_amount else 0.0,
            'target_price': h.get('target_price') if pd.notna(h.get('target_price')) else None,
            'stop_price': h.get('stop_price') if pd.notna(h.get('stop_price')) else None,
        })
        print(f"[OK] {h['name']}({ticker}): {current:,.0f}원 ({q['change_pct']:+.2f}%)")

    return rows


def add_weights(rows):
    """비중은 현금을 제외한 '주식 평가금액' 대비로 계산한다(기존 보고서와 동일 기준)."""
    stock_total = sum(r['eval_amount'] for r in rows if r['ticker'] != CASH_TICKER)
    for r in rows:
        if r['ticker'] == CASH_TICKER:
            r['weight_pct'] = 0.0
        else:
            r['weight_pct'] = r['eval_amount'] / stock_total * 100 if stock_total else 0.0
    return rows


def fetch_price_history(ticker, start):
    """RS 비교 탭 등에서 보유 종목의 시계열(Close 포함 DataFrame)이 필요할 때 쓴다.
    start 는 'YYYY-MM-DD' 문자열 또는 datetime."""
    return fdr.DataReader(ticker, start)


def compute_portfolio_rows(holdings=None):
    """app.py 에서 직접 호출하는 라이브러리 진입점. CSV로 저장하지 않고 바로 반환한다.
    holdings 를 안 넘기면 로컬 portfolio.csv 에서 읽는다 — 클라우드 배포본은 이 파일이
    저장소에 없으므로(개인정보라 커밋하지 않음) app.py 가 st.secrets 로 만든 holdings 를
    직접 넘긴다."""
    if holdings is None:
        holdings = load_holdings()
    listing = fetch_krx_listing()
    rows = build_rows(holdings, listing)
    return add_weights(rows)


def save(rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    df = pd.DataFrame(rows)
    df['fetched_at'] = datetime.now(timezone.utc).isoformat()
    df = df.sort_values('eval_amount', ascending=False)
    df.to_csv(OUTPUT_CSV, index=False, encoding='utf-8-sig')
    print(f"-> {len(df)}건을 {OUTPUT_CSV} 에 저장했습니다.")

    stocks = df[df['ticker'] != CASH_TICKER]
    buy, ev = stocks['buy_amount'].sum(), stocks['eval_amount'].sum()
    pct = (ev - buy) / buy * 100 if buy else 0.0
    print(f"   주식 매입 {buy:,.0f}원 -> 평가 {ev:,.0f}원 ({pct:+.2f}%)")


def main():
    holdings = load_holdings()
    listing = fetch_krx_listing()
    rows = build_rows(holdings, listing)
    if not rows:
        print('수집된 보유 종목이 없어 CSV를 저장하지 않았습니다.')
        return
    save(add_weights(rows))


if __name__ == '__main__':
    main()
