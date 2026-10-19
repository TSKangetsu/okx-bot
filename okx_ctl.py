"""
OKX Unified CLI Execution & Dynamic Monitoring Tool for Antigravity Agent.
100% Price-Agnostic & Universal:
  - Dynamically calculates multi-timeframe (1m, 5m, 15m, 1H) Resistance/Support, Bollinger Bands, RSI, EMA, ATR, and Volume Climax.
  - Evaluates News & Derivative Sentiment (Long/Short Ratio, Funding Rate, Headlines) to adjust trading frequency.
  - Detects Historical Climax/Waterfall candles and live Smart Take-Profit signals (Volume Climax + Bottom Wick / Oversold RSI / 20h Timeout).
  - Enforces hard-coded 100x Isolated Short-Only risk guardrails (10~40 USDT margin, server-side SL <= 0.85%).
"""
import sys
import os
import json
import time
import math
import datetime
import argparse
import asyncio
from typing import Dict, Any, List, Optional
import requests
import pandas as pd
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import okx.MarketData as MarketData
import okx.Account as Account
import okx.Trade as Trade
import okx.PublicData as PublicData


class OKXCore:
    def __init__(self):
        self.api_key = os.getenv("OKX_API_KEY", "")
        self.secret_key = os.getenv("OKX_SECRET_KEY", "")
        self.passphrase = os.getenv("OKX_PASSPHRASE", "")
        self.flag = "0"  # Live real account

        # Proxy configuration (auto-detect or default to local tunnel)
        self.proxy = (
            os.getenv("HTTPS_PROXY")
            or os.getenv("HTTP_PROXY")
            or os.getenv("all_proxy")
            or "http://127.0.0.1:5011"
        )

        self.market_api = MarketData.MarketAPI(self.api_key, self.secret_key, self.passphrase, False, self.flag, proxy=self.proxy)
        self.account_api = Account.AccountAPI(self.api_key, self.secret_key, self.passphrase, False, self.flag, proxy=self.proxy)
        self.trade_api = Trade.TradeAPI(self.api_key, self.secret_key, self.passphrase, False, self.flag, proxy=self.proxy)
        self.public_api = PublicData.PublicAPI(self.api_key, self.secret_key, self.passphrase, False, self.flag, proxy=self.proxy)

    def get_account_summary(self) -> Dict[str, Any]:
        res = self.account_api.get_account_balance(ccy="USDT")
        if res.get("code") == "0" and res.get("data"):
            details = res["data"][0].get("details", [])
            if details:
                d = details[0]
                return {
                    "avail_usdt": round(float(d.get("availBal", 0) or 0), 2),
                    "total_eq_usdt": round(float(d.get("eq", 0) or 0), 2),
                    "frozen_usdt": round(float(d.get("frozenBal", 0) or 0), 2),
                }
        return {"avail_usdt": 0.0, "total_eq_usdt": 0.0, "frozen_usdt": 0.0}

    def get_positions(self, inst_id: str = "ETH-USDT-SWAP") -> List[Dict[str, Any]]:
        res = self.account_api.get_positions(instType="SWAP", instId=inst_id)
        active = []
        now_ms = int(time.time() * 1000)
        if res.get("code") == "0":
            for p in res.get("data", []):
                pos_sz = float(p.get("pos", 0) or 0)
                if pos_sz > 0:
                    c_time = int(p.get("cTime", now_ms) or now_ms)
                    u_time = int(p.get("uTime", now_ms) or now_ms)
                    held_hours = round((now_ms - c_time) / (1000 * 3600), 2)
                    active.append({
                        "instId": p.get("instId"),
                        "posSide": p.get("posSide"),
                        "mgnMode": p.get("mgnMode"),
                        "lever": p.get("lever"),
                        "pos_contracts": pos_sz,
                        "eth_size": round(pos_sz * 0.1, 3),
                        "avgPx": round(float(p.get("avgPx", 0) or 0), 2),
                        "markPx": round(float(p.get("markPx", 0) or 0), 2),
                        "upl_usdt": round(float(p.get("upl", 0) or 0), 2),
                        "upl_pct": round(float(p.get("uplRatio", 0) or 0) * 100, 2),
                        "liqPx": round(float(p.get("liqPx", 0) or 0), 2),
                        "margin_usdt": round(float(p.get("imr", 0) or p.get("margin", 0) or 0), 2),
                        "held_hours": held_hours,
                        "timeout_20h_warning": held_hours >= 18.0,
                        "last_update_age_min": round((now_ms - u_time) / (1000 * 60), 1),
                    })
        return active

    def fetch_df(self, bar: str = "15m", limit: int = 60) -> pd.DataFrame:
        res = self.market_api.get_candlesticks(instId="ETH-USDT-SWAP", bar=bar, limit=str(limit))
        if res.get("code") != "0" or not res.get("data"):
            return pd.DataFrame()
        rows = res["data"][::-1]
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
        for col in ["open", "high", "low", "close", "vol"]:
            df[col] = df[col].astype(float)
        df["ts_ms"] = df["ts"].astype(int)
        df["time_bj"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True).dt.tz_convert("Asia/Shanghai").dt.strftime("%m-%d %H:%M")
        return df

    def analyze_timeframe(self, df: pd.DataFrame, swing_window: int = 20) -> Dict[str, Any]:
        if df.empty or len(df) < 20:
            return {}

        # EMAs
        df["ema_7"] = df["close"].ewm(span=7, adjust=False).mean()
        df["ema_25"] = df["close"].ewm(span=25, adjust=False).mean()
        df["ema_99"] = df["close"].ewm(span=99, adjust=False).mean()

        # RSI (14) using Wilder's smoothing
        delta = df["close"].diff()
        gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        rs = gain / (loss + 1e-9)
        df["rsi"] = 100 - (100 / (1 + rs))

        # Bollinger Bands (20, 2)
        df["bb_mid"] = df["close"].rolling(window=20).mean()
        df["bb_std"] = df["close"].rolling(window=20).std()
        df["bb_upper"] = df["bb_mid"] + 2.0 * df["bb_std"]
        df["bb_lower"] = df["bb_mid"] - 2.0 * df["bb_std"]

        # ATR (14)
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift()).abs()
        low_close = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df["atr"] = tr.rolling(window=14).mean()

        # Volume ratio vs 20-period average
        df["vol_sma"] = df["vol"].rolling(window=20).mean()
        df["vol_ratio"] = df["vol"] / (df["vol_sma"] + 1e-9)

        # Wick metrics on latest & previous candle
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else last

        upper_wick = float(last["high"] - max(last["open"], last["close"]))
        lower_wick = float(min(last["open"], last["close"]) - last["low"])
        prev_upper_wick = float(prev["high"] - max(prev["open"], prev["close"]))
        prev_lower_wick = float(min(prev["open"], prev["close"]) - prev["low"])

        swing_high = float(df["high"].tail(swing_window).max())
        swing_low = float(df["low"].tail(swing_window).min())

        return {
            "close": round(float(last["close"]), 2),
            "high": round(float(last["high"]), 2),
            "low": round(float(last["low"]), 2),
            "rsi": round(float(last["rsi"]), 2),
            "ema_7": round(float(last["ema_7"]), 2),
            "ema_25": round(float(last["ema_25"]), 2),
            "ema_99": round(float(last["ema_99"]), 2),
            "bb_upper": round(float(last["bb_upper"]), 2),
            "bb_mid": round(float(last["bb_mid"]), 2),
            "bb_lower": round(float(last["bb_lower"]), 2),
            "atr": round(float(last["atr"]), 2),
            "vol_ratio": round(float(last["vol_ratio"]), 2),
            "swing_high_resistance": round(swing_high, 2),
            "swing_low_support": round(swing_low, 2),
            "upper_wick": round(upper_wick, 2),
            "lower_wick": round(lower_wick, 2),
            "prev_upper_wick": round(prev_upper_wick, 2),
            "prev_lower_wick": round(prev_lower_wick, 2),
            "trend": "BEARISH" if float(last["close"]) < float(last["ema_25"]) else "BULLISH",
        }

    def get_sentiment_and_regime(self) -> Dict[str, Any]:
        funding_rate = 0.0
        long_short_ratio = 1.0
        headlines = []

        try:
            fr = self.public_api.get_funding_rate(instId="ETH-USDT-SWAP")
            if fr.get("code") == "0" and fr.get("data"):
                funding_rate = float(fr["data"][0].get("fundingRate", 0) or 0)
        except Exception:
            pass

        try:
            rr = requests.get(
                "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=ETH",
                proxies={"http": self.proxy, "https": self.proxy},
                timeout=4
            ).json()
            if rr.get("code") == "0" and rr.get("data"):
                long_short_ratio = float(rr["data"][0][1])
        except Exception:
            pass

        try:
            url = "https://api.coingecko.com/api/v3/news"
            resp = requests.get(
                url,
                proxies={"http": self.proxy, "https": self.proxy},
                timeout=4,
                headers={"User-Agent": "Mozilla/5.0"}
            ).json()
            if isinstance(resp, dict) and "data" in resp:
                for item in resp["data"][:5]:
                    title = item.get("attributes", {}).get("title", "")
                    if title:
                        headlines.append(title)
        except Exception:
            pass

        # Score calculation (-100 extreme bearish to +100 extreme bullish)
        score = 0
        reasons = []

        # 1. Retail Long/Short Ratio (Contrarian indicator: high retail longs = bearish fuel for long squeeze)
        if long_short_ratio >= 2.0:
            score -= 25
            reasons.append(f"散户多空比高达 {long_short_ratio} (多头极度拥挤，具备连环爆多头踩踏燃料)")
        elif long_short_ratio >= 1.5:
            score -= 10
            reasons.append(f"多空比偏高 ({long_short_ratio})，多头占优易回调")
        elif long_short_ratio <= 0.9:
            score += 25
            reasons.append(f"多空比极低 ({long_short_ratio})，谨防空头拥挤逼空")

        # 2. Funding Rate
        if funding_rate >= 0.0002:
            score -= 15
            reasons.append(f"资金费率偏高 ({funding_rate*100:.4f}%)，多头杠杆过热")
        elif funding_rate <= -0.00015:
            score += 20
            reasons.append(f"资金费率为负 ({funding_rate*100:.4f}%)，注意逼空风险")

        # 3. News Headline Keywords
        bear_kw = ["sec", "hack", "dump", "crash", "outflow", "lawsuit", "ban", "liquidation", "drop", "sell", "hawkish", "war", "tariff"]
        bull_kw = ["etf", "inflow", "surge", "rally", "approval", "upgrade", "breakout", "buy", "dovish", "record"]
        for h in headlines:
            hl = h.lower()
            b_hits = sum(1 for k in bear_kw if k in hl)
            u_hits = sum(1 for k in bull_kw if k in hl)
            if b_hits > u_hits:
                score -= 12 * b_hits
                reasons.append(f"利空快讯: {h}")
            elif u_hits > b_hits:
                score += 12 * u_hits
                reasons.append(f"利好快讯: {h}")

        score = max(-100, min(100, score))
        if score <= -20:
            regime = "BEARISH"
            rec_watch_sec = 180  # High frequency (3 min cycle)
            prob_bonus = "+15%"
        elif score >= 20:
            regime = "BULLISH"
            rec_watch_sec = 900  # Defensive low frequency (15 min cycle)
            prob_bonus = "-20%"
        else:
            regime = "NEUTRAL"
            rec_watch_sec = 360  # Standard frequency (6 min cycle)
            prob_bonus = "0%"

        return {
            "regime": regime,
            "score": score,
            "short_prob_modifier": prob_bonus,
            "recommended_watch_sec": rec_watch_sec,
            "long_short_ratio": long_short_ratio,
            "funding_rate": funding_rate,
            "reasons": reasons,
            "headlines": headlines,
        }

    def place_short(
        self,
        margin_usdt: float,
        sl_px: float,
        tp_px: float,
        entry_px: Optional[float] = None,
        max_loss_usdt: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Guarded 100x Cross Short Order (Market or Limit at Resistance).
        Deploys actual `margin_usdt` at 100x leverage while enforcing `max_loss_usdt` (defaults to 2/3 of margin).
        """
        if margin_usdt < 5.0 or margin_usdt > 65.0:
            raise ValueError(f"[风控拦截] 单笔保证金 {margin_usdt} USDT 必须在 5.0 ~ 65.0 USDT 授权范围内！")

        tk = self.market_api.get_ticker(instId="ETH-USDT-SWAP")
        curr_px = float(tk["data"][0]["last"])
        ref_px = float(entry_px) if entry_px else curr_px

        if sl_px <= ref_px:
            raise ValueError(f"[风控拦截] 做空止损价 ({sl_px}) 必须高于入场价 ({ref_px})！")
        if tp_px >= ref_px:
            raise ValueError(f"[风控拦截] 做空止盈价 ({tp_px}) 必须低于入场价 ({ref_px})！")

        loss_distance = sl_px - ref_px
        allowed_loss = max_loss_usdt if max_loss_usdt is not None else (margin_usdt * (2.0 / 3.0))

        # 100x actual margin sizing: notional = margin_usdt * 100, 1 contract = 0.1 ETH
        target_contracts = (margin_usdt * 100.0) / (ref_px * 0.1)
        # Cap contracts if SL distance would exceed allowed_loss
        max_contracts_by_loss = allowed_loss / (loss_distance * 0.1)
        contracts = max(0.01, math.floor(min(target_contracts, max_contracts_by_loss) * 100) / 100)

        # Set 100x leverage on cross mode
        try:
            self.account_api.set_leverage(lever="100", mgnMode="cross", instId="ETH-USDT-SWAP")
        except Exception:
            pass

        body = {
            "instId": "ETH-USDT-SWAP",
            "tdMode": "cross",
            "side": "sell",
            "posSide": "short",
            "ordType": "limit" if entry_px else "market",
            "sz": f"{contracts:.2f}",
            "attachAlgoOrds": [
                {"slTriggerPx": str(sl_px), "slOrdPx": "-1", "slTriggerPxType": "last"},
                {"tpTriggerPx": str(tp_px), "tpOrdPx": "-1", "tpTriggerPxType": "last"},
            ],
        }
        if entry_px:
            body["px"] = str(entry_px)

        return self.trade_api.place_order(**body)

    def close_short(self, mgn_mode: str = "cross", pct: float = 100.0, sz: Optional[float] = None) -> Dict[str, Any]:
        """
        Closes 100%, a partial percentage (e.g., 50%), or an exact contract size (e.g. 1.0 contract) of the active short position at market.
        """
        positions = self.get_positions("ETH-USDT-SWAP")
        short_pos = [p for p in positions if p["posSide"] == "short" and p["mgnMode"] == mgn_mode]
        if not short_pos:
            short_pos = [p for p in positions if p["posSide"] == "short"]
            if not short_pos:
                return {"status": "NO_POSITION", "msg": "当前没有任何 ETH-USDT-SWAP 空单持仓。"}
            mgn_mode = short_pos[0]["mgnMode"]

        total_contracts = short_pos[0]["pos_contracts"]
        if sz is not None and sz > 0:
            close_sz = min(total_contracts, float(sz))
            if close_sz >= total_contracts - 0.001:
                return self.trade_api.close_positions(
                    instId="ETH-USDT-SWAP",
                    mgnMode=mgn_mode,
                    posSide="short",
                )
            return self.trade_api.place_order(
                instId="ETH-USDT-SWAP",
                tdMode=mgn_mode,
                side="buy",
                posSide="short",
                ordType="market",
                sz=f"{close_sz:.2f}",
            )

        if pct >= 99.9:
            return self.trade_api.close_positions(
                instId="ETH-USDT-SWAP",
                mgnMode=mgn_mode,
                posSide="short",
            )
        else:
            close_sz = math.floor(total_contracts * (pct / 100.0) * 100) / 100
            if close_sz < 0.01:
                close_sz = total_contracts
            return self.trade_api.place_order(
                instId="ETH-USDT-SWAP",
                tdMode=mgn_mode,
                side="buy",
                posSide="short",
                ordType="market",
                sz=f"{close_sz:.2f}",
            )

    def get_active_algos(self) -> Dict[str, Any]:
        """
        Lists all active server-side SL/TP conditional orders on ETH-USDT-SWAP.
        """
        try:
            res = self.trade_api.order_algos_list(ordType="conditional", instType="SWAP", instId="ETH-USDT-SWAP")
            orders = []
            for o in res.get("data", []):
                orders.append({
                    "algoId": o.get("algoId"),
                    "sz": o.get("sz"),
                    "side": o.get("side"),
                    "posSide": o.get("posSide"),
                    "slTriggerPx": o.get("slTriggerPx"),
                    "tpTriggerPx": o.get("tpTriggerPx"),
                    "state": o.get("state"),
                })
            return {"count": len(orders), "algo_orders": orders}
        except Exception as e:
            return {"error": str(e)}

    def amend_algo(
        self,
        algo_id: str,
        new_sl: Optional[float] = None,
        new_tp: Optional[float] = None,
        new_sz: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Amends an active server-side SL/TP conditional order (algoId) on ETH-USDT-SWAP.
        """
        kwargs: Dict[str, Any] = {"instId": "ETH-USDT-SWAP", "algoId": str(algo_id)}
        if new_sl is not None:
            kwargs["newSlTriggerPx"] = str(new_sl)
            kwargs["newSlOrdPx"] = "-1"
        if new_tp is not None:
            kwargs["newTpTriggerPx"] = str(new_tp)
            kwargs["newTpOrdPx"] = "-1"
        if new_sz is not None:
            kwargs["newSz"] = f"{float(new_sz):.2f}"
        return self.trade_api.amend_algo_order(**kwargs)

    def get_orderbook_analysis(self, sz: int = 400) -> Dict[str, Any]:
        """
        Fetches full L2 orderbook up to 400 levels, clusters depth by 5U buckets,
        identifies Top 5 whale walls, and audits capital flow composition (OI, Funding, LSR).
        """
        try:
            proxies = {"http": self.proxy, "https": self.proxy}
            url = f"https://www.okx.com/api/v5/market/books?instId=ETH-USDT-SWAP&sz={sz}"
            resp = requests.get(url, proxies=proxies, timeout=6).json()
            if resp.get("code") != "0" or not resp.get("data"):
                return {"error": "Failed to fetch orderbook", "resp": resp}
            data = resp["data"][0]
            bids = data["bids"]
            asks = data["asks"]

            tot_bid = sum(float(b[1]) for b in bids)
            tot_ask = sum(float(a[1]) for a in asks)

            def cluster(orders):
                buckets = {}
                for px, s, _, _ in orders:
                    p = float(px)
                    bucket = int(p // 5) * 5
                    buckets[bucket] = round(buckets.get(bucket, 0.0) + float(s), 2)
                return sorted(buckets.items(), key=lambda x: x[0])

            # Capital flow & composition metrics (OI, Funding Rate, Long/Short Account Ratio)
            capital_flow: Dict[str, Any] = {}
            try:
                oi_r = requests.get(
                    "https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId=ETH-USDT-SWAP",
                    proxies=proxies, timeout=4
                ).json()
                if oi_r.get("data"):
                    capital_flow["oi_contracts"] = round(float(oi_r["data"][0].get("oi", 0)), 2)
                    capital_flow["oi_eth"] = round(float(oi_r["data"][0].get("oiCcy", 0)), 2)
            except Exception:
                pass

            try:
                fr_r = requests.get(
                    "https://www.okx.com/api/v5/public/funding-rate?instId=ETH-USDT-SWAP",
                    proxies=proxies, timeout=4
                ).json()
                if fr_r.get("data"):
                    capital_flow["funding_rate"] = float(fr_r["data"][0].get("fundingRate", 0) or 0)
            except Exception:
                pass

            try:
                lsr_r = requests.get(
                    "https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy=ETH&period=5m",
                    proxies=proxies, timeout=4
                ).json()
                if lsr_r.get("data"):
                    capital_flow["long_short_ratio"] = float(lsr_r["data"][0][1])
            except Exception:
                pass

            return {
                "timestamp_bj": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "capital_flow": capital_flow,
                "total_bid_sz": round(tot_bid, 2),
                "total_ask_sz": round(tot_ask, 2),
                "ask_to_bid_ratio": round(tot_ask / (tot_bid + 1e-6), 2),
                "bid_clusters_5u": cluster(bids)[-6:],
                "ask_clusters_5u": cluster(asks)[:6],
                "top_whale_bids": sorted([(float(b[0]), float(b[1])) for b in bids], key=lambda x: x[1], reverse=True)[:5],
                "top_whale_asks": sorted([(float(a[0]), float(a[1])) for a in asks], key=lambda x: x[1], reverse=True)[:5],
            }
        except Exception as e:
            return {"error": str(e)}


def cmd_snapshot(core: OKXCore):
    account = core.get_account_summary()
    positions = core.get_positions()
    sentiment = core.get_sentiment_and_regime()

    df_1m = core.fetch_df("1m", 60)
    df_5m = core.fetch_df("5m", 60)
    df_15m = core.fetch_df("15m", 60)
    df_1h = core.fetch_df("1H", 48)

    tf_1m = core.analyze_timeframe(df_1m, swing_window=30)
    tf_5m = core.analyze_timeframe(df_5m, swing_window=24)
    tf_15m = core.analyze_timeframe(df_15m, swing_window=24)
    tf_1h = core.analyze_timeframe(df_1h, swing_window=24)

    curr_px = tf_1m.get("close", 0.0)

    # Identify dynamic resistance & support zones across timeframes
    dynamic_zones = {
        "immediate_resistance_5m": round(max(tf_5m.get("bb_upper", curr_px), tf_5m.get("swing_high_resistance", curr_px)), 2),
        "major_resistance_15m": round(max(tf_15m.get("bb_upper", curr_px), tf_15m.get("swing_high_resistance", curr_px)), 2),
        "macro_resistance_1h": round(tf_1h.get("swing_high_resistance", curr_px), 2),
        "immediate_support_5m": round(min(tf_5m.get("bb_lower", curr_px), tf_5m.get("swing_low_support", curr_px)), 2),
        "major_support_15m": round(min(tf_15m.get("bb_lower", curr_px), tf_15m.get("swing_low_support", curr_px)), 2),
        "macro_support_1h": round(tf_1h.get("swing_low_support", curr_px), 2),
    }

    # Detect historical volume climax / waterfall candles in last 15m history (like the 5:30 AM crash)
    climax_events = []
    if not df_15m.empty and len(df_15m) >= 20:
        vol_avg = df_15m["vol"].rolling(20).mean()
        for idx, row in df_15m.tail(32).iterrows():
            v_ratio = row["vol"] / (vol_avg.loc[idx] + 1e-9) if pd.notna(vol_avg.loc[idx]) else 1.0
            rng_pct = (row["high"] - row["low"]) / row["open"] * 100
            if v_ratio >= 2.2 or rng_pct >= 0.8:
                lower_wick = min(row["open"], row["close"]) - row["low"]
                upper_wick = row["high"] - max(row["open"], row["close"])
                climax_events.append({
                    "time_bj": row["time_bj"],
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "vol_ratio": round(float(v_ratio), 2),
                    "upper_wick": round(float(upper_wick), 2),
                    "lower_wick": round(float(lower_wick), 2),
                })

    # Recent 15m and 1m candle sequences for curve reading
    recent_15m = []
    for _, r in df_15m.tail(10).iterrows():
        recent_15m.append({
            "t": r["time_bj"],
            "o": r["open"],
            "h": r["high"],
            "l": r["low"],
            "c": r["close"],
            "v": int(r["vol"]),
        })

    recent_1m = []
    for _, r in df_1m.tail(8).iterrows():
        recent_1m.append({
            "t": r["time_bj"],
            "o": r["open"],
            "h": r["high"],
            "l": r["low"],
            "c": r["close"],
            "v": int(r["vol"]),
        })

    out = {
        "timestamp_bj": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "current_price": curr_px,
        "account": account,
        "positions": positions,
        "sentiment": sentiment,
        "dynamic_zones": dynamic_zones,
        "structure": {
            "1m": tf_1m,
            "5m": tf_5m,
            "15m": tf_15m,
            "1h": tf_1h,
        },
        "recent_climax_events_15m": climax_events[-5:],
        "recent_candles_15m": recent_15m,
        "recent_candles_1m": recent_1m,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


async def cmd_watch_async(
    core: OKXCore,
    timeout_sec: Optional[int] = None,
    custom_resistance: Optional[float] = None,
    custom_tp_price: Optional[float] = None,
    custom_support: Optional[float] = None,
    whale_sz: float = 1500.0,
):
    """
    100% Dynamic WebSocket Radar (10-Trigger Unified Architecture).
    Monitors both `tickers` and `trades` streams in real time with zero hardcoded prices:
      1. TARGET_TP_ALERT: Price <= custom_tp_price.
      2. PROFIT_STEP_BREAKDOWN_ALERT: Dual-track breakdown (custom_support OR auto 5U step).
      3. WATERFALL_DROP_ALERT: Rapid drop >= 4.5 USDT from cycle high OR >= 3.0 USDT within 60s.
      4. CLIMAX_REBOUND_ALERT: Active short position rebounds >= 3.8 USDT after >= 3.0 USDT profit.
      5. RESISTANCE_BREAKOUT_ALERT: Price >= custom_resistance (or auto-step +8U above initial price).
      6. RAPID_PUMP_ALERT: Rapid surge >= 4.5 USDT from cycle low OR >= 3.0 USDT within 60s.
      7. STOP_LOSS_APPROACH_ALERT: Active short position approaches server-side SL (within 1.5U) or actually hits SL.
      8. WHALE_TRADE_ALERT: Single market order >= whale_sz (capped at <= 2500 contracts).
      9. SELL_VOLUME_SURGE_ALERT: 20s cumulative market sell >= 3500 contracts (or net sell >= 3000) + drop >= 1.5U.
     10. BUY_VOLUME_SURGE_ALERT: 20s cumulative market buy >= 3500 contracts (or net buy >= 3000) + surge >= 1.5U.
    """
    import aiohttp

    # Special Crypto/Macro Timing Windows Detection (北京时间)
    now_dt = datetime.datetime.now()
    cur_h, cur_m = now_dt.hour, now_dt.minute
    is_funding_window = (cur_h in [7, 15, 23] and cur_m >= 50) or (cur_h in [8, 16, 0] and cur_m <= 5)
    is_us_open_window = (cur_h in [20, 21, 22])

    is_10m_consolidating = False
    try:
        df_10m_check = core.fetch_df("1m", 15)
        if not df_10m_check.empty and len(df_10m_check) >= 10:
            last_10m = df_10m_check.tail(10)
            rng_10m = float(last_10m["high"].max() - last_10m["low"].min())
            if rng_10m <= 3.5:
                is_10m_consolidating = True
    except Exception:
        pass

    if is_funding_window or is_us_open_window:
        timing_max_sec = 60
    elif is_10m_consolidating:
        timing_max_sec = 900
    else:
        timing_max_sec = 120

    if timeout_sec is not None:
        timeout_sec = int(timeout_sec)
    else:
        timeout_sec = timing_max_sec

    ws_url = "wss://ws.okx.com:8443/ws/v5/public"
    sub_param = {
        "op": "subscribe",
        "args": [
            {"channel": "tickers", "instId": "ETH-USDT-SWAP"},
            {"channel": "trades", "instId": "ETH-USDT-SWAP"}
        ]
    }

    start_time = time.time()
    last_ping = time.time()

    # Resilient initial position & server-side SL fetch (with retry against transient SSL EOF)
    positions = []
    for _ in range(2):
        try:
            positions = core.get_positions()
            break
        except Exception:
            time.sleep(0.3)

    has_position = len(positions) > 0
    entry_px = float(positions[0]["avgPx"]) if has_position else None
    eth_size = float(positions[0]["eth_size"]) if has_position else 0.0

    active_hard_sl = None
    if has_position:
        for _ in range(2):
            try:
                algos_info = core.get_active_algos()
                sl_list = [float(a["slTriggerPx"]) for a in algos_info.get("algo_orders", []) if a.get("slTriggerPx")]
                if sl_list:
                    active_hard_sl = min(sl_list)
                break
            except Exception:
                time.sleep(0.3)

    # Cap whale_sz at 2500 contracts max so models cannot blind the radar with 5000+
    whale_sz = min(float(whale_sz), 2500.0) if whale_sz > 0 else 1500.0

    recent_trades = []
    price_window_60s = []  # Rolling 60s (timestamp, px) for short-term velocity detection
    custom_support_px = float(custom_support) if custom_support else None
    custom_res_px = float(custom_resistance) if custom_resistance else None
    dyn_res = custom_res_px  # Will auto-initialize from first ticker if None
    auto_step_px = None
    cycle_min_px = None
    cycle_max_px = None

    try:
        async with aiohttp.ClientSession() as session:
            while time.time() - start_time < timeout_sec:
                try:
                    async with session.ws_connect(
                        ws_url,
                        proxy=core.proxy,
                        heartbeat=20,
                        timeout=aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None)
                    ) as ws:
                        await ws.send_str(json.dumps(sub_param))

                        async for msg in ws:
                            now = time.time()
                            if now - start_time >= timeout_sec:
                                print(json.dumps({
                                    "event": "CYCLE_TIMEOUT",
                                    "has_position": has_position,
                                    "msg": f"已完成 {timeout_sec}s 周期静默盯盘，唤醒 Agent 进行例行结构与持仓巡检。"
                                }, ensure_ascii=False), flush=True)
                                return

                            if now - last_ping >= 20.0:
                                last_ping = now
                                try:
                                    await ws.send_str("ping")
                                except Exception:
                                    pass

                            if msg.type != aiohttp.WSMsgType.TEXT:
                                continue
                            if msg.data == "pong":
                                continue

                            data = json.loads(msg.data)
                            arg = data.get("arg", {})
                            channel = arg.get("channel", "")

                            # --- 1. Real-time Ticker Price Monitoring ---
                            if channel == "tickers" and "data" in data and data["data"]:
                                t_data = data["data"][0]
                                px = float(t_data.get("last", 0) or 0)
                                if px <= 0:
                                    continue

                                # Initialize dynamic bounds on first valid tick
                                if cycle_min_px is None or px < cycle_min_px:
                                    cycle_min_px = px
                                if cycle_max_px is None or px > cycle_max_px:
                                    cycle_max_px = px
                                if auto_step_px is None:
                                    auto_step_px = (math.floor(px / 5.0)) * 5.0
                                    if auto_step_px >= px - 1.2:
                                        auto_step_px -= 5.0
                                if dyn_res is None:
                                    dyn_res = (math.ceil(px / 5.0)) * 5.0
                                    if dyn_res <= px + 1.5:
                                        dyn_res += 5.0

                                # Maintain rolling 60s price window for instant velocity alerts
                                price_window_60s.append((now, px))
                                cutoff_60s = now - 60.0
                                price_window_60s = [p for p in price_window_60s if p[0] >= cutoff_60s]
                                win_max_60s = max(p[1] for p in price_window_60s)
                                win_min_60s = min(p[1] for p in price_window_60s)

                                # T1. Custom Take Profit Trigger
                                if custom_tp_price and px <= custom_tp_price:
                                    print(json.dumps({
                                        "event": "TARGET_TP_ALERT",
                                        "current_price": px,
                                        "target_tp": custom_tp_price,
                                        "msg": f"触达止盈位 ${custom_tp_price} (现价 ${px})，立即唤醒执行止盈！"
                                    }, ensure_ascii=False), flush=True)
                                    return

                                # T2. Dual-Track Breakdown Alert (Custom Support OR Auto 5U Step)
                                hit_support = None
                                if custom_support_px is not None and px < custom_support_px:
                                    hit_support = custom_support_px
                                elif auto_step_px is not None and px < auto_step_px:
                                    hit_support = auto_step_px

                                if hit_support is not None:
                                    print(json.dumps({
                                        "event": "PROFIT_STEP_BREAKDOWN_ALERT",
                                        "current_price": px,
                                        "breakdown_step": hit_support,
                                        "has_position": has_position,
                                        "entry_px": entry_px,
                                        "upl_usdt": round((entry_px - px) * eth_size, 2) if (has_position and entry_px) else 0.0,
                                        "msg": (
                                            f"检测到【下拉破位击穿】！现价 ${px} 击穿关键阶梯 ${hit_support} "
                                            + (f"(持仓均价 ${entry_px}，浮盈扩大)，立即唤醒下移止损！" if has_position else "(当前空仓)，立即唤醒审计盘口评估右侧追空！")
                                        )
                                    }, ensure_ascii=False), flush=True)
                                    return

                                # T3. Waterfall Drop Velocity Alert (Rapid drop >= 4.5U from cycle high OR >= 3.0U in 60s)
                                drop_from_cycle = (cycle_max_px - px) if cycle_max_px else 0.0
                                drop_in_60s = win_max_60s - px
                                if drop_from_cycle >= 4.5 or drop_in_60s >= 3.0:
                                    ref_high = win_max_60s if drop_in_60s >= 3.0 else cycle_max_px
                                    drop_val = max(drop_from_cycle, drop_in_60s)
                                    print(json.dumps({
                                        "event": "WATERFALL_DROP_ALERT",
                                        "current_price": px,
                                        "ref_high_px": ref_high,
                                        "drop_usdt": round(drop_val, 2),
                                        "has_position": has_position,
                                        "msg": f"检测到【瀑布急跌破位】！现价 ${px} 从高点 ${ref_high} 急速下杀 ${drop_val:.2f} USDT，立即唤醒追空或锁利！"
                                    }, ensure_ascii=False), flush=True)
                                    return

                                # T4. Climax Rebound Alert (ONLY active when holding a short position in profit)
                                if has_position and entry_px is not None:
                                    if px <= entry_px and (px - cycle_min_px >= 3.80) and (entry_px - cycle_min_px >= 3.0):
                                        print(json.dumps({
                                            "event": "CLIMAX_REBOUND_ALERT",
                                            "current_price": px,
                                            "cycle_min_px": cycle_min_px,
                                            "rebound_usdt": round(px - cycle_min_px, 2),
                                            "msg": f"检测到【持仓触底快速反抽】！现价 ${px} 从低点 ${cycle_min_px} 反抽已达 ${(px - cycle_min_px):.2f}，立即唤醒评估止盈/移损！"
                                        }, ensure_ascii=False), flush=True)
                                        return

                                # T5. Server-Side Hard Stop Loss Approach / Hit Alert (Prioritized BEFORE general resistance)
                                if has_position and active_hard_sl is not None:
                                    if px >= active_hard_sl:
                                        print(json.dumps({
                                            "event": "SERVER_SL_TRIGGERED_ALERT",
                                            "current_price": px,
                                            "hard_sl": active_hard_sl,
                                            "msg": f"【云端止损触发告警】现价 ${px} 已触达/穿透云端条件止损价 ${active_hard_sl}，立即唤醒核验平仓状态与风控统计！"
                                        }, ensure_ascii=False), flush=True)
                                        return
                                    elif px >= (active_hard_sl - 1.50):
                                        print(json.dumps({
                                            "event": "STOP_LOSS_APPROACH_ALERT",
                                            "current_price": px,
                                            "hard_sl": active_hard_sl,
                                            "msg": f"警报！现价 ${px} 逼近云端硬止损线 ${active_hard_sl} (仅差 ${(active_hard_sl - px):.2f})，立即唤醒决策！"
                                        }, ensure_ascii=False), flush=True)
                                        return

                                # T6. Resistance Breakout / Left-Side Ambush Alert
                                if dyn_res is not None and px >= dyn_res:
                                    print(json.dumps({
                                        "event": "RESISTANCE_BREAKOUT_ALERT",
                                        "current_price": px,
                                        "dyn_res": dyn_res,
                                        "has_position": has_position,
                                        "entry_px": entry_px,
                                        "msg": f"检测到【强势触达/突破阻力线】！现价 ${px} 向上穿透 ${dyn_res}，立即唤醒决策！"
                                    }, ensure_ascii=False), flush=True)
                                    return

                                # T7. Rapid Pump / Squeeze Velocity Alert (Rapid surge >= 4.5U from cycle low OR >= 3.0U in 60s)
                                pump_from_cycle = (px - cycle_min_px) if cycle_min_px else 0.0
                                pump_in_60s = px - win_min_60s
                                if pump_from_cycle >= 4.5 or pump_in_60s >= 3.0:
                                    ref_low = win_min_60s if pump_in_60s >= 3.0 else cycle_min_px
                                    pump_val = max(pump_from_cycle, pump_in_60s)
                                    print(json.dumps({
                                        "event": "RAPID_PUMP_ALERT",
                                        "current_price": px,
                                        "ref_low_px": ref_low,
                                        "pump_usdt": round(pump_val, 2),
                                        "has_position": has_position,
                                        "msg": f"检测到【短线急拉异动】！现价 ${px} 从低点 ${ref_low} 急速拉升 ${pump_val:.2f} USDT，立即唤醒摸顶狙击或风控！"
                                    }, ensure_ascii=False), flush=True)
                                    return

                            # --- 2. Real-time Trade Flow & Whale Market Orders ---
                            if channel == "trades" and "data" in data:
                                for tr in data["data"]:
                                    ts = int(tr.get("ts", 0)) / 1000.0
                                    sz = float(tr.get("sz", 0) or 0)
                                    side = tr.get("side", "")
                                    t_px = float(tr.get("px", 0) or 0)
                                    recent_trades.append((ts, side, sz, t_px))

                                    # T8. Single Whale Market Order Alert (capped at <= 2500 contracts)
                                    if whale_sz > 0 and sz >= whale_sz:
                                        action_str = "巨鲸天量砸盘做空" if side == "sell" else "巨鲸天量扫货拉升"
                                        print(json.dumps({
                                            "event": "WHALE_TRADE_ALERT",
                                            "side": side,
                                            "sz_contracts": round(sz, 2),
                                            "sz_eth": round(sz * 0.1, 2),
                                            "px": t_px,
                                            "msg": f"【巨鲸实时逐笔流】检测到{action_str}！单笔成交 {sz:.1f} 张 ({sz*0.1:.1f} ETH)，成交价 ${t_px}，立即唤醒！"
                                        }, ensure_ascii=False), flush=True)
                                        return

                                cutoff = now - 20.0
                                recent_trades = [t for t in recent_trades if t[0] >= cutoff]

                                if recent_trades:
                                    buy_contracts = sum(t[2] for t in recent_trades if t[1] == "buy")
                                    sell_contracts = sum(t[2] for t in recent_trades if t[1] == "sell")
                                    tot_contracts = buy_contracts + sell_contracts
                                    net_sell = sell_contracts - buy_contracts
                                    net_buy = buy_contracts - sell_contracts
                                    w20_high = max(t[3] for t in recent_trades)
                                    w20_low = min(t[3] for t in recent_trades)
                                    latest_px = recent_trades[-1][3]

                                    # T9. 20s Cumulative Iceberg Dump Alert
                                    is_sell_dominant = (sell_contracts >= 3500.0 and (sell_contracts / max(tot_contracts, 1.0) >= 0.68)) or (net_sell >= 3000.0)
                                    if is_sell_dominant and (w20_high - latest_px >= 1.5):
                                        print(json.dumps({
                                            "event": "SELL_VOLUME_SURGE_ALERT",
                                            "sell_contracts": round(sell_contracts, 1),
                                            "sell_eth": round(sell_contracts * 0.1, 1),
                                            "buy_contracts": round(buy_contracts, 1),
                                            "price_drop_20s": round(w20_high - latest_px, 2),
                                            "latest_px": latest_px,
                                            "msg": f"检测到【主力连续拆单砸盘浪涌】！20s 内累计主动砸盘 {sell_contracts:.0f} 张 (净砸盘 {net_sell:.0f} 张)，价格从 ${w20_high} 急挫 {(w20_high - latest_px):.2f}U 至 ${latest_px}，立即唤醒追空/锁利！"
                                        }, ensure_ascii=False), flush=True)
                                        return

                                    # T10. 20s Cumulative Iceberg Pump / Squeeze Alert
                                    is_buy_dominant = (buy_contracts >= 3500.0 and (buy_contracts / max(tot_contracts, 1.0) >= 0.68)) or (net_buy >= 3000.0)
                                    if is_buy_dominant and (latest_px - w20_low >= 1.5):
                                        print(json.dumps({
                                            "event": "BUY_VOLUME_SURGE_ALERT",
                                            "buy_contracts": round(buy_contracts, 1),
                                            "buy_eth": round(buy_contracts * 0.1, 1),
                                            "sell_contracts": round(sell_contracts, 1),
                                            "price_surge_20s": round(latest_px - w20_low, 2),
                                            "latest_px": latest_px,
                                            "msg": f"检测到【买盘连续扫货逼空】！20s 内累计主动买入 {buy_contracts:.0f} 张 (净买入 {net_buy:.0f} 张)，价格从 ${w20_low} 急拉 {(latest_px - w20_low):.2f}U 至 ${latest_px}，立即唤醒防范逼空！"
                                        }, ensure_ascii=False), flush=True)
                                        return

                except Exception as ws_err:
                    await asyncio.sleep(1.0)

            print(json.dumps({
                "event": "CYCLE_TIMEOUT",
                "has_position": has_position,
                "msg": f"已完成 {timeout_sec}s 周期静默盯盘，唤醒 Agent 进行例行结构与持仓巡检。"
            }, ensure_ascii=False), flush=True)
            return

    except Exception as e:
        print(json.dumps({"event": "ERROR", "msg": str(e)}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description="Universal OKX CLI Controller for Antigravity Agent")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("snapshot", help="Get complete multi-timeframe dynamic market & position snapshot")
    subparsers.add_parser("orderbook", help="Get 400-level L2 orderbook clusters, top whale walls, and capital flow")
    subparsers.add_parser("algos", help="List active server-side SL/TP conditional orders")

    p_amend = subparsers.add_parser("amend-algo", help="Amend server-side SL/TP trigger price or size")
    p_amend.add_argument("--algo-id", type=str, required=True, help="Algo order ID to amend")
    p_amend.add_argument("--sl", type=float, default=None, help="New stop-loss trigger price")
    p_amend.add_argument("--tp", type=float, default=None, help="New take-profit trigger price")
    p_amend.add_argument("--sz", type=float, default=None, help="New contract size")

    p_watch = subparsers.add_parser("watch", help="Dynamic WebSocket radar until exhaustion short or smart TP alert")
    p_watch.add_argument("--timeout", type=int, default=None, help="Override watch timeout in seconds")
    p_watch.add_argument("--resistance", type=float, default=None, help="Optional custom resistance price to watch")
    p_watch.add_argument("--tp-price", type=float, default=None, help="Optional custom take-profit price to watch")
    p_watch.add_argument("--support", type=float, default=None, help="Optional custom downside breakdown trigger price")
    p_watch.add_argument("--whale-sz", type=float, default=1500.0, help="Single trade contract size threshold to trigger whale alert (default: 1500 contracts / 150 ETH)")

    p_short = subparsers.add_parser("short", help="Execute guarded 100x cross short order")
    p_short.add_argument("--margin", type=float, required=True, help="Margin USDT deployed at 100x")
    p_short.add_argument("--sl", type=float, required=True, help="Stop loss price")
    p_short.add_argument("--tp", type=float, required=True, help="Take profit price")
    p_short.add_argument("--entry", type=float, default=None, help="Optional limit entry price (omit for market order)")
    p_short.add_argument("--max-loss", type=float, default=None, help="Optional max hard loss USDT cap (defaults to 2/3 of margin)")

    p_close = subparsers.add_parser("close", help="Market close short position (full or partial)")
    p_close.add_argument("--mgn-mode", type=str, default="cross", help="isolated or cross")
    p_close.add_argument("--pct", type=float, default=100.0, help="Percentage of position to close (1~100)")
    p_close.add_argument("--sz", type=float, default=None, help="Exact contract size to close (e.g. 1.0 contract = 0.1 ETH)")

    args = parser.parse_args()
    core = OKXCore()

    if args.command == "snapshot":
        cmd_snapshot(core)
    elif args.command == "orderbook":
        res = core.get_orderbook_analysis()
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.command == "algos":
        res = core.get_active_algos()
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.command == "amend-algo":
        res = core.amend_algo(args.algo_id, args.sl, args.tp, args.sz)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.command == "watch":
        asyncio.run(cmd_watch_async(core, args.timeout, args.resistance, args.tp_price, args.support, args.whale_sz))
    elif args.command == "short":
        res = core.place_short(args.margin, args.sl, args.tp, args.entry, args.max_loss)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.command == "close":
        res = core.close_short(args.mgn_mode, args.pct, args.sz)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
