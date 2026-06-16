# DCF Undervalued Stock Screener

A terminal-based stock screener that values equities using a Discounted Cash Flow (DCF) model. Pulls free cash flow data directly from SEC EDGAR 10-K filings, calculates WACC via CAPM with Bloomberg-style blended betas, and cross-validates intrinsic value against Yahoo Finance analyst price targets.

## Features

- **SEC EDGAR integration** — pulls operating cash flow and CapEx from 10-K filings (no paid data feed required)
- **Blended beta** — Bloomberg-style: ⅔ Damodaran industry unlevered beta (re-levered with Hamada equation) + ⅓ raw 3-year historical beta vs SPY
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

1. **FCF Series** — fetches `NetCashProvidedByUsedInOperatingActivities` and `PaymentsToAcquirePropertyPlantAndEquipment` from SEC EDGAR XBRL facts and computes annual FCF.

2. **FCF Growth Rate** — 5-year CAGR of historical FCF, capped at ±50%.

3. **Beta** — raw historical beta (3-year weekly returns vs SPY) is blended with the Damodaran industry unlevered beta re-levered using the Hamada equation at the company's D/E ratio.

4. **WACC** — cost of equity via CAPM (`Rf + β × ERP`), cost of debt from actual interest expense / total debt, weighted by market cap and debt.

5. **DCF** — projects FCF for 5 years, discounts to present value, then adds a Gordon Growth terminal value (`FCF × (1+g) / (WACC − g)`).

6. **Intrinsic Value** — `(PV of FCFs + PV of terminal value − net debt) / shares outstanding`.

## Model Assumptions

| Parameter         | Default | Description                        |
|-------------------|---------|------------------------------------|
| Risk-free rate    | 4.30%   | 10-year US Treasury yield          |
| Equity risk premium | 5.50% | Damodaran ERP estimate             |
| Tax rate          | 21%     | US corporate tax rate              |
| Projection years  | 5       | Explicit forecast horizon          |
| Terminal growth   | 2.50%   | Long-run GDP growth approximation  |
| Beta benchmark    | SPY     | S&P 500 ETF                        |

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
