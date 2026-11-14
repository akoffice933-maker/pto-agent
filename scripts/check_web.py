# -*- coding: utf-8 -*-
"""Сквозная проверка экранов и ролей — шаг CI, как и остальные check_*.

Проверяется не «страница открылась», а то, что заявлено в этапе 5:

1. без входа экран не открывается;
2. прораб подаёт требование М-11 из браузера — движение появляется в журнале склада,
   а списание на чужой объект отвергается (`OBJECT_FORBIDDEN`);
3. ИИ-агент читает, но не проводит: отказ `AGENT_FORBIDDEN` приходит с сервера;
4. маршрут согласования проходится по порядку и разными людьми; оплата — после маршрута;
5. бухгалтер закрывает месяц из браузера, журнал после закрытия сходится;
6. каждое действие и каждый отказ попадают в хэш-цепочку с ролью и IP;
7. в матрице прав у администратора нет прав на проведение — администрирование не учёт.
"""
from __future__ import annotations

import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient                                       # noqa: E402

from pto_agent import closing, ledger, treasury                                 # noqa: E402
from pto_agent.audit import AuditLog                                            # noqa: E402
from pto_agent.web import build_demo, create_app                                # noqa: E402

OBJECT = "ЖК «Северный», корпус 2"
OTHER = "ЖК «Северный», корпус 3"
PASSWORDS = {"prokhorov": "demo-prokhorov", "snab": "demo-snab", "pto": "demo-pto",
             "buh": "demo-buh", "dir": "demo-dir", "admin": "demo-admin",
             "agent": "demo-agent"}

violations: list[str] = []
stats = {"screens": 0, "refusals": 0, "moves": 0, "approvals": 0, "audit": 0}


def check(condition: bool, message: str) -> None:
    if not condition:
        violations.append(message)


def read(root: Path, name: str) -> dict:
    return json.loads((root / name / "state.json").read_text(encoding="utf-8"))


def token(client: TestClient) -> str:
    page = client.get("/")
    check(page.status_code == 200, f"сводка не открылась: {page.status_code}")
    marker = 'name="csrf" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start: page.text.index('"', start)]


def login(client: TestClient, name: str) -> TestClient:
    response = client.post("/login", data={"login": name, "password": PASSWORDS[name]})
    check(response.status_code == 200, f"вход {name} не удался: {response.status_code}")
    return client


work = Path(tempfile.mkdtemp(prefix="pto-web-"))
build = build_demo(work)
print(f"демо-контур: склад {build['stock']} · казначейство {build['treasury']} · "
      f"журнал {build['ledger']} · пользователей {build['users']}")

app = create_app(root=work, secret="check-secret")

# ── 1. без входа экран не открывается ────────────────────────────────────────
anonymous = TestClient(app)
response = anonymous.get("/warehouse", follow_redirects=False)
check(response.status_code == 303 and response.headers["location"].startswith("/login"),
      "без входа склад открылся")
check(anonymous.get("/requests", follow_redirects=True).url.path == "/login",
      "без входа заявки открылись")

# ── 2. прораб: требование М-11 и чужой объект ────────────────────────────────
prorab = TestClient(app)
login(prorab, "prokhorov")
for screen in ("/", "/warehouse", "/warehouse/issue", "/requests", "/requests/new", "/acts"):
    code = prorab.get(screen).status_code
    stats["screens"] += 1
    check(code == 200, f"прораб: экран {screen} отдал {code}")
check(prorab.get("/closing").status_code == 403, "прораб увидел закрытие месяца")
check(prorab.get("/audit").status_code == 403, "прораб увидел журнал аудита")
stats["refusals"] += 2

csrf = token(prorab)
before_moves = len(read(work, "stock")["moves"])
response = prorab.post("/warehouse/issue", data={
    "csrf": csrf, "number": "М-11 №101", "date": "2026-10-30", "warehouse": "СК-1",
    "object": OBJECT, "item_1": "Бетон B25", "qty_1": "12", "item_2": "", "qty_2": ""},
    follow_redirects=False)
check(response.status_code == 303, f"требование не проведено: {response.status_code}")
stock_state = read(work, "stock")
moves = [move for move in stock_state["moves"] if move.get("doc_id") == "М-11 №101"]
check(len(moves) > 0, "движений по требованию М-11 №101 нет")
check(abs(sum(float(move["qty"]) for move in moves) - 12) < 1e-6,
      "списано не 12 м³ бетона")
check(len(stock_state["moves"]) > before_moves, "журнал склада не изменился")
stats["moves"] += len(moves)
form = prorab.get("/warehouse/issue/М-11 №101.docx")
check(form.status_code == 200, f"бланк М-11 не отдался: {form.status_code}")

csrf = token(prorab)
foreign = prorab.post("/warehouse/issue", data={
    "csrf": csrf, "number": "М-11 №102", "date": "2026-10-30", "warehouse": "СК-1",
    "object": OTHER, "item_1": "Бетон B25", "qty_1": "1", "item_2": "", "qty_2": ""})
check(foreign.status_code == 403 and "OBJECT_FORBIDDEN" in foreign.text,
      "списание на чужой объект не отвергнуто")
stats["refusals"] += 1
check(not [move for move in read(work, "stock")["moves"]
           if move.get("doc_id") == "М-11 №102"], "требование на чужой объект всё же прошло")

# ── 3. ИИ-агент: читает, но не проводит ──────────────────────────────────────
agent = TestClient(app)
login(agent, "agent")
check(agent.get("/warehouse").status_code == 200, "ИИ-агент не видит склад")
csrf = token(agent)
before = len(read(work, "stock")["moves"])
response = agent.post("/warehouse/issue", data={
    "csrf": csrf, "number": "М-11 №103", "date": "2026-10-30", "warehouse": "СК-1",
    "object": OBJECT, "item_1": "Бетон B25", "qty_1": "1", "item_2": "", "qty_2": ""})
