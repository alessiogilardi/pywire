# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

PyWire is a minimal Dependency Injection container for Python 3.13, inspired by Spring's
`@Component`/`@Autowired`. It is a small library (no runtime dependencies) living entirely
under `src/pywire/`.

## Commands

Package management is `uv`-based (`uv.lock` present).

```bash
uv sync                # install dependencies (incl. dev group)
uv run pytest          # run the full test suite
uv run pytest tests/test_container.py::test_register_and_resolve   # run a single test
uv run ruff check .    # lint
uv run pyright         # type-check
./scripts/bump-version.sh major|minor|patch   # bump version, commit, and tag (see below)
```

There are no `[project.scripts]` entries — this is a library, not a CLI. Maintainer-only
tooling (versioning) lives in `scripts/`, outside `src/pywire/`, so it never ships as part
of the built package.

### Bumping the version

Run `./scripts/bump-version.sh major|minor|patch` only when the user explicitly asks for a
version bump/release — never run it proactively as part of an unrelated task. Requirements
and behavior:

- The working tree must be clean (`git status --porcelain` empty); the script aborts
  otherwise. Do not stash or commit unrelated changes just to force it through — surface the
  dirty state to the user instead.
- It bumps `pyproject.toml` via `uv version --bump <part>`, re-locks `uv.lock`, commits both
  as `chore: bump version to X`, and creates local tag `vX`. It never pushes.
- After it runs, ask the user before pushing — `git push && git push --tags` is the follow-up
  command, but pushing commits/tags is a shared-state action and needs explicit confirmation
  per this session's operating rules, even though the script itself never pushes.

## Architecture

### Registration & resolution flow

- `Container` (`container.py`) owns a private `_registry: dict[type, BeanDefinition]`.
  Each `Container` instance is an independent scope: the same class registered in two
  different containers produces two different singleton instances.
- `container.register(cls)` stores a `BeanDefinition` (`definitions.py`) and immediately
  calls `self._instrument(cls)` — it does **not** instantiate the class yet.
- `container.resolve(cls)` / `container.get(cls)` (alias) lazily creates the singleton
  on first access and returns the cached instance afterward.

### Field injection mechanism (`Container._instrument`)

This is the core trick of the library and the most important thing to understand before
touching `container.py`:

- `Autowired[T]` (`markers.py`) is `Annotated[T, _AUTOWIRED]`, where `_AUTOWIRED` is a
  private sentinel. Building it on `Annotated` (rather than a custom marker class) means
  static type checkers see the field as plain `T`, while the container still recovers the
  `_AUTOWIRED` tag at runtime via `typing.get_origin`/`get_args`.
- `_instrument()` monkey-patches `__new__` and `__init__` on every registered class:
  - The patched `__new__` writes the freshly created (but not yet `__init__`-ed) instance
    into `BeanDefinition.instance` **before** running `__init__`. This early registration is
    what makes circular dependencies resolvable — see below.
  - The patched `__init__` walks `inspect.get_annotations(cls, eval_str=True)`, resolves each
    `Autowired[...]` field (via `get_origin`/`get_args`; a leftover `ForwardRef` from a string
    forward reference is evaluated against the owning module's globals) to a concrete type,
    and injects the resolved dependency via `self.resolve(field_type)` before calling the
    original `__init__`.
- Circular dependencies (A depends on B, B depends on A) are handled via two instance flags
  set on each object: `_di_initializing` and `_di_initialized`. If `__init__` is re-entered
  on an instance that is already mid-construction (found via the early registry write in
  `__new__`), it returns immediately, leaving the partially-constructed instance to be wired
  up on an outer stack frame. See `tests/test_circular_dependencies.py` for the exact
  scenarios this supports (mutual references, self-reference, forward-ref strings).

### Component decorators (`decorators.py`)

- `component` (and its aliases `service`, `repository`, `agent`, `client` — currently pure
  synonyms with no distinct behavior) always registers a class against a lazily-created
  module-level default container (`get_default_container()`). It takes no container
  argument by design — use `container.register(cls)` directly when an explicit container
  is needed.

### Versioning (`scripts/bump-version.sh`)

- `scripts/bump-version.sh <major|minor|patch>` bumps `pyproject.toml`'s `version` via
  `uv version --bump <part>` (which also re-locks `uv.lock`), then commits both files
  as `chore: bump version to X` and creates a local git tag `vX`. It refuses to run on
  a dirty working tree and never pushes — push explicitly with `git push && git push --tags`.
- Lives in `scripts/` at the repo root, not under `src/pywire/`, so it's maintainer-only
  tooling and never ships inside the built package.

### Module layout

| File | Responsibility |
|---|---|
| `container.py` | `Container`: registry, resolve/register, `__new__`/`__init__` instrumentation |
| `definitions.py` | `BeanDefinition` (registration metadata) and `Scope` enum (only `SINGLETON` is implemented; `PROTOTYPE` is declared but unused) |
| `decorators.py` | `@component` and aliases, global container accessor |
| `markers.py` | `Autowired[T]` (`Annotated[T, _AUTOWIRED]`) |
| `exceptions.py` | `DependencyResolutionError` |

## Conventions to preserve

- All docstrings, comments, and error/exception messages are written in English only.
- `ruff` config (`pyproject.toml`) enables `E, F, I, UP, RUF` rule sets, target `py313`,
  line length 88, first-party import group `pywire`.
