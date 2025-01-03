from detectron2.config.compat import downgrade_config, upgrade_config

from .config import CfgNode, get_cfg, global_cfg, set_global_cfg

__all__ = [
    "CfgNode",
    "get_cfg",
    "global_cfg",
    "set_global_cfg",
    "downgrade_config",
    "upgrade_config",
]
