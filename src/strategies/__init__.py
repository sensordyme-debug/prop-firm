"""Strategy framework and candidate library.

Importing this package registers every strategy, both implemented and
declared-but-unavailable, so the research runner sees the full catalogue.
"""

from strategies import catalogue, library  # noqa: F401  (import registers)
from strategies.base import (  # noqa: F401
    REGISTRY,
    DataRequirement,
    ParameterSpec,
    Strategy,
    StrategyFamily,
    StrategyMeta,
    all_strategies,
    available_strategies,
    config_hash,
    dataset_hash,
    get_strategy,
    register,
    unavailable_strategies,
)
