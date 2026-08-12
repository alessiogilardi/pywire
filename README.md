# PyWire — Dependency Injection Container

A minimal Dependency Injection container for Python 3.12+, inspired by Spring's `@Component` and `@Autowired`.

## Overview

PyWire provides a lightweight API for dependency injection:

```python
from pywire import Autowired, component


@component
class Repository:
    client: Autowired["DBClient"]


@component
class DBClient:
    pass
```

## Features

- Component registration via `@component` decorator
- Lazy singleton creation
- Field injection via `Autowired[T]`
- Forward references support
- Circular dependency detection and handling
- Explicit, independent containers
- `BeanDefinition` metadata for registered components

## Installation

```bash
uv pip install -e .
```

## Usage

### Register Components

```python
from pywire import component, Container

@component
class UserRepository:
    pass

@component
class UserService:
    repository: Autowired["UserRepository"]
```

### Create and Use a Container

```python
container = Container()
container.register(UserRepository)
container.register(UserService)

service = container.resolve(UserService)
```

## Architecture

```
pywire/
├── container.py       # Main DI container
├── definitions.py     # BeanDefinition metadata
├── decorators.py      # @component decorator
├── exceptions.py      # Exception hierarchy
├── markers.py         # Autowired[T] marker
└── __init__.py        # Public API
```

## Testing

```bash
uv run pytest
```
