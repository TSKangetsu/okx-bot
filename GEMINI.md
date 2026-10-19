# Workspace Operational Guidelines: ETH Short Sniper

## Core Rules for Antigravity AI
1. **Skill Architecture & Separation of Concerns**:
   - **Main Skill ([`eth-sniper/SKILL.md`](file:///home/user/okx-bot/eth-sniper/SKILL.md))**: Strictly stores pure, price-agnostic technical specifications, quantitative formulas, risk control rules, orderbook & whale trade-stream protocols, and CLI standards.
   - **Low-Token Test Skill ([`eth-sniper-test/SKILL.md`](file:///home/user/okx-bot/eth-sniper-test/SKILL.md))**: 3-tier zero-waste architecture (`okx_ctl.py` WebSocket + [`scout_brief.py`](file:///home/user/okx-bot/scout_brief.py) local delta-state cache `/tmp/okx_last_state.json` + one-shot `flash` subagent `okx-flash-scout`).
   - **Operational Experience ([`OPERATIONAL_EXPERIENCE.md`](file:///home/user/okx-bot/eth-sniper/references/OPERATIONAL_EXPERIENCE.md))**: Stores all historical battle logs, session-specific price levels, orderbook evolution case studies, and API/trigger troubleshooting lessons.
2. **Instrument, Direction, Margin Mode & Session Kill-Switch**:
   - `ETH-USDT-SWAP` ONLY, strictly **SHORT ONLY (`side="sell"`, `posSide="short"`)**.
   - Unified **100x Cross Margin (`tdMode="cross"`, `lever="100"`)** paired with mandatory **Server-Side Conditional Stop-Loss (`algoId`)** to cap maximum loss within authorized quota (`MaxLoss <= 2/3 * Margin`).
   - Strictly separate total account equity from authorized trading quota (`Batch 1 Base Quota + Batch 2 Tactical Reserve Quota`) and enforce the session-level risk parameters in [`session_risk_config.json`](file:///home/user/okx-bot/session_risk_config.json) (e.g., max margin `30 USDT`, max cumulative session loss `10 USDT` / hard stop equity threshold).
   - **Mandatory Anomaly Halt**: If a server-side stop-loss (`SERVER_SL_TRIGGERED_ALERT`) or unexpected position closure occurs without prior trigger warning, or if cumulative loss hits the session cap, immediately halt new entries and audit the cause.
3. **Defense Against False Breakouts & Structural Profit-Locking Ratchet**:
   - Never remove or widen server-side stop-loss to "hard-eat" extreme false breakouts beyond max loss tolerance.
   - Enforce **Segmented Stop-Loss + Dual-Track Re-entry**: Track 1 (Left-side exhaustion pinbar at extreme resistance) or Track 2 (Right-side SFP breakdown below neckline if price reverses early).
   - Once in deep profit, execute **Structural Profit-Locking Ratchet**: place server-side SL `+3 ~ +4 USDT` behind major whale ask walls (anti-wick buffer) rather than directly on round numbers, and step-lock profits (`75%~85%` of peak swing) after each liquidation cascade wave.
4. **10-Trigger Unified WebSocket Engine ([`okx_ctl.py`](file:///home/user/okx-bot/okx_ctl.py)) & Whale Flow Rules**:
   - **Zero Hardcoded Prices & Dynamic Position Sync**: Startup automatically queries live positions (`entry_px`, `has_position`) and active conditional algos (`live_sl_px`, `live_tp_px`). Position-only alerts (**T4 `CLIMAX_REBOUND_ALERT`**, **T5 `STOP_LOSS_APPROACH_ALERT` / `SERVER_SL_TRIGGERED_ALERT`**, **T6 `TARGET_TP_ALERT`**) are strictly gated by `has_position == True` to prevent ghost wakeups when flat.
   - **Dual-Track Breakdown & Velocity Triggers (Zero Blind Spots)**:
     - **T1 (`RESISTANCE_BREAKOUT_ALERT`)**: Price pierces `--resistance` or auto `5U` ceiling.
     - **T2 (`PROFIT_STEP_BREAKDOWN_ALERT`)**: Dual-track parallel check — fires when price breaks custom `--support` **OR** auto `5U` step (`auto_step_px`). Custom `--support` never disables auto `5U` step tracking.
     - **T3 (`WATERFALL_DROP_ALERT`) & T7 (`RAPID_PUMP_ALERT`)**: Rolling `60s` velocity (`>= 3.0 USDT` move in `60s`) or cycle swing (`>= 4.5 USDT` from `cycle_max_px` / `cycle_min_px`).
   - **Whale Single-Trade & 20s Iceberg Volume Surge Triggers**:
     - **T8 (`WHALE_TRADE_ALERT`)**: Hard-capped at `whale_sz <= 2500` contracts in code (recommended `1500 ~ 2000`; never set `>= 3500` which blinds the radar to split orders).
     - **T9 (`SELL_VOLUME_SURGE_ALERT`) & T10 (`BUY_VOLUME_SURGE_ALERT`)**: Rolling `20s` window detecting TWAP/iceberg dumps or pumps (`>= 3500` contracts with `>= 68%` dominance or `>= 3000` net directional contracts, paired with `>= 1.5 USDT` price displacement).
   - **Mandatory Breakdown Audit Rule**: When woken while flat by `PROFIT_STEP_BREAKDOWN_ALERT`, `WATERFALL_DROP_ALERT`, or `SELL_VOLUME_SURGE_ALERT`, never blindly stay idle — immediately run [`scout_brief.py`](file:///home/user/okx-bot/scout_brief.py) (or `orderbook`) to verify if whale ask walls are pressing down for a right-side short entry.
5. **Multi-Speed Monitoring & Special Windows**:
   - **High-Speed Mode (`30s~120s`)**: Active during breakouts, waterfall cascades, or when 10m range `> 2.5 USDT`. Mandatory `60s` mode during **15:00~17:00 BJ (EU session wake-up & 16:00 funding)**, funding windows (`08:00/16:00/00:00`), and US open (`20:30~22:30`).
   - **Event-Driven Cruise Mode (`900s ~ 1800s`)**: Enabled during consolidation with all 10 WebSocket triggers active, paired with [`scout_brief.py`](file:///home/user/okx-bot/scout_brief.py) delta audits at key session inflection nodes.
6. **Core Tool Commands**:
   - [`okx_ctl.py`](file:///home/user/okx-bot/okx_ctl.py): `snapshot`, `orderbook`, `algos`, `amend-algo`, `watch`, `short`, `close`.
   - [`scout_brief.py`](file:///home/user/okx-bot/scout_brief.py): Single-command delta-state brief (`ΔPrice`, `ΔOI`, `ΔLSR`, 100-trade liquidation summary, Top whale walls, and [`session_risk_config.json`](file:///home/user/okx-bot/session_risk_config.json) kill-switch verification).

