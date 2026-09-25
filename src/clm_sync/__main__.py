from . import __version__
from .cli import run
from .config import load_config, write_config_atomic
from .models import ModelSyncError
from .sync import sync_config

__all__ = [
    "__version__",
    "run",
    "load_config",
    "write_config_atomic",
    "sync_config",
    "ModelSyncError",
]


def main() -> int:
    raise SystemExit(run())


# Executed when invoked via `python -m clm_sync`.
main()
