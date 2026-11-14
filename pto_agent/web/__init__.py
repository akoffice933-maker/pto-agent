# -*- coding: utf-8 -*-
"""Экраны учёта: FastAPI + Jinja2 + HTMX, права по матрице ролей."""
from .app import create_app  # noqa: F401
from .demo import build as build_demo  # noqa: F401

__all__ = ["create_app", "build_demo"]
