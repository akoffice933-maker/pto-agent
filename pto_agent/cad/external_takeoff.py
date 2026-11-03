# -*- coding: utf-8 -*-
"""Обмер чужим MCP-сервером → наша схема позиций (`takeoff`) без импорта чужого кода.

Зачем отдельный модуль
----------------------
Свой обмер умеет мерить **наш** DXF и **наш** скан. Чужие форматы (PDF, экспорт из Revit,
IFC «как есть») — не наша сила, и писать PDF-обмер значит месяц дублировать то, что уже есть
(opentakeoff, estimaite-mcp и т.п.). Но у этих проектов лицензии AGPL-3.0 / MIT — то есть их код
внутрь MIT-пакета не переносится.

Поэтому: чужой обмер остаётся **отдельным процессом**, а мы говорим с ним по MCP (JSON-RPC).
Ни одного `import` из их репозитория: граница проходит по проводу.

Что даёт результат
------------------
Второй, независимый источник объёмов. Расхождение «наш DXF ↔ их PDF-обмер» — это не спор
с подрядчиком, а проверка исходных данных **до** выпуска: где в смете объём, которого нет
на чертеже, и наоборот.

Правило, которое модуль соблюдает
---------------------------------
Позиция без объёма остаётся **не оценённой** (`qty = None`), а не нулевой: «не оценено ≠ 0»
зафиксировано в документе 08. Ноль в смете — это утверждение, а не отсутствие данных.
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from ..parsers import norm_text, norm_unit

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT = 120.0
# лицензии, при которых обмер обязан оставаться отдельным сервисом
AGPL_LIKE = {"AGPL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later",
             "GPL-3.0", "GPL-2.0", "GPL-3.0-only"}

CODE_KEYS = ("code", "cost_code", "csi", "item_code", "position", "id", "шифр", "расценка")
NAME_KEYS = ("name", "description", "title", "item", "work", "наименование")
QTY_KEYS = ("qty", "quantity", "amount", "value", "volume", "объём", "объем", "количество")
UNIT_KEYS = ("unit", "uom", "units", "measure", "ед", "единица")
LIST_KEYS = ("items", "positions", "lines", "takeoff", "quantities", "rooms", "data", "result")


class ExternalTakeoffError(RuntimeError):
    """Чужой сервер ответил ошибкой или ответ не разобрать."""


class ExternalLicenseError(ExternalTakeoffError):
    """Лицензия внешнего обмера требует режима «отдельный сервис»."""


# ─────────────────────────── MCP-клиент ──────────────────────────────────
class MCPClient:
    """Минимальный MCP-клиент: initialize → tools/list → tools/call.

    Поддерживает два транспорта:
      * `stdio` — дочерний процесс, по строке JSON на сообщение (как `pto-mcp --transport stdio`);
      * `http`  — POST JSON-RPC на `/mcp`, ответ либо JSON, либо SSE (`data: {...}`).
    """

    def __init__(self, transport: str = "stdio", command: list[str] | None = None,
                 url: str | None = None, timeout: float = DEFAULT_TIMEOUT,
                 env: dict | None = None):
        if transport not in ("stdio", "http"):
            raise ExternalTakeoffError(f"неизвестный транспорт: {transport}")
        if transport == "stdio" and not command:
            raise ExternalTakeoffError("для stdio нужна команда запуска сервера")
        if transport == "http" and not url:
            raise ExternalTakeoffError("для http нужен url")
        self.transport = transport
        self.command = [str(c) for c in (command or [])]
        self.url = url
        self.timeout = float(timeout)
        self.env = env
        self.protocol_version: str | None = None
        self.server_info: dict | None = None
        self._proc: subprocess.Popen | None = None
        self._id = 0

    # ── lifecycle ──
    def __enter__(self) -> "MCPClient":
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def start(self) -> "MCPClient":
        if self.transport == "stdio" and self._proc is None:
            self._proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
                env=self.env)
        return self

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        finally:
            self._proc = None

    # ── protocol ──
    def initialize(self) -> dict:
        res = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "pto-agent-external-takeoff", "version": "0.1"},
        })
        self.protocol_version = res.get("protocolVersion")
        self.server_info = res.get("serverInfo") or {}
        # нотификация — по спецификации после инициализации
        self._notify("notifications/initialized")
        return res

    def list_tools(self) -> list[dict]:
        return list(self._request("tools/list", {}).get("tools") or [])

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        res = self._request("tools/call", {"name": name, "arguments": arguments or {}})
        if res.get("isError"):
            raise ExternalTakeoffError(f"инструмент {name} вернул ошибку: {_content_text(res)[:400]}")
        return res

    # ── transport ──
    def _request(self, method: str, params: dict) -> dict:
        self._id += 1
        rid = self._id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        if self.transport == "http":
            raw = self._post(msg)
            resp = _parse_http_body(raw)
        else:
            resp = self._stdio_roundtrip(msg, rid)
        if resp.get("error"):
            err = resp["error"] or {}
            raise ExternalTakeoffError(
                f"{method}: код {err.get('code')} — {err.get('message')}")
        return resp.get("result") or {}

    def _notify(self, method: str) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        try:
            if self.transport == "http":
                self._post(msg)
            elif self._proc is not None and self._proc.stdin is not None:
                self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
        except (BrokenPipeError, OSError, ExternalTakeoffError, ValueError):
            pass   # нотификация не критична: сервер мог её не ждать или не знать такой метод

    def _stdio_roundtrip(self, msg: dict, rid: int) -> dict:
        self.start()
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise ExternalTakeoffError("не удалось запустить внешний MCP-сервер")
        try:
            self._proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise ExternalTakeoffError(f"сервер закрыл stdin: {e}") from e
        while True:
            line = self._proc.stdout.readline()
            if not line:
                err = (self._proc.stderr.read() or "")[-500:] if self._proc.stderr else ""
                raise ExternalTakeoffError(f"сервер завершил вывод без ответа. stderr: {err}")
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue          # чужой сервер может писать логи в stdout — пропускаем
            if resp.get("id") == rid or ("error" in resp and "id" not in resp):
                return resp

    def _post(self, msg: dict) -> bytes:
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, method="POST", headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol_version or PROTOCOL_VERSION,
            "User-Agent": "pto-agent-external-takeoff/0.1",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise ExternalTakeoffError(f"HTTP {e.code} от {self.url}: {e.read()[:300]!r}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise ExternalTakeoffError(f"внешний сервер недоступен ({self.url}): {e}") from e


def _parse_http_body(raw: bytes) -> dict:
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return {}
    if text.startswith("{"):
        return json.loads(text)
    # SSE: строки вида "event: message" / "data: {...}"
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk.startswith("{"):
                return json.loads(chunk)
    raise ExternalTakeoffError(f"не удалось разобрать ответ сервера: {text[:200]!r}")


def _content_text(result: dict) -> str:
    parts = []
    for c in result.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(str(c.get("text") or ""))
        elif isinstance(c, str):
            parts.append(c)
    if not parts and isinstance(result.get("structuredContent"), (dict, list)):
        return json.dumps(result["structuredContent"], ensure_ascii=False)
    return "\n".join(parts)


def payload_from_result(result: dict):
    """Результат `tools/call` → структура: JSON из текста, structuredContent или строка."""
    if isinstance(result.get("structuredContent"), (dict, list)):
        return result["structuredContent"]
    text = _content_text(result).strip()
    if not text:
        raise ExternalTakeoffError("инструмент вернул пустой ответ")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"items": [], "raw_text": text}


# ─────────────────────────── конфиг ──────────────────────────────────────
REQUIRED_KEYS = ("name", "transport", "license", "tool")


def load_config(path: str | Path) -> dict:
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml
        cfg = yaml.safe_load(raw) or {}
    else:
        cfg = json.loads(raw)
    if "external_takeoff" in cfg:      # допускаем файл с общим корнем
        cfg = cfg["external_takeoff"]
    return cfg


def validate_config(cfg: dict, ack_separate_service: bool = False) -> dict:
    """Проверка конфига: обязательные поля, транспорт, лицензионный режим."""
    if not isinstance(cfg, dict):
        raise ExternalTakeoffError("конфиг должен быть объектом (JSON/YAML)")
    missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
    if missing:
        raise ExternalTakeoffError(f"в конфиге нет полей: {', '.join(missing)}")
    transport = cfg["transport"]
    if transport == "stdio" and not cfg.get("command"):
        raise ExternalTakeoffError("transport=stdio требует command (список: программа + аргументы)")
    if transport == "http" and not cfg.get("url"):
        raise ExternalTakeoffError("transport=http требует url")
    if transport not in ("stdio", "http"):
        raise ExternalTakeoffError(f"неизвестный транспорт: {transport}")
    license_ = str(cfg["license"])
    cfg["separate_service"] = license_ in AGPL_LIKE
    if cfg["separate_service"] and not (ack_separate_service or cfg.get("separate_service_ack")):
        raise ExternalLicenseError(
            f"{cfg['name']}: лицензия {license_} — код нельзя встраивать в MIT-проект. "
            f"Допустим только режим «отдельный сервис»: подтвердите флагом "
            f"--ack-separate-service (или separate_service_ack: true в конфиге).")
    return cfg


# ─────────────────────── нормализация позиций ────────────────────────────
def payload_items(payload, depth: int = 0) -> list:
    """Найти список позиций в ответе: items / positions / lines / голый список.

    Чужие серверы кладут его на разную глубину (`{"data": {"lines": [...]}}`), поэтому
    поиск рекурсивный, но с ограничением — иначе на большом ответе можно уйти вглубь.
    """
    if depth > 4:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)] or payload
    if isinstance(payload, dict):
        for key in LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                found = payload_items(value, depth + 1)
                if found:
                    return found
        for value in payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
            if isinstance(value, dict):
                found = payload_items(value, depth + 1)
                if found:
                    return found
    return []


def _first(raw: dict, keys) -> object:
    for k in keys:
        if k in raw and raw[k] not in (None, ""):
            return raw[k]
        for rk, rv in raw.items():      # регистронезависимо, включая вложенные словари
            if isinstance(rv, dict) and str(rk).lower() in keys:
                return _first(rv, keys)
            if str(rk).lower() == k and rv not in (None, ""):
                return rv
    return None


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if v > 0 else None
    s = str(value).replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def normalize_item(raw: dict, source: str = "external") -> dict:
    qty = _num(_first(raw, QTY_KEYS))
    item = {
        "code": (_first(raw, CODE_KEYS) or None),
        "name": str(_first(raw, NAME_KEYS) or "").strip(),
        "qty": qty,
        "qty_raw": _first(raw, QTY_KEYS),
        "unit": norm_unit(_first(raw, UNIT_KEYS) or ""),
        "source": source,
    }
    if isinstance(item["code"], str):
        item["code"] = item["code"].strip() or None
    # объём неизвестен → «не оценено», а не ноль
    item["estimated"] = qty is not None
    return item


def normalize(payload, source: str = "external") -> dict:
    """Ответ чужого обмера → наши позиции + предупреждения (без тихой подмены нулями)."""
    raw_items = payload_items(payload)
    positions, warnings = [], []
    for i, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            warnings.append({"type": "ITEM_NOT_OBJECT", "index": i, "value": str(raw)[:120]})
            continue
        item = normalize_item(raw, source)
        if not item["code"] and not item["name"]:
            warnings.append({"type": "ITEM_WITHOUT_CODE_AND_NAME", "index": i})
            continue
        if not item["estimated"]:
            warnings.append({"type": "QTY_NOT_ESTIMATED", "index": i,
                             "code": item["code"], "name": item["name"],
                             "message": "объём не передан — позиция остаётся не оценённой"})
        if not item["unit"]:
            warnings.append({"type": "UNIT_MISSING", "index": i,
                             "code": item["code"], "name": item["name"]})
        positions.append(item)
    return {
        "source": source,
        "positions": positions,
        "count": len(positions),
        "estimated": sum(1 for p in positions if p["estimated"]),
        "not_estimated": sum(1 for p in positions if not p["estimated"]),
        "warnings": warnings,
    }


# ───────────────────── сверка с нашим обмером/сметой ─────────────────────
def align(external_positions: list, local_positions: list,
          pct: float = 5.0) -> dict:
    """Сверка «чужой обмер ↔ наш обмер/смета»: где расходятся объёмы и позиции.

    `local_positions`: [{code,name,unit,qty}] — из `cad_takeoff` или из `summary.json`
    (там объём лежит в `qty_journal`).
    """
    def key(item):
        return (item.get("code") or "").strip(), norm_text(item.get("name") or "")

    ext_by_code = {p["code"]: p for p in external_positions if p.get("code")}
    ext_by_name = {norm_text(p["name"]): p for p in external_positions if p.get("name")}
    used_ext: set[int] = set()
    matched, issues = [], []

    for loc in local_positions:
        code = (loc.get("code") or "").strip()
        name_key = norm_text(loc.get("name") or "")
        ext = ext_by_code.get(code) if code else None
        kind = "code"
        if ext is None and name_key:
            ext = ext_by_name.get(name_key)
            kind = "name"
        local_qty = _num(loc.get("qty") if loc.get("qty") is not None else loc.get("qty_journal"))
        if ext is None:
            if local_qty:
                issues.append({
                    "type": "LOCAL_ONLY", "severity": "major",
                    "code": code or None, "name": loc.get("name"),
                    "local_qty": local_qty, "unit": norm_unit(loc.get("unit") or ""),
                    "message": "позиция есть в нашей смете/схеме, но не подтверждена внешним обмером",
                })
            continue
        used_ext.add(id(ext))
        ext_qty = ext.get("qty")
        if ext_qty is None:
            issues.append({
                "type": "EXTERNAL_NOT_ESTIMATED", "severity": "warning",
                "code": code or None, "name": loc.get("name"),
                "local_qty": local_qty, "unit": ext.get("unit"),
                "message": "внешний обмер не дал объём — сравнение невозможно, остаётся «не оценено»",
            })
            continue
        if ext.get("unit") and local_qty is not None and norm_unit(loc.get("unit") or "") \
                and ext["unit"] != norm_unit(loc["unit"]):
            issues.append({
                "type": "UNIT_MISMATCH", "severity": "critical",
                "code": code or None, "name": loc.get("name"),
                "external_unit": ext["unit"], "local_unit": norm_unit(loc["unit"]),
                "message": f"единицы не совпадают: {ext['unit']} против {norm_unit(loc['unit'])}",
            })
            continue
        delta_pct = round(100 * (ext_qty - local_qty) / local_qty, 2) if local_qty else None
        record = {"code": code or None, "name": loc.get("name"), "match": kind,
                  "external_qty": ext_qty, "local_qty": local_qty, "unit": ext.get("unit") or
                  norm_unit(loc.get("unit") or ""), "delta_pct": delta_pct}
        matched.append(record)
        if delta_pct is not None and abs(delta_pct) > pct:
            issues.append({
                "type": "QTY_DELTA", "severity": "critical" if abs(delta_pct) > 15 else "major",
                "code": code or None, "name": loc.get("name"),
                "external_qty": ext_qty, "local_qty": local_qty,
                "delta_pct": delta_pct, "threshold_pct": pct,
                "message": f"внешний обмер {ext_qty:g} {record['unit']} против "
                           f"наших {local_qty:g} ({delta_pct:+.1f} %)",
            })

    for ext in external_positions:
        if id(ext) in used_ext:
            continue
        issues.append({
            "type": "EXTERNAL_ONLY", "severity": "major" if ext.get("qty") else "warning",
            "code": ext.get("code"), "name": ext.get("name"),
            "external_qty": ext.get("qty"), "unit": ext.get("unit"),
            "message": "внешний обмер нашёл работу, которой нет в нашей смете/схеме",
        })

    return {
        "external_positions": len(external_positions),
        "local_positions": len(local_positions),
        "matched": matched,
        "issues": issues,
        "critical": sum(1 for i in issues if i["severity"] == "critical"),
        "tolerance_pct": pct,
    }


# ─────────────────────────── прогон ──────────────────────────────────────
def run_external_takeoff(cfg: dict, pdf_path: str | Path, *,
                         ack_separate_service: bool = False,
                         timeout: float | None = None) -> dict:
    """Вызвать чужой MCP-обмер и вернуть нормализованный результат (без записи на диск)."""
    cfg = validate_config(dict(cfg), ack_separate_service=ack_separate_service)
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise ExternalTakeoffError(f"нет файла для обмера: {pdf_path}")

    tool = cfg["tool"]
    arguments = {}
    for k, v in (cfg.get("arguments") or {}).items():
        arguments[k] = v.format(pdf=str(pdf_path), pdf_path=str(pdf_path)) \
            if isinstance(v, str) else v
    if not arguments:      # самый частый случай: единственный параметр — путь к PDF
        arguments = {"pdf_path": str(pdf_path)}

    report = {
        "source": cfg["name"],
        "license": cfg["license"],
        "separate_service": bool(cfg.get("separate_service")),
        "transport": cfg["transport"],
        "tool": tool,
        "file": str(pdf_path),
        "ok": False,
        "positions": [],
        "warnings": [],
    }
    client = MCPClient(transport=cfg["transport"], command=cfg.get("command"),
                       url=cfg.get("url"), timeout=timeout or cfg.get("timeout_s", DEFAULT_TIMEOUT))
    try:
        with client:
            init = client.initialize()
            report["server"] = client.server_info or {}
            report["protocol"] = client.protocol_version or init.get("protocolVersion")
            tools = [t.get("name") for t in client.list_tools()]
            report["tools"] = tools
            if tool not in tools:
                raise ExternalTakeoffError(
                    f"у сервера нет инструмента {tool!r}; доступны: {', '.join(map(str, tools)) or '—'}")
            result = client.call_tool(tool, arguments)
    except ExternalTakeoffError:
        raise
    except Exception as e:  # noqa: BLE001 — наружу отдаём один тип ошибки
        raise ExternalTakeoffError(f"сбой вызова внешнего обмера: {type(e).__name__}: {e}") from e

    payload = payload_from_result(result)
    normalized = normalize(payload, source=cfg["name"])
    report.update({
        "ok": True,
        "positions": normalized["positions"],
        "warnings": normalized["warnings"],
        "count": normalized["count"],
        "estimated": normalized["estimated"],
        "not_estimated": normalized["not_estimated"],
    })
    return report


def local_positions_from_dxf(drawing) -> list:
    """Позиции нашего обмера (блоки с PTO_*) в форме, пригодной для `align`."""
    from .takeoff import takeoff
    return [{"code": i["code"], "name": i["name"], "unit": i["unit"],
             "qty": i["qty"], "handle": i.get("handle")} for i in takeoff(drawing)]


def local_positions_from_summary(summary: dict) -> list:
    """Позиции сметы/журнала из `out/summary.json`."""
    out = []
    for line in summary.get("lines") or []:
        out.append({"code": line.get("code"), "name": line.get("name"),
                    "unit": line.get("unit"), "qty": line.get("qty_journal")})
    return out


def to_markdown(report: dict, alignment: dict | None = None) -> str:
    lines = [f"# Внешний обмер: {report['source']}", ""]
    lines.append(f"- файл: `{report['file']}`")
    lines.append(f"- транспорт: {report['transport']} · протокол: {report.get('protocol') or '—'}")
    lines.append(f"- лицензия: {report['license']}"
                 + (" (отдельный сервис, код не копируется)" if report.get("separate_service") else ""))
    lines.append(f"- инструмент: `{report['tool']}` · позиций: {report.get('count', 0)} · "
                 f"с объёмом: {report.get('estimated', 0)} · "
                 f"не оценено: {report.get('not_estimated', 0)}")
    positions = report.get("positions") or []
    if positions:
        lines += ["", "| шифр | наименование | объём | ед. |", "|---|---|---|---|"]
        for p in positions:
            qty = f"{p['qty']:g}" if p.get("qty") is not None else "не оценено"
            lines.append(f"| {p.get('code') or '—'} | {p.get('name') or '—'} | {qty} | "
                         f"{p.get('unit') or '—'} |")
    if alignment:
        lines += ["", f"## Сверка с нашим обмером (±{alignment['tolerance_pct']:g} %)", ""]
        lines.append(f"- совпало по шифру/наименованию: {len(alignment['matched'])} "
                     f"из {alignment['local_positions']}")
        lines.append(f"- расхождений: {len(alignment['issues'])} "
                     f"(критичных: {alignment['critical']})")
        if alignment["issues"]:
            lines += ["", "| тип | шифр | сообщение |", "|---|---|---|"]
            for i in alignment["issues"]:
                lines.append(f"| {i['type']} | {i.get('code') or '—'} | {i['message']} |")
    warnings = report.get("warnings") or []
    if warnings:
        lines += ["", f"Предупреждения: {len(warnings)}"]
        for w in warnings[:20]:
            lines.append(f"- {w['type']}: {w.get('code') or w.get('index')} "
                         f"{w.get('message') or ''}".rstrip())
    lines += ["", "— «не оценено» означает отсутствие данных, а не ноль."]
    return "\n".join(lines) + "\n"


def write_report(report: dict, alignment: dict | None, out_dir: str | Path) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = dict(report)
    if alignment is not None:
        payload["alignment"] = alignment
    json_path = out / "external_takeoff.json"
    md_path = out / "external_takeoff.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    md_path.write_text(to_markdown(report, alignment), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}
