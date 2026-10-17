
import os
import json
import time
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

if not TOKEN:
    raise RuntimeError(
        "Set TELEGRAM_BOT_TOKEN in your hosting environment."
    )

BASE_URL = f"https://api.telegram.org/bot{TOKEN}"

# SEC asks automated users to identify their application.
# Set this environment variable to an accurate contact email.
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT",
    "BenjiEducationalBot contact admin@example.com"
)

SEC_TICKERS_URL = (
    "https://www.sec.gov/files/company_tickers.json"
)

SEC_FACTS_URL = (
    "https://data.sec.gov/api/xbrl/companyfacts/CIK{}.json"
)

# Indicative quote endpoint. This is not an official SEC feed.
YAHOO_CHART_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{}"
)

CALENDAR_URL = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
)

NEWS_URL = (
    "https://api.rss2json.com/v1/api.json"
    "?rss_url=https%3A%2F%2Fsearch.yahoo.com%2Frss%2Fheadline"
    "%3Fs%3D%255EGSPC"
)

EASTERN = ZoneInfo("America/New_York")

SUBSCRIBERS_FILE = Path("subscribers.json")
ALERTS_FILE = Path("sent_alerts.json")
CACHE_DIR = Path("benji_cache")

CALENDAR_CHECK_SECONDS = 300
ALERT_HOURS_BEFORE = 24

# Illustrative default DCF assumptions.
DCF_YEARS = 5
DCF_GROWTH = 0.05
DCF_DISCOUNT_RATE = 0.10
DCF_TERMINAL_GROWTH = 0.025

# Scenario assumptions are sensitivities, not forecasts.
DCF_SCENARIOS = [
    ("Conservative", 0.02, 0.12, 0.02),
    ("Base case", 0.05, 0.10, 0.025),
    ("Optimistic", 0.08, 0.09, 0.03),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": SEC_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
})

CACHE_DIR.mkdir(exist_ok=True)


# ============================================================
# JSON STORAGE AND CACHING
# ============================================================

def load_json_file(path, default):
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return default


def save_json_file(path, data):
    temp = path.with_suffix(".tmp")

    with temp.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)

    temp.replace(path)


def cached_get_json(url, cache_name, max_age=3600, headers=None):
    path = CACHE_DIR / cache_name

    if path.exists():
        age = time.time() - path.stat().st_mtime

        if age < max_age:
            try:
                with path.open("r", encoding="utf-8") as file:
                    return json.load(file)
            except (OSError, json.JSONDecodeError):
                pass

    response = SESSION.get(
        url,
        headers=headers,
        timeout=20
    )
    response.raise_for_status()
    data = response.json()

    save_json_file(path, data)
    return data


# ============================================================
# TELEGRAM
# ============================================================

def telegram_request(method, payload=None, timeout=15):
    try:
        response = requests.post(
            f"{BASE_URL}/{method}",
            json=payload or {},
            timeout=timeout
        )
        response.raise_for_status()
        data = response.json()

        if not data.get("ok"):
            logging.error(
                "Telegram API error: %s",
                data.get("description", "Unknown error")
            )

        return data

    except (requests.RequestException, ValueError) as error:
        logging.error("Telegram request failed: %s", error)
        return {"ok": False}


def send_message(chat_id, text):
    # Keep messages under Telegram's normal message-size limit.
    if len(text) > 4000:
        text = text[:3950] + "\n\n[Message shortened]"

    return telegram_request(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
    )


# ============================================================
# SUBSCRIBERS AND ALERT HISTORY
# ============================================================

def load_subscribers():
    data = load_json_file(SUBSCRIBERS_FILE, [])
    return set(str(item) for item in data)


def save_subscribers(subscribers):
    save_json_file(SUBSCRIBERS_FILE, sorted(subscribers))


def load_sent_alerts():
    return load_json_file(ALERTS_FILE, {})


def save_sent_alerts(alerts):
    save_json_file(ALERTS_FILE, alerts)


# ============================================================
# STOCK SYMBOL AND SEC DATA
# ============================================================

