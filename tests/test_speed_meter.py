"""Tests use a local HTTP server and never contact the public internet."""

import contextlib
import io
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import speed_meter


PAYLOAD = bytes(range(256)) * 1025


class DownloadHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        state = self.server.state
        with state["lock"]:
            state["paths"].append(self.path)
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        active = True
        try:
            if self.path == "/missing":
                self.send_error(404, "Not found")
                return
            if self.path == "/slow":
                time.sleep(0.15)
            self.send_response(200)
            if self.path != "/no-length":
                length = len(PAYLOAD) + (100 if self.path == "/truncated" else 0)
                self.send_header("Content-Length", str(length))
            self.end_headers()
            # Delay part of the body: measuring only response headers or using
            # Content-Length instead of reading the body must not pass.
            self.wfile.write(PAYLOAD[:-1])
            self.wfile.flush()
            time.sleep(0.005)
            # Release before the final byte can reach the client, so bookkeeping
            # after completion cannot cause a spurious concurrency failure.
            with state["lock"]:
                state["active"] -= 1
            active = False
            self.wfile.write(PAYLOAD[-1:])
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if active:
                with state["lock"]:
                    state["active"] -= 1


@contextlib.contextmanager
def local_server():
    # HTTPServer normally reverse-resolves even a loopback address. Keep these
    # tests independent of the machine's DNS configuration as well as internet.
    with patch("http.server.socket.getfqdn", return_value="localhost"):
        server = ThreadingHTTPServer(("127.0.0.1", 0), DownloadHandler)
    server.state = {"lock": threading.Lock(), "paths": [], "active": 0, "max_active": 0}
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server.state
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


def run_cli(arguments):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = speed_meter.main(arguments)
        except SystemExit as exc:
            code = exc.code
    return code, stdout.getvalue(), stderr.getvalue()


class SpeedMeterTests(unittest.TestCase):
    def test_reads_entire_body_with_and_without_content_length(self):
        with local_server() as (base_url, _):
            for path in ("/file", "/no-length"):
                with self.subTest(path=path):
                    measurement = speed_meter.measure_once(base_url + path, timeout=1)
                    self.assertEqual(measurement.size_bytes, len(PAYLOAD))
                    self.assertGreaterEqual(measurement.elapsed_seconds, 0.005)

    def test_cli_performs_ten_sequential_complete_downloads(self):
        measurements = []
        measure_once = speed_meter.measure_once

        def record_measurement(*args, **kwargs):
            result = measure_once(*args, **kwargs)
            measurements.append(result)
            return result

        with local_server() as (base_url, state):
            with patch.object(speed_meter, "measure_once", side_effect=record_measurement):
                code, stdout, stderr = run_cli([base_url + "/no-length", "--timeout", "1"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(state["paths"], ["/no-length"] * 10)
        self.assertEqual(state["max_active"], 1)
        self.assertEqual(len(measurements), 10)
        self.assertTrue(all(item.size_bytes == len(PAYLOAD) for item in measurements))
        self.assertIn(f"Скачано: {len(PAYLOAD) * 10} байт", stdout)
        self.assertEqual(stderr, "")

    def test_summary_uses_total_bytes_divided_by_total_elapsed_time(self):
        # Individual request rates differ, so averaging their speeds would
        # produce an incorrect answer even though the total is exactly 4 MB/s.
        measurements = [
            speed_meter.Measurement(n * 1_000_000, (11 - n) * 0.25)
            for n in range(1, 11)
        ]
        with patch.object(speed_meter, "measure_once", side_effect=measurements) as measure:
            code, stdout, stderr = run_cli(["https://example.com/file"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(measure.call_count, 10)
        self.assertIn("Среднее время запроса: 1.375 с", stdout)
        self.assertIn("Скачано: 55000000 байт (55.000 МБ)", stdout)
        self.assertIn("Скорость: 4.000 МБ/с (32.000 Мбит/с)", stdout)

    def test_http_error_and_incomplete_response_fail_fast(self):
        with local_server() as (base_url, state):
            for path in ("/missing", "/truncated"):
                with self.subTest(path=path):
                    previous_count = len(state["paths"])
                    code, stdout, stderr = run_cli([base_url + path, "--timeout", "1"])
                    self.assertNotEqual(code, 0)
                    self.assertEqual(len(state["paths"]) - previous_count, 1)
                    self.assertTrue(stderr.strip())
                    self.assertNotRegex(stdout, r"(?i)(?:MB/s|Mbit/s|МБ/с|Мбит/с)")

    def test_timeout_fails_fast(self):
        with local_server() as (base_url, state):
            code, stdout, stderr = run_cli([base_url + "/slow", "--timeout", "0.02"])
        self.assertNotEqual(code, 0)
        self.assertEqual(state["paths"], ["/slow"])
        self.assertTrue(stderr.strip())
        self.assertNotRegex(stdout, r"(?i)(?:MB/s|Mbit/s|МБ/с|Мбит/с)")

    def test_rejects_invalid_urls_before_downloading(self):
        with patch.object(speed_meter, "measure_once") as measure:
            for url in ("ftp://example.com/file", "file:///etc/passwd", "example.com/file", "http://", "https://"):
                with self.subTest(url=url):
                    code, _, stderr = run_cli([url])
                    self.assertNotEqual(code, 0)
                    self.assertTrue(stderr.strip())
            measure.assert_not_called()

    def test_rejects_nonpositive_or_nonfinite_timeout(self):
        with patch.object(speed_meter, "measure_once") as measure:
            for value in ("0", "-1", "nan", "inf", "-inf"):
                with self.subTest(timeout=value):
                    code, _, stderr = run_cli(["https://example.com/file", "--timeout=" + value])
                    self.assertNotEqual(code, 0)
                    self.assertTrue(stderr.strip())
            measure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
