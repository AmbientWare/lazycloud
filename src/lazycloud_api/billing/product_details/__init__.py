from .base import base_product
from .base_models import ProductDefinition
from .enterprise import enterprise_product
from .pro import pro_product

# Product definitions
PRODUCT_DEFINITIONS: tuple[ProductDefinition, ...] = (
    base_product,
    pro_product,
    enterprise_product,
)

__all__ = ["PRODUCT_DEFINITIONS"]