def clean_ticker(value):
    ticker = value.strip().upper()

    if not re.fullmatch(r"[A-Z0-9.-]{1,10}", ticker):
        raise ValueError(
            "Enter a valid US stock ticker, such as /value AAPL."
        )

    return ticker


def get_company_tickers():
    return cached_get_json(
        SEC_TICKERS_URL,
        "sec_tickers.json",
        max_age=86400,
    )


def find_company(ticker):
    directory = get_company_tickers()

    for item in directory.values():
        if item.get("ticker", "").upper() == ticker:
            return {
                "ticker": ticker,
                "name": item.get("title", ticker),
                "cik": str(item["cik_str"]).zfill(10),
            }

    raise ValueError(
        f"{ticker} was not found in the SEC company ticker list. "
        "Check the symbol or try another US-listed company."
    )


def get_company_facts(company):
    cache_name = f"facts_{company['cik']}.json"

    return cached_get_json(
        SEC_FACTS_URL.format(company["cik"]),
        cache_name,
        max_age=21600,
    )


# ============================================================
# FINANCIAL FACT HELPERS
# ============================================================

def get_fact_entries(facts, tags, unit="USD"):
    """
    Return entries for the first available standard US-GAAP tag.
    Values remain in the units reported by the SEC.
    """

    gaap = facts.get("facts", {}).get("us-gaap", {})

    for tag in tags:
        concept = gaap.get(tag)

        if not concept:
            continue

        entries = concept.get("units", {}).get(unit, [])

        if entries:
            return entries

    return []


def latest_instant_value(facts, tags, unit="USD"):
    entries = get_fact_entries(facts, tags, unit)

    instants = [
        entry for entry in entries
        if entry.get("end") and not entry.get("start")
    ]

    if not instants:
        return None

    # Prefer the most recently filed reported instant.
    instants.sort(
        key=lambda item: (
            item.get("end", ""),
            item.get("filed", "")
        ),
        reverse=True
    )

    return instants[0].get("val")


def latest_annual_value(facts, tags, unit="USD"):
    entries = get_fact_entries(facts, tags, unit)

    annual = [
        entry for entry in entries
        if entry.get("form") == "10-K"
        and entry.get("fp") == "FY"
        and entry.get("start")
        and entry.get("end")
    ]

    # Remove duplicate comparative facts from the same period.
    by_period = {}

    for entry in annual:
        key = (entry["start"], entry["end"])
        existing = by_period.get(key)

        if (
            existing is None
            or entry.get("filed", "") > existing.get("filed", "")
        ):
            by_period[key] = entry

    annual = list(by_period.values())

    # Prefer full fiscal years, not short transition periods.
    annual = [
        entry for entry in annual
        if 330 <= (
            datetime.strptime(entry["end"], "%Y-%m-%d")
            - datetime.strptime(entry["start"], "%Y-%m-%d")
        ).days <= 380
    ]

    if not annual:
        return None

    annual.sort(key=lambda item: item["end"], reverse=True)
    return annual[0].get("val")


def latest_annual_eps(facts):
    # Diluted EPS is preferable for the earnings cross-check.
    return latest_annual_value(
        facts,
        ["EarningsPerShareDiluted"],
        "USD/shares"
    )


def latest_annual_shares(facts):
    return latest_annual_value(
        facts,
        [
            "WeightedAverageNumberOfDilutedSharesOutstanding",
            "WeightedAverageNumberOfSharesOutstandingBasic",
        ],
        "shares"
    )


