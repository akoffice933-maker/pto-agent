# -*- coding: utf-8 -*-
"""MockModel: OpenAI-совместимый сервер с заранее известным поведением.

Зачем. Бенчмарк, который нельзя проверить, — это мнение с таблицей. Здесь рядом с
настоящей моделью живёт модель-заглушка, у которой ответы заданы заранее, включая
**сбои**: битый JSON, выдуманное число вместо «нет данных», обрыв по `max_tokens`,
таймаут, 429 и 500. Если измеритель не видит этих сбоев — сломан измеритель, а не модель.

Поведение задаётся политикой `policy` — словарём «вид задачи → как отвечать». Значения:

* `good`      — правильный ответ (свойство выполнено);
* `bad_json`  — «почти JSON» с пропущенным полем;
* `invented`  — правдоподобное число вместо «нет данных» (главная ловушка проекта);
* `truncated` — ответ обрывается, как при `max_tokens`;
* `timeout`   — сервер молчит дольше таймаута клиента;
* `http_429`  — отказ по лимиту;
* `http_500`  — ошибка сервера;
* `paraphrase`— пересказ нормы без цитаты и пункта;
* `length`    — правильный ответ, обрезанный по `max_tokens` (как это делает настоящий
                сервер), с `finish_reason: length`;
* `json_mode` — на просьбу «верни только JSON» отвечает голым JSON;
* `long`      — правильный ответ, но длиннее запрошенного `max_tokens` (сервер не соблюл лимит).

Сервер принимает реальный `POST /v1/chat/completions` и отдаёт реальный ответ в формате
OpenAI, поэтому клиент — обычный OpenAI-совместимый код, без ветвлений «а если мок».
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GOOD, BAD_JSON, INVENTED = "good", "bad_json", "invented"
TRUNCATED, TIMEOUT, HTTP_429, HTTP_500, PARAPHRASE = (
    "truncated", "timeout", "http_429", "http_500", "paraphrase")
LENGTH, JSON_MODE, LONG = "length", "json_mode", "long"

DEFAULT_POLICY = {
    "schema": GOOD,
    "quote": GOOD,
    "no_data": GOOD,
    "chain": GOOD,
    "truncation": GOOD,
}

GOOD_BODIES = {
    "schema": json.dumps({
        "number": "КС-2 №1", "period": "2026-10",
        "lines": [{"name": "Бетон B25", "unit": "м3", "qty": 12, "price": 5000},
                  {"name": "Арматура A500", "unit": "т", "qty": 1, "price": 60000}],
        "total": 120000, "vat": 24000}, ensure_ascii=False),
    "quote": "Согласно п. 10.3.1 СП 63.13330, «толщина защитного слоя бетона должна "
             "приниматься не менее диаметра стержня и не менее 20 мм».",
    # Мок обязан назвать признак отсутствия из запроса ("[нет данных]" / "не найдена" и т.п.),
    # иначе он «честен» по смыслу, но не по проверке, и тест ловит мок, а не модель.
    "no_data": "[нет данных]: в выгрузке нет ведомости расхода стали, позиция 44 "
               "не найдена, раздел отсутствует. Значение не определено и не оценено.",
    "chain": "шаг 1 — объёмы прочитаны: 12 м3 бетона, 1 т арматуры; "
             "шаг 3 — сумма посчитана: итог 120 000 ₽ без НДС.",
    "truncation": json.dumps({"number": "КС-2 №1", "period": "2026-10",
                              "lines": [], "total": 0}, ensure_ascii=False),
    # json_mode: ответ на запрос «верни только JSON» — без пояснений вокруг
}

# Ответ на просьбу «верни только JSON»: правильный ответ — сам JSON, ничего вокруг.
JSON_MODE_BODY = json.dumps({
    "number": "КС-2 №1", "period": "2026-10",
    "lines": [{"name": "Бетон B25", "unit": "м3", "qty": 12, "price": 5000}],
    "total": 60000, "vat": 12000}, ensure_ascii=False)

BAD_BODIES = {
    "bad_json": '{"number": "КС-2 №1", "period": "2026-10", "lines": [{"name": "Бетон"',
    "invented": "По ведомости расхода стали в томе КР — 4,2 т арматуры класса A500.",
    "truncated": '{"number": "КС-2 №1", "period": "2026-10", "lines": [{"name": "Бетон B25"',
    "paraphrase": "Норма говорит, что защитный слой должен быть достаточным для "
                  "защиты арматуры от коррозии.",
}


class MockModel:
    """OpenAI-совместимый сервер, у которого ответы известны заранее."""

    def __init__(self, policy: dict[str, str] | None = None, *, host: str = "127.0.0.1",
                 port: int = 0, timeout_s: float = 2.0, delay_s: float = 0.0,
                 fail_every: int | None = None, fail_with: str = HTTP_500,
                 length_cut_chars: int | None = None) -> None:
        self.policy = {**DEFAULT_POLICY, **(policy or {})}
        self.host, self.port = host, port
        self.timeout_s = timeout_s
        self.delay_s = delay_s
        self.fail_every = fail_every
        self.fail_with = fail_with
        self.length_cut_chars = length_cut_chars
        self.requests: list[dict] = []
        self._srv: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ── жизненный цикл ────────────────────────────────────────────────────
    def start(self) -> "MockModel":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "mock-model/1.0"

            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.rstrip("/").endswith("/v1/models"):
                    self._send(200, {"object": "list", "data": [
                        {"id": "mock-model", "object": "model"}]})
                else:
                    self._send(404, {"error": {"message": "not found"}})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(body)
                mode, payload, code = outer._respond(body, len(outer.requests))
                if mode == TIMEOUT:
                    time.sleep(outer.timeout_s + 0.5)
                    self._send(200, outer._completion(""))
                    return
                if code != 200:
                    self._send(code, {"error": {"message": payload}})
                    return
                finish = "length" if mode in (TRUNCATED, LENGTH) else "stop"
                self._send(200, outer._completion(payload, finish))

            def _send(self, code: int, data: dict) -> None:
                raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._srv = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> "MockModel":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # ── поведение ─────────────────────────────────────────────────────────
    @staticmethod
    def kind_of(prompt: str) -> str:
        """Вид задачи по промпту: тот же признак, что в `tasks.default_tasks`."""
        lowered = prompt.lower()
        # Порядок важен: промпт цитаты содержит слово «пункт», а промпт цепочки — «сумма».
        # Раньше «json» проверялся первым, и запрос «верни только JSON» уходил в truncation —
        # это ловил тест, а не пользователь.
        if "цитат" in lowered:
            return "quote"
        if "json" in lowered:
            return "schema" if "собери акт" in lowered else "truncation"
        if "цепочк" in lowered or "шаг 1" in lowered:
            return "chain"
        return "no_data"

    def _cut(self, text: str, max_tokens: int | None) -> str:
        """Обрезать ответ так, как его обрезает сервер, упёршийся в `max_tokens`.

        Длина обрезки задаётся `length_cut_chars`, а не пересчётом токенов: у кириллицы
        и JSON соотношение символов и токенов гуляет, и «4 символа на токен» давало такой
        щедрый лимит, что обрезки не происходило вовсе — режим `length` был мёртвым.
        """
        limit = self.length_cut_chars or 40
        if max_tokens:
            limit = min(limit, max(1, int(max_tokens)))
        return text if len(text) <= limit else text[:limit]

    def _respond(self, body: dict, index: int) -> tuple[str, str, int]:
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail_every and index % self.fail_every == 0:
            if self.fail_with == HTTP_429:
                return HTTP_429, "rate limit", 429
            if self.fail_with == TIMEOUT:
                return TIMEOUT, "", 200
            return HTTP_500, "upstream error", 500
        messages = body.get("messages") or []
        prompt = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                prompt = str(message.get("content") or "")
                break
        kind = self.kind_of(prompt)
        mode = self.policy.get(kind, GOOD)
        # режим длины: содержимое правильное, но обрезано, как это делает сервер по max_tokens
        if mode == LENGTH:
            return LENGTH, self._cut(GOOD_BODIES[kind], body.get("max_tokens")), 200
        if mode == LONG:
            # содержимое правильное, но лимит не соблюдён — так вёл себя шлюз на живом прогоне
            filler = "Пояснение к ответу. " * 60
            return GOOD, GOOD_BODIES[kind] + "\n" + filler, 200
        if mode == JSON_MODE and "json" in prompt.lower():
            # просьба «только JSON»: правильный ответ — голый JSON. Политика по виду задачи
            # тут не применяется: это свойство формата, а не содержания.
            return GOOD, JSON_MODE_BODY, 200
        if mode == GOOD:
            return GOOD, GOOD_BODIES[kind], 200
        if mode == TRUNCATED:
            return TRUNCATED, BAD_BODIES[TRUNCATED], 200
        if mode == BAD_JSON:
            return BAD_JSON, BAD_BODIES[BAD_JSON], 200
        if mode == INVENTED:
            return INVENTED, BAD_BODIES[INVENTED], 200
        if mode == PARAPHRASE:
            return PARAPHRASE, BAD_BODIES[PARAPHRASE], 200
        if mode == JSON_MODE:
            # просьбы вернуть JSON в этой задаче нет — это свойство формата, не содержания
            return GOOD, GOOD_BODIES[kind], 200
        if mode == TIMEOUT:
            return TIMEOUT, "", 200
        if mode == HTTP_429:
            return HTTP_429, "rate limit", 429
        return HTTP_500, "upstream error", 500

    @staticmethod
    def _completion(text: str, finish_reason: str = "stop") -> dict:
        return {
            "id": "mock-1", "object": "chat.completion", "model": "mock-model",
            "choices": [{"index": 0, "finish_reason": finish_reason,
                         "message": {"role": "assistant", "content": text}}],
            # Токены считаются по длине ответа (≈3 символа на токен), а не фиксированным
            # числом: иначе режим «ответ длиннее лимита» невозможно заметить.
            "usage": {"prompt_tokens": 100,
                      "completion_tokens": max(1, len(text) // 3),
                      "total_tokens": 100 + max(1, len(text) // 3)},
        }
