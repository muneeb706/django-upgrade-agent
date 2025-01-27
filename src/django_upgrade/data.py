from __future__ import annotations

import ast
import pkgutil
import re
from collections import defaultdict
from collections.abc import Iterable
from functools import cached_property
from typing import TYPE_CHECKING
from typing import Any
from typing import Callable
from typing import DefaultDict
from typing import TypeVar

from tokenize_rt import Offset
from tokenize_rt import Token

from django_upgrade import fixers


class Settings:
    __slots__ = (
        "target_version",
        "enabled_fixers",
    )

    def __init__(
        self,
        target_version: tuple[int, int],
        only_fixers: set[str] | None = None,
        skip_fixers: set[str] | None = None,
    ) -> None:
        self.target_version = target_version
        self.enabled_fixers = {
            name
            for name in FIXERS
            if (only_fixers is None or name in only_fixers)
            and (skip_fixers is None or name not in skip_fixers)
        }


admin_re = re.compile(r"(\b|_)admin(\b|_)")
commands_re = re.compile(r"(^|[\\/])management[\\/]commands[\\/]")
dunder_init_re = re.compile(r"(^|[\\/])__init__\.py$")
migrations_re = re.compile(r"(^|[\\/])migrations([\\/])")
settings_re = re.compile(r"(\b|_)settings(\b|_)")
test_re = re.compile(r"(\b|_)tests?(\b|_)")
models_re = re.compile(r"(^|[\\/])models([\\/]|\.py)")


class State:
    __slots__ = ("settings", "filename", "from_imports", "__weakref__", "__dict__")

    def __init__(
        self,
        settings: Settings,
        filename: str,
        from_imports: DefaultDict[str, set[str]],
    ) -> None:
        self.settings = settings
        self.filename = filename
        self.from_imports = from_imports

    @cached_property
    def looks_like_admin_file(self) -> bool:
        return admin_re.search(self.filename) is not None

    @cached_property
    def looks_like_command_file(self) -> bool:
        return commands_re.search(self.filename) is not None

    @cached_property
    def looks_like_dunder_init_file(self) -> bool:
        return dunder_init_re.search(self.filename) is not None

    @cached_property
    def looks_like_migrations_file(self) -> bool:
        return migrations_re.search(self.filename) is not None

    @cached_property
    def looks_like_settings_file(self) -> bool:
        return settings_re.search(self.filename) is not None

    @cached_property
    def looks_like_test_file(self) -> bool:
        return test_re.search(self.filename) is not None

    @cached_property
    def looks_like_models_file(self) -> bool:
        return models_re.search(self.filename) is not None


AST_T = TypeVar("AST_T", bound=ast.AST)
TokenFunc = Callable[[list[Token], int], None]
ASTFunc = Callable[
    [State, AST_T, tuple[ast.AST, ...]], Iterable[tuple[Offset, TokenFunc]]
]

if TYPE_CHECKING:  # pragma: no cover
    from typing import Protocol
else:
    Protocol = object


class ASTCallbackMapping(Protocol):
    def __getitem__(self, tp: type[AST_T]) -> list[ASTFunc[AST_T]]:  # pragma: no cover
        ...

    def items(self) -> Iterable[tuple[Any, Any]]:  # pragma: no cover
        ...


def visit(
    tree: ast.Module,
    settings: Settings,
    filename: str,
) -> dict[Offset, list[TokenFunc]]:
    """
    Traverses the abstract syntax tree (AST) and applies transformation functions.

    This function visits each node in the given AST, applies the appropriate
    transformation functions based on the node type, and collects the results.
    It also handles specific import statements related to Django and unittest.

    Args:
        tree (ast.Module): The root node of the AST to traverse.
        settings (Settings): The settings object containing configuration for the visit.
        filename (str): The name of the file being processed.

    Returns:
        dict[Offset, list[TokenFunc]]: A dictionary where the keys are offsets in the
                                       source code and the values are lists of token
                                       transformation functions to be applied at those
                                       offsets.

    Notes:
        - The function initializes a state object to keep track of settings, filename,
          and import statements.
        - It retrieves the appropriate AST transformation functions based on the node
          types.
        - It traverses the AST in a depth-first manner, applying transformation functions
          and collecting the results.
        - It specifically handles 'from' import statements for Django and unittest modules,
          updating the state with the imported names.

    Example:
        >>> source_code = "import django\nprint('Hello, world!')"
        >>> tree = ast.parse(source_code)
        >>> settings = Settings(target_version=(3, 2))
        >>> filename = "example.py"
        >>> result = visit(tree, settings, filename)
        >>> print(result)
        defaultdict(<class 'list'>, {Offset(...): [<TokenFunc ...>]})
    """
    state = State(
        settings=settings,
        filename=filename,
        from_imports=defaultdict(set),
    )
    ast_funcs = get_ast_funcs(state, settings)

    # nodes is a list of tuples where each tuple contains an AST node and its parent nodes.
    # list is initialized with the root node of the AST and an empty tuple representing no parent nodes.
    nodes: list[tuple[ast.AST, tuple[ast.AST, ...]]] = [(tree, ())]
    ret = defaultdict(list)
    while nodes:
        node, parents = nodes.pop()

        # This loop iterates over the results of applying the transformation function (ast_func)
        # to the current node and its parent nodes.
        # The transformation function returns an iterable of tuples, where each tuple contains
        # an offset (represents a position in the source code) and a token function 
        # (that performs specific transformation) at that offset.

        # Why transofrmation function is applied to the current node parent nodes?
        # Ans: It provides the necessary context for more accurate, context aware and robust transformations.
        for ast_func in ast_funcs[type(node)]:
            for offset, token_func in ast_func(state, node, parents):
                ret[offset].append(token_func)

        # track specific imports from django and unittest modules.
        # Ensures only top level imports are considered, exlcuding renmaed imports and wildcard imports.
        if (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and (
                node.module is not None
                and (
                    node.module.startswith("django.")
                    or node.module in ("django", "unittest")
                )
            )
        ):
            state.from_imports[node.module].update(
                name.name
                for name in node.names
                if name.asname is None and name.name != "*"
            )

        # traversing the child nodes of the current node and adding them to the nodes list,
        # along with the parent nodes.
        subparents = parents + (node,)
        for name in reversed(node._fields):
            value = getattr(node, name)

            if isinstance(value, ast.AST):
                nodes.append((value, subparents))
            elif isinstance(value, list):
                for subvalue in reversed(value):
                    if isinstance(subvalue, ast.AST):
                        nodes.append((subvalue, subparents))
    return ret


