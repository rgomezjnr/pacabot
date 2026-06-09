import yfinance as yf
from statsmodels.tsa.stattools import coint
from statsmodels.tsa.vector_ar.vecm import coint_johansen
from itertools import combinations
import pandas as pd

# Retrieve tickers from are-they-coint.txt file
with open('are-they-coint.txt', 'r') as f:
    tickers = [line.strip() for line in f if line.strip()]

# Get price data
raw = yf.download(tickers, period='5y')
price_col = 'Close' if 'Close' in raw.columns.get_level_values(0) else 'Adj Close'
data = raw[price_col].dropna(axis=1, how='all')
available = list(data.columns)
missing = [t for t in tickers if t not in available]
if missing:
    print(f"Skipping (no data): {missing}")

# Test pair correlations
for pair in combinations(available, 2):
    series = data[[pair[0], pair[1]]].dropna()
    score, pvalue, _ = coint(series[pair[0]], series[pair[1]])
    print(f"{pair}: p-value = {pvalue:.4f}")  # <0.05 suggests cointegration

# Johansen test for multiple assets
result = coint_johansen(data.dropna(), det_order=0, k_ar_diff=1)
