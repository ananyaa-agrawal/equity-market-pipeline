"""Equity market data pipeline and backtester.

Each module in this package does one job (download, check, store, decide, trade,
reconcile, measure, report), and main.py strings them together. Keeping the jobs
separate is how real trading systems stay testable: you can prove each piece
works on its own before trusting the whole chain with money.
"""
