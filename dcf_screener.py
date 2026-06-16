"""
DCF-based Undervalued Stock Screener
Pulls FCF data from SEC EDGAR, calculates WACC via CAPM, runs DCF with Gordon Growth
terminal value, and computes upside vs current market price.
Cross-validates against Yahoo Finance analyst targets.

Usage:
  python dcf_screener.py                    # interactive mode (prompt for tickers)
  python dcf_screener.py AAPL MSFT NVDA     # batch mode
  python dcf_screener.py --help
"""

import time
import sys
import argparse
from datetime import datetime, timedelta
from typing import Optional

import requests
import yfinance as yf
import pandas as pd
import numpy as np
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text
from rich import box

console = Console()

HEADERS = {
    "User-Agent": "DCF Screener juhanchang0606@gmail.com",
    "Accept": "application/json",
}

RISK_FREE_RATE = 0.043      # 10-year US Treasury yield
MARKET_PREMIUM = 0.055      # Equity risk premium
TAX_RATE = 0.21             # US corporate tax rate
MARKET_TICKER = "SPY"       # Beta benchmark
PROJECTION_YEARS = 5
TERMINAL_GROWTH_DEFAULT = 0.025

DAMODARAN_BETAS_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/Betas.html"

# Yahoo Finance (sector, industry) → Damodaran industry name
INDUSTRY_BETA_MAP: dict[tuple[str, str], str] = {
    # Technology
    ("Technology", "Software - Infrastructure"):           "Software (System & Application)",
    ("Technology", "Software - Application"):              "Software (System & Application)",
    ("Technology", "Semiconductors"):                      "Semiconductor",
    ("Technology", "Semiconductor Equipment & Materials"): "Semiconductor Equip",
    ("Technology", "Consumer Electronics"):                "Electronics (Consumer & Office)",
    ("Technology", "Computer Hardware"):                   "Computers/Peripherals",
    ("Technology", "Information Technology Services"):     "Computer Services",
    ("Technology", "Electronic Components"):               "Electronics (General)",
    # Communication Services
    ("Communication Services", "Internet Content & Information"): "Software (Internet)",
    ("Communication Services", "Social Media"):            "Software (Internet)",
    ("Communication Services", "Entertainment"):           "Entertainment",
    ("Communication Services", "Broadcasting"):            "Broadcasting",
    ("Communication Services", "Telecom Services"):        "Telecom. Services",
    ("Communication Services", "Wireless Telecom Services"): "Telecom (Wireless)",
    # Healthcare
    ("Healthcare", "Biotechnology"):                       "Biotechnology",
    ("Healthcare", "Drug Manufacturers - General"):        "Drug (Pharmaceutical)",
    ("Healthcare", "Medical Devices"):                     "Healthcare Products",
    ("Healthcare", "Healthcare Plans"):                    "Healthcare Support Services",
    ("Healthcare", "Medical Care Facilities"):             "Hospitals/Healthcare Facilities",
    ("Healthcare", "Health Information Services"):         "Heathcare Information and Technology",
    # Financial Services
    ("Financial Services", "Banks - Diversified"):         "Banks (Money Center)",
    ("Financial Services", "Banks - Regional"):            "Banks (Regional)",
    ("Financial Services", "Insurance - Diversified"):     "Insurance (General)",
    ("Financial Services", "Insurance - Life"):            "Insurance (Life)",
    ("Financial Services", "Asset Management"):            "Investments & Asset Management",
    ("Financial Services", "Capital Markets"):             "Brokerage & Investment Banking",
    # Consumer
    ("Consumer Cyclical", "Retail - Specialty"):           "Retail (Special Lines)",
    ("Consumer Cyclical", "Retail - Apparel & Specialty"): "Retail (Special Lines)",
    ("Consumer Cyclical", "Internet Retail"):              "Retail (Online)",
    ("Consumer Cyclical", "Auto Manufacturers"):           "Auto & Truck",
    ("Consumer Cyclical", "Restaurants"):                  "Restaurant/Dining",
    ("Consumer Cyclical", "Hotels & Motels"):              "Hotel/Gaming",
    ("Consumer Defensive", "Beverages - Non-Alcoholic"):   "Beverage (Soft)",
    ("Consumer Defensive", "Beverages - Alcoholic"):       "Beverage (Alcoholic)",
    ("Consumer Defensive", "Food - Consumer Packaged Goods"): "Food Processing",
    ("Consumer Defensive", "Tobacco"):                     "Tobacco",
    # Energy
    ("Energy", "Oil & Gas Integrated"):                    "Oil/Gas (Integrated)",
    ("Energy", "Oil & Gas E&P"):                           "Oil/Gas (Production and Exploration)",
    ("Energy", "Oil & Gas Refining & Marketing"):          "Oil/Gas (Refining & Marketing)",
    # Industrials
    ("Industrials", "Aerospace & Defense"):                "Aerospace/Defense",
    ("Industrials", "Airlines"):                           "Air Transport",
    ("Industrials", "Railroads"):                          "Transportation (Railroads)",
    ("Industrials", "Trucking"):                           "Trucking",
    # Utilities
    ("Utilities", "Utilities - Regulated Electric"):       "Utility (General)",
    ("Utilities", "Utilities - Regulated Water"):          "Utility (Water)",
    # Real Estate
    ("Real Estate", "REIT - Diversified"):                 "R.E.I.T.",
    ("Real Estate", "Real Estate - Development"):          "Real Estate (Development)",
}