def get_financial_metrics(facts):
    operating_cash_flow = latest_annual_value(
        facts,
        [
            "NetCashProvidedByUsedInOperatingActivities",
            "CashFlowsFromUsedInOperatingActivities",
        ]
    )

    capital_expenditure = latest_annual_value(
        facts,
        [
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
        ]
    )

    # A company must have both reported cash flow and capex.
    free_cash_flow = None

    if (
        operating_cash_flow is not None
        and capital_expenditure is not None
    ):
        free_cash_flow = (
            operating_cash_flow - abs(capital_expenditure)
        )

    cash = latest_instant_value(
        facts,
        [
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        ]
    )

    current_debt = latest_instant_value(
        facts,
        [
            "LongTermDebtCurrent",
            "LongTermDebtAndFinanceLeaseObligationsCurrent",
        ]
    )

    long_term_debt = latest_instant_value(
        facts,
        [
            "LongTermDebtNoncurrent",
            "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
            "LongTermDebt",
        ]
    )

    shares = latest_instant_value(
        facts,
        [
            "EntityCommonStockSharesOutstanding",
        ],
        "shares"
    )

    # Fall back to annual weighted-average diluted shares when
    # the latest instant share count is unavailable.
    if shares is None:
        shares = latest_annual_shares(facts)

    eps = latest_annual_eps(facts)

    return {
        "operating_cash_flow": operating_cash_flow,
        "capex": capital_expenditure,
        "fcf": free_cash_flow,
        "cash": cash,
        "current_debt": current_debt,
        "long_term_debt": long_term_debt,
        "shares": shares,
        "eps": eps,
    }


# ============================================================
# MARKET QUOTE
# ============================================================

def get_market_quote(ticker):
    data = cached_get_json(
        YAHOO_CHART_URL.format(ticker),
        f"quote_{ticker}.json",
        max_age=300,
        headers={"User-Agent": "Mozilla/5.0"},
    )

    chart = data.get("chart", {})
    results = chart.get("result") or []

    if not results:
        message = chart.get("error")
        raise ValueError(
            f"Market quote unavailable for {ticker}: {message}"
        )

    result = results[0]
    meta = result.get("meta", {})

    price = (
        meta.get("regularMarketPrice")
        or meta.get("previousClose")
        or meta.get("chartPreviousClose")
    )

    if price is None:
        raise ValueError(
            f"No usable market price was returned for {ticker}."
        )

    timestamp = meta.get("regularMarketTime")

    quote_time = (
        datetime.fromtimestamp(timestamp, timezone.utc)
        if timestamp
        else None
    )

    return {
        "price": float(price),
        "currency": meta.get("currency", "USD"),
        "exchange": meta.get("exchangeName", "Unknown"),
        "quote_time": quote_time,
    }


# ============================================================
# DISCOUNTED CASH FLOW VALUATION
# ============================================================

def calculate_dcf(
    fcf,
    cash,
    debt,
    shares,
    growth,
    discount_rate,
    terminal_growth,
):
    """
    Enterprise DCF:
    1. Project annual free cash flow for five years.
    2. Discount each year to present value.
    3. Discount a perpetual-growth terminal value.
    4. Add cash, subtract debt, divide by shares.

    This simplified model assumes a constant growth rate.
    """

    if fcf is None or fcf <= 0:
        return None

    if shares is None or shares <= 0:
        return None

    if cash is None:
        cash = 0

    if debt is None:
        debt = 0

    if discount_rate <= terminal_growth:
        return None

    present_value_fcf = 0
    projected_fcf = fcf

    for year in range(1, DCF_YEARS + 1):
        projected_fcf *= (1 + growth)

        present_value_fcf += (
            projected_fcf / ((1 + discount_rate) ** year)
        )

    terminal_value = (
        projected_fcf * (1 + terminal_growth)
        / (discount_rate - terminal_growth)
    )

    present_value_terminal = (
        terminal_value / ((1 + discount_rate) ** DCF_YEARS)
    )

    enterprise_value = present_value_fcf + present_value_terminal

    equity_value = enterprise_value + cash - debt

    return equity_value / shares


# ============================================================
# STOCK VALUATION REPORT
# ============================================================

def money(value):
    if value is None:
        return "Unavailable"

    return f"${value:,.2f}"


def percentage(value):
    if value is None:
        return "Unavailable"

    return f"{value:+.1%}"


