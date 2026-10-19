#!/usr/bin/env python3
"""
Hyperliquid On-Chain Multi-Whale Position Tracker.
Directly monitors top tracked whale addresses:
  - Whale 1: 0xb83de012dba672c76a7dbbbf3e459cb59d7d6e36 (79k ETH Short)
  - Whale 2: 0x5b5d51203a0f9079f8aeb098a6523a13f298c060 (106k ETH Short)
"""
import os
import sys
import json
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

WHALE_ADDRESSES = [
    {
        "alias": "Whale 1 (0xb83d)",
        "address": "0xb83de012dba672c76a7dbbbf3e459cb59d7d6e36"
    },
    {
        "alias": "Whale 2 (0x5b5d)",
        "address": "0x5b5d51203a0f9079f8aeb098a6523a13f298c060"
    }
]

def fetch_whale_state(address: str):
    url = "https://api.hyperliquid.xyz/info"
    headers = {"Content-Type": "application/json"}
    body = {"type": "clearinghouseState", "user": address}
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or "http://172.17.0.1:5010"
    proxies = {"http": proxy, "https": proxy}

    try:
        r = requests.post(url, headers=headers, json=body, proxies=proxies, timeout=10)
        data = r.json()
        margin = data.get("marginSummary", {})
        positions = data.get("assetPositions", [])

        out = {
            "whale_address": address,
            "account_value_usd": round(float(margin.get("accountValue", 0) or 0), 2),
            "total_ntl_pos_usd": round(float(margin.get("totalNtlPos", 0) or 0), 2),
            "total_margin_used": round(float(margin.get("totalMarginUsed", 0) or 0), 2),
            "positions": []
        }

        for p in positions:
            pos = p.get("position", {})
            coin = pos.get("coin")
            szi = float(pos.get("szi", 0) or 0)
            entryPx = float(pos.get("entryPx", 0) or 0)
            upl = float(pos.get("unrealizedPnl", 0) or 0)
            liq = float(pos.get("liquidationPx", 0) or 0)
            val = abs(szi * entryPx)
            side = "SHORT" if szi < 0 else "LONG"

            out["positions"].append({
                "coin": coin,
                "side": side,
                "size": abs(szi),
                "entry_price": entryPx,
                "notional_usd": round(val, 2),
                "upl_usd": round(upl, 2),
                "liq_price": round(liq, 2)
            })

        out["positions"].sort(key=lambda x: x["notional_usd"], reverse=True)
        return out
    except Exception as e:
        return {"whale_address": address, "error": str(e)}

def main():
    target_coin = sys.argv[1].upper() if len(sys.argv) > 1 else None
    results = []
    agg_eth_short_sz = 0.0
    agg_eth_short_ntl = 0.0

    for w in WHALE_ADDRESSES:
        state = fetch_whale_state(w["address"])
        state["alias"] = w["alias"]
        if target_coin:
            state["positions"] = [p for p in state.get("positions", []) if p.get("coin") == target_coin]
        for p in state.get("positions", []):
            if p.get("coin") == "ETH" and p.get("side") == "SHORT":
                agg_eth_short_sz += p.get("size", 0)
                agg_eth_short_ntl += p.get("notional_usd", 0)
        results.append(state)

    summary = {
        "aggregated_eth_short_size": round(agg_eth_short_sz, 2),
        "aggregated_eth_short_notional_usd": round(agg_eth_short_ntl, 2),
        "whales": results
    }
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
