from __future__ import annotations

import argparse
import sys
import tokenize
from collections.abc import Sequence
from importlib import metadata
from typing import Any
from typing import cast

from tokenize_rt import UNIMPORTANT_WS
from tokenize_rt import Token
from tokenize_rt import reversed_enumerate
from tokenize_rt import src_to_tokens
from tokenize_rt import tokens_to_src

from django_upgrade.ast import ast_parse
from django_upgrade.data import FIXERS
from django_upgrade.data import Settings
from django_upgrade.data import visit
from django_upgrade.tokens import DEDENT


def main(argv: Sequence[str] | None = None) -> int:
    """
    Main entry point for the django-upgrade command-line tool.

    This function parses command-line arguments, initializes settings, and processes
    the specified files to apply necessary Django upgrade fixers.

    Args:
        argv (Sequence[str] | None): A list of command-line arguments. If None, defaults to sys.argv.

    Returns:
        int: A return code indicating the result of the operation. Zero if no files were changed
             or if --exit-zero-even-if-changed is set, non-zero otherwise.

    Command-line Arguments:
        filenames (list of str): A list of filenames to process.
        --target-version (str): The target Django version for the upgrade. Defaults to "2.2".
        --exit-zero-even-if-changed: Exit with a zero return code even if files have changed.
        --version: Show the version number and exit.
        --only (list of str): Run only the selected fixers.
        --skip (list of str): Skip the selected fixers.
        --list-fixers: List all available fixer names and exit.

    Example:
        python -m django_upgrade.main myfile.py --target-version 3.2
    """
    parser = argparse.ArgumentParser(prog="django-upgrade")
    parser.add_argument("filenames", nargs="+")
    parser.add_argument(
        "--target-version",
        default="2.2",
        choices=[
            "1.7",
            "1.8",
            "1.9",
            "1.10",
            "1.11",
            "2.0",
            "2.1",
            "2.2",
            "3.0",
            "3.1",
            "3.2",
            "4.0",
            "4.1",
            "4.2",
            "5.0",
            "5.1",
        ],
        help="The version of Django to target.",
    )
    parser.add_argument(
        "--exit-zero-even-if-changed",
        action="store_true",
        help="Exit with a zero return code even if files have changed.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=metadata.version("django-upgrade"),
        help="Show the version number and exit.",
    )
    parser.add_argument(
        "--only",
        action="append",
        type=fixer_type,
        help="Run only the selected fixers.",
    )
    parser.add_argument(
        "--skip",
        action="append",
        type=fixer_type,
        help="Skip the selected fixers.",
    )
    parser.add_argument(
        "--list-fixers", nargs=0, action=ListFixersAction, help="List all fixer names."
    )

    args = parser.parse_args(argv)

    target_version: tuple[int, int] = cast(
        tuple[int, int],
        tuple(int(x) for x in args.target_version.split(".", 1)),
    )

    settings = Settings(
        target_version=target_version,
        only_fixers=set(args.only) if args.only else None,
        skip_fixers=set(args.skip) if args.skip else None,
    )

    ret = 0
    for filename in args.filenames:
        ret |= fix_file(
            filename,
            settings,
            exit_zero_even_if_changed=args.exit_zero_even_if_changed,
        )

    return ret


def fixer_type(string: str) -> str:
    if string not in FIXERS:
        raise argparse.ArgumentTypeError(f"Unknown fixer: {string!r}")
    return string


class ListFixersAction(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        for name in sorted(FIXERS):
            print(name)
        parser.exit()


def fix_file(
    filename: str,
    settings: Settings,
    exit_zero_even_if_changed: bool,
) -> int:
    """
    Processes a file to apply Django upgrade fixers.

    This function reads the contents of the specified file, applies the necessary
    fixers based on the provided settings, and writes the changes back to the file
    if any modifications were made.

    Args:
        filename (str): The name of the file to process. Use "-" to read from stdin.
        settings (Settings): The settings object containing fixer configurations.
        exit_zero_even_if_changed (bool): If True, the function will return 0 even if
                                          the file was changed.

    Returns:
        int: A return code indicating the result of the operation. Zero if no changes
             were made or if exit_zero_even_if_changed is True, non-zero otherwise.
    """
    if filename == "-":
        # If the filename is "-", read the contents from standard input (stdin) as bytes
        contents_bytes = sys.stdin.buffer.read()
    else:
        # Otherwise, open the file in binary read mode and read its contents as bytes
        with open(filename, "rb") as fb:
            contents_bytes = fb.read()

    try:
        contents_text_orig = contents_text = contents_bytes.decode()
    except UnicodeDecodeError:
        print(f"{filename} is non-utf-8 (not supported)")
        return 1

    contents_text = apply_fixers(contents_text, settings, filename)

    if filename == "-":
        print(contents_text, end="")
    elif contents_text != contents_text_orig:
        print(f"Rewriting {filename}", file=sys.stderr)
        with open(filename, "w", encoding="UTF-8", newline="") as f:
            f.write(contents_text)

    if exit_zero_even_if_changed:
        return 0
    return contents_text != contents_text_orig


def apply_fixers(contents_text: str, settings: Settings, filename: str) -> str:
    try:
        ast_obj = ast_parse(contents_text)
    except SyntaxError:
        return contents_text

    callbacks = visit(ast_obj, settings, filename)
    # if no transformation functions were collected, return the original source
    if not callbacks:
        return contents_text

    # convert source code text to tokens
    try:
        tokens = src_to_tokens(contents_text)
    except tokenize.TokenError:  # pragma: no cover (bpo-2180)
        return contents_text

    # dedent tokents are used in python to indicate the end of a block.
    # making sure that dedent tokens in the tokenized source code are according to 
    # the indentation levels set in the source code
    fixup_dedent_tokens(tokens)

    # applying transformation functions to the tokens in reverser order
    # to ensure that the indices of the token that are not processed yet are not affected.
    for i, token in reversed_enumerate(tokens):
        if not token.src:
            continue
        # though this is a defaultdict, by using `.get()` this function's
        # self time is almost 50% faster
        for callback in callbacks.get(token.offset, ()):
            callback(tokens, i)

    # no types for tokenize-rt
    return tokens_to_src(tokens)  # type: ignore [no-any-return]


def fixup_dedent_tokens(tokens: list[Token]) -> None:
    """For whatever reason the DEDENT / UNIMPORTANT_WS tokens are misordered

    | if True:
    |     if True:
    |         pass
    |     else:
    |^    ^- DEDENT
    |+----UNIMPORTANT_WS
    """
    for i, token in enumerate(tokens):
        if token.name == UNIMPORTANT_WS and tokens[i + 1].name == DEDENT:
            tokens[i], tokens[i + 1] = tokens[i + 1], tokens[i]