def value_stock(ticker_input):
    ticker = clean_ticker(ticker_input)
    company = find_company(ticker)
    facts = get_company_facts(company)
    quote = get_market_quote(ticker)
    metrics = get_financial_metrics(facts)

    price = quote["price"]
    currency = quote["currency"]

    fcf = metrics["fcf"]
    shares = metrics["shares"]
    cash = metrics["cash"] or 0

    debt = (
        (metrics["current_debt"] or 0)
        + (metrics["long_term_debt"] or 0)
    )

    # Base-case DCF.
    base_value = calculate_dcf(
        fcf=fcf,
        cash=cash,
        debt=debt,
        shares=shares,
        growth=DCF_GROWTH,
        discount_rate=DCF_DISCOUNT_RATE,
        terminal_growth=DCF_TERMINAL_GROWTH,
    )

    # Scenario range. This is a sensitivity analysis, not a
    # statistical confidence interval.
    scenarios = []

    for label, growth, discount, terminal in DCF_SCENARIOS:
        value = calculate_dcf(
            fcf=fcf,
            cash=cash,
            debt=debt,
            shares=shares,
            growth=growth,
            discount_rate=discount,
            terminal_growth=terminal,
        )

        scenarios.append((label, value))

    valid_values = [
        value for _, value in scenarios
        if value is not None
    ]

    # Cross-check valuation ratios.
    eps = metrics["eps"]

    pe_ratio = (
        price / eps
        if eps is not None and eps > 0
        else None
    )

    fcf_per_share = (
        fcf / shares
        if fcf is not None and shares and shares > 0
        else None
    )

    pfcf_ratio = (
        price / fcf_per_share
        if fcf_per_share is not None and fcf_per_share > 0
        else None
    )

    earnings_yield = (
        eps / price
        if eps is not None and price > 0
        else None
    )

    # Premium/discount uses the DCF base case.
    upside = (
        base_value / price - 1
        if base_value is not None and price > 0
        else None
    )

    if base_value is None:
        assessment = (
            "DCF estimate unavailable. The company may have "
            "negative free cash flow, missing financial data, "
            "or an unavailable share count."
        )
    elif upside >= 0.20:
        assessment = (
            "The base-case model estimates value at least 20% "
            "above the quoted price. This result is highly "
            "sensitive to the model assumptions."
        )
    elif upside > 0:
        assessment = (
            "The base-case model estimates value above the "
            "quoted price, but the estimated difference is "
            "not a guarantee of future returns."
        )
    elif upside <= -0.20:
        assessment = (
            "The base-case model estimates value at least 20% "
            "below the quoted price."
        )
    else:
        assessment = (
            "The base-case model estimates value near or below "
            "the quoted price."
        )

    lines = [
        f"📊 BENJI STOCK VALUATION: {ticker}",
        "",
        f"Company: {company['name']}",
        f"Exchange: {quote['exchange']}",
        f"Market quote: {money(price)} {currency}",
    ]

    if quote["quote_time"]:
        lines.append(
            "Quote timestamp (UTC): "
            + quote["quote_time"].strftime("%Y-%m-%d %H:%M")
        )

    lines.extend([
        "",
        "💵 INTRINSIC VALUE — DCF",
        f"Base-case estimate: {money(base_value)}",
        f"Difference vs price: {percentage(upside)}",
        "",
        "Scenario sensitivity:",
    ])

    for label, value in scenarios:
        lines.append(f"• {label}: {money(value)}")

    if valid_values:
        lines.extend([
            "",
            "Scenario range: "
            f"{money(min(valid_values))} – "
            f"{money(max(valid_values))}",
            "This is an assumption-based range, not a "
            "statistical confidence interval.",
        ])

    lines.extend([
        "",
        "📐 VALUATION CROSS-CHECKS",
        f"Latest annual diluted EPS: {money(eps)}",
        (
            f"P/E ratio: {pe_ratio:.2f}x"
            if pe_ratio is not None
            else "P/E ratio: unavailable or not meaningful"
        ),
        (
            f"Annual FCF per share: {money(fcf_per_share)}"
            if fcf_per_share is not None
            else "Annual FCF per share: unavailable"
        ),
        (
            f"Price / FCF per share: {pfcf_ratio:.2f}x"
            if pfcf_ratio is not None
            else "Price / FCF: unavailable or not meaningful"
        ),
        (
            f"Earnings yield: {earnings_yield:.2%}"
            if earnings_yield is not None
            else "Earnings yield: unavailable"
        ),
        "",
        "📚 REPORTED FINANCIAL DATA",
        f"Annual operating cash flow: "
        f"{money(metrics['operating_cash_flow'])}",
        f"Annual capital expenditure: "
        f"{money(metrics['capex'])}",
        f"Annual free cash flow: {money(fcf)}",
        f"Cash used in model: {money(cash)}",
        f"Debt used in model: {money(debt)}",
        "",
        "🧠 BENJI'S INTERPRETATION",
        assessment,
        "",
        "⚙️ BASE-CASE ASSUMPTIONS",
        f"FCF growth: {DCF_GROWTH:.1%} annually",
        f"Discount rate: {DCF_DISCOUNT_RATE:.1%}",
        f"Terminal growth: {DCF_TERMINAL_GROWTH:.1%}",
        f"Projection period: {DCF_YEARS} years",
        "",
        "⚠️ LIMITATIONS",
        "• SEC figures are reported financial data, not forecasts.",
        "• The model uses the latest available annual cash-flow "
        "period, which may not represent current conditions.",
        "• Missing cash or debt fields are treated as zero; "
        "check the financial statements before relying on results.",
        "• Some companies report financial concepts differently. "
        "Missing data can make the DCF unavailable or incomplete.",
        "• P/E and P/FCF need industry and growth context; there "
        "is no single correct ratio for every company.",
        "• Quote data may be delayed or unavailable.",
        "• This is educational analysis, not financial advice "
        "or a guarantee of investment returns.",
    ])

    return "\n".join(lines)


