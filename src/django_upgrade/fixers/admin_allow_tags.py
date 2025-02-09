"""
Drop lines that set `allow_tags` to `True` on functions.

https://docs.djangoproject.com/en/2.0/releases/2.0/#features-removed-in-2-0
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from functools import partial

from tokenize_rt import Offset

from django_upgrade.ast import ast_start_offset
from django_upgrade.data import Fixer
from django_upgrade.data import State
from django_upgrade.data import TokenFunc
from django_upgrade.tokens import erase_node

fixer = Fixer(
    __name__,
    min_version=(2, 0),
)

"""
This fixer is designed to identify and remove assignments to the allow_tags attribute in Django admin classes. 
The allow_tags attribute was used in older versions of Django to indicate that a method's output should be rendered as HTML. 
This fixer ensures that such assignments are removed, as they are no longer needed in modern versions of Django.
"""
@fixer.register(ast.Assign)
def visit_Assign(
    state: State,
    node: ast.Assign,
    parents: tuple[ast.AST, ...],
) -> Iterable[tuple[Offset, TokenFunc]]:
    # whether the admin module has been imported from either django.contrib or django.contrib.gis. 
    # This is used to determine if the current context is related to Django admin classes.
    if (
        (
            "admin" in state.from_imports["django.contrib"]
            or "admin" in state.from_imports["django.contrib.gis"]
        )
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Attribute)
        and node.targets[0].attr == "allow_tags"
        and isinstance(node.value, ast.Constant)
        and node.value.value is True
    ):
        # returns the start offset of the node and a function that will erase the node.
        # partial function is used to fix certain number of arguments of a function and generate a new function.
        yield ast_start_offset(node), partial(erase_node, node=node)
