from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "immich-smart-stacker.py"
spec = importlib.util.spec_from_file_location("immich_smart_stacker", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Could not load module spec from {MODULE_PATH}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


Asset = module.Asset
ImmichClient = module.ImmichClient
SmartStacker = module.SmartStacker
main = module.main
unstack_all = module.unstack_all
logger = module.logger
