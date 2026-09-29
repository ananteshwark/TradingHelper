"""Factor library. Importing this package registers every factor."""

from igs.factors import (  # noqa: F401
    growth,
    low_volatility,
    momentum,
    ownership,
    quality,
    valuation,
)
from igs.factors.registry import REGISTRY, FactorSpec, factor

__all__ = ["REGISTRY", "FactorSpec", "factor"]
