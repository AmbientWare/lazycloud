from machines.services.fly import FlyAppManager
from machines.services.platform.platform_manager import PlatformManager

fly_app_manager = FlyAppManager()
platform_manager = PlatformManager()


__all__ = ["fly_app_manager", "platform_manager"]
