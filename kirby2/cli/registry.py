"""Deterministic, explicit registration for expansion CLI commands."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from dataclasses import dataclass


CommandHandler = Callable[[argparse.Namespace], int]
ParserConfigurer = Callable[[argparse.ArgumentParser], None]
_HANDLER_ATTRIBUTE = "_kirby2_expansion_handler"


class CommandRegistrationError(ValueError):
    """A deterministic command-registration refusal."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One explicitly declared top-level command."""

    command_id: str
    name: str
    help: str
    handler: CommandHandler
    configure: ParserConfigurer | None = None

    def __post_init__(self) -> None:
        if not self.command_id or any(
            character.isspace() for character in self.command_id
        ):
            raise CommandRegistrationError(
                "INVALID_COMMAND_ID",
                "command IDs must be nonempty whitespace-free tokens: "
                f"{self.command_id!r}",
            )
        if (
            not self.name
            or self.name.startswith("-")
            or any(character.isspace() for character in self.name)
        ):
            raise CommandRegistrationError(
                "INVALID_COMMAND_NAME",
                f"command names must be nonempty option-free tokens: {self.name!r}",
            )
        if not self.help:
            raise CommandRegistrationError(
                "INVALID_COMMAND_HELP",
                f"command {self.name!r} requires deterministic help text",
            )
        if not callable(self.handler):
            raise CommandRegistrationError(
                "INVALID_COMMAND_HANDLER",
                f"command {self.name!r} handler is not callable",
            )
        if self.configure is not None and not callable(self.configure):
            raise CommandRegistrationError(
                "INVALID_COMMAND_CONFIGURER",
                f"command {self.name!r} configurer is not callable",
            )


@dataclass(frozen=True, slots=True)
class CommandModule:
    """A named, ordered collection of explicit command declarations."""

    module_id: str
    commands: tuple[CommandSpec, ...]

    def __post_init__(self) -> None:
        if not self.module_id or any(
            character.isspace() for character in self.module_id
        ):
            raise CommandRegistrationError(
                "INVALID_MODULE_ID",
                "module IDs must be nonempty whitespace-free tokens: "
                f"{self.module_id!r}",
            )
        if not self.commands:
            raise CommandRegistrationError(
                "EMPTY_COMMAND_MODULE",
                f"module {self.module_id!r} declares no commands",
            )


class CommandRegistry:
    """Register declared modules in caller-supplied canonical order."""

    def __init__(self, subcommands: argparse._SubParsersAction) -> None:
        if not isinstance(subcommands, argparse._SubParsersAction):
            raise TypeError("subcommands must be an argparse subparser action")
        self._subcommands = subcommands
        self._module_ids: list[str] = []
        self._command_ids: list[str] = []
        self._registered_names: list[str] = []

    @property
    def module_ids(self) -> tuple[str, ...]:
        return tuple(self._module_ids)

    @property
    def registered_names(self) -> tuple[str, ...]:
        return tuple(self._registered_names)

    @property
    def command_ids(self) -> tuple[str, ...]:
        return tuple(self._command_ids)

    def register_modules(self, modules: Iterable[CommandModule]) -> None:
        declared = tuple(modules)
        if any(not isinstance(module, CommandModule) for module in declared):
            raise TypeError("command modules must use CommandModule")

        module_ids = tuple(module.module_id for module in declared)
        duplicate_module = next(
            (
                module_id
                for module_id in module_ids
                if module_ids.count(module_id) > 1
                or module_id in self._module_ids
            ),
            None,
        )
        if duplicate_module is not None:
            raise CommandRegistrationError(
                "DUPLICATE_MODULE_ID",
                f"module ID {duplicate_module!r} is already registered or repeated",
            )

        names = tuple(
            command.name for module in declared for command in module.commands
        )
        command_ids = tuple(
            command.command_id for module in declared for command in module.commands
        )
        duplicate_command_id = next(
            (
                command_id
                for command_id in command_ids
                if command_ids.count(command_id) > 1
                or command_id in self._command_ids
            ),
            None,
        )
        if duplicate_command_id is not None:
            raise CommandRegistrationError(
                "DUPLICATE_COMMAND_ID",
                f"command ID {duplicate_command_id!r} is already registered "
                "or repeated",
            )
        duplicate_name = next(
            (name for name in names if names.count(name) > 1),
            None,
        )
        if duplicate_name is not None:
            raise CommandRegistrationError(
                "DUPLICATE_COMMAND",
                f"command {duplicate_name!r} is declared more than once",
            )
        occupied = set(self._subcommands.choices)
        shadowed = next((name for name in names if name in occupied), None)
        if shadowed is not None:
            raise CommandRegistrationError(
                "SHADOWED_COMMAND",
                f"command {shadowed!r} already exists",
            )

        for module in declared:
            self._register_preflighted_module(module)

    def register_module(self, module: CommandModule) -> None:
        self.register_modules((module,))

    def _register_preflighted_module(self, module: CommandModule) -> None:
        names = tuple(command.name for command in module.commands)
        for command in module.commands:
            parser = self._subcommands.add_parser(command.name, help=command.help)
            if command.configure is not None:
                command.configure(parser)
            parser.set_defaults(**{_HANDLER_ATTRIBUTE: command.handler})
        self._module_ids.append(module.module_id)
        self._command_ids.extend(command.command_id for command in module.commands)
        self._registered_names.extend(names)


def dispatch_registered_command(args: argparse.Namespace) -> bool:
    """Dispatch one private expansion handler, or let legacy dispatch continue."""

    handler = vars(args).get(_HANDLER_ATTRIBUTE)
    if handler is None:
        return False
    if not callable(handler):
        raise TypeError("registered command handler is not callable")
    exit_code = handler(args)
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code < 0:
        raise TypeError("registered command handlers must return a nonnegative int")
    if exit_code:
        raise SystemExit(exit_code)
    return True
