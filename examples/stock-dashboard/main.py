from datetime import datetime

import yfinance as yf
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Stock Dashboard")

templates = Jinja2Templates(directory="templates")

# Cache for stock data
cache: dict = {}
CACHE_TTL = 300  # 5 minutes


def get_stock_data(ticker: str, period: str = "1mo"):
    cache_key = f"{ticker}:{period}"
    now = datetime.now().timestamp()

    if cache_key in cache:
        data, timestamp = cache[cache_key]
        if now - timestamp < CACHE_TTL:
            return data

    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=period)
        info = stock.info

        if hist.empty:
            return None

        data = {
            "ticker": ticker.upper(),
            "name": info.get("shortName", ticker.upper()),
            "price": info.get("currentPrice") or info.get("regularMarketPrice", 0),
            "change": info.get("regularMarketChangePercent", 0),
            "volume": info.get("volume", 0),
            "market_cap": info.get("marketCap", 0),
            "pe_ratio": info.get("trailingPE"),
            "week_52_high": info.get("fiftyTwoWeekHigh"),
            "week_52_low": info.get("fiftyTwoWeekLow"),
            "history": [
                {"date": d.strftime("%Y-%m-%d"), "close": round(c, 2)}
                for d, c in zip(hist.index, hist["Close"])
            ],
        }

        cache[cache_key] = (data, now)
        return data
    except Exception:
        return None


def format_number(num):
    if num is None:
        return "N/A"
    if num >= 1_000_000_000_000:
        return f"${num / 1_000_000_000_000:.2f}T"
    if num >= 1_000_000_000:
        return f"${num / 1_000_000_000:.2f}B"
    if num >= 1_000_000:
        return f"${num / 1_000_000:.2f}M"
    return f"${num:,.0f}"


templates.env.filters["format_number"] = format_number


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    # Default tickers to show
    default_tickers = ["AAPL", "GOOGL", "MSFT", "AMZN"]
    stocks = []

    for ticker in default_tickers:
        data = get_stock_data(ticker, "5d")
        if data:
            stocks.append(data)

    return templates.TemplateResponse(
        "index.html", {"request": request, "stocks": stocks}
    )


@app.post("/search", response_class=HTMLResponse)
async def search(request: Request, ticker: str = Form(...)):
    period = "1mo"  # Default period for search
    data = get_stock_data(ticker.strip().upper(), period)

    if not data:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": f"Could not find data for '{ticker}'"},
        )

    return templates.TemplateResponse(
        "partials/stock_detail.html",
        {"request": request, "stock": data, "current_period": period},
    )


@app.get("/stock/{ticker}", response_class=HTMLResponse)
async def stock_detail(request: Request, ticker: str, period: str = "1mo"):
    data = get_stock_data(ticker.upper(), period)

    if not data:
        return templates.TemplateResponse(
            "partials/error.html",
            {"request": request, "message": f"Could not find data for '{ticker}'"},
        )

    return templates.TemplateResponse(
        "partials/stock_detail.html",
        {"request": request, "stock": data, "current_period": period},
    )


@app.get("/health")
async def health():
    return {"status": "healthy"}
