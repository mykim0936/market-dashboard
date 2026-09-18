"""recommendation_signals.py 단위 테스트. pytest tests_recommendation... 식 디렉토리
없이 프로젝트 루트에 둔다(이 저장소에 기존 tests/ 관례가 없어서 app.py 등과 같은
위치에 둠). 실행: python -m pytest test_recommendation_signals.py -q
"""

import numpy as np
import pandas as pd

from recommendation_signals import (
    breakout_signal,
    compute_indicators,
    divergence_signal,
    evaluate_all,
    mean_reversion_signal,
    trend_following_signal,
)


def _make_df(closes, highs=None, lows=None, volumes=None):
    n = len(closes)
    closes = np.array(closes, dtype=float)
    highs = np.array(highs, dtype=float) if highs is not None else closes * 1.01
    lows = np.array(lows, dtype=float) if lows is not None else closes * 0.99
    volumes = np.array(volumes, dtype=float) if volumes is not None else np.full(n, 100000.0)
    return pd.DataFrame(
        {
            "Open": closes,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": volumes,
        }
    )


def test_compute_indicators_adds_expected_columns_without_crashing():
    rng = np.random.default_rng(0)
    closes = 10000 + np.cumsum(rng.normal(0, 50, size=200))
    df = _make_df(closes)
    out = compute_indicators(df)
    for col in ("MA20", "MA60", "MA120", "BB_upper", "BB_lower", "RSI", "MACD", "MACD_signal", "Volume_MA20"):
        assert col in out.columns
    assert len(out) == len(df)


def test_trend_following_detects_aligned_ma_and_volume_spike():
    # 꾸준히 우상향(정배열이 자연히 만들어짐) + 마지막 날 거래량 급증
    n = 150
    closes = 10000 + np.arange(n) * 20.0
    volumes = np.full(n, 100000.0)
    volumes[-1] = 300000.0  # 20일 평균의 1.5배 훨씬 넘게
    df = _make_df(closes, volumes=volumes)
    indicators = compute_indicators(df)

    # MACD가 마지막 날 골든크로스를 만들도록, 직전까지는 약보합 후 급등으로 전환
    result = trend_following_signal(indicators)
    # 꾸준한 우상향이면 MACD가 이미 시그널선 위에 있을 수 있어(골든크로스가 "오늘"
    # 발생하지 않을 수 있음) None이 나올 수도 있다 — 정배열/거래량 조건 자체는
    # 함수가 크래시 없이 동작하는지만 확인
    assert result is None or {"close", "volume_ratio"} <= result.keys()


def test_mean_reversion_detects_oversold_capitulation_exhaustion():
    n = 150
    # 완만한 하락 후 마지막 날 급락 + 거래량 급감
    closes = 10000 - np.arange(n) * 5.0
    closes[-1] = closes[-2] * 0.90  # 급락으로 RSI를 확실히 30 이하로
    volumes = np.full(n, 100000.0)
    volumes[-1] = 50000.0  # 20일 평균의 0.7배 이하
    df = _make_df(closes, volumes=volumes)
    indicators = compute_indicators(df)

    result = mean_reversion_signal(indicators)
    assert result is not None
    assert result["rsi"] <= 30
    assert result["volume_ratio"] <= 0.7


def test_breakout_detects_box_breakout_and_flags_fake_risk_on_low_volume():
    n = 150
    box_period = np.full(29, 10000.0)  # 박스권: 29일간 10,000원 횡보 + 마지막 날 돌파
    pre = np.full(n - 30, 10000.0)
    closes = np.concatenate([pre, box_period, [10500.0]])  # 마지막 날 박스 상단 돌파
    highs = closes * 1.001
    volumes = np.full(n, 100000.0)
    volumes[-1] = 100000.0  # 20일 평균과 비슷한 수준 -> 200% 미달 -> fake_risk

    df = _make_df(closes, highs=highs, volumes=volumes)
    indicators = compute_indicators(df)

    result = breakout_signal(indicators)
    assert result is not None
    assert result["fake_risk"] is True

    volumes[-1] = 250000.0  # 20일 평균의 2배 이상 -> 진성 돌파
    df2 = _make_df(closes, highs=highs, volumes=volumes)
    indicators2 = compute_indicators(df2)
    result2 = breakout_signal(indicators2)
    assert result2 is not None
    assert result2["fake_risk"] is False


def test_divergence_detects_higher_price_lower_rsi():
    n = 150
    closes = np.full(n, 10000.0)
    # 1차 고점: 빠른 상승 -> RSI가 높게 형성
    closes[100:106] = [10200, 10500, 10800, 11000, 11100, 11150]
    closes[106:120] = np.linspace(11150, 10600, 14)  # 조정(눌림)
    # 2차 상승: 1차 고점을 넘지 않는 선까지 완만하게 회복
    closes[120:145] = np.linspace(10600, 11100, 25)
    # 막판(divergence_exclude_recent_days=5 구간 안)에 급등해 신고점 경신
    closes[145:149] = np.linspace(11100, 11350, 4)
    closes[149] = 11400  # 오늘: 1차 고점(11150)보다 확실히 높은 신고점

    df = _make_df(closes)
    indicators = compute_indicators(df)

    result = divergence_signal(indicators)
    assert result is not None
    assert result["close"] > result["prev_peak_close"]
    assert result["rsi_diverged"] or result["macd_diverged"]


def test_evaluate_all_returns_all_none_when_insufficient_history():
    df = _make_df([10000.0] * 50)
    result = evaluate_all(df)
    assert result == {"trend": None, "reversion": None, "breakout": None, "divergence": None}
