#!/usr/bin/env python3
import json
import os
import sys
from okx_ctl import OKXCore

CACHE_FILE = "/tmp/okx_last_state.json"

def main():
    core = OKXCore()
    
    # 1. Ticker & Position
    tk = core.market_api.get_ticker(instId="ETH-USDT-SWAP")
    curr_px = float(tk["data"][0]["last"])
    
    pos_res = core.account_api.get_positions(instType="SWAP", instId="ETH-USDT-SWAP")
    short_pos = None
    for p in pos_res.get("data", []):
        if p.get("posSide") == "short" and float(p.get("pos", 0)) > 0:
            short_pos = p
            break
            
    bal_res = core.account_api.get_account_balance()
    avail_eq = 0.0
    total_eq = 0.0
    for b in bal_res.get("data", [{}])[0].get("details", []):
        if b.get("ccy") == "USDT":
            avail_eq = float(b.get("availEq", 0.0) or 0.0)
            total_eq = float(b.get("eq", 0.0) or 0.0)
            break

    # Loss Limit Guard (10 USDT hard stop)
    risk_file = "/home/user/okx-bot/session_risk_config.json"
    session_loss = 0.0
    kill_switch_triggered = False
    if os.path.exists(risk_file):
        try:
            with open(risk_file, "r") as rf:
                r_cfg = json.load(rf)
                init_eq = float(r_cfg.get("initial_equity", 414.53))
                max_loss = float(r_cfg.get("max_allowed_loss", 10.0))
                session_loss = init_eq - total_eq
                if session_loss >= max_loss:
                    kill_switch_triggered = True
        except Exception:
            pass
            
    # 2. Algo order
    algo_res = core.get_active_algos()
    sl_px = None
    tp_px = None
    for a in algo_res.get("algo_orders", []):
        if a.get("slTriggerPx"):
            sl_px = float(a["slTriggerPx"])
        if a.get("tpTriggerPx"):
            tp_px = float(a["tpTriggerPx"])
            
    # 3. Orderbook & Derivative Metrics
    ob = core.get_orderbook_analysis(sz=400)
    capital = ob.get("capital_flow", {})
    oi_eth = capital.get("oi_eth", 0.0)
    lsr = capital.get("long_short_ratio", 0.0)
    fr = capital.get("funding_rate", 0.0)
    
    # 4. Liquidations (last 100)
    liq_res = core.public_api._request_with_params("GET", "/api/v5/public/liquidation-orders", {
        "instType": "SWAP",
        "instFamily": "ETH-USDT",
        "state": "filled",
        "limit": "100"
    })
    long_liq_sz = 0.0
    short_liq_sz = 0.0
    long_liq_cnt = 0
    short_liq_cnt = 0
    for order in liq_res.get("data", []):
        for det in order.get("details", []):
            side = det.get("side")
            sz = float(det.get("sz", 0) or 0)
            if side == "buy": # Forced buy = short liquidated
                short_liq_sz += sz
                short_liq_cnt += 1
            else: # Forced sell = long liquidated
                long_liq_sz += sz
                long_liq_cnt += 1
                
    # 5. Delta Cache calculation
    delta_oi = 0.0
    delta_lsr = 0.0
    delta_px = 0.0
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                last_data = json.load(f)
                delta_oi = oi_eth - float(last_data.get("oi_eth", oi_eth))
                delta_lsr = lsr - float(last_data.get("lsr", lsr))
                delta_px = curr_px - float(last_data.get("price", curr_px))
        except Exception:
            pass
            
    # Save cache
    with open(CACHE_FILE, "w") as f:
        json.dump({
            "price": curr_px,
            "oi_eth": oi_eth,
            "lsr": lsr,
            "top_whale_asks": ob.get("top_whale_asks", [])[:3],
            "top_whale_bids": ob.get("top_whale_bids", [])[:3]
        }, f)
        
    # Output concise payload
    res = {
        "price": curr_px,
        "delta_price": round(delta_px, 2),
        "equity": round(avail_eq, 2),
        "total_equity": round(total_eq, 2),
        "session_loss": round(session_loss, 2),
        "kill_switch_triggered": kill_switch_triggered,
        "short_pos": {
            "contracts": float(short_pos.get("pos")) if short_pos else 0.0,
            "avg_px": float(short_pos.get("avgPx")) if short_pos else 0.0,
            "upl": float(short_pos.get("upl")) if short_pos else 0.0,
            "sl_px": sl_px,
            "tp_px": tp_px
        } if short_pos else None,
        "oi_eth": round(oi_eth, 1),
        "delta_oi": round(delta_oi, 1),
        "lsr": lsr,
        "delta_lsr": round(delta_lsr, 3),
        "funding_rate": fr,
        "liq_stats": {
            "long_liq_eth": round(long_liq_sz * 0.1, 1),
            "long_cnt": long_liq_cnt,
            "short_liq_eth": round(short_liq_sz * 0.1, 1),
            "short_cnt": short_liq_cnt
        },
        "top_whale_asks": ob.get("top_whale_asks", [])[:3],
        "top_whale_bids": ob.get("top_whale_bids", [])[:3],
        "ask_clusters_5u": ob.get("ask_clusters_5u", [])[:2],
        "bid_clusters_5u": ob.get("bid_clusters_5u", [])[:2]
    }
    print(json.dumps(res, ensure_ascii=False))

if __name__ == "__main__":
    main()