# Sector-level fallback when no specific industry match exists
SECTOR_BETA_MAP: dict[str, str] = {
    "Technology":             "Software (System & Application)",
    "Communication Services": "Software (Internet)",
    "Healthcare":             "Drug (Pharmaceutical)",
    "Financial Services":     "Banks (Regional)",
    "Consumer Cyclical":      "Retail (General)",
    "Consumer Defensive":     "Food Processing",
    "Energy":                 "Oil/Gas (Integrated)",
    "Industrials":            "Machinery",
    "Basic Materials":        "Metals & Mining",
    "Real Estate":            "R.E.I.T.",
    "Utilities":              "Utility (General)",
}

_damodaran_cache: Optional[pd.DataFrame] = None


# ─── SEC EDGAR helpers ────────────────────────────────────────────────────────

def get_cik(ticker: str) -> Optional[str]:
    url = "https://www.sec.gov/files/company_tickers.json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        for entry in r.json().values():
            if entry["ticker"].upper() == ticker.upper():
                return str(entry["cik_str"]).zfill(10)
    except Exception as e:
        console.print(f"  [yellow]CIK lookup failed: {e}[/yellow]")
    return None


def get_company_facts(cik: str) -> Optional[dict]:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        console.print(f"  [yellow]EDGAR facts fetch failed: {e}[/yellow]")
    return None


def extract_annual_values(facts: dict, concept: str, unit: str = "USD") -> pd.Series:
    try:
        entries = facts["facts"]["us-gaap"][concept]["units"][unit]
    except KeyError:
        return pd.Series(dtype=float)

    records = []
    for e in entries:
        if e.get("form") == "10-K" and e.get("fp") == "FY":
            try:
                records.append({
                    "end": pd.to_datetime(e["end"]),
                    "val": float(e["val"]),
                    "filed": pd.to_datetime(e.get("filed", "1900-01-01")),
                })
            except Exception:
                continue

    if not records:
        return pd.Series(dtype=float)

    df = (pd.DataFrame(records)
          .sort_values("filed")
          .drop_duplicates(subset="end", keep="last"))
    df["year"] = df["end"].dt.year
    df = df.sort_values("year")
    return pd.Series(df["val"].values, index=df["year"].values, dtype=float)


def compute_fcf_series(facts: dict) -> pd.Series:
    # Returns FCFF (Free Cash Flow to the Firm), not levered FCF.
    #
    # Why FCFF is required here:
    #   US GAAP OCF is a *levered* figure — interest paid is already deducted
    #   before the cash flow statement is prepared (IAS 7 / ASC 230).  Using
    #   levered OCF as the numerator while discounting at WACC (an *unlevered*
    #   rate) double-counts the interest tax shield: once implicitly inside OCF
    #   and again through the lower WACC.  Subtracting net debt at the end then
    #   adds a third inconsistency (bridge from enterprise → equity value is
    #   only valid for unlevered cash flows).
    #
    # Correct FCFF formula:
    #   FCFF = OCF + InterestExpense × (1 − T) − CapEx
    #
    # The add-back strips interest out of OCF (net of the tax shield we do want
    # to keep), producing an unlevered operating cash flow.  WACC discounting
    # + net-debt subtraction is then fully consistent.
    ocf_tags = [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ]
    capex_tags = [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForCapitalImprovements",
        "CapitalExpendituresIncurredButNotYetPaid",
    ]

    ocf = pd.Series(dtype=float)
    for tag in ocf_tags:
        ocf = extract_annual_values(facts, tag)
        if not ocf.empty:
            break

    capex = pd.Series(dtype=float)
    for tag in capex_tags:
        capex = extract_annual_values(facts, tag)
        if not capex.empty:
            break

    if ocf.empty:
        return pd.Series(dtype=float)

    capex = capex.reindex(ocf.index).fillna(0).abs()

    # Add back after-tax interest expense to convert levered OCF → unlevered FCFF.
    # Years with no EDGAR interest data (e.g., zero-debt periods) default to 0,
    # which is correct: no interest was paid, so no add-back is needed.
    interest = extract_annual_values(facts, "InterestExpense")
    interest_addback = interest.reindex(ocf.index).fillna(0).abs() * (1 - TAX_RATE)

    return ocf - capex + interest_addback


def five_year_fcf_growth(fcf: pd.Series) -> Optional[float]:
    fcf = fcf.dropna().sort_index()
    if len(fcf) < 2:
        return None
    recent = fcf.iloc[-min(6, len(fcf)):]
    base, end_ = recent.iloc[0], recent.iloc[-1]
    years = recent.index[-1] - recent.index[0]
    if years <= 0 or base <= 0:
        return None
    cagr = (end_ / base) ** (1 / years) - 1
    return float(np.clip(cagr, -0.50, 0.50))


# ─── Market data helpers ──────────────────────────────────────────────────────

def get_beta(ticker: str, window_years: int = 3) -> float:
    """Raw historical beta from weekly returns vs SPY."""
    try:
        end = datetime.today()
        start = end - timedelta(days=365 * window_years)
        prices = yf.download(
            [ticker, MARKET_TICKER], start=start, end=end,
            interval="1wk", progress=False, auto_adjust=True,
        )["Close"]
        if prices.empty or ticker not in prices.columns:
            return 1.0
        rets = prices.pct_change().dropna()
        cov = rets[ticker].cov(rets[MARKET_TICKER])
        var = rets[MARKET_TICKER].var()
        return float(cov / var) if var != 0 else 1.0
    except Exception:
        return 1.0


