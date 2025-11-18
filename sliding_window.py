# Requires: pip install yfinance pandas
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta

def fetch_adj_close(tickers, start='2006-01-01', end=None):
    if end is None:
        end = datetime.today().strftime('%Y-%m-%d')
    data = yf.download(tickers, start=start, end=end, progress=False, auto_adjust=False)
    # yfinance returns a DataFrame with columns like ('Adj Close', 'VOO')
    if ('Adj Close' in data.columns):
        adj = data['Adj Close'].copy()
    else:
        # single ticker returns simpler frame
        adj = data[['Adj Close']].copy()
        adj.columns = [tickers]  # fallback
    return adj

def nearest_trade_price(series, target_date):
    # series index is DatetimeIndex (trading days). Return price for nearest trading day on or after target_date,
    # if none, choose previous trading day.
    idx = series.index
    target = pd.to_datetime(target_date)
    if target in idx:
        return series.loc[target]
    # try next trading day
    later = idx[idx > target]
    if len(later):
        return series.loc[later[0]]
    # fallback to last available before target
    earlier = idx[idx < target]
    if len(earlier):
        return series.loc[earlier[-1]]
    return float('nan')

def compute_annualized(adj_df, window_starts, years=5):
    """
    Compute annualized return over sliding windows.

    adj_df: DataFrame of adjusted close prices (columns are tickers)
    window_starts: iterable of start dates (datetime/date or 'YYYY-MM-DD')
    years: integer number of years for each window (default 5)

    Returns a DataFrame with columns: start, end, and <ticker>_ann{years}
    """
    rows = []
    for start in window_starts:
        start_dt = pd.to_datetime(start)
        end_dt = start_dt + pd.DateOffset(years=years)
        row = {'start': start_dt.date(), 'end': end_dt.date()}
        for col in adj_df.columns:
            s_price = nearest_trade_price(adj_df[col], start_dt)
            e_price = nearest_trade_price(adj_df[col], end_dt)
            if pd.isna(s_price) or pd.isna(e_price):
                ann = float('nan')
            else:
                ann = (e_price / s_price) ** (1.0 / float(years)) - 1.0
            row[f'{col}_ann{years}'] = ann
        rows.append(row)
    return pd.DataFrame(rows)

if __name__ == '__main__':
    tickers = ['VOO', 'SSO']
    # fetch wide date range so start+10y windows are covered
    adj = fetch_adj_close(tickers, start='2011-01-01', end=datetime.today().strftime('%Y-%m-%d'))

    now = datetime.now().year
    period = 1

    # create start dates (yearly). adjust range to cover the desired start years.
    start_years = list(range(2011, now - period))   # adjust as you like
    start_dates = [f'{y}-01-01' for y in start_years]

    # compute 5-year annualized returns using sliding windows
    result = compute_annualized(adj, start_dates, years=period)

    # pretty print as percentages
    display = result.copy()
    for c in display.columns:
        if '_ann' in c:
            display[c] = display[c].apply(lambda x: f'{x:.2%}' if pd.notna(x) else 'n/a')

    print(display.to_string(index=False))

    # save numeric results to CSV for further analysis
    result.to_csv(f'dat/voo_sso_{period}y_annualized_by_start.csv', index=False)
    print("\nSaved numeric results to voo_sso_5y_annualized_by_start.csv")
