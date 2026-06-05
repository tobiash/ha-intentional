"""Static structural checks for the async-safety of the integration.

The v0.3.3 hotfix moved ``starter_source.glob()`` from inline
(in async_setup_entry, blocking the event loop) into an
``async_add_executor_job`` wrapper. HA logged ``Detected blocking
call to scandir`` and the subsequent bootstrap timed out waiting
on the tick loop task.

The check below is structural — it parses the integration source
and verifies that every synchronous I/O call (``.glob()``,
``.iterdir()``, ``.scandir()``, ``.listdir()``) inside the
integration is either:
  (a) inside a function passed to ``hass.async_add_executor_job``,
      so it runs in a thread, or
  (b) inside a sync helper at module scope that isn't called from
      an async function (rare; usually only the engine's pure-Python
      code paths).

We do NOT verify behavior at runtime — that's what the integration
tests in test_integration.py do. We just catch the structural bug
class before it ships.

Limitations:
- Heuristic, not sound. A clever person could construct code that
  passes the test but still has async-safety bugs. The point is to
  catch the common, easy-to-introduce mistake (inline ``.glob()``
  in an async function).
- Module-level sync helpers are assumed safe by default because
  they're only called from engine code (which is the bundled
  ``_engine`` subpackage) or from tests. If you add a new top-level
  sync helper that's called from async code in __init__.py, this
  test won't catch it.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "intentional"
INIT_PY = INTEGRATION_DIR / "__init__.py"

# Calls that block the event loop when used on Path objects. These
# all invoke os.scandir / os.listdir / readdir() under the hood.
BLOCKING_ATTRS = {"glob", "iglob", "iterdir", "scandir", "listdir"}


def _is_in_executor_helper(node: ast.Call) -> bool:
    """True if this Call is the first arg of an async_add_executor_job."""
    # Walk up to find a parent that's a Call to async_add_executor_job
    # We don't have a parent map, so we approximate by checking the
    # call's own context. The simplest signal: this call's parent in
    # the source is `hass.async_add_executor_job(...)` where this
    # call is the first positional arg. We do a more thorough check
    # by also accepting "any sync function definition whose body
    # contains the call" if that function is referenced by
    # async_add_executor_job in async_setup_entry.
    return False  # Handled by a different mechanism below


class _ExecutorWrapFinder(ast.NodeVisitor):
    """Find all sync function names that are passed to async_add_executor_job.

    Walks the AST and collects the set of function names that are
    referenced as the first arg of an async_add_executor_job call.
    Those functions are "safe" — they run in a thread, so any
    blocking calls inside them don't block the event loop.
    """

    def __init__(self) -> None:
        self.wrapped_names: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "async_add_executor_job"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            self.wrapped_names.add(node.args[0].id)
        self.generic_visit(node)


class _BlockingCallFinder(ast.NodeVisitor):
    """Find blocking I/O calls that are NOT inside an executor-wrapped helper.

    Tracks whether the current node is inside:
      - a function whose name is in `executor_wrapped_names` (safe)
      - any other function (potentially unsafe if the function is
        called from an async context)
    """

    def __init__(self, executor_wrapped_names: set[str]) -> None:
        self.executor_wrapped_names = executor_wrapped_names
        self.function_stack: list[str] = []
        self.offenders: list[tuple[int, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        # Async functions are the danger zone. Any blocking call
        # directly in an async def is the bug class we're catching.
        # (Calls inside an inner sync helper that we then pass to
        # async_add_executor_job are still safe — see the inner
        # FunctionDef visit below.)
        self.function_stack.append(f"async {node.name}")
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in BLOCKING_ATTRS:
            # The caller is some function. Is it safe?
            # Safe if the *immediate* enclosing function (innermost
            # on the stack) is one that's passed to
            # async_add_executor_job.
            # Module-level (empty stack) is also safe (engine code).
            if not self.function_stack:
                # Module-level call. Not our concern.
                self.generic_visit(node)
                return
            innermost = self.function_stack[-1]
            # Strip the "async " prefix for the lookup
            name_for_lookup = (
                innermost[6:] if innermost.startswith("async ") else innermost
            )
            if name_for_lookup in self.executor_wrapped_names:
                # Inside a sync helper that's wrapped — safe.
                self.generic_visit(node)
                return
            # Otherwise, this is a blocking call in a non-wrapped
            # function. If that function is async, it's the bug.
            if innermost.startswith("async "):
                self.offenders.append(
                    (node.lineno, f".{func.attr}(...) inside {innermost}")
                )
        self.generic_visit(node)


def test_no_blocking_io_in_async_paths() -> None:
    """No .glob()/.iterdir()/.scandir()/.listdir() in async code paths.

    Catches the v0.3.3 hotfix bug: ``starter_source.glob()`` was
    called inline in ``async_setup_entry``, blocking the event loop
    on every integration load. HA logged ``Detected blocking call
    to scandir`` and the bootstrap timed out.

    The fix is to wrap the glob (and the rest of the sync filesystem
    work) in ``hass.async_add_executor_job(...)``. This test verifies
    that any new blocking call is similarly wrapped.
    """
    tree = ast.parse(INIT_PY.read_text())

    # First pass: find all function names passed to
    # async_add_executor_job. Those are "safe" wrappers.
    wrap_finder = _ExecutorWrapFinder()
    wrap_finder.visit(tree)
    wrapped = wrap_finder.wrapped_names

    # Second pass: find blocking calls that are NOT inside a wrapped helper.
    finder = _BlockingCallFinder(wrapped)
    finder.visit(tree)

    assert not finder.offenders, (
        f"Blocking I/O call(s) found in {INIT_PY.name} that are NOT "
        "wrapped in hass.async_add_executor_job(...). Wrap the call "
        "in a sync helper and pass that helper to the executor:\n\n"
        "  def _do_work() -> ...:\n"
        "      ... # sync I/O here is safe\n"
        "  result = await hass.async_add_executor_job(_do_work)\n\n"
        "This is the v0.3.3 hotfix for the blocking-scandir bug. "
        "Offenders:\n"
        + "\n".join(f"  {INIT_PY.name}:{ln}: {desc}" for ln, desc in finder.offenders)
    )
