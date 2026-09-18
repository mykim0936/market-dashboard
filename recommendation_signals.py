"""종목추천 탭 — 4가지 기술적 신호 판정. 순수 pandas 계산, 네트워크 호출 없음
(collect_recommendations.py가 받아온 OHLCV로 이 모듈을 호출).

지표 파라미터는 app.py의 "이동평균선·이격도·RSI·MACD" 섹션(_render_moving_average_section)
과 동일하게 맞췄다(MA 20/60/120, BB 20±2표준편차, RSI 14, MACD 12/26/9) — 대시보드
다른 곳과 같은 기준으로 보이게 하려고.

각 신호의 구체적 임계값(거래량 배수, 돌파 기간 등)은 사용자가 준 정성적 설명
("거래량 증가", "거래량 급감" 등)을 숫자로 고정한 것 — CONFIG 딕셔너리에 몰아넣어
튜닝하기 쉽게 했다.
"""

import pandas as pd

MA_WINDOWS = (20, 60, 120)
BB_WINDOW = 20
BB_STD_MULT = 2
RSI_WINDOW = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

CONFIG = {
    # 추세추종형: 정배열 + MACD 골든크로스(0선 위) + 거래량 증가
    "trend_volume_ratio": 1.5,  # 당일 거래량이 20일 평균의 1.5배 이상
    # 역추세형: 볼린저 하단 + RSI 과매도 + 거래량 급감(투매 소진)
    "reversion_bb_tolerance": 1.02,  # 종가 <= 하단밴드 x 1.02 (하단 터치/근접)
    "reversion_rsi_max": 30,
    "reversion_volume_ratio": 0.7,  # 당일 거래량이 20일 평균의 0.7배 이하
    # 돌파매매: 박스권(N일 최고가) 상단 돌파 + 거래량
    "breakout_box_days": 20,
    "breakout_volume_ratio": 2.0,  # 200%+ 여야 "진성 돌파"
    # 다이버전스: 신고점 윈도우 + 이전 고점 탐색 범위
    "divergence_window_days": 60,
    "divergence_exclude_recent_days": 5,  # 오늘 바로 직전 며칠은 "이전 고점" 탐색에서 제외
}

MIN_ROWS_REQUIRED = 130  # MA120이 안정되려면 최소 이 정도 일봉이 있어야 함


