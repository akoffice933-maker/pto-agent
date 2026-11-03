#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Установка MCP-обвязки: подключить ИИ-агента ПТО к Claude Desktop, Cursor или VS Code.

Что делает:
  * собирает конфиг MCP-сервера с правильными путями (python + папка проекта);
  * по умолчанию печатает его — можно вставить руками;
  * с `--write` вливает в конфиг клиента, сохраняя уже подключённые серверы;
  * делает резервную копию изменённого файла.

Примеры:
  python3 scripts/install_mcp.py                            # печать конфига (stdio)
  python3 scripts/install_mcp.py --client cursor --write    # влить в ~/.cursor/mcp.json
  python3 scripts/install_mcp.py --client claude --write
  python3 scripts/install_mcp.py --http --print             # вариант через http

Имя сервера по умолчанию `pto-cad`; `--name` меняет его (если подключено несколько копий).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NAME = "pto-cad"

CLIENT_PATHS = {
    # Claude Desktop
    "claude": {
        "linux": "~/.config/Claude/claude_desktop_config.json",
        "darwin": "~/Library/Application Support/Claude/claude_desktop_config.json",
        "win32": "%APPDATA%/Claude/claude_desktop_config.json",
    },
    # Cursor (глобальный конфиг MCP)
    "cursor": {
        "linux": "~/.cursor/mcp.json",
        "darwin": "~/.cursor/mcp.json",
        "win32": "%USERPROFILE%/.cursor/mcp.json",
    },
    # VS Code (проектный файл .vscode/mcp.json)
    "vscode": {
        "linux": "{project}/.vscode/mcp.json",
        "darwin": "{project}/.vscode/mcp.json",
        "win32": "{project}/.vscode/mcp.json",
    },
}


def client_config_path(client: str, project: Path) -> Path:
    raw = CLIENT_PATHS[client][sys.platform if sys.platform != "linux" else "linux"]
    raw = raw.replace("{project}", str(project))
    raw = os.path.expandvars(os.path.expanduser(raw))
    return Path(raw)


def build_config(name: str, project: Path, http: bool = False,
                 python: str | None = None, port: int = 8765,
                 client: str = "claude") -> dict:
    """Конфиг под конкретный клиент: у VS Code другой ключ и обязателен type."""
    key = "servers" if client == "vscode" else "mcpServers"
    if http:
        server = {"type": "http", "url": f"http://localhost:{port}/mcp",
                  "headers": {"mcp-protocol-version": "2025-06-18"}}
    else:
        server = {"command": python or sys.executable,
                  "args": ["-m", "pto_agent.mcp_server", "--base-dir", str(project)],
                  "env": {"PTO_MCP_BASE_DIR": str(project),
                          "PTO_MCP_AUDIT": "out/cad/audit.jsonl"}}
        if client == "vscode":
            server = {"type": "stdio", **server}
    return {key: {name: server}}


def merge(existing: dict, new: dict) -> dict:
    """Добавить сервер, не затирая остальные: чужие MCP-серверы остаются на месте."""
    key = "servers" if "servers" in new else "mcpServers"
    merged = dict(existing)
    servers = dict(merged.get(key) or {})
    servers.update(new.get(key) or {})
    merged[key] = servers
    return merged


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Подключить MCP-сервер ПТО к клиенту")
    ap.add_argument("--client", choices=sorted(CLIENT_PATHS), default="claude",
                    help="куда устанавливать (по умолчанию claude)")
    ap.add_argument("--name", default=DEFAULT_NAME, help="имя сервера в конфиге")
    ap.add_argument("--project", default=str(ROOT), help="папка проекта (base-dir)")
    ap.add_argument("--python", default=None, help="интерпретатор для запуска сервера")
    ap.add_argument("--http", action="store_true", help="конфиг для http-транспорта")
    ap.add_argument("--port", type=int, default=8765, help="порт для --http")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="напечатать конфиг (по умолчанию, если нет --write)")
    ap.add_argument("--write", action="store_true", help="записать в конфиг клиента")
    args = ap.parse_args(argv)

    project = Path(args.project).resolve()
    config = build_config(args.name, project, http=args.http,
                          python=args.python, port=args.port, client=args.client)
    text = json.dumps(config, ensure_ascii=False, indent=2)

    if not args.write:
        print(text)
        print(f"\n# клиент: {args.client} · файл: {client_config_path(args.client, project)}",
              file=sys.stderr)
        return 0

    path = client_config_path(args.client, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        backup = path.with_suffix(path.suffix + f".bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, backup)
        print(f"резервная копия: {backup}")
        merged = merge(existing, config)
    else:
        merged = config

    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"записано: {path}")
    key = "servers" if "servers" in merged else "mcpServers"
    print(f"сервер «{args.name}»: {json.dumps(merged[key][args.name], ensure_ascii=False)}")
    print("перезапустите клиент, чтобы он увидел инструменты")
    return 0


if __name__ == "__main__":
    sys.exit(main())
