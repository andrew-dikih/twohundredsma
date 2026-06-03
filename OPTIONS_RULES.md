# Options Overlay Rules — Synthetic Backtest Results

## Setup
- **Engine**: 18.3-year backtest (2008–2026) on `pure_div` + `qual_value` champions, with an options sleeve carved out of total portfolio.
- **IRA-compatible only**: long calls/puts (LEAPS, PUTS), covered calls (CC), cash-secured puts (CSP). No naked shorts, no margin.
- **Pricing**: Black-Scholes with synthesized IV from 60-day realized vol × 1.15 base, stressed ×1.5/×2/×3 by SPY drawdown depth.
- **Refill modes**: `excess_only` (sleeve refills only when overflow) vs `two_way` (continuously rebalances sleeve to ceiling % of PV).
- **72 variants tested** across 2 bases × 4 overlays × 3 ceilings (5/10/15%) × 2 refill modes × 1–2 deltas.
- **Caveat**: Synthetic IV, no skew, CSP/CC assignments are cash-settled approximations. Treat as *directional sensitivity*, not tradable backtest.

## Key Findings

### Only 4/72 variants beat their base IRR
| base | overlay | ceiling | mode | delta | IRR | Δ-IRR | final | Δ-final |
|---|---|---|---|---|---|---|---|---|
| pure_div | **CC** | **5%** | excess_only | 0.30 | **28.95%** | +0.45% | $3.53M | +$128k |
| qual_value | **CSP** | **5%** | two_way | 0.30 | **22.19%** | +0.42% | $1.58M | +$56k |
| qual_value | CSP | 5% | excess_only | 0.30 | 22.06% | +0.29% | $1.56M | +$31k |
| qual_value | CSP | 10% | excess_only | 0.30 | 21.91% | +0.14% | $1.53M | +$5k |

### The Verdict: **5% sleeve max; premium-collection only**
| overlay | 5% excess_only | 5% two_way |
|---|---|---|
| CC | −0.06% | −0.81% |
| CSP | −0.08% | −0.20% |
| LEAPS (long) | −0.30% | −1.74% |
| PUTS (protective) | −0.30% | −4.18% |

**Worst losers**: PUTS @ 15% two_way (−15.3% IRR on qual_value). Protective hedges cost more than the crash protection delivers when the base strategy is already a low-drawdown machine.

## The Rules

### When to ADD an options sleeve
- **Only if base IRR ≥ 20%** — otherwise the opportunity cost (idle sleeve cash) dominates any premium income.
- **Ceiling = 5% maximum** of total PV. 10–15% drags returns by 0.5–4% IRR.
- **Use `excess_only` refill for pure_div** (CC overlay); **use `two_way` for qual_value** (CSP overlay).

### What overlay to use
| If your base is... | Use... | Why |
|---|---|---|
| Income-focused (pure_div) | **Covered calls @ Δ0.30, 30-day** | High-quality dividend names move slowly; selling calls monetizes vol you weren't going to capture anyway. |
| Quality-value (qual_value) | **Cash-secured puts @ Δ0.30, 30-day** | Names you'd already buy on a dip; CSP gives you a 1–2% discount via premium when assignment doesn't fire. |
| Anything else | **NONE** | The drag of idle sleeve cash isn't worth it. |

### Strikes & tenor
- **Delta 0.30** beats delta 0.20 in 12 of 12 head-to-head comparisons — more premium, manageable assignment.
- **30 days to expiry** is the sweet spot. 45-day expiry tested similar; we recommend 30-day for theta efficiency.
- **CC strikes**: ~3–5% out-of-the-money on names with >3 years dividend history.
- **CSP strikes**: ~5–8% below current price, on names ranked in top-quintile by your `qual_value` score.

### When to AVOID
- **Never buy long puts as protection** (PUTS overlay was always net-negative). Costs 30+ bps minimum, blows out to −15% IRR at higher ceilings.
- **Never buy LEAPS calls** on a base that's already 85%+ equity — you're just paying time-value for leverage you don't need.
- **Never run sleeve > 5%**. Every additional 5% of ceiling cost ~0.5–2% IRR.

### Candidate filter for option underlyings
Same as the base strategy's filter:
- ≥5 years price history
- ≥12 months of dividend payments (relax for CC if needed)
- Sector-diversified (no >20% concentration)
- For CSP: only on names you'd happily own at the strike

### Position sizing within the sleeve
- **CC**: write 1 contract per 100 uncovered shares; cap committed shares at 50% of position.
- **CSP**: collateralize fully with sleeve cash; max 2 contracts per name simultaneously.
- **Roll**: at 50% of max profit OR 7 days to expiry, whichever comes first.

## Bottom-line dollar impact (18.3 years, $7k annual IRA contributions)

| Strategy | Final value | vs base |
|---|---|---|
| pure_div bare | $3.40M | — |
| **pure_div + 5% CC @ Δ0.30** | **$3.53M** | **+$128k (+3.8%)** |
| qual_value bare | $1.53M | — |
| **qual_value + 5% CSP @ Δ0.30** | **$1.58M** | **+$56k (+3.7%)** |

## Honest disclosure
- Options uplift is **small** (~40 bps IRR) and **likely overstated** because:
  - Synth IV doesn't capture vol-skew premium that real CSP sellers face during crashes
  - CSP assignment is cash-settled — real-life you'd own the falling stock and might ride it further down
  - No transaction costs beyond 2% slippage modeled
- Bottom line: a small (5%) options sleeve is **at best a modest tailwind, at worst a drag**. Most variants lose money. The base strategy is doing the heavy lifting.
- If you're not comfortable monitoring options weekly, skip the sleeve entirely — the bare base captures 99%+ of the value.
