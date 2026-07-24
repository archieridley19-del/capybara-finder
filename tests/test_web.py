"""Web layer: lead marks, the job manager, and the HTTP API.

The server is driven in-process against fixture providers, so these tests need
no network and no real port beyond a loopback bind that is torn down again.
"""

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from capyfind.providers import build_providers
from capyfind.store import Store
from capyfind.verify import verify
from capyfind.web import JobManager, make_handler
from http.server import ThreadingHTTPServer


def fixture_providers():
    return build_providers(use_fixtures=True)


class LeadMarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "m.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_mark_is_new(self):
        self.assertEqual(self.store.get_mark("x")["status"], "new")

    def test_set_status_persists(self):
        self.store.set_mark("x", status="shortlist")
        self.assertEqual(self.store.get_mark("x")["status"], "shortlist")

    def test_notes_and_status_are_independent(self):
        self.store.set_mark("x", status="chasing")
        self.store.set_mark("x", notes="called them")
        mark = self.store.get_mark("x")
        self.assertEqual(mark["status"], "chasing")
        self.assertEqual(mark["notes"], "called them")

    def test_bad_status_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.set_mark("x", status="banana")

    def test_marks_surface_in_list_runs(self):
        run = verify("invoice generator", fixture_providers(), depth="deep")
        self.store.save_run(run, seed="s")
        self.store.set_mark(run.candidate, status="rejected", notes="saturated")
        listed = {r["candidate"]: r for r in self.store.list_runs()}
        row = listed[run.candidate]
        self.assertEqual(row["lead_status"], "rejected")
        self.assertEqual(row["lead_notes"], "saturated")


class JobManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "j.db")
        self.jobs = JobManager(self.store, fixture_providers())

    def tearDown(self):
        self.tmp.cleanup()

    def _wait(self, job_id, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.jobs.get(job_id)
            if job and job["status"] in ("done", "error"):
                return job
            time.sleep(0.05)
        self.fail("job did not finish in time")

    def test_verify_job_completes_and_persists(self):
        job_id = self.jobs.start_verify("invoice generator", "UK", "sole trader")
        job = self._wait(job_id)
        self.assertEqual(job["status"], "done")
        self.assertTrue(self.store.has_run("invoice generator", "deep"))

    def test_discover_job_reports_progress(self):
        job_id = self.jobs.start_discover("UK trades", 5, "UK", "sole trader")
        job = self._wait(job_id)
        self.assertEqual(job["status"], "done")
        self.assertGreaterEqual(job["done"], 1)

    def test_unknown_job_is_none(self):
        self.assertIsNone(self.jobs.get("nope"))


class HttpApiTests(unittest.TestCase):
    """Drive the real handler over loopback against fixtures."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        store = Store(Path(cls.tmp.name) / "api.db")
        # Seed one stored run so read endpoints have something to serve.
        run = verify("invoice generator", fixture_providers(), depth="deep")
        store.save_run(run, seed="seedy")
        cls.store = store
        jobs = JobManager(store, fixture_providers())
        handler = make_handler(store, jobs, fixture_providers())
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()

    def _post(self, path, body):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())

    def test_index_serves_the_page(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("capyfind", body)

    def test_runs_endpoint_returns_stored_run(self):
        status, body = self._get("/api/runs")
        data = json.loads(body)
        self.assertTrue(any(r["candidate"] == "invoice generator" for r in data))
        self.assertIn("lead_status", data[0])

    def test_report_endpoint_renders(self):
        status, body = self._get("/api/report?candidate=invoice%20generator&depth=deep")
        self.assertEqual(status, 200)
        self.assertIn("INVOICE GENERATOR", body)

    def test_preview_lists_candidates(self):
        status, body = self._post("/api/preview", {"seed": "UK trades", "limit": 10})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(body["candidates"]), 1)

    def test_lead_endpoint_sets_status(self):
        status, body = self._post(
            "/api/lead", {"candidate": "invoice generator", "status": "shortlist"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "shortlist")

    def test_lead_rejects_bad_status(self):
        try:
            self._post("/api/lead", {"candidate": "x", "status": "nope"})
            self.fail("expected an HTTP error")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_run_endpoint_requires_a_seed(self):
        try:
            self._post("/api/run", {"mode": "discover"})
            self.fail("expected an HTTP error")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_run_then_poll_to_completion(self):
        _, res = self._post("/api/run", {"mode": "verify", "candidate": "invoice generator"})
        job_id = res["job_id"]
        deadline = time.time() + 10
        state = None
        while time.time() < deadline:
            _, state = self._get(f"/api/job?id={job_id}")
            state = json.loads(state)
            if state["status"] in ("done", "error"):
                break
            time.sleep(0.1)
        self.assertEqual(state["status"], "done")

    def test_csv_export_downloads(self):
        status, body = self._get("/export.csv")
        self.assertEqual(status, 200)
        self.assertIn("candidate", body)


if __name__ == "__main__":
    unittest.main()