def fetch_damodaran_betas() -> Optional[pd.DataFrame]:
    """
    Download Damodaran's industry unlevered-beta table (cached in memory).
    Uses requests so SSL is handled by the system cert bundle.
    Returns a DataFrame with columns ['industry', 'unlevered_beta'].
    """
    global _damodaran_cache
    if _damodaran_cache is not None:
        return _damodaran_cache
    try:
        resp = requests.get(DAMODARAN_BETAS_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        from io import StringIO
        tables = pd.read_html(StringIO(resp.text), flavor="lxml")
        df = max(tables, key=len)

        # Damodaran's page has no <thead>: row 0 is the header row.
        # Column index type can be numpy.int64, so check against str instead.
        if not isinstance(df.columns[0], str):
            # Read column names from row 0 BEFORE dropping it
            col_names = [str(v).strip().lower() for v in df.iloc[0]]
            df = df.iloc[1:].reset_index(drop=True)
        else:
            col_names = [str(c).strip().lower() for c in df.columns]

        name_idx = next(
            (i for i, c in enumerate(col_names) if "industry" in c), 0
        )
        ul_idx = next(
            (i for i, c in enumerate(col_names) if "unlevered" in c and "cash" in c),
            None,
        )
        if ul_idx is None:
            ul_idx = next(
                (i for i, c in enumerate(col_names) if "unlevered" in c), None
            )
        if ul_idx is None:
            return None

        result = df.iloc[:, [name_idx, ul_idx]].copy()
        result.columns = ["industry", "unlevered_beta"]
        result["industry"] = result["industry"].astype(str).str.strip()
        result["unlevered_beta"] = pd.to_numeric(result["unlevered_beta"], errors="coerce")
        result = result.dropna(subset=["unlevered_beta"])
        result = result[~result["industry"].str.lower().str.contains("total market", na=False)]
        _damodaran_cache = result.reset_index(drop=True)
        return _damodaran_cache
    except Exception as e:
        console.print(f"  [yellow]Damodaran table unavailable: {e}[/yellow]")
        return None


def _lookup_unlevered_beta(sector: str, industry: str, df: pd.DataFrame) -> Optional[float]:
    """
    Map Yahoo Finance sector/industry to a Damodaran industry row.
    Tries exact industry match → sector fallback → fuzzy match.
    """
    from difflib import get_close_matches

    damod_name = INDUSTRY_BETA_MAP.get((sector, industry)) or SECTOR_BETA_MAP.get(sector)
    if damod_name is None:
        return None

    row = df[df["industry"].str.lower() == damod_name.lower()]
    if row.empty:
        matches = get_close_matches(
            damod_name.lower(), df["industry"].str.lower().tolist(), n=1, cutoff=0.55
        )
        if matches:
            row = df[df["industry"].str.lower() == matches[0]]

    return float(row["unlevered_beta"].iloc[0]) if not row.empty else None


def relever_beta(unlevered_beta: float, de_ratio: float) -> float:
    """Hamada equation: β_L = β_U × (1 + (1−T) × D/E)"""
    return unlevered_beta * (1 + (1 - TAX_RATE) * de_ratio)


def get_blended_beta(
    ticker: str,
    sector: str,
    industry: str,
    de_ratio: float,
) -> dict:
    """
    Bloomberg-style blended beta: (2/3) × industry_relevered + (1/3) × raw_historical.
    Falls back to raw beta if the Damodaran lookup fails.
    Returns a dict with all intermediate values for display.
    """
    raw = get_beta(ticker)

    damod_df = fetch_damodaran_betas()
    if damod_df is None:
        return {"blended": raw, "raw": raw, "industry_levered": None, "unlevered": None, "source": "raw only"}

    unlevered = _lookup_unlevered_beta(sector, industry, damod_df)
    if unlevered is None:
        console.print(
            f"  [yellow]⚠ No Damodaran match for '{industry}' ({sector}) — using raw beta[/yellow]"
        )
        return {"blended": raw, "raw": raw, "industry_levered": None, "unlevered": None, "source": "raw only"}

    industry_levered = relever_beta(unlevered, de_ratio)
    blended = (2 / 3) * industry_levered + (1 / 3) * raw
    return {
        "blended": blended,
        "raw": raw,
        "industry_levered": industry_levered,
        "unlevered": unlevered,
        "source": "blended",
    }


def get_current_price(ticker: str) -> Optional[float]:
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception:
        pass
    return None


def get_yahoo_info(ticker: str) -> dict:
    try:
        info = yf.Ticker(ticker).info
        return {
            "target": info.get("targetMeanPrice") or info.get("targetMedianPrice"),
            "market_cap": info.get("marketCap", 0) or 0,
            "shares": info.get("sharesOutstanding", 0) or 0,
            "cash": info.get("totalCash", 0) or 0,
            "name": info.get("longName") or info.get("shortName") or ticker,
            "sector": info.get("sector", "N/A"),
            "industry": info.get("industry", "N/A"),
        }
    except Exception:
        return {"target": None, "market_cap": 0, "shares": 0, "cash": 0,
                "name": ticker, "sector": "N/A", "industry": "N/A"}


def get_debt_interest(facts: dict) -> tuple[float, float]:
    debt = extract_annual_values(facts, "LongTermDebt")
    if debt.empty:
        debt = extract_annual_values(facts, "LongTermDebtNoncurrent")
    total_debt = float(debt.iloc[-1]) if not debt.empty else 0.0

    interest = extract_annual_values(facts, "InterestExpense")
    interest_exp = float(abs(interest.iloc[-1])) if not interest.empty else 0.0

    return total_debt, interest_exp


# ─── Valuation engine ─────────────────────────────────────────────────────────

def cost_of_equity(beta: float) -> float:
    return RISK_FREE_RATE + beta * MARKET_PREMIUM


def calc_wacc(ke: float, kd_pre_tax: float, equity: float, debt: float) -> float:
    total = equity + debt
    if total <= 0:
        return ke
    return (equity / total) * ke + (debt / total) * kd_pre_tax * (1 - TAX_RATE)


def run_dcf(
    latest_fcf: float,
    fcf_growth: float,
    terminal_growth: float,
    discount_rate: float,
    shares: float,
    net_debt: float,
) -> Optional[dict]:
    """
    Returns a detail dict with projected FCFs, PVs, terminal value, and per-share value.
    Returns None if inputs are invalid.
    """
    if discount_rate <= terminal_growth or latest_fcf <= 0 or shares <= 0:
        return None

    rows = []
    fcf = latest_fcf
    pv_sum = 0.0

    for yr in range(1, PROJECTION_YEARS + 1):
        fcf *= (1 + fcf_growth)
        df = (1 + discount_rate) ** yr
        pv = fcf / df
        pv_sum += pv
        rows.append({"year": yr, "fcf": fcf, "df": df, "pv": pv})

    terminal_fcf = fcf * (1 + terminal_growth)
    terminal_value = terminal_fcf / (discount_rate - terminal_growth)
    pv_terminal = terminal_value / (1 + discount_rate) ** PROJECTION_YEARS

    enterprise_value = pv_sum + pv_terminal
    equity_value = enterprise_value - net_debt
    intrinsic_per_share = equity_value / shares

    return {
        "rows": rows,
        "pv_fcf_sum": pv_sum,
        "terminal_value": terminal_value,
        "pv_terminal": pv_terminal,
        "enterprise_value": enterprise_value,
        "net_debt": net_debt,
        "equity_value": equity_value,
        "shares": shares,
        "intrinsic_per_share": intrinsic_per_share,
    }


# ─── Full pipeline ─────────────────────────────────────────────────────────────

def screen_ticker(ticker: str, terminal_growth: float = TERMINAL_GROWTH_DEFAULT) -> Optional[dict]:
    ticker = ticker.upper()
    console.print(f"\n  [dim]Fetching SEC EDGAR data...[/dim]")

    cik = get_cik(ticker)
    if not cik:
        console.print(f"  [red]✗ CIK not found for {ticker}[/red]")
        return None

    time.sleep(0.15)
    facts = get_company_facts(cik)
    if not facts:
        console.print(f"  [red]✗ Could not retrieve EDGAR company facts[/red]")
        return None

    console.print(f"  [dim]Computing FCF series...[/dim]")
    fcf_series = compute_fcf_series(facts)
    if fcf_series.empty or (fcf_series > 0).sum() < 2:
        console.print(f"  [red]✗ Insufficient positive FCF history in EDGAR filings[/red]")
        return None

    fcf_growth = five_year_fcf_growth(fcf_series)
    if fcf_growth is None:
        console.print(f"  [yellow]⚠ FCF growth undetermined — defaulting to 5%[/yellow]")
        fcf_growth = 0.05

    latest_fcf = float(fcf_series.dropna().iloc[-1])

    # Fetch market data first — need sector/industry for industry beta lookup
    console.print(f"  [dim]Fetching market data...[/dim]")
    yinfo = get_yahoo_info(ticker)
    mkt_cap = yinfo["market_cap"]
    shares = yinfo["shares"]
    cash = yinfo["cash"]

    total_debt, interest_exp = get_debt_interest(facts)
    net_debt = total_debt - cash
    if shares == 0:
        s = extract_annual_values(facts, "CommonStockSharesOutstanding", unit="shares")
        shares = float(s.iloc[-1]) if not s.empty else 1.0

    de_ratio = total_debt / mkt_cap if mkt_cap > 0 else 0.0

    console.print(f"  [dim]Calculating blended beta & cost of capital...[/dim]")
    beta_info = get_blended_beta(ticker, yinfo["sector"], yinfo["industry"], de_ratio)
    blended_beta = beta_info["blended"]
    ke = cost_of_equity(blended_beta)

    kd_pretax = (interest_exp / total_debt) if total_debt > 1e6 else 0.05
    kd_pretax = float(np.clip(kd_pretax, 0.02, 0.15))

    w = calc_wacc(ke, kd_pretax, mkt_cap, total_debt)
    console.print(f"  [dim]Running DCF...[/dim]")

    dcf = run_dcf(latest_fcf, fcf_growth, terminal_growth, w, shares, net_debt)
    if dcf is None:
        console.print(f"  [red]✗ DCF failed — WACC ({w*100:.1f}%) must exceed terminal growth ({terminal_growth*100:.1f}%)[/red]")
        return None

    price = get_current_price(ticker)
    if price is None:
        console.print(f"  [red]✗ Could not fetch current price[/red]")
        return None

    intrinsic = dcf["intrinsic_per_share"]
    upside = (intrinsic - price) / price * 100
    yahoo_target = yinfo["target"]
    yahoo_upside = (yahoo_target - price) / price * 100 if yahoo_target else None

    return {
        # identifiers
        "ticker": ticker,
        "name": yinfo["name"],
        "sector": yinfo["sector"],
        "industry": yinfo["industry"],
        # market
        "price": price,
        "market_cap": mkt_cap,
        "shares": shares,
        "total_debt": total_debt,
        "cash": cash,
        "net_debt": net_debt,
        # FCF
        "fcf_series": fcf_series.tail(7),
        "fcf_growth_5yr": fcf_growth,
        "latest_fcf": latest_fcf,
        # cost of capital
        "beta": blended_beta,                             # blended beta used in WACC
        "raw_beta": beta_info["raw"],
        "industry_levered_beta": beta_info["industry_levered"],
        "unlevered_beta": beta_info["unlevered"],
        "beta_source": beta_info["source"],
        "de_ratio": de_ratio,
        "ke": ke,
        "kd_pretax": kd_pretax,
        "kd_aftertax": kd_pretax * (1 - TAX_RATE),
        "wacc": w,
        # DCF detail
        "dcf": dcf,
        "terminal_growth": terminal_growth,
        # valuation
        "intrinsic_value": intrinsic,
        "upside_pct": upside,
        # cross-validation
        "yahoo_target": yahoo_target,
        "yahoo_upside_pct": yahoo_upside,
    }


# ─── Display helpers ──────────────────────────────────────────────────────────

def _fmt_b(val: float) -> str:
    """Format a dollar value in billions."""
    if abs(val) >= 1e12:
        return f"${val/1e12:.2f}T"
    if abs(val) >= 1e9:
        return f"${val/1e9:.2f}B"
    if abs(val) >= 1e6:
        return f"${val/1e6:.0f}M"
    return f"${val:,.0f}"


def _upside_style(pct: float) -> str:
    if pct >= 20:
        return "bold green"
    if pct >= 0:
        return "green"
    if pct >= -15:
        return "yellow"
    return "red"


def print_ticker_detail(r: dict):
    """Print a full multi-section breakdown table for one ticker."""
    ticker = r["ticker"]
    console.print()
    console.print(Rule(f"[bold magenta] {ticker} — {r['name']} [/bold magenta]", style="magenta"))

    # ── Section 1: Company & Market Data ──
    t1 = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0, 2))
    t1.add_column("Field", style="bold dim", width=28)
    t1.add_column("Value", justify="right")

    t1.add_row("Sector", r["sector"])
    t1.add_row("Industry", r["industry"])
    t1.add_row("Current Price", f"[bold]${r['price']:.2f}[/bold]")
    t1.add_row("Market Cap", _fmt_b(r["market_cap"]))
    t1.add_row("Shares Outstanding", f"{r['shares']/1e9:.2f}B")
    t1.add_row("Total Debt", _fmt_b(r["total_debt"]))
    t1.add_row("Cash & Equivalents", _fmt_b(r["cash"]))
    t1.add_row("Net Debt (Debt − Cash)", _fmt_b(r["net_debt"]))

    console.print(Panel(t1, title="[bold]Market & Balance Sheet[/bold]", border_style="blue", padding=(0, 1)))

    # ── Section 2: FCF History ──
    t2 = Table(box=box.SIMPLE_HEAVY, show_header=True, padding=(0, 2),
               header_style="bold cyan")
    t2.add_column("Fiscal Year", justify="center", width=12)
    t2.add_column("Operating CF", justify="right")
    t2.add_column("FCF", justify="right")
    t2.add_column("YoY Δ", justify="right")

    fcf_s = r["fcf_series"].sort_index()
    prev = None
    for yr, val in fcf_s.items():
        yoy = f"{(val/prev - 1)*100:+.1f}%" if prev is not None and prev != 0 else "—"
        yoy_style = "green" if (prev and val > prev) else ("red" if (prev and val < prev) else "white")
        t2.add_row(
            str(yr),
            "—",
            _fmt_b(val),
            f"[{yoy_style}]{yoy}[/{yoy_style}]",
        )
        prev = val

    cagr = r["fcf_growth_5yr"]
    cagr_color = "green" if cagr > 0 else "red"
    t2.add_row(
        "[bold]5yr CAGR[/bold]", "",
        f"[{cagr_color}][bold]{cagr*100:+.1f}%[/bold][/{cagr_color}]", "",
    )

    console.print(Panel(t2, title="[bold]FCFF History (SEC EDGAR)[/bold]", border_style="cyan", padding=(0, 1)))

    # ── Section 3: Cost of Capital ──
    t3 = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0, 2))
    t3.add_column("Component", style="bold dim", width=38)
    t3.add_column("Value", justify="right")

    # Beta breakdown
    if r["beta_source"] == "blended":
        t3.add_row("[dim]── Beta Calculation ──[/dim]", "")
        t3.add_row("Raw Historical β  (3yr weekly vs SPY)", f"{r['raw_beta']:.3f}")
        t3.add_row(
            f"Damodaran Unlevered β  (industry avg)",
            f"{r['unlevered_beta']:.3f}",
        )
        t3.add_row(
            f"Re-levered Industry β  (D/E={r['de_ratio']:.3f})",
            f"{r['industry_levered_beta']:.3f}",
        )
        t3.add_row(
            "[bold]Blended β  (⅔ industry + ⅓ historical)[/bold]",
            f"[bold]{r['beta']:.3f}[/bold]",
        )
    else:
        t3.add_row("[dim]── Beta (Damodaran lookup failed — raw only) ──[/dim]", "")
        t3.add_row("Raw Historical β  (3yr weekly vs SPY)", f"{r['raw_beta']:.3f}")
        t3.add_row("[bold]Beta used[/bold]", f"[bold]{r['beta']:.3f}[/bold]")

    t3.add_row("─" * 38, "─" * 10)
    t3.add_row("[dim]── CAPM ──[/dim]", "")
    t3.add_row("Risk-Free Rate (Rf)", f"{RISK_FREE_RATE*100:.2f}%")
    t3.add_row("Equity Risk Premium (ERP)", f"{MARKET_PREMIUM*100:.2f}%")
    t3.add_row("Cost of Equity (Ke = Rf + β×ERP)", f"[bold]{r['ke']*100:.2f}%[/bold]")
    t3.add_row("─" * 38, "─" * 10)
    t3.add_row("[dim]── Debt ──[/dim]", "")
    t3.add_row("Pre-Tax Cost of Debt (Kd)", f"{r['kd_pretax']*100:.2f}%")
    t3.add_row("Tax Rate", f"{TAX_RATE*100:.0f}%")
    t3.add_row("After-Tax Cost of Debt", f"{r['kd_aftertax']*100:.2f}%")
    t3.add_row("─" * 38, "─" * 10)
    t3.add_row("[bold]WACC[/bold]", f"[bold yellow]{r['wacc']*100:.2f}%[/bold yellow]")

    console.print(Panel(t3, title="[bold]Cost of Capital (Blended β → CAPM → WACC)[/bold]", border_style="yellow", padding=(0, 1)))

    # ── Section 4: DCF Projection Table ──
    dcf = r["dcf"]
    t4 = Table(box=box.SIMPLE_HEAVY, show_header=True, padding=(0, 2),
               header_style="bold green")
    t4.add_column("Year", justify="center", width=6)
    t4.add_column("Projected FCF", justify="right")
    t4.add_column("FCF Growth", justify="right")
    t4.add_column("Discount Factor", justify="right")
    t4.add_column("Present Value", justify="right")

    for row in dcf["rows"]:
        t4.add_row(
            f"Y+{row['year']}",
            _fmt_b(row["fcf"]),
            f"{r['fcf_growth_5yr']*100:.1f}%",
            f"{1/row['df']:.4f}",
            _fmt_b(row["pv"]),
        )

    console.print(Panel(t4, title="[bold]DCF Projection  (5-Year + Gordon Growth Terminal)[/bold]", border_style="green", padding=(0, 1)))

    # DCF bridge: separate simple 2-col table so labels never truncate
    tg = r["terminal_growth"]
    t4b = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0, 2))
    t4b.add_column("Item", style="bold dim", width=36)
    t4b.add_column("Amount", justify="right", min_width=14)

    t4b.add_row("PV of Projected FCFs (Y1–Y5)", _fmt_b(dcf["pv_fcf_sum"]))
    t4b.add_row(f"+ PV of Terminal Value  (g = {tg*100:.1f}%)", _fmt_b(dcf["pv_terminal"]))
    t4b.add_row("[bold]= Enterprise Value[/bold]", f"[bold]{_fmt_b(dcf['enterprise_value'])}[/bold]")
    t4b.add_row("─" * 36, "─" * 14)
    nd_label = "(−) Net Debt" if dcf["net_debt"] > 0 else "(+) Net Cash"
    nd_val = f"({_fmt_b(dcf['net_debt'])})" if dcf["net_debt"] > 0 else _fmt_b(-dcf["net_debt"])
    t4b.add_row(nd_label, nd_val)
    t4b.add_row("[bold]= Equity Value[/bold]", f"[bold]{_fmt_b(dcf['equity_value'])}[/bold]")
    t4b.add_row(f"÷ Shares Outstanding ({dcf['shares']/1e9:.2f}B)", "")
    t4b.add_row("[bold]= Intrinsic Value per Share[/bold]",
                f"[bold green]${dcf['intrinsic_per_share']:.2f}[/bold green]")

    console.print(Panel(t4b, title="[bold]DCF Bridge to Equity Value[/bold]", border_style="green", padding=(0, 1)))

    # ── Section 5: Verdict ──
    upside = r["upside_pct"]
    upside_style = _upside_style(upside)
    verdict = (
        "STRONGLY UNDERVALUED" if upside >= 30 else
        "UNDERVALUED" if upside >= 15 else
        "FAIRLY VALUED" if upside >= -10 else
        "OVERVALUED"
    )

    t5 = Table(box=box.SIMPLE_HEAVY, show_header=False, padding=(0, 2))
    t5.add_column("Metric", style="bold dim", width=32)
    t5.add_column("Value", justify="right")

    t5.add_row("Current Market Price", f"[bold]${r['price']:.2f}[/bold]")
    t5.add_row("DCF Intrinsic Value", f"[bold]${r['intrinsic_value']:.2f}[/bold]")
    t5.add_row(
        "DCF Upside / (Downside)",
        f"[{upside_style}][bold]{upside:+.1f}%  →  {verdict}[/bold][/{upside_style}]",
    )
    t5.add_row("─" * 32, "─" * 20)
    if r["yahoo_target"]:
        yu = r["yahoo_upside_pct"] or 0
        yu_style = _upside_style(yu)
        t5.add_row("Yahoo Finance Analyst Target", f"${r['yahoo_target']:.2f}")
        t5.add_row("Yahoo Upside", f"[{yu_style}]{yu:+.1f}%[/{yu_style}]")
    else:
        t5.add_row("Yahoo Finance Analyst Target", "[dim]N/A[/dim]")

    verdict_border = "green" if upside >= 15 else ("yellow" if upside >= 0 else "red")
    console.print(Panel(t5, title="[bold]Valuation Verdict[/bold]", border_style=verdict_border, padding=(0, 1)))


