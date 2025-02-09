import ast
from collections.abc import Iterable
from functools import partial

from tokenize_rt import Offset, Token

from django_upgrade.ast import ast_start_offset
from django_upgrade.data import Fixer
from django_upgrade.data import State
from django_upgrade.data import TokenFunc
from django_upgrade.tokens import find_and_replace_name

fixer = Fixer(
    __name__,
    min_version=(3, 0),  # Assuming psycopg3 is compatible with Django 3.0 and above
)

# Mapping of old module names to new module names
MODULE_MAP = {
    "psycopg2": "psycopg",
}

# The fixer is registered to handle both ast.Import and ast.ImportFrom nodes, 
# which represent import statements in the AST.
@fixer.register(ast.Import)
@fixer.register(ast.ImportFrom)
def visit_Import(
    state: State,
    node: ast.Import,
    parents: tuple[ast.AST, ...],
) -> Iterable[tuple[Offset, TokenFunc]]:
    for alias in node.names:
        if alias.name in MODULE_MAP:
            new_name = MODULE_MAP[alias.name]
            yield ast_start_offset(node), partial(
                replace_module_name, name=alias.name, new=new_name
            )

def replace_module_name(tokens: list[Token], i: int, *, name: str, new: str) -> None:
    """Replace the old module name with the new module name in the tokens."""
    while i < len(tokens):
        if tokens[i].src == name:
            tokens[i] = tokens[i]._replace(src=new)
            break
        i += 1