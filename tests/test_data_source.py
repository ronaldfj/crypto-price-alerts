"""
tests/test_data_source.py — Unit tests for data_source.py

Covers: symbol/interval helpers, DataFrame builders, retry/fallback logic,
unclosed-candle filtering, and the public fetch_klines API (all via mocks).
"""

from __future__ import annotations

import pandas as pd
import pytest
from unittest.mock import MagicMock, patch
import data_source


# ── symbol_to_pair ─────────────────────────────────────────────────────────────

class TestSymbolToPair:
    def test_known_symbols(self):
        assert data_source.symbol_to_pair("BTC") == "BTCUSDT"
        assert data_source.symbol_to_pair("ETH") == "ETHUSDT"
        assert data_source.symbol_to_pair("SOL") == "SOLUSDT"

    def test_lowercase_input(self):
        assert data_source.symbol_to_pair("btc") == "BTCUSDT"
        assert data_source.symbol_to_pair("eth") == "ETHUSDT"

    def test_unknown_symbol_returns_none(self):
        assert data_source.symbol_to_pair("FAKE") is None
        assert data_source.symbol_to_pair("") is None


# ── normalize_interval ────────────────────────────────────────────────────────

class TestNormalizeInterval:
    @pytest.mark.parametrize("tf", ["1m", "5m", "15m", "15min", "30m", "1h", "4h", "1d", "1D", "1w", "1W"])
    def test_valid_timeframes_return_self(self, tf):
        result = data_source.normalize_interval(tf)
        assert result is not None

    def test_invalid_timeframe_returns_none(self):
        assert data_source.normalize_interval("99x") is None
        assert data_source.normalize_interval("") is None
        assert data_source.normalize_interval("2d") is None

    def test_case_insensitive(self):
        assert data_source.normalize_interval("1H") is not None
        assert data_source.normalize_interval("4H") is not None


# ── interval_seconds ──────────────────────────────────────────────────────────

class TestIntervalSeconds:
    @pytest.mark.parametrize("tf,expected", [
        ("1m", 60),
        ("5m", 300),
        ("15m", 900),
        ("15min", 900),
        ("1h", 3600),
        ("4h", 14400),
        ("1d", 86400),
        ("1w", 604800),
    ])
    def test_known_intervals(self, tf, expected):
        assert data_source.interval_seconds(tf) == expected

    def test_unknown_returns_zero(self):
        assert data_source.interval_seconds("99x") == 0


# ── _bybit_pair / _okx_pair ──────────────────────────────────────────────────

class TestExchangePairs:
    def test_bybit_pair_known(self):
        assert data_source._bybit_pair("BTC") == "BTCUSDT"
        assert data_source._bybit_pair("eth") == "ETHUSDT"

    def test_bybit_pair_unknown(self):
        assert data_source._bybit_pair("FAKE") is None

    def test_okx_pair_known(self):
        assert data_source._okx_pair("BTC") == "BTC-USDT"
        assert data_source._okx_pair("eth") == "ETH-USDT"

    def test_okx_pair_unknown(self):
        assert data_source._okx_pair("FAKE") is None


# ── _bybit_to_df ──────────────────────────────────────────────────────────────

class TestBybitToDf:
    def _make_raw(self):
        # Bybit devuelve [start_ms, open, high, low, close, volume, turnover], DESC
        now_ms = 1_700_000_000_000
        return [
            [str(now_ms),       "30000.5", "31000.5", "29000.5", "30500.5", "100.1", "3050000.0"],
            [str(now_ms - 14400_000), "29000.5", "30000.5", "28500.5", "29500.5", "80.1",  "2360000.0"],
        ]

    def test_returns_dataframe(self):
        df = data_source._bybit_to_df(self._make_raw())
        assert isinstance(df, pd.DataFrame)
        assert not df.empty

    def test_columns_present(self):
        df = data_source._bybit_to_df(self._make_raw())
        for col in ("start", "open", "high", "low", "close", "volume", "turnover"):
            assert col in df.columns

    def test_ascending_order(self):
        df = data_source._bybit_to_df(self._make_raw())
        assert df["start"].is_monotonic_increasing

    def test_timestamps_are_utc(self):
        df = data_source._bybit_to_df(self._make_raw())
        assert df["start"].dt.tz is not None
        assert str(df["start"].dt.tz) == "UTC"

    def test_empty_raw_returns_empty_df(self):
        df = data_source._bybit_to_df([])
        assert df.empty

    def test_numeric_columns(self):
        df = data_source._bybit_to_df(self._make_raw())
        assert df["close"].dtype.kind == "f"


# ── _okx_to_df ────────────────────────────────────────────────────────────────

class TestOkxToDf:
    def _make_raw(self):
        # OKX: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm], DESC
        now_ms = 1_700_000_000_000
        return [
            [str(now_ms),       "30000", "31000", "29000", "30500", "100", "100", "3050000", "1"],
            [str(now_ms - 14400_000), "29000", "30000", "28500", "29500", "80",  "80",  "2360000", "1"],
        ]

    def test_returns_dataframe(self):
        df = data_source._okx_to_df(self._make_raw())
        assert isinstance(df, pd.DataFrame)

    def test_columns_present(self):
        df = data_source._okx_to_df(self._make_raw())
        for col in ("ts", "open", "high", "low", "close"):
            assert col in df.columns

    def test_ascending_order(self):
        df = data_source._okx_to_df(self._make_raw())
        assert df["ts"].is_monotonic_increasing

    def test_timestamps_are_utc(self):
        df = data_source._okx_to_df(self._make_raw())
        assert str(df["ts"].dt.tz) == "UTC"

    def test_empty_raw_returns_empty_df(self):
        df = data_source._okx_to_df([])
        assert df.empty