def print_comparison_table(results: list[dict]):
    """Render a compact side-by-side comparison for all analyzed tickers."""
    if not results:
        return

    results_sorted = sorted(results, key=lambda r: r["upside_pct"], reverse=True)

    console.print()
    console.print(Rule("[bold magenta] Comparison Table [/bold magenta]", style="magenta"))

    t = Table(
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold white on dark_blue",
        title="DCF Undervalued Stock Screen — All Results",
        title_style="bold magenta",
    )

    t.add_column("Ticker", style="bold", justify="center", min_width=7)
    t.add_column("Price", justify="right", min_width=8)
    t.add_column("Intrinsic Value", justify="right", min_width=14)
    t.add_column("DCF Upside", justify="right", min_width=10)
    t.add_column("Verdict", justify="center", min_width=16)
    t.add_column("FCF CAGR\n(5yr)", justify="right", min_width=10)
    t.add_column("Beta", justify="right", min_width=6)
    t.add_column("WACC", justify="right", min_width=7)
    t.add_column("Yahoo\nTarget", justify="right", min_width=9)
    t.add_column("YF\nUpside", justify="right", min_width=8)

    for r in results_sorted:
        upside = r["upside_pct"]
        us = _upside_style(upside)
        verdict = (
            "UNDERVALUED" if upside >= 15 else
            "FAIR" if upside >= -10 else
            "OVERVALUED"
        )

        yu = r["yahoo_upside_pct"]
        yu_str = f"{yu:+.1f}%" if yu is not None else "N/A"
        yu_style = _upside_style(yu) if yu is not None else "dim"

        t.add_row(
            r["ticker"],
            f"${r['price']:.2f}",
            f"${r['intrinsic_value']:.2f}",
            f"[{us}]{upside:+.1f}%[/{us}]",
            f"[{us}]{verdict}[/{us}]",
            f"{r['fcf_growth_5yr']*100:.1f}%",
            f"{r['beta']:.2f}",
            f"{r['wacc']*100:.2f}%",
            f"${r['yahoo_target']:.2f}" if r["yahoo_target"] else "N/A",
            f"[{yu_style}]{yu_str}[/{yu_style}]",
        )

    console.print(t)

    undervalued = [r for r in results if r["upside_pct"] >= 15]
    console.print(Panel(
        f"[bold]Total screened:[/bold] {len(results)}  |  "
        f"[bold green]Undervalued (≥15% upside):[/bold green] {len(undervalued)}  |  "
        f"[bold red]Overvalued (<0%):[/bold red] {sum(1 for r in results if r['upside_pct'] < 0)}\n"
        f"[dim]Assumptions — Rf: {RISK_FREE_RATE*100:.1f}%  ERP: {MARKET_PREMIUM*100:.1f}%  "
        f"Tax: {TAX_RATE*100:.0f}%  Terminal g: {results[0]['terminal_growth']*100:.1f}%  "
        f"Beta benchmark: {MARKET_TICKER}[/dim]",
        title="Summary",
        border_style="blue",
    ))