class Fixer:
    """
    Represents a code transformation fixer.

    The Fixer class encapsulates the logic for a specific code transformation, including
    the conditions under which it should be applied and the functions that perform the
    transformation on specific AST node types.

    Attributes:
        name (str): The name of the fixer, derived from the module name.
        min_version (tuple[int, int]): The minimum version of the target environment
                                       for which this fixer is applicable.
        ast_funcs (ASTCallbackMapping): A dictionary where the keys are AST node types
                                        and the values are lists of transformation functions
                                        to be applied to nodes of those types.
        condition (Callable[[State], bool] | None): An optional callable that takes a State
                                                    object and returns a boolean indicating
                                                    whether the fixer should be applied.

    Methods:
        __init__(module_name: str, min_version: tuple[int, int], condition: Callable[[State], bool] | None = None):
            Initializes a new Fixer instance with the given module name, minimum version,
            and optional condition.
        register(type_: type[AST_T]) -> Callable[[ASTFunc[AST_T]], ASTFunc[AST_T]]:
            A decorator method that registers a transformation function for a specific AST node type.

    Example:
        >>> fixer = Fixer("my_module.my_fixer", (3, 2))
        >>> @fixer.register(ast.FunctionDef)
        ... def transform_function_def(state, node, parents):
        ...     # Transformation logic here
        ...     pass
    """
    # __slots__ declaration in a Python class is used to explicitly 
    # declare data members (attributes) and to prevent the creation of a 
    # __dict__ for each instance of the class. This can lead to memory savings and potentially
    # faster attribute access.
    __slots__ = (
        "name",
        "min_version",
        "ast_funcs",
        "condition",
    )

    def __init__(
        self,
        module_name: str,
        min_version: tuple[int, int],
        condition: Callable[[State], bool] | None = None,
    ) -> None:
        self.name = module_name.rpartition(".")[2]
        self.min_version = min_version
        self.ast_funcs: ASTCallbackMapping = defaultdict(list)
        self.condition = condition

        FIXERS[self.name] = self

    def register(
        self, type_: type[AST_T]
    ) -> Callable[[ASTFunc[AST_T]], ASTFunc[AST_T]]:
        def decorator(func: ASTFunc[AST_T]) -> ASTFunc[AST_T]:
            self.ast_funcs[type_].append(func)
            return func

        return decorator


FIXERS: dict[str, Fixer] = {}


def _import_fixers() -> None:
    # https://github.com/python/mypy/issues/1422
    fixers_path: str = fixers.__path__  # type: ignore [assignment]
    mod_infos = pkgutil.walk_packages(fixers_path, f"{fixers.__name__}.")
    for _, name, _ in mod_infos:
        __import__(name, fromlist=["_trash"])


_import_fixers()


def get_ast_funcs(state: State, settings: Settings) -> ASTCallbackMapping:
    """
    Retrieves a mapping of AST node types to their corresponding transformation functions.

    This function iterates over all available fixers, checks if they are enabled and applicable
    based on the target version and any additional conditions, and collects their AST transformation
    functions into a dictionary.

    Args:
        state (State): The current state object containing settings and other contextual information.
        settings (Settings): The settings object containing configuration for the fixers.

    Returns:
        ASTCallbackMapping: A dictionary where the keys are AST node types and the values are lists
                            of transformation functions to be applied to nodes of those types.

    Notes:
        - The function initializes an empty defaultdict to store the AST transformation functions.
        - It iterates over all fixers defined in the FIXERS dictionary.
        - For each fixer, it checks if the fixer is enabled in the settings.
        - It further checks if the fixer's minimum version is less than or equal to the target version
          and if any additional conditions specified by the fixer are met.
        - If the fixer is applicable, its transformation functions are added to the corresponding
          node types in the defaultdict.

    Example:
        >>> state = State(settings=Settings(target_version=(3, 2)))
        >>> settings = Settings(enabled_fixers={'fixer1', 'fixer2'})
        >>> ast_funcs = get_ast_funcs(state, settings)
        >>> print(ast_funcs)
        defaultdict(<class 'list'>, {<class '_ast.FunctionDef'>: [<function ...>]})
    """
    ast_funcs: ASTCallbackMapping = defaultdict(list)
    for fixer in FIXERS.values():
        if fixer.name not in settings.enabled_fixers:
            continue
        if fixer.min_version <= state.settings.target_version and (
            fixer.condition is None or fixer.condition(state)
        ):
            for type_, type_funcs in fixer.ast_funcs.items():
                ast_funcs[type_].extend(type_funcs)
    return ast_funcs