# ============================================================
# ECONOMIC CALENDAR
# ============================================================

def fetch_economic_calendar():
    try:
        response = requests.get(
            CALENDAR_URL,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15
        )
        response.raise_for_status()

        events = response.json()

        return events if isinstance(events, list) else []

    except (requests.RequestException, ValueError) as error:
        logging.error("Calendar fetch failed: %s", error)
        return []


def get_event_title(event):
    return event.get("title") or "Economic release"


def get_event_currency(event):
    return event.get("country") or event.get("currency") or "N/A"


def is_high_impact(event):
    impact = str(event.get("impact", "")).lower().strip()

    return impact in {
        "high", "red", "high impact", "high-impact"
    }


def parse_event_datetime(event):
    raw = event.get("datetimeUtc") or event.get("date")

    if not isinstance(raw, str) or not raw.strip():
        return None

    raw = raw.strip()

    try:
        parsed = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=EASTERN)

        return parsed.astimezone(timezone.utc)

    except ValueError:
        pass

    for fmt in (
        "%b %d, %Y %I:%M%p",
        "%b %d, %Y %I:%M %p",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %I:%M%p",
        "%m/%d/%Y %I:%M %p",
    ):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.replace(
                tzinfo=EASTERN
            ).astimezone(timezone.utc)
        except ValueError:
            continue

    return None


def check_red_news_alerts():
    subscribers = load_subscribers()

    if not subscribers:
        return

    events = fetch_economic_calendar()

    if not events:
        return

    now = datetime.now(timezone.utc)
    sent = load_sent_alerts()
    changed = False

    for event in events:
        if not is_high_impact(event):
            continue

        event_time = parse_event_datetime(event)

        if event_time is None:
            continue

        seconds = (event_time - now).total_seconds()

        if not (
            ALERT_HOURS_BEFORE * 3600 - CALENDAR_CHECK_SECONDS
            <= seconds
            <= ALERT_HOURS_BEFORE * 3600
        ):
            continue

        event_id = (
            f"{get_event_currency(event)}|"
            f"{get_event_title(event)}|"
            f"{event_time.isoformat()}"
        )

        if event_id in sent:
            continue

        local_time = event_time.astimezone(EASTERN)

        message = (
            "🔴 BENJI: HIGH-IMPACT NEWS TOMORROW\n\n"
            f"Event: {get_event_title(event)}\n"
            f"Currency/region: {get_event_currency(event)}\n"
            f"Release: {local_time:%A, %B %d at %I:%M %p} Eastern\n"
            f"Forecast: {event.get('forecast') or 'N/A'}\n"
            f"Previous: {event.get('previous') or 'N/A'}\n\n"
            "Economic releases can affect inflation expectations, "
            "interest-rate policy, currencies, and financial markets. "
            "The market reaction depends on actual results relative "
            "to expectations and the broader economic context.\n\n"
            "Educational information only; not a trading signal."
        )

        success = True

        for chat_id in subscribers:
            result = send_message(chat_id, message)

            if not result.get("ok"):
                success = False

        if success:
            sent[event_id] = now.isoformat()
            changed = True

    if changed:
        save_sent_alerts(sent)


