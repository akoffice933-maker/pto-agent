# -*- coding: utf-8 -*-
"""Mock-провайдер VL-модели: тот же HTTP-протокол, но без модели.

Зачем нужен: в песочнице нет GPU и доступа к контуру 10.20.0.x, а показать и
протестировать весь путь «скан → HTTP-запрос с картинкой → строгий JSON →
анти-галлюцинационная проверка → операции» нужно. Заглушка принимает реальный
POST /v1/chat/completions, декодирует картинку фрагмента, находит цветные пометки
классическим CV и подставляет тексты из эталонного словаря.

Это НЕ распознавание текста: в боевом контуре вместо неё поднимается vLLM
с Qwen2.5-VL (config/llm_providers.yaml → local-qwen-vl-32b).
"""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path




class MockVisionServer:
    """Заглушка VL-модели: отдаёт эталонные пометки для запрошенного фрагмента.

    Нужна, чтобы прогонять весь путь (тайлы → HTTP → проверки → операции) без
    внутреннего контура с GPU. Принимает путь к JSON или сам словарь.
    """

    def __init__(self, truth: str | Path | dict, host: str = "127.0.0.1", port: int = 0):
        self.truth = (truth if isinstance(truth, dict)
                      else json.loads(Path(truth).read_text(encoding="utf-8")))
        self.host, self.port = host, port
        self._srv = None
        self._thread = None

    # ── жизненный цикл ────────────────────────────────────────────────────
    def start(self) -> "MockVisionServer":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "mock-vlm/0.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                try:
                    resp = outer._answer(body)
                    code = 200
                except Exception as exc:  # noqa: BLE001
                    resp, code = {"error": {"message": str(exc)}}, 500
                data = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._srv = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # ── «модель» ──────────────────────────────────────────────────────────
    def _answer(self, body: dict) -> dict:
        """Вернуть пометки, попавшие во фрагмент, в координатах фрагмента (0..1).

        Заглушка не «смотрит» на картинку: она отдаёт эталонный ответ для
        запрошенного фрагмента. Настоящая модель в контуре делает то же самое,
        но по изображению — формат ответа идентичен.
        """
        tile = body.get("tile") or {}
        origin = tile.get("origin") or [0, 0]
        size = tile.get("size") or self.truth.get("image_size", [1, 1])
        if not any(msg.get("content") for msg in body.get("messages", [])):
            raise ValueError("в запросе нет сообщений")
        has_image = any(
            part.get("type") == "image_url"
            for msg in body.get("messages", []) if isinstance(msg.get("content"), list)
            for part in msg["content"])
        if not has_image:
            raise ValueError("в запросе нет изображения")

        tx0, ty0 = origin[0], origin[1]
        tx1, ty1 = origin[0] + size[0], origin[1] + size[1]
        marks = []
        for t in self.truth.get("marks", []):
            bx0, by0, bx1, by1 = t["bbox_px"]
            if bx1 < tx0 or bx0 > tx1 or by1 < ty0 or by0 > ty1:
                continue      # пометка не попадает во фрагмент
            marks.append({
                "type": t["type"],
                "bbox": [round(max(bx0 - tx0, 0) / size[0], 5),
                         round(max(by0 - ty0, 0) / size[1], 5),
                         round(min(bx1 - tx0, size[0]) / size[0], 5),
                         round(min(by1 - ty0, size[1]) / size[1], 5)],
                "text": t.get("text", ""),
                "confidence": float(t.get("confidence", 0.85)),
                "ref": f"mock-{t.get('mark_id')}",
            })

        content = json.dumps({"marks": marks}, ensure_ascii=False)
        return {
            "id": "mock-cmpl-1",
            "model": "mock-vl-redline",
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 980 + 40 * len(body.get("messages", [])),
                      "completion_tokens": 40 + 25 * len(marks)},
        }


__all__ = ["MockVisionServer"]