def export_csv(results: list[dict], path: str):
    rows = []
    for r in results:
        rows.append({
            "ticker": r["ticker"],
            "name": r["name"],
            "sector": r["sector"],
            "price": r["price"],
            "intrinsic_value": r["intrinsic_value"],
            "upside_pct": round(r["upside_pct"], 2),
            "verdict": ("UNDERVALUED" if r["upside_pct"] >= 15 else
                        "FAIR" if r["upside_pct"] >= -10 else "OVERVALUED"),
            "fcf_cagr_5yr": round(r["fcf_growth_5yr"] * 100, 2),
            "beta": round(r["beta"], 3),
            "ke_pct": round(r["ke"] * 100, 2),
            "kd_pretax_pct": round(r["kd_pretax"] * 100, 2),
            "wacc_pct": round(r["wacc"] * 100, 2),
            "terminal_growth_pct": round(r["terminal_growth"] * 100, 2),
            "latest_fcf_B": round(r["latest_fcf"] / 1e9, 2),
            "net_debt_B": round(r["net_debt"] / 1e9, 2),
            "yahoo_target": r["yahoo_target"],
            "yahoo_upside_pct": round(r["yahoo_upside_pct"], 2) if r["yahoo_upside_pct"] is not None else None,
        })
    pd.DataFrame(rows).to_csv(path, index=False)
    console.print(f"[green]Exported {len(rows)} result(s) to {path}[/green]")


