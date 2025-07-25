from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
import os
import json

app = FastAPI()

# Directory containing JSON files
DATA_DIR = "dat"

# Set up templates directory
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """List all JSON files as links."""
    if not os.path.exists(DATA_DIR):
        return HTMLResponse("<h1>No data directory found</h1>", status_code=404)
    
    json_files = [f for f in os.listdir(DATA_DIR) if f.endswith(".json")]
    return templates.TemplateResponse("index.html", {"request": request, "files": json_files})

@app.get("/data/{filename}", response_class=HTMLResponse)
async def display_data(request: Request, filename: str):
    """Display the content of a specific JSON file."""
    file_path = os.path.join(DATA_DIR, filename)
    if os.path.exists(file_path):
        with open(file_path, "r") as file:
            data = json.load(file)
        return templates.TemplateResponse("data.html", {"request": request, "data": data, "filename": filename})
    raise HTTPException(status_code=404, detail="File not found")

@app.get("/graph", response_class=HTMLResponse)
async def graph(request: Request):
    """Render the graph page with stock data."""
    stocks = []
    for file in os.listdir(DATA_DIR):
        if file.endswith(".json"):
            file_path = os.path.join(DATA_DIR, file)
            with open(file_path, "r") as f:
                file_data = json.load(f)
                # Extract the date from the file name (e.g., results_2025-07-17.json -> 2025-07-17)
                file_date = file.split("_")[-1].replace(".json", "")
                for stock in file_data:
                    stock["date"] = file_date  # Add the date to each stock entry
                stocks.extend(file_data)

    # Group stocks by ticker and create a history field
    grouped_stocks = {}
    for stock in stocks:
        ticker = stock["Ticker"]
        if ticker not in grouped_stocks:
            grouped_stocks[ticker] = {
                "Ticker": ticker,
                "Company Name": stock["Company Name"],
                "history": []
            }
        grouped_stocks[ticker]["history"].append({
            "date": stock["date"],
            "price": stock.get("Price")  # Use .get() to handle missing keys
        })

    # Ensure distinct and alphabetized tickers
    sorted_stocks = sorted(grouped_stocks.values(), key=lambda x: x["Ticker"])

    # Log the processed data for debugging
    print("Processed Stocks Data:", sorted_stocks)

    return templates.TemplateResponse("graph.html", {"request": request, "stocks": sorted_stocks})
