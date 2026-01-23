from .base import free_product
from .base_models import ProductDefinition
from .hobby import developer_product
from .pro import pro_product
from .scale import scale_product

# Product definitions (ordered by tier)
PRODUCT_DEFINITIONS: tuple[ProductDefinition, ...] = (
    free_product,
    developer_product,
    pro_product,
    scale_product,
)

__all__ = ["PRODUCT_DEFINITIONS"]
