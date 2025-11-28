from .base import base_product
from .base_models import ProductDefinition
from .hobby import hobby_product
from .pro import pro_product

# Product definitions
PRODUCT_DEFINITIONS: tuple[ProductDefinition, ...] = (
    base_product,
    hobby_product,
    pro_product,
)

__all__ = ["PRODUCT_DEFINITIONS"]
