"""keyboard brightness control — delegates to prism.mac.brightness.

# TODO: swap brightness-monitor to use prism.mac.brightness
# (swap complete — this module now re-exports from prism)
"""

from prism.mac.brightness import (  # noqa: F401
    get_brightness,
    set_auto_brightness,
    set_brightness,
    suspend_idle_dimming,
)