# ─── Interactive mode ─────────────────────────────────────────────────────────

BANNER = """
  ██████   ██████ ███████      ███████  ██████ ██████  ███████ ███████ ███    ██ ███████ ██████
  ██   ██ ██      ██           ██      ██      ██   ██ ██      ██      ████   ██ ██      ██   ██
  ██   ██ ██      █████        ███████ ██      ██████  █████   █████   ██ ██  ██ █████   ██████
  ██   ██ ██      ██                ██ ██      ██   ██ ██      ██      ██  ██ ██ ██      ██   ██
  ██████   ██████ ██           ███████  ██████ ██   ██ ███████ ███████ ██   ████ ███████ ██   ██
"""


def interactive_mode(terminal_growth: float = TERMINAL_GROWTH_DEFAULT):
    console.print(f"[bold cyan]{BANNER}[/bold cyan]")
    console.print(Panel(
        "[bold]DCF Undervalued Stock Screener[/bold]\n"
        "Powered by SEC EDGAR · Yahoo Finance · CAPM / WACC · Gordon Growth Model\n\n"
        "[dim]Type a ticker symbol to analyze it. "
        "Type [bold]compare[/bold] to see a side-by-side table of all results so far.\n"
        "Type [bold]export[/bold] to save results to CSV. "
        "Type [bold]clear[/bold] to reset the session. "
        "Type [bold]quit[/bold] to exit.[/dim]",
        border_style="magenta",
        padding=(1, 4),
    ))

    session_results: list[dict] = []

    while True:
        try:
            raw = console.input("\n[bold white]Enter ticker symbol:[/bold white] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Exiting.[/dim]")
            break

        if not raw:
            continue

        cmd = raw.lower()

        if cmd in ("quit", "exit", "q"):
            if session_results:
                console.print("\n[dim]Showing final comparison before exit...[/dim]")
                print_comparison_table(session_results)
            console.print("[bold]Goodbye.[/bold]")
            break

        if cmd in ("compare", "c"):
            if not session_results:
                console.print("[yellow]No results yet. Analyze at least one ticker first.[/yellow]")
            else:
                print_comparison_table(session_results)
            continue

        if cmd in ("clear", "reset"):
            session_results.clear()
            console.print("[green]Session cleared.[/green]")
            continue

        if cmd == "export":
            if not session_results:
                console.print("[yellow]No results to export yet.[/yellow]")
            else:
                path = f"dcf_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                export_csv(session_results, path)
            continue

        if cmd == "help":
            console.print(
                "[dim]Commands: [bold]<TICKER>[/bold] · [bold]compare[/bold] · "
                "[bold]export[/bold] · [bold]clear[/bold] · [bold]quit[/bold][/dim]"
            )
            continue

        # treat input as a ticker
        ticker = raw.upper()
        already = next((r for r in session_results if r["ticker"] == ticker), None)
        if already:
            console.print(f"[yellow]{ticker} already analyzed this session. Showing cached result.[/yellow]")
            print_ticker_detail(already)
            continue

        console.print(f"\n[bold cyan]Analyzing {ticker}...[/bold cyan]")
        result = screen_ticker(ticker, terminal_growth=terminal_growth)

        if result:
            print_ticker_detail(result)
            session_results.append(result)
            if len(session_results) > 1:
                console.print(
                    f"\n[dim]Tip: type [bold]compare[/bold] to see all {len(session_results)} "
                    f"tickers side by side.[/dim]"
                )
        else:
            console.print(f"[red]Could not complete analysis for {ticker}.[/red]")

        time.sleep(0.3)


