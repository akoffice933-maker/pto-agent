# -*- coding: utf-8 -*-
"""Один вход в веб-контур — для тестов и для шагов CI.

Зачем отдельный модуль. CSRF-защита формы входа (аудит 2026-11-22) сломала разом двух
клиентов: `tests/test_web.py` и `scripts/check_web.py`. Тесты починили в том же коммите,
а скрипт — нет; скрипт при этом не упал на входе, а дошёл до шага «закрытие месяца»
и умер на `StopIteration`, из которого причина не видна. Разница была в одной строке,
написанной дважды.

Теперь вход один на всех: получить токен с формы → отправить его вместе с паролем →
на не-200 остановиться сразу, с кодом и телом ответа. Если форма входа снова изменится,
сломается ровно этот файл, и сломается громко.

Модуль намеренно не зависит от pytest и от `scripts.*`: его импортируют и тесты, и
проверочные скрипты.
"""
from __future__ import annotations

CSRF_MARKER = 'name="csrf" value="'


class LoginRefused(RuntimeError):
    """Вход не состоялся — с кодом ответа и куском тела, чтобы причина была видна.

    Не `StopIteration` и не молчаливый `assert False`: сообщение должно отвечать на вопрос
    «почему», а не только «что-то не так».
    """

    def __init__(self, login: str, status: int, body: str) -> None:
        self.login = login
        self.status = status
        self.body = body
        super().__init__(f"вход {login} не удался: HTTP {status}{self._hint()}"
                         f"{self._tail()}")

    def _hint(self) -> str:
        if self.status == 401:
            return " · неверный логин или пароль"
        if self.status == 403:
            return " · отказ по правам"
        # 400 на форме входа — это CSRF. Проверяем код, а не слово в теле: в HTML формы
        # входа слово «csrf» есть всегда, и по нему диагноз был бы неверным.
        if self.status == 400 and "csrf" in self.body.lower():
            return " · форма требует CSRF-токен: возьмите его со страницы /login"
        return ""

    def _tail(self) -> str:
        text = " ".join(self.body.split())
        return f" · ответ: {text[:300]}" if text else ""


def _extract(text: str, marker: str = CSRF_MARKER) -> str:
    start = text.find(marker)
    if start < 0:
        raise LoginRefused("—", 200, f"на странице нет поля {marker!r}: форма входа изменилась")
    start += len(marker)
    end = text.find('"', start)
    if end < 0:
        raise LoginRefused("—", 200, f"поле {marker!r} не закрыто кавычкой")
    return text[start:end]


def login_csrf(client, page: str = "/login") -> str:
    """Токен формы входа. Берётся с самого `/login`, а не с любой другой страницы."""
    response = client.get(page)
    if response.status_code != 200:
        raise LoginRefused("—", response.status_code,
                           f"страница {page} не открылась: {response.text[:300]}")
    return _extract(response.text)


def page_csrf(client, page: str = "/") -> str:
    """Токен для POST из уже открытой сессии (обычно со сводки)."""
    response = client.get(page)
    if response.status_code != 200:
        raise LoginRefused("—", response.status_code,
                           f"страница {page} не открылась: {response.text[:300]}")
    return _extract(response.text)


def login(client, login_name: str, password: str, *, page: str = "/login"):
    """Войти и вернуть того же клиента. На любой не-200 — `LoginRefused` сразу."""
    token = login_csrf(client, page)
    response = client.post("/login", data={"login": login_name, "password": password,
                                          "csrf": token})
    if response.status_code != 200:
        raise LoginRefused(login_name, response.status_code, response.text)
    return client
