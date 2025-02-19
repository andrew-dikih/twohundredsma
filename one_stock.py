import yfinance as yf
import pandas as pd

# Download historical stock data (about 4 years to cover 200 weeks)
ticker = "BRK-B"
data = yf.download(ticker, period="4y", interval="1d")  # Daily data

# Filter only Fridays
data = data[data.index.weekday == 4]  # 4 represents Friday in Python's datetime weekday()

# Select the last 200 Fridays
data = data.tail(200)

# Calculate the 200-week SMA
data["200_Week_SMA"] = data["Close"].rolling(window=200).mean()

# Get the current price
current_price = yf.Ticker(ticker).history(period="1d")["Close"].iloc[-1]

# Print results
print(f"Current Price: {current_price}")
print(f"Most Recent 200-Week SMA: {data['200_Week_SMA'].dropna().iloc[-1]}")
