# -*- coding: utf-8 -*-
"""Точка входа MCP-сервера ИИ-агента ПТО (фасад над модулем CAD).

Зачем фасад: MCP-клиенту в конфиге нужна одна стабильная команда, а не путь
внутри пакета. Здесь же собираются настройки из окружения — чтобы конфиг
Claude Desktop / Cursor / VS Code был коротким и переносимым:

    PTO_MCP_BASE_DIR   — рабочая папка проекта (по умолчанию текущая)
    PTO_MCP_AUDIT      — журнал аудита (по умолчанию out/cad/audit.jsonl)
    PTO_MCP_TRANSPORT  — stdio (по умолчанию) или http
    PTO_MCP_HOST       — адрес для http (по умолчанию 0.0.0.0)
    PTO_MCP_PORT       — порт для http (по умолчанию 8765)

Запуск:  python -m pto_agent.mcp_server [--transport stdio|http] [--base-dir .]
         pto-mcp                     (после pip install -e .)
"""
from __future__ import annotations

import os
import sys

from .cad.mcp_server import (PROTOCOL_VERSION, SERVER_INFO, TOOLS,  # noqa: F401
                             _tools_public, handle_rpc, make_ctx, serve_http,
                             serve_stdio)

DEFAULT_BASE_DIR = "."
DEFAULT_AUDIT = "out/cad/audit.jsonl"


def build_argv(argv: list[str] | None = None) -> list[str]:
    """Собрать аргументы CLI: явные флаги важнее переменных окружения."""
    argv = list(sys.argv[1:] if argv is None else argv)
    flags = {a.split("=")[0] for a in argv}

    def add(flag: str, value: str | None) -> None:
        if flag not in flags and value:
            argv.append(flag)      # append, а не += : иначе argv становится локальным
            argv.append(value)

    add("--transport", os.environ.get("PTO_MCP_TRANSPORT"))
    add("--host", os.environ.get("PTO_MCP_HOST"))
    add("--port", os.environ.get("PTO_MCP_PORT"))
    add("--base-dir", os.environ.get("PTO_MCP_BASE_DIR", DEFAULT_BASE_DIR))
    add("--audit", os.environ.get("PTO_MCP_AUDIT", DEFAULT_AUDIT))
    return argv


def tool_names() -> list[str]:
    """Имена инструментов — для проверок и документации."""
    return [t["name"] for t in TOOLS]


def main(argv: list[str] | None = None) -> int:
    from .cad.mcp_server import main as _main
    return _main(build_argv(argv))


if __name__ == "__main__":
    sys.exit(main())
