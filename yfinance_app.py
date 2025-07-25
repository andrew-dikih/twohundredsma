import pandas as pd
import asyncio
import time
from datetime import datetime, timedelta
import yfinance as yf
import numpy as np
import random
import os
import json
from ast import literal_eval
import requests

from requests import Session
from requests_cache import CacheMixin, SQLiteCache
from requests_ratelimiter import LimiterMixin, MemoryQueueBucket
from pyrate_limiter import Duration, RequestRate, Limiter

from russell_2000 import russell2000_data

class CachedLimiterSession(CacheMixin, LimiterMixin, Session):
    pass

session = CachedLimiterSession(
    limiter=Limiter(RequestRate(2, Duration.SECOND * 5)),  # Increased to 10 requests per 3 seconds
    bucket_class=MemoryQueueBucket,
    backend=SQLiteCache("yfinance.cache"),
)
session.headers['User-agent'] = 'yfinance_app/1.0'

# Load the list of S&P 500 tickers and company names
sp500_data = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies')[0]
sp500_tickers = [(symbol.replace('.', '-'), name, 'S&P') for symbol, name in sp500_data[['Symbol', 'Security']].values.tolist()]
sp500_set = {symbol for symbol, _, _ in sp500_tickers}

# Load the list of Russell 2000 tickers and company names
russell2000_tickers = [(symbol.replace('.', '-'), name, 'Russel') for symbol, name in russell2000_data]

all_tickers = sp500_tickers

# Combine S&P 500 and Russell 2000 tickers
for x in russell2000_tickers:
    if x[0] not in sp500_set:
        all_tickers.append(x)

owned_tickers = {
    "AMD", "OXY", "SIRI", "CDW", "COP", "FDX", "MRK", "VRSN", "FANG", "FSLR", "HII", "LEN", "NUE", "PRU", "TSM", "TSLA", "META", "NVDA", "BRK-B",
    "ACLS", "AMPH", "AMR", "ATKR", "CCS", "HOV", "ICFI", "PRU", "TDW", "TSM", "VAL", "ARCB", "BXC", "STNG", "ZEUS", "ARCB", "PCVX", "UNH", "IOSP", "LEN",
    "MTRN", "NSSC", "VAL"}

# Calculate the start date (400 weeks ago) and end date (current date)
end_date = datetime.today()
start_date = end_date - timedelta(weeks=400)

DATA_FILE = f"dat/stock_data_{end_date.date()}.pkl"
RESULT_FILE = f"dat/results_{end_date.date()}.json"
CLOSE_COL = "Close"
CUR_PRICE = "current_price"

async def load_data(tickers):
    # Check if saved data exists and load it if not forcing refresh
    missing_tickers = []
    if os.path.exists(DATA_FILE):
        print("Loading cached stock data...")
        histories = await asyncio.to_thread(pd.read_pickle, DATA_FILE)
        print(f"Found data for {len(histories.columns.levels[1])} tickers in {DATA_FILE}")
        missing_tickers = [ticker for ticker in tickers if ticker not in histories.columns.levels[1]]
        if len(missing_tickers) == 0:
            print("All tickers are already downloaded.")
            return histories
    else:
        histories = pd.DataFrame()

    missing_tickers = missing_tickers if missing_tickers else tickers
    print(f"Downloading {len(missing_tickers)} stocks...")
    stocks = yf.Tickers(" ".join(missing_tickers), session=session)
    new_histories = await asyncio.to_thread(stocks.history, period="400wk", interval="1d")

    if not histories.empty:
        histories = pd.concat([histories, new_histories], axis=1)
    else:
        histories = new_histories

    # Save the downloaded data
    await asyncio.to_thread(histories.to_pickle, DATA_FILE)
    print(f"Saved stock data to {DATA_FILE}")

    return histories

async def download_stock_data(tickers):
    results = {}
    total_tickers = len(tickers)
    histories = await load_data(tickers)

    histories.index = pd.to_datetime(histories.index)
    histories = histories[(histories.index >= start_date) & (histories.index <= end_date)]

    print(f"Returning data for {len(histories)} tickers.")
    return histories

def should_print_results(ticker, recent_sma, current_price, sma_slope, slope_200_w) -> bool:
    if ticker in owned_tickers:
        return True
    return recent_sma and recent_sma > current_price and sma_slope >= 0.1 and slope_200_w >= 0.1

async def get_url(ticker):
    base_url = f"https://www.google.com/finance/quote/{ticker}:NYSE?window=5Y"
    try:
        async with session.get(base_url) as response:
            text = await response.text()
            if "We couldn't find any match for your search" in text:
                base_url = f"https://www.google.com/finance/quote/{ticker}:NASDAQ?window=5Y"
    except Exception as e:
        print(f"Error accessing URL for {ticker}: {e}")
        base_url = f"https://www.google.com/finance/quote/{ticker}:NASDAQ?window=5Y"

    return base_url  # Return the original URL

