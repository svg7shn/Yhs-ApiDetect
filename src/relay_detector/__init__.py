"""Claude relay station quality detector."""

from .proxy_env import repair_proxy_environment

__version__ = "1.1.1"

# Run before anything can construct an httpx client: httpx reads NO_PROXY at
# client construction time and raises InvalidURL on a bracketed IPv6 entry.
# See relay_detector/proxy_env.py for the exact failure mode.
repair_proxy_environment()
