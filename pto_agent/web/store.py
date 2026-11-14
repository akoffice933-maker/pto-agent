# -*- coding: utf-8 -*-
"""Доступ к состояниям контуров и журналу аудита для экранов.

Экраны не держат состояние в памяти: каждый запрос читает файл, каждая запись
пишется под замком. Так браузер и CLI видят одно и то же, а параллельная запись
не «затирает» проводку.
"""
from __future__ import annotations

import json
import threading
from decimal import Decimal
from pathlib import Path

from ..audit import AuditLog
from .. import closing, ledger, stock, treasury


class Store:
    def __init__(self, root: str | Path, *, data_dir: str | Path | None = None,
                 demo_dir: str | Path | None = None):
        self.root = Path(root)
        repo = Path(__file__).resolve().parents[2]
        self.data_dir = Path(data_dir or repo / "data" / "reference")
        self.demo_dir = Path(demo_dir or repo / "data" / "demo")
        self.lock = threading.RLock()

    # ── пути ──────────────────────────────────────────────────────
    @property
    def stock_path(self) -> Path:
        return self.root / "stock" / "state.json"

    @property
    def treasury_path(self) -> Path:
        return self.root / "treasury" / "state.json"

    @property
    def ledger_path(self) -> Path:
        return self.root / "ledger" / "state.json"

    @property
    def audit_path(self) -> Path:
        return self.root / "audit.jsonl"

    @property
    def acts_path(self) -> Path:
        return self.demo_dir / "acts" / "acts.json"

    @property
    def users_path(self) -> Path:
        return self.data_dir / "users.json"

    @property
    def rbac_path(self) -> Path:
        return self.data_dir / "rbac.json"

    # ── чтение и запись ───────────────────────────────────────────
    @staticmethod
    def _read(path: Path, loader, factory):
        return loader(path) if path.exists() else factory()

    def stock_state(self) -> dict:
        return self._read(self.stock_path, stock.load,
                          lambda: stock.new_state(object_name=""))

    def treasury_state(self) -> dict:
        return self._read(self.treasury_path, treasury.load, treasury.new_state)

    def ledger_state(self) -> dict:
        return self._read(self.ledger_path, ledger.load, ledger.new_state)

    def save_stock(self, state: dict) -> None:
        with self.lock:
            stock.save(state, self.stock_path)

    def save_treasury(self, state: dict) -> None:
        with self.lock:
            treasury.save(state, self.treasury_path)

    def save_ledger(self, state: dict) -> None:
        with self.lock:
            ledger.save(state, self.ledger_path)

    # ── аудит ─────────────────────────────────────────────────────
    @property
    def audit(self) -> AuditLog:
        return AuditLog(self.audit_path)

    def acts(self) -> dict:
        if not self.acts_path.exists():
            return {"acts": [], "note": "реестра актов нет"}
        return json.loads(self.acts_path.read_text(encoding="utf-8"))

    # ── сводка для дашборда ───────────────────────────────────────
    def summary(self, *, period: str, date: str) -> dict:
        stock_state = self.stock_state()
        ledger_state = self.ledger_state()
        treasury_state = self.treasury_state()
        rows = stock.balances(stock_state)
        money = float(treasury.balance_at(treasury_state, date))
        verify = ledger.verify(ledger_state)
        requests = treasury_state.get("requests") or []
        return {
            "date": date,
            "period": period,
            "stock_amount": round(sum(float(row.get("amount") or 0) for row in rows), 2),
            "stock_rows": len(rows),
            "stock_negative": sum(1 for row in rows if row.get("negative")),
            "money": round(money, 2),
            "money_ledger": _balance(ledger_state, "51", date),
            "money_gap": round(money - _balance(ledger_state, "51", date), 2),
            "entries": verify.get("entries", 0),
            "journal_ok": bool(verify.get("ok")),
            "journal_summary": verify.get("summary", ""),
            "requests_total": len(requests),
            "requests_open": sum(1 for row in requests
                                 if row.get("status") not in ("оплачена", "отклонена")),
            "requests_paid": round(sum(float(row.get("paid") or 0) for row in requests), 2),
            "proposals": len(ledger_state.get("proposals") or []),
            "closed": [row.get("period") for row in ledger_state.get("periods") or []
                       if row.get("closed")],
            "mode": ledger_state.get("mode") or ledger.MODE_POST,
        }


def _balance(state: dict, account: str, date: str) -> float:
    """Сальдо счёта на дату — сумма по аналитике: пустой журнал даёт 0, а не ошибку."""
    try:
        rows = closing.balance_of(state, account, date=date)
    except Exception:  # noqa: BLE001 — сводка не должна падать из-за пустого журнала
        return 0.0
    return round(float(sum(rows.values(), Decimal("0"))), 2)