# ─── Batch mode ───────────────────────────────────────────────────────────────

def batch_mode(tickers: list[str], terminal_growth: float, min_upside: Optional[float], export_path: Optional[str]):
    console.print(Panel(
        f"[bold]DCF Screener — Batch Mode[/bold]\n"
        f"Tickers: {', '.join(tickers)}\n"
        f"Terminal growth: {terminal_growth*100:.1f}%  ·  "
        f"Rf: {RISK_FREE_RATE*100:.1f}%  ·  ERP: {MARKET_PREMIUM*100:.1f}%",
        border_style="magenta",
    ))

    results = []
    for ticker in tickers:
        console.print(f"\n[bold cyan]Analyzing {ticker}...[/bold cyan]")
        r = screen_ticker(ticker, terminal_growth=terminal_growth)
        if r:
            print_ticker_detail(r)
            results.append(r)
        time.sleep(0.5)

    if min_upside is not None:
        results = [r for r in results if r["upside_pct"] >= min_upside]

    if results:
        print_comparison_table(results)
        if export_path:
            export_csv(results, export_path)


# ─── Entry point ──────────────────────────────────────────────────────────────

DEFAULT_WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "META", "AMZN",
    "NVDA", "TSLA", "JPM", "BRK-B", "JNJ",
]


