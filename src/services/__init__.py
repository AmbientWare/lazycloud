# from services.aws.route53 import Route53Service
from services.fly.apps import FlyAppManager
from services.platform.platform_manager import PlatformManager

# route_53 = Route53Service()
fly_app_manager = FlyAppManager()
platform_manager = PlatformManager()


__all__ = ["fly_app_manager", "platform_manager"]
