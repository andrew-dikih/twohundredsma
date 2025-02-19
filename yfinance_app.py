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

from requests import Session
from requests_cache import CacheMixin, SQLiteCache
from requests_ratelimiter import LimiterMixin, MemoryQueueBucket
from pyrate_limiter import Duration, RequestRate, Limiter

class CachedLimiterSession(CacheMixin, LimiterMixin, Session):
    pass

session = CachedLimiterSession(
    limiter=Limiter(RequestRate(5, Duration.SECOND*5)),  # max 2 requests per 5 seconds
    bucket_class=MemoryQueueBucket,
    backend=SQLiteCache("yfinance.cache"),
)
session.headers['User-agent'] = 'yfinance_app/1.0'

# Load the list of S&P 500 tickers and company names
sp500_data = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies')[0]
sp500_tickers = [(symbol.replace('.', '-'), name) for symbol, name in sp500_data[['Symbol', 'Security']].values.tolist()]

# Calculate the start date (400 weeks ago) and end date (current date)
end_date = datetime.today()
start_date = end_date - timedelta(weeks=400)

DATA_FILE = "dat/stock_data.pkl"
RESULT_FILE = f"dat/results_{end_date.date()}.json"
CLOSE_COL = "Close"
CUR_PRICE = "current_price"

def load_data(tickers, force_refresh):
    # Check if saved data exists and load it if not forcing refresh
    if os.path.exists(DATA_FILE) and not force_refresh:
        print("Loading cached stock data...")
        histories = pd.read_pickle(DATA_FILE)           
    else:
        stocks = yf.Tickers(" ".join(tickers), session=session)
        histories = stocks.history(period="400wk", interval="1d")

        # Save the downloaded data
        histories.to_pickle(DATA_FILE)
        print(f"Saved stock data to {DATA_FILE}")

    return histories

async def download_stock_data(tickers, force_refresh=False):
    results = {}
    total_tickers = len(tickers)
    attempt = 0
    wait_time = 0
    while attempt < 5:
        try:
            histories = load_data(tickers, force_refresh)

            histories.index = pd.to_datetime(histories.index)
            histories = histories[(histories.index >= start_date) & (histories.index <= end_date)]

            for index, ticker in enumerate(tickers, start=1):
                try:
                    df_close = histories[CLOSE_COL][ticker]
                    
                    current_price = float(df_close.iloc[-1]) if not df_close.empty else None
                    results[ticker] = {CLOSE_COL: df_close, CUR_PRICE: current_price}

                    # print(f"Downloaded {index}/{total_tickers}: {ticker}")
                except Exception as e:
                    print(f"Error processing data for {ticker}: {e}")
                    results[ticker] = {CLOSE_COL: None, CUR_PRICE: None}

            wait_time = 0  # Reset wait time on success
            break
        except Exception as e:
            print(f"Error downloading data, attempt {attempt + 1}: {e}")
            attempt += 1
            wait_time = 2 ** attempt + random.uniform(0, 1)
            print(f"Retrying in {wait_time:.2f} seconds...")
            await asyncio.sleep(wait_time)

    return results

# Function to process stock data and calculate SMA and its slope
def process_stock_data(ticker, company_name, stock_data):
    entry = stock_data.get(ticker, None)
    if not entry or entry[CLOSE_COL] is None or entry[CUR_PRICE] is None:
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
        if recent_sma and recent_sma > current_price and sma_slope >= 0.1 and slope_200_w >= 0.1:
            return {
                "Ticker": ticker,
                "Company Name": company_name,
                "Current Price": current_price,
                "Most Recent 200-Week SMA": recent_sma,
                "% Below SMA": percentage_below_sma,
                "SMA Slope": sma_slope,
                "Slope 200W": slope_200_w,
                "url": f"https://www.google.com/finance/quote/{ticker}:NYSE?window=5Y",
            }
    except Exception as e:
        print(f"Error processing {ticker}: {e}")
    return None

async def main(force_refresh=False):
    tickers = [ticker for ticker, _ in sp500_tickers]
    stock_data = await download_stock_data(tickers, force_refresh=force_refresh)

    results = [process_stock_data(ticker, company_name, stock_data) for ticker, company_name in sp500_tickers]
    results = [result for result in results if result]

    results_df = pd.DataFrame(results).sort_values(by="% Below SMA", ascending=False)
    pd.set_option('display.max_rows', None)
    pd.set_option('display.max_colwidth', None)  # Show full content in columns
    pd.set_option('display.width', 1500)
    print(results_df)
    results_df.to_json(RESULT_FILE, orient='records', indent=4)

# Run with caching (set force_refresh=True if you want fresh data)
asyncio.run(main(force_refresh=False))