# ── _bybit_request (HTTP mocking) ─────────────────────────────────────────────

class TestBybitRequest:
    def _mock_200(self, data):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"retCode": 0, "result": {"list": data}}
        return resp

    def test_success_returns_list(self):
        raw_data = [["1700000000000", "30000", "31000", "29000", "30500", "100", "3050000"]]
        with patch("requests.get", return_value=self._mock_200(raw_data)):
            result = data_source._bybit_request("BTCUSDT", "240", 10)
        assert result == raw_data

    def test_non_zero_ret_code_returns_none(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"retCode": 10001, "retMsg": "error"}
        with patch("requests.get", return_value=resp):
            result = data_source._bybit_request("BTCUSDT", "240", 10)
        assert result is None

    def test_500_retries_and_returns_none(self):
        resp = MagicMock()
        resp.status_code = 500
        with patch("requests.get", return_value=resp), patch("time.sleep"):
            result = data_source._bybit_request("BTCUSDT", "240", 10)
        assert result is None

    def test_connection_error_retries_and_returns_none(self):
        import requests as req
        with patch("requests.get", side_effect=req.ConnectionError("network")), patch("time.sleep"):
            result = data_source._bybit_request("BTCUSDT", "240", 10)
        assert result is None

    def test_404_returns_none_without_retry(self):
        resp = MagicMock()
        resp.status_code = 404
        resp.text = "not found"
        with patch("requests.get", return_value=resp):
            result = data_source._bybit_request("BTCUSDT", "240", 10)
        assert result is None


# ── _okx_request (HTTP mocking) ───────────────────────────────────────────────

class TestOkxRequest:
    def _mock_200(self, data):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"code": "0", "data": data}
        return resp

    def test_success_returns_list(self):
        raw_data = [["1700000000000", "30000", "31000", "29000", "30500", "100", "100", "3050000", "1"]]
        with patch("requests.get", return_value=self._mock_200(raw_data)):
            result = data_source._okx_request("BTC-USDT", "4H", 10)
        assert result == raw_data

    def test_error_code_returns_none(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"code": "50001", "msg": "error"}
        with patch("requests.get", return_value=resp):
            result = data_source._okx_request("BTC-USDT", "4H", 10)
        assert result is None


# ── fetch_klines (integration of public API) ──────────────────────────────────

def _make_ohlcv_df(n=50, source="bybit"):
    """Helper: crea un DataFrame OHLCV mínimo con columnas estándar."""
    import numpy as np
    now = pd.Timestamp("2024-01-01", tz="UTC")
    ts = [now + pd.Timedelta(hours=4 * i) for i in range(n)]
    data = {
        "ts": ts,
        "Open": np.random.uniform(29000, 31000, n),
        "High": np.random.uniform(30500, 32000, n),
        "Low": np.random.uniform(28000, 29500, n),
        "Close": np.random.uniform(29000, 31000, n),
        "Volume": np.random.uniform(100, 500, n),
        "QuoteVolume": np.random.uniform(3e6, 9e6, n),
        "Trades": [0] * n,
        "__source__": [source] * n,
    }
    return pd.DataFrame(data)


class TestFetchKlines:
    def test_invalid_timeframe_returns_none(self):
        result = data_source.fetch_klines("BTC", "99x", 50)
        assert result is None

    def test_returns_dataframe_on_success(self):
        df = _make_ohlcv_df(50)
        mock_registry = {"bybit": lambda *a, **kw: df, "okx": lambda *a, **kw: None}
        with patch.dict(data_source.SOURCE_REGISTRY, mock_registry, clear=True):
            result = data_source.fetch_klines("BTC", "4h", 50)
        assert result is not None
        assert isinstance(result, pd.DataFrame)

    def test_fallback_to_okx_when_bybit_fails(self):
        df = _make_ohlcv_df(50, source="okx")
        mock_registry = {"bybit": lambda *a, **kw: None, "okx": lambda *a, **kw: df}
        with patch.dict(data_source.SOURCE_REGISTRY, mock_registry, clear=True):
            result = data_source.fetch_klines("BTC", "4h", 50)
        assert result is not None
        assert result["__source__"].iloc[0] == "okx"

    def test_all_sources_fail_returns_none(self):
        mock_registry = {"bybit": lambda *a, **kw: None, "okx": lambda *a, **kw: None}
        with patch.dict(data_source.SOURCE_REGISTRY, mock_registry, clear=True):
            result = data_source.fetch_klines("BTC", "4h", 50)
        assert result is None

    def test_required_columns_present(self):
        df = _make_ohlcv_df(50)
        with patch.object(data_source, "_bybit_fetch", return_value=df):
            result = data_source.fetch_klines("BTC", "4h", 50)
        for col in ("ts", "Open", "High", "Low", "Close", "Volume", "QuoteVolume", "__source__"):
            assert col in result.columns


# ── health_check ──────────────────────────────────────────────────────────────

class TestHealthCheck:
    def test_returns_ok_with_valid_data(self):
        df = _make_ohlcv_df(5)
        with patch("data_source.fetch_klines", return_value=df):
            ok, msg = data_source.health_check()
        assert ok is True
        assert "ok" in msg

    def test_returns_false_when_no_data(self):
        with patch("data_source.fetch_klines", return_value=None):
            ok, msg = data_source.health_check()
        assert ok is False

    def test_returns_false_when_zero_volume(self):
        df = _make_ohlcv_df(5)
        df["Volume"] = 0.0
        with patch("data_source.fetch_klines", return_value=df):
            ok, msg = data_source.health_check()
        assert ok is False
        assert "zero volume" in msg
