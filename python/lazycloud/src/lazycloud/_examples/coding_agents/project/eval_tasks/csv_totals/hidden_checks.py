from decimal import Decimal

from report import totals_by_category


def test_quoted_commas_stay_in_their_field() -> None:
    text = 'description,category,amount\n"Pens, blue",office,3.10\n"Paper, A4",office,4.90\n'
    assert totals_by_category(text) == {"office": Decimal("8.00")}


def test_quoted_newlines_stay_in_their_field() -> None:
    text = 'category,description,amount\ntravel,"Taxi\nto airport",30\ntravel,Train,12.5\n'
    assert totals_by_category(text) == {"travel": Decimal("42.5")}
