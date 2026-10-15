Users report that the last page of search results goes missing. Fix
`page_count(total, per_page)` in `pagination.py` so it returns the number of
pages needed to show `total` items, `per_page` at a time. A `per_page` below
1 is a caller error and raises `ValueError`.
