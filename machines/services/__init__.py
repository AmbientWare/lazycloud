from machines.services.fly import FlyAppManager
from machines.services.pricing_manager import PricingManager

fly_app_manager = FlyAppManager()
pricing_manager = PricingManager()


__all__ = ["fly_app_manager", "pricing_manager"]
