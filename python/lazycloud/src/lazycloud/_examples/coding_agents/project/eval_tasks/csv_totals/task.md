`totals_by_category(csv_text)` in `report.py` sums the `amount` column per
`category`. A customer's export broke it: some descriptions contain quoted
commas. Make it read any valid CSV with a header row, whatever the column
order.
