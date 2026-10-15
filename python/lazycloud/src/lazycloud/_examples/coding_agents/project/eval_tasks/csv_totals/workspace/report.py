from decimal import Decimal


def totals_by_category(csv_text: str) -> dict[str, Decimal]:
    lines = csv_text.strip().splitlines()
    header = lines[0].split(",")
    category_at = header.index("category")
    amount_at = header.index("amount")
    totals: dict[str, Decimal] = {}
    for line in lines[1:]:
        fields = line.split(",")
        category = fields[category_at]
        totals[category] = totals.get(category, Decimal(0)) + Decimal(fields[amount_at])
    return totals
