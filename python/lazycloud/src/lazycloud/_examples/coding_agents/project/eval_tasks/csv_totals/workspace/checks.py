from decimal import Decimal

from report import totals_by_category


def test_amounts_add_up_per_category() -> None:
    text = "category,amount,description\nfood,1.50,lunch\nfood,2.25,coffee\nrent,900,march\n"
    assert totals_by_category(text) == {"food": Decimal("3.75"), "rent": Decimal("900")}
