# DCF Undervalued Stock Screener

A terminal-based stock screener that values equities using a Discounted Cash Flow (DCF) model. Pulls free cash flow data directly from SEC EDGAR 10-K filings, calculates WACC via CAPM with a fundamentals-based blended beta, and cross-validates intrinsic value against Yahoo Finance analyst price targets.

## Features

- **SEC EDGAR integration** — pulls operating cash flow and CapEx from 10-K filings (no paid data feed required)
- **Fundamental blended beta** — ⅔ Damodaran industry unlevered beta (re-levered via Hamada equation at the company's D/E ratio) + ⅓ raw 3-year historical beta vs `^SP500TR` (S&P 500 Total Return index). Non-US / ADR names trigger a country risk premium warning. *Note: Bloomberg Adjusted Beta uses the Blume mean-reversion formula (0.67 × β_raw + 0.33 × 1.0), which adjusts toward the market mean rather than an industry fundamental — this is a different methodology.*
- **CAPM → WACC** — cost of equity via CAPM, after-tax cost of debt from actual interest expense, weighted by market cap and debt
- **Gordon Growth DCF** — 5-year FCF projection using historical CAGR + perpetuity terminal value
- **Cross-validation** — compares DCF intrinsic value against Yahoo Finance analyst consensus targets
- **Rich terminal UI** — color-coded tables with full DCF bridge, cost of capital breakdown, and FCF history
- **Batch & interactive modes** — analyze one ticker at a time or screen a list in bulk
- **CSV export** — save screening results for further analysis

## Installation

```bash
git clone https://github.com/Justin-C-Eng/dcf-screener.git
cd dcf-screener
pip install -r requirements.txt
```

## Usage

```bash
# Interactive mode — enter tickers one at a time
python dcf_screener.py

# Batch mode — screen multiple tickers at once
python dcf_screener.py AAPL MSFT NVDA

# Screen the built-in default watchlist (AAPL, MSFT, GOOGL, META, AMZN, ...)
python dcf_screener.py --watchlist

# Export results to CSV
python dcf_screener.py AAPL NVDA TSM --export results.csv

# Custom terminal growth rate (default: 2.5%)
python dcf_screener.py AAPL --terminal-growth 0.03

# Filter comparison table to only undervalued names
python dcf_screener.py --watchlist --min-upside 15
```

### Interactive mode commands

| Command   | Action                                      |
|-----------|---------------------------------------------|
| `<TICKER>`| Analyze a stock                             |
| `compare` | Side-by-side comparison table of all results|
| `export`  | Save current session results to CSV         |
| `clear`   | Reset the session                           |
| `quit`    | Exit                                        |

## How It Works

1. **FCFF Series** — fetches `NetCashProvidedByUsedInOperatingActivities` and `PaymentsToAcquirePropertyPlantAndEquipment` from SEC EDGAR XBRL facts. US GAAP OCF is a **levered** cash flow (interest expense is already deducted), so using it directly with WACC (an unlevered discount rate) would double-count the interest tax shield. After-tax interest is therefore added back each year to produce **unlevered FCFF**:

   > `FCFF = OCF + InterestExpense × (1 − T) − CapEx`

   This makes WACC discounting and the subsequent net-debt subtraction internally consistent.

2. **FCFF Growth Rate** — 5-year CAGR of historical FCFF, capped at ±50%.

3. **Beta** — raw historical beta is computed from 3-year weekly returns against `^SP500TR` (S&P 500 Total Return index; preferred over SPY because SPY's adjusted price lags true total return by ~20 bps/yr due to dividend timing and fees). This raw beta is blended with the Damodaran industry unlevered beta re-levered using the Hamada equation: `β_L = β_U × (1 + (1−T) × D/E)`.

   > **Fundamental blended β = ⅔ × β_industry_relevered + ⅓ × β_raw**

   The 2/3 weight on the industry figure reduces idiosyncratic noise and anchors the estimate toward a forward-looking sector norm. This differs from Bloomberg Adjusted Beta (Blume adjustment: `0.67 × β_raw + 0.33 × 1.0`), which regresses toward the market mean (1.0) rather than an industry fundamental.

   For non-US / ADR names, a `country_risk_premium` can be passed to `cost_of_equity()` — Damodaran publishes annual CRP estimates by country.

4. **Net Debt** — total financial debt is built from SEC EDGAR XBRL tags to capture all interest-bearing obligations:

   | Tag | Role |
   |-----|------|
   | `LongTermDebtNoncurrent` | Bonds, term loans due after 12 months |
   | `LongTermDebtCurrent` | Current maturities of long-term debt (only added when the non-current tag is present, to avoid double-counting with the aggregate `LongTermDebt` fallback) |
   | `LongTermDebt` | Aggregate fallback for filers that don't split current / non-current |
   | `ShortTermBorrowings` | Revolvers, commercial paper, short-term notes — never overlaps with LTD tags |
   | `OperatingLeaseLiabilityNoncurrent/Current` | ASC 842 (effective FY2019) requires operating leases on-balance-sheet; they represent fixed obligations ranking alongside debt in distress (toggle via `INCLUDE_LEASE_LIABILITIES`) |

   `net_debt = total_debt − cash`. The terminal output shows which tags were found for each ticker.

5. **WACC** — cost of equity via CAPM (`Rf + β × ERP`), pre-tax cost of debt from actual `InterestExpense / total_debt`, weighted by market cap and expanded total debt.

6. **DCF** — projects FCFF for 5 years, discounts to present value at WACC, then adds a Gordon Growth terminal value (`FCFF × (1+g) / (WACC − g)`).

7. **Intrinsic Value** — `(PV of FCFFs + PV of terminal value − net debt) / shares outstanding`. Subtracting net debt converts enterprise value (what WACC-discounted FCFF produces) to equity value.

## Model Assumptions

| Parameter         | Default | Description                        |
|-------------------|---------|------------------------------------|
| Risk-free rate    | 4.30%   | 10-year US Treasury yield          |
| Equity risk premium | 5.50% | Damodaran ERP estimate             |
| Tax rate          | 21%     | US corporate tax rate              |
| Projection years  | 5       | Explicit forecast horizon          |
| Terminal growth   | 2.50%   | Long-run GDP growth approximation  |
| Beta benchmark    | ^SP500TR | S&P 500 Total Return index (dividends reinvested) |

## Disclaimer

This tool is for educational and research purposes only. It is not financial advice. DCF models are highly sensitive to assumptions — always do your own due diligence before making investment decisions.

## Dependencies

| Package    | Purpose                            |
|------------|------------------------------------|
| `requests` | SEC EDGAR and Damodaran HTTP calls |
| `yfinance` | Market price, shares, analyst targets |
| `pandas`   | Data manipulation                  |
| `numpy`    | Numerical calculations             |
| `rich`     | Terminal UI and color formatting   |
| `lxml`     | HTML table parsing (Damodaran)     |
