import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from companion.todos.b_client import (
    CREATED,
    DUPLICATE,
    REJECTED,
    REPLAYED,
    UNAVAILABLE,
    USER_NOT_FOUND,
    TodoApi,
)

OK_BODY = {"todo_id": "t_001", "title": "치과 예약", "next_at": "2026-11-25T15:30:00+09:00", "reminder": True}


class StubB:
    """정해 둔 응답을 차례로 돌려주는 가짜 B 서버. 받은 요청 본문을 기록한다. 다른 경로면 404.

    prefix=True면 path로 시작하는 경로를 모두 받는다 (PATCH /alerts/{id}). requests에 (메서드, 경로)를 남긴다.
    """

    def __init__(self, path="/todos", prefix=False):
        self.responses = []
        self.received = []
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                stub.received.append(json.loads(self.rfile.read(length)))
                stub.requests.append((self.command, self.path))
                if not (self.path.startswith(path) if prefix else self.path == path):
                    status, body = 404, {"error": {"code": "NOT_FOUND", "message": self.path}}
                else:
                    status, body = stub.responses.pop(0)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_PATCH = do_POST

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def error(code, **extra):
    return {"error": {"code": code, "message": "…", **extra}}


class TodoApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = StubB()

    @classmethod
    def tearDownClass(cls):
        cls.b.server.shutdown()

    def setUp(self):
        self.b.responses.clear()
        self.b.received.clear()
        self.slept = []
        self.api = TodoApi(self.b.url, sleep=self.slept.append)

    def register(self):
        return self.api.register({"request_id": "rid-1", "title": "치과 예약"})

    def test_등록(self):
        self.b.responses.append((201, OK_BODY))
        result = self.register()
        self.assertEqual((result.status, result.todo), (CREATED, OK_BODY))
        self.assertTrue(result.ok)

    def test_재요청은_replayed(self):
        self.b.responses.append((200, OK_BODY))
        self.assertEqual(self.register().status, REPLAYED)

    def test_5xx는_같은_request_id로_재시도(self):
        self.b.responses.extend([(503, error("MOCK_FAILURE")), (500, error("X")), (201, OK_BODY)])
        self.assertEqual(self.register().status, CREATED)
        self.assertEqual(self.slept, [1, 3])
        self.assertEqual({r["request_id"] for r in self.b.received}, {"rid-1"})

    def test_재시도를_다_하면_unavailable(self):
        self.b.responses.extend([(503, error("X"))] * 4)
        self.assertEqual(self.register().status, UNAVAILABLE)
        self.assertEqual(self.slept, [1, 3, 10])
        self.assertEqual(len(self.b.received), 4)

    def test_중복(self):
        self.b.responses.append((409, error("DUPLICATE_TODO", existing_todo_id="t_007")))
        result = self.register()
        self.assertEqual((result.status, result.existing_todo_id), (DUPLICATE, "t_007"))
        self.assertEqual(self.slept, [])

    def test_4xx는_재시도하지_않음(self):
        self.b.responses.append((400, error("INVALID_DATE")))
        result = self.register()
        self.assertEqual((result.status, result.error_code), (REJECTED, "INVALID_DATE"))
        self.b.responses.append((404, error("USER_NOT_FOUND")))
        self.assertEqual(self.register().status, USER_NOT_FOUND)
        self.assertEqual(self.slept, [])

    def test_에러_본문이_객체가_아니어도_죽지_않음(self):
        self.b.responses.append((400, {"error": "bad request"}))
        self.assertEqual(self.register().status, REJECTED)
        self.b.responses.append((409, ["x"]))
        self.assertEqual(self.register().status, REJECTED)

    def test_성공인데_객체가_아니면_재시도(self):
        self.b.responses.extend([(201, ["x"]), (201, OK_BODY)])
        result = self.register()
        self.assertEqual((result.status, self.slept), (CREATED, [1]))

    def test_음성용은_짧게_기다림(self):
        api = TodoApi.for_voice(self.b.url, sleep=self.slept.append)
        self.b.responses.extend([(503, error("X"))] * 2)
        self.assertEqual(api.register({"request_id": "rid"}).status, UNAVAILABLE)
        self.assertEqual((self.slept, len(self.b.received), api.timeout), ([1], 2, 3.0))

    def test_연결_안_됨(self):
        api = TodoApi("http://127.0.0.1:9", timeout=0.5, sleep=self.slept.append)
        self.assertEqual(api.register({"request_id": "rid"}).status, UNAVAILABLE)
        self.assertEqual(self.slept, [1, 3, 10])


if __name__ == "__main__":
    unittest.main()
