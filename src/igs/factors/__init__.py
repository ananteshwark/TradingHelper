"""Factor library. Importing this package registers every factor."""

from igs.factors import growth, momentum, ownership, quality, valuation  # noqa: F401
from igs.factors.registry import REGISTRY, FactorSpec, factor

__all__ = ["REGISTRY", "FactorSpec", "factor"]
