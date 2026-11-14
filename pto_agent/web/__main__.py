# -*- coding: utf-8 -*-
"""`python3 -m pto_agent.web` — поднять экраны вручную."""
import argparse

import uvicorn

from .app import create_app
from .demo import build


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Экраны учёта ПТО")
    parser.add_argument("--root", default="out", help="каталог состояний контуров")
    parser.add_argument("--demo", action="store_true",
                        help="собрать демо-состояния перед запуском")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if args.demo:
        report = build(args.root)
        print("демо собрано:", report)
    uvicorn.run(create_app(root=args.root), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
