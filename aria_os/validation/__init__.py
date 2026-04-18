"""Geometry validation utilities — part contracts + assertion gate."""
from aria_os.validation.part_contract import (
    Contract,
    ValidationError,
    ValidationResult,
    validate_part,
)

__all__ = ["Contract", "ValidationError", "ValidationResult", "validate_part"]
