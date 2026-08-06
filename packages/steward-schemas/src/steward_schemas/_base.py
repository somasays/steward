"""Shared model configuration for every contract in this package.

Every published contract is immutable (frozen) and closed to unknown fields
(extra="forbid") so that a typo or a silently-dropped field fails fast at
validation time rather than laundering through as an untyped dict (I3).
Deviations must be documented on the model that needs one — see
`errors.ProblemDetails` for the one exception, and why.
"""

from pydantic import BaseModel, ConfigDict


class SchemaModel(BaseModel):
    """Base class for Steward's published contracts."""

    model_config = ConfigDict(frozen=True, extra="forbid")