def build_calendar_message(events):
    now = datetime.now(timezone.utc)
    upcoming = []

    for event in events:
        event_time = parse_event_datetime(event)

        if event_time and event_time >= now:
            upcoming.append((event_time, event))

    upcoming.sort(key=lambda pair: pair[0])

    if not upcoming:
        return (
            "No upcoming events with readable timestamps were found. "
            "The calendar feed may be unavailable or use an unsupported "
            "date format."
        )

    lines = ["📅 BENJI ECONOMIC CALENDAR\n"]

    for event_time, event in upcoming[:15]:
        impact = "🔴 HIGH" if is_high_impact(event) else "⚪ OTHER"
        local_time = event_time.astimezone(EASTERN)

        lines.append(
            f"{impact}\n"
            f"Event: {get_event_title(event)}\n"
            f"Currency: {get_event_currency(event)}\n"
            f"When: {local_time:%a, %b %d at %I:%M %p} Eastern\n"
            f"Forecast: {event.get('forecast') or 'N/A'}\n"
            f"Previous: {event.get('previous') or 'N/A'}\n"
        )

    return "\n".join(lines)


# ============================================================
# MARKET NEWS
# ============================================================

def fetch_market_news():
    try:
        response = requests.get(NEWS_URL, timeout=15)
        response.raise_for_status()

        items = response.json().get("items", [])

        return [
            item for item in items if item.get("title")
        ][:5]

    except (requests.RequestException, ValueError) as error:
        logging.error("Market news fetch failed: %s", error)
        return []


def build_market_message(items):
    if not items:
        return "Market headlines unavailable. Try /markets later."

    lines = ["📈 MARKET NEWS\n"]

    for item in items:
        lines.append(f"📰 {item.get('title', 'Market update')}")

        link = item.get("link", "")

        if link.startswith(("https://", "http://")):
            lines.append(link)

        lines.append("")

    lines.append(
        "🎓 Treasury prices and yields generally move in opposite "
        "directions. Changes in yields can influence borrowing costs, "
        "equity valuations, and currency markets."
    )

    return "\n".join(lines)


# ============================================================
# EDUCATION
# ============================================================

EDUCATION = {
    "/cpi": (
        "📊 CPI — CONSUMER PRICE INDEX\n\n"
        "CPI measures changes in consumer prices and is a key "
        "inflation indicator. Higher-than-expected inflation may "
        "increase expectations of tighter monetary policy. The "
        "market reaction depends on expectations and context."
    ),
    "/nfp": (
        "👷 NFP — NON-FARM PAYROLLS\n\n"
        "The US monthly employment report tracks payroll employment "
        "outside certain categories, including farm workers. Watch "
        "the unemployment rate, wage growth, and revisions alongside "
        "the headline number."
    ),
    "/fomc": (
        "🏛 FOMC — FEDERAL RESERVE POLICY\n\n"
        "The Federal Open Market Committee sets US monetary policy. "
        "Rate decisions and guidance influence borrowing costs, "
        "bond yields, and market expectations."
    ),
    "/gdp": (
        "📈 GDP — GROSS DOMESTIC PRODUCT\n\n"
        "GDP measures the value of final goods and services produced "
        "within a country. Growth can inform expectations for "
        "corporate earnings, interest rates, and currency demand."
    ),
}


# ============================================================
# COMMAND HANDLER
# ============================================================

