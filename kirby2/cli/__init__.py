"""Explicit command registration for post-baseline Kirby2 commands."""

from .registry import (
    CommandModule,
    CommandRegistrationError,
    CommandRegistry,
    CommandSpec,
    dispatch_registered_command,
)

__all__ = [
    "CommandModule",
    "CommandRegistrationError",
    "CommandRegistry",
    "CommandSpec",
    "dispatch_registered_command",
]
