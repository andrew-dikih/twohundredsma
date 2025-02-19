import pandas as pd
import asyncio
import aiohttp
import time
from datetime import datetime, timedelta
import requests
import numpy as np

# Set your Alpha Vantage API key
ALPHA_VANTAGE_API_KEY = "demo"
ALPHA_VANTAGE_BASE_URL = "https://www.alphavantage.co/query"

# Load the list of S&P 500 tickers and company names
sp500_data = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies')[0]
sp500_tickers = [(symbol.replace('.', '-'), name) for symbol, name in sp500_data[['Symbol', 'Security']].values.tolist()]

# Remove specific tickers, to keep total stock count to 500
tickers_to_remove = {"GOOGL", "FOX", "NWSA"}
sp500_tickers = [(ticker, name) for ticker, name in sp500_tickers if ticker not in tickers_to_remove][:500]
sp500_tickers = [("IBM", "IBM")]

# Calculate the start date (400 weeks ago) and end date (current date)
end_date = datetime.today().strftime('%Y-%m-%d')
start_date = (datetime.today() - timedelta(weeks=400)).strftime('%Y-%m-%d')

# Time Series key
time_series_key = "Weekly Adjusted Time Series"

# Function to download stock data
async def download_stock_data(tickers):
    results = {}
    async with aiohttp.ClientSession() as session:
        for ticker in tickers:
            params = {
                "function": "TIME_SERIES_WEEKLY_ADJUSTED",
                "symbol": ticker,
                "apikey": ALPHA_VANTAGE_API_KEY,
            }
            try:
                async with session.get(ALPHA_VANTAGE_BASE_URL, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        if time_series_key in data:
                            df = pd.DataFrame.from_dict(data[time_series_key], orient="index")
                            df.index = pd.to_datetime(df.index)
                            df = df.rename(columns={"5. adjusted close": "close"})
                            df = df.sort_index()
                            
                            # Filter data within the date range
                            df = df[(df.index >= start_date) & (df.index <= end_date)]
                            
                            current_price = float(df["close"].iloc[-1]) if not df.empty else None
                            results[ticker] = (df, current_price)
                        else:
                            results[ticker] = (None, None)
                    else:
                        print(f"Error downloading data for {ticker}: HTTP {response.status}")
                        results[ticker] = (None, None)
            except Exception as e:
                print(f"Error downloading data for {ticker}: {e}")
                results[ticker] = (None, None)
    return results

# Function to process stock data and calculate SMA and its slope
def process_stock_data(ticker, company_name, stock_data):
    data, current_price = stock_data.get(ticker, (None, None))
    if data is None or current_price is None:
        return None
    
    try:      
        # Filter only Fridays
        data = data[data.index.weekday == 4]

        # Select the last 200 Fridays
        data = data.tail(200)

        # Calculate the 200-week SMA
        data["200_Week_SMA"] = data["close"].astype(float).rolling(window=200, min_periods=1).mean()

        # Get the most recent 200-week SMA
        recent_sma = float(data['200_Week_SMA'].dropna().iloc[-1]) if not data['200_Week_SMA'].dropna().empty else None

        # Calculate the percentage below the SMA
        if recent_sma and current_price is not None:
            percentage_below_sma = ((recent_sma - current_price) / recent_sma) * 100
        else:
            percentage_below_sma = None
        
        # Calculate the slope of the SMA
        if len(data['200_Week_SMA'].dropna()) >= 2:
            y = data['200_Week_SMA'].dropna().values
            x = np.arange(len(y))
            slope, _ = np.polyfit(x, y, 1)
        else:
            slope = None
        
        # Store results only if the current price is below the 200-week SMA
        if recent_sma: # and current_price < recent_sma:
            return {
                "Ticker": ticker,
                "Company Name": company_name,
                "Current Price": current_price,
                "Most Recent 200-Week SMA": recent_sma,
                "% Below SMA": percentage_below_sma,
                "SMA Slope": slope
            }
    except Exception as e:
        print(f"Error processing {ticker}: {e}")
    return None

async def main():
    tickers = [ticker for ticker, _ in sp500_tickers]
    stock_data = await download_stock_data(tickers)
    
    results = [process_stock_data(ticker, company_name, stock_data) for ticker, company_name in sp500_tickers]
    results = [result for result in results if result]

    # Convert results to DataFrame and display all rows
    results_df = pd.DataFrame(results)
    pd.set_option('display.max_rows', None)
    print(results_df)

# Run the asyncio event loop
asyncio.run(main())