def handle_message(message):
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()

    if not chat_id or not text:
        return

    command_parts = text.split()
    command = command_parts[0].lower().split("@")[0]
    subscribers = load_subscribers()

    if command == "/start":
        subscribers.add(str(chat_id))
        save_subscribers(subscribers)

        send_message(
            chat_id,
            "🤖 Welcome to Benji Forex & Macro Education!\n\n"
            "You are subscribed to high-impact news alerts.\n\n"
            "Commands:\n"
            "/calendar - Economic calendar\n"
            "/subscribe - Enable automatic alerts\n"
            "/unsubscribe - Disable automatic alerts\n"
            "/markets - Market headlines\n"
            "/value AAPL - Estimate a stock's intrinsic value\n"
            "/cpi - Inflation education\n"
            "/nfp - Employment education\n"
            "/fomc - Central bank education\n"
            "/gdp - Economic growth education\n"
            "/help - Show commands\n\n"
            "Valuations are estimates, not guarantees."
        )

    elif command == "/help":
        send_message(
            chat_id,
            "🤖 BENJI COMMANDS\n\n"
            "/calendar - Economic calendar\n"
            "/subscribe - Enable red-news alerts\n"
            "/unsubscribe - Disable red-news alerts\n"
            "/markets - Market headlines\n"
            "/value AAPL - Stock valuation\n"
            "/cpi - Inflation\n"
            "/nfp - Employment\n"
            "/fomc - Federal Reserve policy\n"
            "/gdp - Economic growth"
        )

    elif command == "/subscribe":
        subscribers.add(str(chat_id))
        save_subscribers(subscribers)
        send_message(chat_id, "🔔 Automatic news alerts enabled.")

    elif command == "/unsubscribe":
        subscribers.discard(str(chat_id))
        save_subscribers(subscribers)
        send_message(chat_id, "🔕 Automatic news alerts disabled.")

    elif command in {"/calendar", "/calender"}:
        send_message(chat_id, "⏳ Fetching economic calendar...")
        send_message(
            chat_id,
            build_calendar_message(fetch_economic_calendar())
        )

    elif command in {"/markets", "/stocks", "/bonds"}:
        send_message(chat_id, "⏳ Fetching market headlines...")
        send_message(
            chat_id,
            build_market_message(fetch_market_news())
        )

    elif command == "/value":
        if len(command_parts) != 2:
            send_message(
                chat_id,
                "Usage: /value AAPL\n\n"
                "Enter one US-listed stock ticker, such as AAPL, "
                "MSFT, or NVDA."
            )
            return

        ticker = command_parts[1]

        send_message(
            chat_id,
            f"⏳ Building the valuation for {ticker.upper()}..."
        )

        try:
            report = value_stock(ticker)
            send_message(chat_id, report)

        except ValueError as error:
            send_message(chat_id, f"⚠️ {error}")

        except (
            requests.RequestException,
            KeyError,
            TypeError,
            ValueError,
            OSError,
        ) as error:
            logging.exception("Stock valuation failed")

            send_message(
                chat_id,
                "⚠️ Benji could not complete this valuation. "
                "The data source may be unavailable or the company "
                "may not have the required financial fields. "
                "Please try again later."
            )

    elif command in EDUCATION:
        send_message(chat_id, EDUCATION[command])

    else:
        send_message(
            chat_id,
            "I don't recognize that command. Type /help."
        )


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    logging.info("Starting Benji...")

    result = telegram_request("getMe")

    if not result.get("ok"):
        raise RuntimeError(
            "Telegram connection failed. Check your bot token."
        )

    logging.info(
        "Connected as @%s",
        result["result"].get("username", "unknown")
    )

    offset = None
    last_calendar_check = 0

    while True:
        try:
            if (
                time.monotonic() - last_calendar_check
                >= CALENDAR_CHECK_SECONDS
            ):
                check_red_news_alerts()
                last_calendar_check = time.monotonic()

            updates = telegram_request(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 10,
                    "allowed_updates": ["message"],
                },
                timeout=15,
            )

            if updates.get("ok"):
                for update in updates.get("result", []):
                    offset = update["update_id"] + 1

                    if "message" in update:
                        handle_message(update["message"])

            time.sleep(1)

        except KeyboardInterrupt:
            logging.info("Benji stopped.")
            break

        except Exception:
            logging.exception("Unexpected error in Benji.")
            time.sleep(5)


if __name__ == "__main__":
    main()