def main():
    parser = argparse.ArgumentParser(
        description="DCF Undervalued Stock Screener — SEC EDGAR + CAPM/WACC + Yahoo Finance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python dcf_screener.py                     # interactive prompt\n"
            "  python dcf_screener.py AAPL NVDA TSM       # batch screen\n"
            "  python dcf_screener.py --watchlist         # screen default watchlist\n"
            "  python dcf_screener.py AAPL --export out.csv\n"
        ),
    )
    parser.add_argument("tickers", nargs="*", help="Ticker symbols to analyze in batch mode")
    parser.add_argument("--watchlist", action="store_true", help="Use the built-in default watchlist")
    parser.add_argument("--terminal-growth", type=float, default=TERMINAL_GROWTH_DEFAULT,
                        metavar="RATE", help="Terminal growth rate (default: 0.025)")
    parser.add_argument("--min-upside", type=float, default=None, metavar="PCT",
                        help="Filter: only show tickers above this upside %% in comparison table")
    parser.add_argument("--export", type=str, default=None, metavar="FILE",
                        help="Export results to CSV")
    args = parser.parse_args()

    tg = args.terminal_growth

    if args.tickers or args.watchlist:
        tickers = [t.upper() for t in args.tickers] if args.tickers else DEFAULT_WATCHLIST
        batch_mode(tickers, tg, args.min_upside, args.export)
    else:
        interactive_mode(terminal_growth=tg)


if __name__ == "__main__":
    main()