def _safe_ratio(numerator, denominator) -> float:
    """분모가 0/NaN이면(장기간 거래정지 등으로 20일 평균 거래량이 0인 경우) 0을
    반환 — 0으로 나누기 경고 없이. json으로 직렬화 가능하도록 항상 float로 캐스팅."""
    if not denominator or pd.isna(denominator):
        return 0.0
    return round(float(numerator) / float(denominator), 2)


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """df: 오름차순(과거->최근) OHLCV, 컬럼 Open/High/Low/Close/Volume 필요.
    MA/BB/RSI/MACD/거래량 이동평균을 추가해서 반환."""
    out = df.copy()

    for window in MA_WINDOWS:
        out[f"MA{window}"] = out["Close"].rolling(window=window).mean()

    bb_mid = out["Close"].rolling(window=BB_WINDOW).mean()
    bb_std = out["Close"].rolling(window=BB_WINDOW).std()
    out["BB_upper"] = bb_mid + BB_STD_MULT * bb_std
    out["BB_lower"] = bb_mid - BB_STD_MULT * bb_std

    delta = out["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / RSI_WINDOW, min_periods=RSI_WINDOW, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_WINDOW, min_periods=RSI_WINDOW, adjust=False).mean()
    rs = avg_gain / avg_loss
    out["RSI"] = 100 - (100 / (1 + rs))
    out.loc[avg_loss == 0, "RSI"] = 100

    ema_fast = out["Close"].ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = out["Close"].ewm(span=MACD_SLOW, adjust=False).mean()
    out["MACD"] = ema_fast - ema_slow
    out["MACD_signal"] = out["MACD"].ewm(span=MACD_SIGNAL, adjust=False).mean()

    out["Volume_MA20"] = out["Volume"].rolling(window=20).mean()

    return out


def _last_two_valid(df: pd.DataFrame, cols: list[str]):
    """마지막 두 행이 cols 전부 결측 없이 있으면 (전일, 당일) 반환, 아니면 None."""
    if len(df) < 2:
        return None
    today, yesterday = df.iloc[-1], df.iloc[-2]
    if today[cols].isna().any() or yesterday[cols].isna().any():
        return None
    return yesterday, today


def trend_following_signal(df: pd.DataFrame) -> dict | None:
    """정배열(MA20>MA60>MA120) + MACD 골든크로스(0선 위) + 거래량 증가."""
    pair = _last_two_valid(df, ["MA20", "MA60", "MA120", "MACD", "MACD_signal", "Volume_MA20"])
    if pair is None:
        return None
    yesterday, today = pair

    aligned = today["MA20"] > today["MA60"] > today["MA120"]
    golden_cross = yesterday["MACD"] <= yesterday["MACD_signal"] and today["MACD"] > today["MACD_signal"]
    above_zero = today["MACD"] > 0
    volume_up = today["Volume"] >= today["Volume_MA20"] * CONFIG["trend_volume_ratio"]

    if aligned and golden_cross and above_zero and volume_up:
        return {
            "close": float(today["Close"]),
            "volume_ratio": _safe_ratio(today["Volume"], today["Volume_MA20"]),
        }
    return None


def mean_reversion_signal(df: pd.DataFrame) -> dict | None:
    """볼린저 하단 근접/이탈 + RSI 과매도 + 거래량 급감(투매 소진)."""
    if len(df) < 1:
        return None
    today = df.iloc[-1]
    cols = ["Close", "BB_lower", "RSI", "Volume", "Volume_MA20"]
    if today[cols].isna().any():
        return None

    near_lower_band = today["Close"] <= today["BB_lower"] * CONFIG["reversion_bb_tolerance"]
    oversold = today["RSI"] <= CONFIG["reversion_rsi_max"]
    volume_dried_up = today["Volume"] <= today["Volume_MA20"] * CONFIG["reversion_volume_ratio"]

    if near_lower_band and oversold and volume_dried_up:
        return {
            "close": float(today["Close"]),
            "rsi": round(float(today["RSI"]), 1),
            "volume_ratio": _safe_ratio(today["Volume"], today["Volume_MA20"]),
        }
    return None


def breakout_signal(df: pd.DataFrame) -> dict | None:
    """박스권(N일 최고가) 상단 돌파. 거래량 200%+ 미만이면 fake_risk=True로 표시하되
    돌파 자체는 여전히 반환한다(사용자가 명시적으로 "페이크 가능성"을 보고 싶어 함)."""
    box_days = CONFIG["breakout_box_days"]
    if len(df) < box_days + 1:
        return None

    today = df.iloc[-1]
    prior_high = df["High"].iloc[-(box_days + 1):-1].max()
    if pd.isna(prior_high) or pd.isna(today["Close"]) or pd.isna(today["Volume_MA20"]):
        return None

    breakout = today["Close"] > prior_high
    if not breakout:
        return None

    volume_ratio = _safe_ratio(today["Volume"], today["Volume_MA20"])
    fake_risk = volume_ratio < CONFIG["breakout_volume_ratio"]

    return {
        "close": float(today["Close"]),
        "box_high": round(float(prior_high), 2),
        "volume_ratio": volume_ratio,
        "fake_risk": bool(fake_risk),
    }


def divergence_signal(df: pd.DataFrame) -> dict | None:
    """가격 신고점 vs RSI/MACD 저점 하락 — 약세 다이버전스 경고.

    단순화된 정의: 최근 divergence_window_days 안에서 오늘이 최고 종가이고,
    (오늘부터 exclude_recent_days일 전까지를 제외한) 그 이전 구간의 최고 종가일과
    비교해서 가격은 더 높은 고점을 찍었는데 RSI 또는 MACD는 그때보다 낮으면
    추세 전환 경고로 판정한다. 교과서적 스윙 고점 탐지보다는 단순하지만,
    "가격 신고점 vs 지표 고점 하락"이라는 핵심은 그대로 잡아낸다.
    """
    window = CONFIG["divergence_window_days"]
    exclude_recent = CONFIG["divergence_exclude_recent_days"]
    if len(df) < window + 1:
        return None

    recent = df.iloc[-window:]
    today = recent.iloc[-1]
    if pd.isna(today["Close"]) or pd.isna(today["RSI"]) or pd.isna(today["MACD"]):
        return None

    if today["Close"] != recent["Close"].max():
        return None  # 오늘이 이 구간의 신고점이 아니면 다이버전스 후보 아님

    earlier = recent.iloc[: -exclude_recent] if exclude_recent < len(recent) else recent.iloc[:0]
    earlier = earlier.dropna(subset=["Close", "RSI", "MACD"])
    if earlier.empty:
        return None

    prev_peak_idx = earlier["Close"].idxmax()
    prev_peak = earlier.loc[prev_peak_idx]

    price_higher = today["Close"] > prev_peak["Close"]
    rsi_lower = today["RSI"] < prev_peak["RSI"]
    macd_lower = today["MACD"] < prev_peak["MACD"]

    if price_higher and (rsi_lower or macd_lower):
        return {
            "close": float(today["Close"]),
            "prev_peak_close": round(float(prev_peak["Close"]), 2),
            "rsi": round(float(today["RSI"]), 1),
            "prev_peak_rsi": round(float(prev_peak["RSI"]), 1),
            "rsi_diverged": bool(rsi_lower),
            "macd_diverged": bool(macd_lower),
        }
    return None


def evaluate_all(raw_df: pd.DataFrame) -> dict:
    """raw_df: 오름차순 OHLCV(Open/High/Low/Close/Volume). 4개 신호를 전부 평가해서
    {"trend": {...}|None, "reversion": {...}|None, "breakout": {...}|None,
     "divergence": {...}|None} 반환. 데이터가 부족하면 전부 None."""
    if len(raw_df) < MIN_ROWS_REQUIRED:
        return {"trend": None, "reversion": None, "breakout": None, "divergence": None}

    df = compute_indicators(raw_df)
    return {
        "trend": trend_following_signal(df),
        "reversion": mean_reversion_signal(df),
        "breakout": breakout_signal(df),
        "divergence": divergence_signal(df),
    }
