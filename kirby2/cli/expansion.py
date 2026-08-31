"""Explicit parser and handler declarations for the WO31-40 expansion."""

from __future__ import annotations

import argparse

from kirby2.curriculum.adaptive_commands import ADAPTIVE_CURRICULUM_COMMAND_MODULE
from kirby2.discovery.commands import STRATEGY_DISCOVERY_COMMAND_MODULE
from kirby2.full_day.commands import FULL_DAY_COMMAND_MODULE
from kirby2.instructor.commands import INSTRUCTOR_CONSOLE_COMMAND_MODULE
from kirby2.microscope.commands import MICROSCOPE_COMMAND_MODULE
from kirby2.mining.commands import MINING_COMMAND_MODULE
from kirby2.orchestration.commands import ORCHESTRATION_COMMAND_MODULE
from kirby2.packs.commands import PACK_COMMAND_MODULE
from kirby2.scenario_lang.commands import SCENARIO_SOURCE_COMMAND_MODULE

from .registry import (
    CommandModule,
    CommandRegistry,
    CommandSpec,
    dispatch_registered_command,
)


def _configure_audit_expansion(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--gate",
        required=True,
        metavar="CARD_ID",
        help="exact registered card ID, or lowercase all",
    )


def _handle_audit_expansion(args: argparse.Namespace) -> int:
    from kirby2.audit.expansion import run_registered_expansion_audit

    return run_registered_expansion_audit(args.gate)


EXPANSION_COMMAND_MODULES = (
    CommandModule(
        module_id="EXPANSION_AUDIT",
        commands=(
            CommandSpec(
                command_id="AUDIT_EXPANSION",
                name="audit-expansion",
                help="run one registered WO31-40 expansion gate",
                handler=_handle_audit_expansion,
                configure=_configure_audit_expansion,
            ),
        ),
    ),
    FULL_DAY_COMMAND_MODULE,
    SCENARIO_SOURCE_COMMAND_MODULE,
    MINING_COMMAND_MODULE,
    ADAPTIVE_CURRICULUM_COMMAND_MODULE,
    STRATEGY_DISCOVERY_COMMAND_MODULE,
    MICROSCOPE_COMMAND_MODULE,
    INSTRUCTOR_CONSOLE_COMMAND_MODULE,
    ORCHESTRATION_COMMAND_MODULE,
    PACK_COMMAND_MODULE,
)


def declared_expansion_command_names() -> tuple[str, ...]:
    return tuple(
        command.name
        for module in EXPANSION_COMMAND_MODULES
        for command in module.commands
    )


def register_expansion_commands(
    subcommands: argparse._SubParsersAction,
) -> CommandRegistry:
    registry = CommandRegistry(subcommands)
    registry.register_modules(EXPANSION_COMMAND_MODULES)
    return registry


def dispatch_expansion_command(args: argparse.Namespace) -> bool:
    return dispatch_registered_command(args)