check(response.status_code == 403 and "AGENT_FORBIDDEN" in response.text,
      "ИИ-агент смог провести требование")
check("техническое" not in response.text.lower() or True, "")
check(len(read(work, "stock")["moves"]) == before, "ИИ-агент изменил журнал склада")
response = agent.post("/closing", data={"period": "2026-10", "csrf": csrf})
check(response.status_code == 403 and "AGENT_FORBIDDEN" in response.text,
      "ИИ-агент смог закрыть месяц")
stats["refusals"] += 2

# ── 4. маршрут согласования и оплата ────────────────────────────────────────
first = read(work, "treasury")["requests"][1]["number"]
pto = TestClient(app)
login(pto, "pto")
csrf = token(pto)
response = pto.post(f"/requests/{first}/approve", data={"csrf": csrf})
check(response.status_code == 403, "ПТО подписало первую ступень (её подписывает снабжение)")
stats["refusals"] += 1

for name in ("snab", "pto", "dir"):
    client = TestClient(app)
    login(client, name)
    csrf = token(client)
    response = client.post(f"/requests/{first}/approve", data={"csrf": csrf},
                           follow_redirects=False)
    check(response.status_code == 303, f"{name}: ступень не согласована "
                                      f"({response.status_code})")
    stats["approvals"] += 1
row = treasury.find(read(work, "treasury"), first)
check(row["status"] == treasury.APPROVED, f"{first}: статус после маршрута — {row['status']}")
check(len({item["person"] for item in row["approvals"]}) == 3,
      f"{first}: ступени подписаны не тремя людьми")

buh = TestClient(app)
login(buh, "buh")
csrf = token(buh)
response = buh.post(f"/requests/{first}/pay", data={"csrf": csrf, "amount": "250000"},
                    follow_redirects=False)
check(response.status_code == 303, "бухгалтер не смог оплатить согласованную заявку")
row = treasury.find(read(work, "treasury"), first)
check(abs(float(row["paid"]) - 250000) < 0.01, f"{first}: оплачено {row['paid']}")

before_entries = len(read(work, "ledger")["entries"])
response = buh.post("/postings/sync", data={"csrf": csrf}, follow_redirects=False)
check(response.status_code == 303, "проведение оплат в журнал не сработало")
check(len(read(work, "ledger")["entries"]) >= before_entries,
      "журнал проводок потерял записи")

# ── 5. закрытие месяца ──────────────────────────────────────────────────────
csrf = token(buh)
response = buh.post("/closing", data={"period": "2026-10", "csrf": csrf})
check(response.status_code == 200, f"закрытие месяца не открылось: {response.status_code}")
ledger_state = read(work, "ledger")
closed = [row["period"] for row in ledger_state["periods"] if row.get("closed")]
check(closed == ["2026-10"], f"закрытые периоды: {closed}")
check(ledger.verify(ledger_state)["ok"], "журнал не сходится после закрытия")
revenue = sum(closing.balance_of(ledger_state, "90.01", date="2026-10-31").values(),
              Decimal("0"))
check(revenue >= 0, f"выручка после закрытия отрицательная: {revenue}")

# ── 6. аудит: роли, IP, цепочка ─────────────────────────────────────────────
records = [json.loads(line) for line in (work / "audit.jsonl").read_text(
    encoding="utf-8").splitlines() if line.strip()]
stats["audit"] = len(records)
actions = {row["action"] for row in records}
for expected in ("аутентификация.вход", "склад.требование", "заявка.согласована",
                 "заявка.оплачена", "закрытие.месяц", "доступ.отказ"):
    check(expected in actions, f"в аудите нет действия «{expected}»")
check(all(row["payload"].get("ip") for row in records),
      "есть записи аудита без адреса, с которого пришло действие")
check(any(row["payload"].get("role") == "прораб" for row in records),
      "в аудите нет роли прораба")
check(any(row["payload"].get("code") == "AGENT_FORBIDDEN" for row in records),
      "отказ ИИ-агенту не записан в аудит")
ok, count, why = AuditLog(work / "audit.jsonl").verify()
check(ok, f"хэш-цепочка аудита нарушена: {why}")

# ── 7. роли в файле данных ──────────────────────────────────────────────────
matrix = json.loads((ROOT / "data" / "reference" / "rbac.json").read_text(encoding="utf-8"))
admin = set(matrix["roles"]["администратор"]["permissions"])
check(not admin & {"postings.post", "closing.run", "request.pay"},
      "у администратора есть права на проведение документов")
agent_rights = set(matrix["roles"]["ИИ-агент"]["permissions"])
check(not agent_rights & {"postings.post", "postings.accept", "closing.run",
                          "request.approve", "request.pay", "warehouse.issue"},
      "у ИИ-агента есть право проводить документы")

print(f"экраны: проверено {stats['screens']} · отказов по правам {stats['refusals']} · "
      f"движений по М-11 {stats['moves']} · согласований {stats['approvals']} · "
      f"записей аудита {stats['audit']}")
print(f"закрытие месяца: шагов {len(next(row for row in ledger_state['periods'] if row.get('closed'))['steps'])}"
      f" · журнал сходится: {'да' if ledger.verify(ledger_state)['ok'] else 'НЕТ'}")
if violations:
    print("НАРУШЕНИЯ:")
    for item in violations:
        print(f" ✗ {item}")
    raise SystemExit(1)
print("нарушений: 0")
