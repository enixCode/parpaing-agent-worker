"""Load tests — measure throughput and latency under concurrent job submission."""

import time


class TestLoad:
    """Submits many dry_run jobs concurrently to measure Tower performance."""

    def _submit_jobs(self, client, count: int) -> list[str]:
        """Submit N dry_run jobs, return their job_ids."""
        job_ids = []
        for i in range(count):
            r = client.post("/jobs", json={
                "agent_id": f"load-{i}",
                "engine": "claude-code",
                "prompt": f"Load test job {i}",
                "dry_run": True,
            })
            assert r.status_code == 202, f"Job {i} failed: {r.text}"
            job_ids.append(r.json()["job_id"])
        return job_ids

    def _wait_all(self, client, job_ids: list[str], timeout: int = 300) -> dict:
        """Poll until all jobs finish. Returns {job_id: status}."""
        deadline = time.time() + timeout
        finished = {}
        while time.time() < deadline and len(finished) < len(job_ids):
            for jid in job_ids:
                if jid in finished:
                    continue
                r = client.get(f"/jobs/{jid}")
                assert r.status_code == 200
                data = r.json()
                if data["status"] in ("completed", "failed", "cancelled"):
                    finished[jid] = data["status"]
            if len(finished) < len(job_ids):
                time.sleep(1)
        return finished

    def test_load_10_jobs(self, client):
        """Submit 10 jobs and verify all complete."""
        start = time.time()
        job_ids = self._submit_jobs(client, 10)
        submit_time = time.time() - start

        results = self._wait_all(client, job_ids)
        total_time = time.time() - start

        completed = sum(1 for s in results.values() if s == "completed")
        failed = sum(1 for s in results.values() if s == "failed")
        missing = len(job_ids) - len(results)

        print(f"\n--- Load Test: 10 jobs ---")
        print(f"Submit time: {submit_time:.2f}s")
        print(f"Total time:  {total_time:.2f}s")
        print(f"Completed:   {completed}/10")
        print(f"Failed:      {failed}/10")
        print(f"Missing:     {missing}/10")
        print(f"Throughput:  {10 / total_time:.2f} jobs/s")

        assert missing == 0, f"{missing} jobs never finished"
        assert completed == 10, f"Only {completed}/10 completed"

    def test_load_25_jobs(self, client):
        """Submit 25 jobs to stress the pool and semaphore."""
        start = time.time()
        job_ids = self._submit_jobs(client, 25)
        submit_time = time.time() - start

        results = self._wait_all(client, job_ids, timeout=600)
        total_time = time.time() - start

        completed = sum(1 for s in results.values() if s == "completed")
        failed = sum(1 for s in results.values() if s == "failed")
        missing = len(job_ids) - len(results)

        print(f"\n--- Load Test: 25 jobs ---")
        print(f"Submit time: {submit_time:.2f}s")
        print(f"Total time:  {total_time:.2f}s")
        print(f"Completed:   {completed}/25")
        print(f"Failed:      {failed}/25")
        print(f"Missing:     {missing}/25")
        print(f"Throughput:  {25 / total_time:.2f} jobs/s")

        assert missing == 0, f"{missing} jobs never finished"
        assert completed >= 20, f"Only {completed}/25 completed (>= 20 expected)"

    def test_health_under_load(self, client):
        """Verify health endpoint stays responsive while jobs run."""
        # Submit jobs without waiting
        job_ids = self._submit_jobs(client, 5)

        # Hammer health endpoint
        latencies = []
        for _ in range(20):
            start = time.time()
            r = client.get("/health")
            latencies.append(time.time() - start)
            assert r.status_code == 200
            data = r.json()
            assert data["status"] in ("ok", "degraded")

        avg_latency = sum(latencies) / len(latencies)
        max_latency = max(latencies)

        print(f"\n--- Health under load ---")
        print(f"Avg latency: {avg_latency * 1000:.1f}ms")
        print(f"Max latency: {max_latency * 1000:.1f}ms")

        assert max_latency < 5.0, f"Health endpoint too slow: {max_latency:.2f}s"

        # Cleanup: wait for jobs
        self._wait_all(client, job_ids)