# Function to process stock data and calculate SMA and its slope
async def process_stock_data(ticker, company_name, index, stock_data):
    df_close = stock_data[CLOSE_COL][ticker]
    current_price = float(df_close.iloc[-1]) if not df_close.empty else None
    entry = {CLOSE_COL: df_close, CUR_PRICE: current_price}

    if entry[CLOSE_COL] is None or entry[CUR_PRICE] is None:
        print(f"Skipping {ticker} due to missing data, try running again.")
        return None
    
    try:
        data = entry[CLOSE_COL]
        current_price = entry[CUR_PRICE]
        
        # Filter only Fridays
        data.index = pd.to_datetime(data.index)
        data = data[data.index.weekday == 4]

        # Select the last 400 Fridays
        data = data.tail(400)

        # Calculate the slope of the last 200 weeks
        data_200 = data.tail(200)
        # Calculate the slope of the SMA
        if len(data_200) >= 2:
            y = data_200.values
            x = np.arange(len(y))
            slope_200_w, _ = np.polyfit(x, y, 1)
        else:
            slope_200_w = None

        # Calculate the 200-week SMA
        data["200_Week_SMA"] = data.rolling(window=200, min_periods=1).mean()

        # Get the most recent 200-week SMA, for the most recent 200 weeks.
        recent_sma_series = data['200_Week_SMA'].tail(200).dropna()
        recent_sma = float(recent_sma_series.iloc[-1]) if not recent_sma_series.empty else None

        # Calculate the percentage below the SMA
        percentage_below_sma = ((recent_sma - current_price) / recent_sma) * 100 if recent_sma and current_price else None
        
        # Calculate the slope of the SMA
        if len(recent_sma_series) >= 2:
            y = recent_sma_series.values
            x = np.arange(len(y))
            sma_slope, _ = np.polyfit(x, y, 1)
        else:
            sma_slope = None
        
        # Store results only if the current price is below the 200-week SMA
        if should_print_results(ticker, recent_sma, current_price, sma_slope, slope_200_w):
            print(f"Processing {ticker}: Current Price: {current_price}, Recent SMA: {recent_sma}, ")
            url = await get_url(ticker)
            print(f"URL for {ticker}: {url}")

            # Fetch additional financial data (total equity and market cap)
            try:
                stock_info = await asyncio.to_thread(yf.Ticker(ticker).get_info)
                market_cap = stock_info.get("marketCap", 0) / 1e9
                book_value = stock_info.get("bookValue", 0)
                price_to_book = stock_info.get("priceToBook", 0)
            except Exception as e:
                print(f"Error fetching financial data for {ticker}: {e}")
                market_cap = None
                book_value = None
                price_to_book = None

            return {
                "Ticker": ticker,
                "Company Name": company_name,
                "Index": index,
                "Price": current_price,
                "Book": book_value,
                "Price/Book": price_to_book,
                "Owned": ticker in owned_tickers,
                "200-Week SMA": recent_sma,
                "% Below SMA": percentage_below_sma,
                "SMA Slope": sma_slope,
                "Slope 200W": slope_200_w,
                "MarketCap(B)": market_cap,
                "url": url,
            }
    except Exception as e:
        print(f"Error processing {ticker}: {e}")
    return None

async def process_batch(batch, stock_data, semaphore):
    """Process a batch of tickers with a concurrency limit."""
    async with semaphore:
        tasks = [
            process_stock_data(ticker, company_name, index, stock_data)
            for ticker, company_name, index in batch
        ]
        return await asyncio.gather(*tasks)

async def main():
    tickers = [ticker for ticker, _, _ in all_tickers]
    stock_data = await download_stock_data(tickers)

    # Define batch size and concurrency limit
    batch_size = 50  # Number of tickers per batch
    max_concurrent_tasks = 10  # Maximum number of concurrent tasks
    semaphore = asyncio.Semaphore(max_concurrent_tasks)

    # Split tickers into batches
    batches = [
        all_tickers[i:i + batch_size]
        for i in range(0, len(all_tickers), batch_size)
    ]

    results = []
    for batch in batches:
        batch_results = await process_batch(batch, stock_data, semaphore)
        results.extend([result for result in batch_results if result])

    results_df = pd.DataFrame(results).sort_values(by="% Below SMA", ascending=False)
    results_df = results_df.round(2)  # Round all values to 2 decimal places
    pd.set_option('display.max_rows', None)
    pd.set_option('display.max_columns', None)  # Ensure all columns are displayed
    pd.set_option('display.max_colwidth', None)  # Show full content in columns
    pd.set_option('display.width', 2000)
    print(results_df.to_string(index=False))  # Exclude the index column when printing
    
    # Overwrite the result file
    results_df.to_json(RESULT_FILE, orient='records', indent=4, mode='w')

# Run with caching (set force_refresh=True if you want fresh data)
asyncio.run(main())
