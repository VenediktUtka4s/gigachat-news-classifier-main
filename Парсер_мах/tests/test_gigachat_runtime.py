"""Transport regressions run entirely offline, using a scripted HTTP boundary."""

import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import requests


try:
    from news_runtime import gigachat
except ModuleNotFoundError:
    gigachat = None


NOW = 1_800_000_000


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.body = body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def token(value="first", expires=1_800_001_800_000):
    return Response(body={"access_token": value, "expires_at": expires})


def completion(content='{"result": "ok"}', reason="stop"):
    return Response(body={
        "choices": [{"message": {"role": "assistant", "content": content},
                     "index": 0, "finish_reason": reason}],
        "created": NOW, "model": "GigaChat", "object": "chat.completion",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })


class ScriptedSession:
    """Record real outgoing request arguments without contacting the service."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected additional HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class GigaChatTransportTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(gigachat, "The GigaChat transport implementation is missing")

    def client(self, responses, **kwargs):
        session = ScriptedSession(responses)
        sleeps = []
        client = gigachat.GigaChatClient(
            "test-credential-never-print", session=session,
            clock=kwargs.pop("clock", lambda: NOW), sleep=sleeps.append, **kwargs)
        return client, session, sleeps

    def test_reuses_token_then_refreshes_at_absolute_expiry(self):
        for expires in [1_800_001_800_000, 1_800_001_800]:
            with self.subTest(expires=expires):
                now = [NOW]
                client, session, _ = self.client(
                    [token(expires=expires), completion(), completion(), token("second"), completion()],
                    clock=lambda: now[0])
                self.assertEqual(client.complete_json("system", "news"), {"result": "ok"})
                now[0] = NOW + 1700
                client.complete_json("system", "news")
                now[0] = NOW + 1740
                client.complete_json("system", "news")
                self.assertEqual([c[1]["headers"]["Authorization"] for c in session.calls],
                                 ["Basic test-credential-never-print", "Bearer first", "Bearer first",
                                  "Basic test-credential-never-print", "Bearer second"])

    def test_401_refreshes_and_replays_with_new_token_once(self):
        client, session, sleeps = self.client([token(), Response(401), token("second"), completion()])
        self.assertEqual(client.complete_json("system", "news"), {"result": "ok"})
        self.assertEqual(session.calls[-1][1]["headers"]["Authorization"], "Bearer second")
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(sleeps, [])

    def test_second_401_is_fatal_and_later_calls_make_no_requests(self):
        client, session, _ = self.client([token(), Response(401), token("second"), Response(401)])
        for _ in range(3):
            with self.assertRaises(gigachat.GigaChatAuthError) as caught:
                client.complete_json("system", "news")
            self.assertNotIn("test-credential", str(caught.exception))
        self.assertEqual(len(session.calls), 4)

    def test_rejected_oauth_credentials_are_fatal_without_retry(self):
        for status in [400, 401, 403]:
            with self.subTest(status=status):
                client, session, sleeps = self.client([Response(status, {"message": "secret-response"})])
                for _ in range(2):
                    with self.assertRaises(gigachat.GigaChatAuthError) as caught:
                        client.complete_json("system", "news")
                    self.assertNotIn("secret-response", str(caught.exception))
                self.assertEqual(len(session.calls), 1)
                self.assertEqual(sleeps, [])

    def test_missing_credentials_are_rejected_before_http(self):
        with self.assertRaises(gigachat.GigaChatAuthError):
            gigachat.GigaChatClient(" ")

    def test_transient_errors_retry_then_succeed(self):
        for transient in [Response(429), Response(503), requests.Timeout("secret"), requests.ConnectionError("secret")]:
            with self.subTest(transient=type(transient).__name__):
                client, session, sleeps = self.client([token(), transient, completion()])
                self.assertEqual(client.complete_json("system", "news"), {"result": "ok"})
                self.assertEqual(len(session.calls), 3)
                self.assertEqual(len(sleeps), 1)

    def test_transient_failures_stop_after_three_attempts(self):
        client, session, sleeps = self.client([token(), Response(503), Response(503), Response(503)])
        with self.assertRaises(gigachat.GigaChatError):
            client.complete_json("system", "news")
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(len(sleeps), 2)

    def test_nonretryable_http_errors_fail_once(self):
        for status in [400, 402, 403, 404, 422]:
            with self.subTest(status=status):
                client, session, sleeps = self.client([token(), Response(status, {"message": "private-news"})])
                with self.assertRaises(gigachat.GigaChatError) as caught:
                    client.complete_json("system", "news")
                self.assertNotIn("private-news", str(caught.exception))
                self.assertEqual(len(session.calls), 2)
                self.assertEqual(sleeps, [])

    def test_tls_failure_is_not_retried(self):
        client, session, sleeps = self.client([requests.exceptions.SSLError("secret")])
        with self.assertRaises(gigachat.GigaChatError) as caught:
            client.complete_json("system", "news")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(sleeps, [])

    def test_verifies_tls_and_sends_current_prompts(self):
        for verify in [True, "path/to/ca.pem"]:
            with self.subTest(verify=verify):
                client, session, _ = self.client([token(), completion()], verify=verify, model="GigaChat-2")
                client.complete_json("system prompt", "article")
                self.assertTrue(all(c[1]["verify"] == verify for c in session.calls))
                self.assertTrue(all(c[1]["timeout"] for c in session.calls))
                self.assertEqual(session.calls[1][1]["json"]["messages"], [
                    {"role": "system", "content": "system prompt"},
                    {"role": "user", "content": "article"}])
                self.assertEqual(session.calls[1][1]["json"]["model"], "GigaChat-2")

    def test_malformed_token_is_fatal_without_leaking_response(self):
        for body in [{}, {"access_token": "secret", "expires_at": "not-a-date"},
                     {"access_token": "secret", "expires_at": NOW - 1},
                     {"access_token": "secret", "expires_at": float("nan")},
                     ValueError("secret-response")]:
            with self.subTest(body=type(body).__name__):
                client, session, _ = self.client([Response(body=body)])
                for _ in range(2):
                    with self.assertRaises(gigachat.GigaChatAuthError) as caught:
                        client.complete_json("system", "news")
                    self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(len(session.calls), 1)

    def test_invalid_or_incomplete_completion_is_not_empty_success(self):
        for response in [Response(body={}), Response(body=ValueError("private-news")),
                         completion(""), completion("not JSON"), completion("[]"),
                         completion("{}", "blacklist"), completion("{}", "length")]:
            with self.subTest(body=type(response.body).__name__):
                client, session, sleeps = self.client([token(), response])
                with self.assertRaises(gigachat.GigaChatError) as caught:
                    client.complete_json("system", "news")
                self.assertNotIn("private-news", str(caught.exception))
                self.assertEqual(len(session.calls), 2)
                self.assertEqual(sleeps, [])

    def test_markdown_json_fence_is_accepted(self):
        client, _, _ = self.client([token(), completion('```json\n{"result": "ok"}\n```')])
        self.assertEqual(client.complete_json("system", "news"), {"result": "ok"})

    def test_concurrent_requests_share_one_token_and_never_overlap(self):
        class ConcurrentSession:
            def __init__(self):
                self.active = 0
                self.max_active = 0
                self.auth_count = 0
                self.lock = threading.Lock()

            def post(self, url, **kwargs):
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    time.sleep(0.005)
                    if "json" not in kwargs:
                        self.auth_count += 1
                        return token()
                    return completion(json.dumps({"result": kwargs["json"]["messages"][1]["content"]}))
                finally:
                    with self.lock:
                        self.active -= 1

        session = ConcurrentSession()
        client = gigachat.GigaChatClient("test-only", session=session, clock=lambda: NOW)
        barrier = threading.Barrier(8)

        def classify(index):
            barrier.wait(timeout=5)
            return client.complete_json("system", str(index))

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(classify, range(8)))
        self.assertEqual(results, [{"result": str(i)} for i in range(8)])
        self.assertEqual(session.auth_count, 1)
        self.assertEqual(session.max_active, 1)


if __name__ == "__main__":
    unittest.main()
