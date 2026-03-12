# flask_iris/__init__.py
from .extension import FlaskIris
from iris import PySystemMessage


def current() -> FlaskIris:
    """Return the FlaskIris extension for the active Flask application."""
    return FlaskIris.current()

__all__ = ["FlaskIris", "PySystemMessage", "current"]
