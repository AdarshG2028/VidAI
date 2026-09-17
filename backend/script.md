# Setu Demo — Recording Script

**Target: ~6-7 min.** Read the SAY lines naturally, don't recite word-for-word.

## Tabs open
1. Swagger: `localhost:8000/docs`
2. Adminer: `localhost:8082` (server `postgres`, user/pass/db `setu`) — tables: `jobs`, `outbox_events`, `worker_executions`, `results`
3. Grafana: `localhost:3000`
4. Jaeger: `localhost:16686`
5. EC2 link: `http://16.171.32.244:8000/docs`

## Terminals
- dummy worker terminal (watch + kill this one)
- scratch terminal for curl

---

## 1. Guaranteed delivery — outbox pattern (60s)

**SAY:** "Every job submission is atomic — the job and its dispatch event are written in the same database transaction, so a job can never be silently lost between submission and the queue."

```powershell
curl.exe -X POST http://localhost:8000/jobs -H "Content-Type: application/json" -H "Idempotency-Key: final-1" -d '{"workflow": ["dummy"], "payload": {}}'
```

→ Switch to Adminer `outbox_events`, refresh, point at newest row (`topic`, `status`, `partition_key`).

**SAY:** "This row existed the instant I submitted. A background publisher polls it and marks it `published` only after Kafka acknowledges receipt — if it crashed before that ack, the row just stays `pending` and retries. Nothing gets lost."

---

## 2. Idempotency (30s)

Resubmit the **exact same command** (same key `final-1`):
```powershell
curl.exe -X POST http://localhost:8000/jobs -H "Content-Type: application/json" -H "Idempotency-Key: final-1" -d '{"workflow": ["dummy"], "payload": {}}'
```

**SAY:** "Same key, same job ID back — not a duplicate."

→ Adminer `jobs` table: still exactly one row.

---

## 3. Retry + exponential backoff + DLQ (60s)

**SAY:** "Now let's force a failure and watch the retry policy."

```powershell
curl.exe -X POST http://localhost:8000/jobs -H "Content-Type: application/json" -H "Idempotency-Key: final-2" -d '{"workflow": ["dummy"], "payload": {"_fail": true}}'
```

→ Cut to dummy worker terminal — 5 attempts, delays growing ~2s/4s/8s/16s.

**SAY:** "Attempt 1 retries after 2 seconds, attempt 2 after 4 — doubling each time."

→ Adminer `worker_executions`, filter to this job — rows piling up, `status='failed'`, `attempt` incrementing.

→ After ~30s: Adminer `jobs` → `status = dead_lettered`.

**SAY:** "Out of retries — marked terminal, not stuck retrying forever."

*(optional)* show DLQ message:
```powershell
docker exec setu-redpanda-1 rpk topic consume dummy.dlq -n 1
```

---

## 4. HEADLINE: survive an induced worker crash (75s)

**SAY:** "This is the core requirement — kill a worker mid-job and prove nothing is lost, nothing duplicated."

```powershell
curl.exe -X POST http://localhost:8000/jobs -H "Content-Type: application/json" -H "Idempotency-Key: final-3" -d '{"workflow": ["dummy"], "payload": {"_hang_seconds": 10}}'
```

→ **Immediately** switch to dummy worker terminal, kill it (Ctrl+C) **before 10s elapse**.

→ Adminer `results` + `worker_executions`, filter to this job → **zero rows**. Adminer `jobs` → still `pending`/`running`.

**SAY:** "Zero result rows, job still pending — the crash happened before anything committed."

→ Restart the worker:
```powershell
uv run python -m backend.workers.cli dummy --worker dummy --metrics-port 9107
```

→ Wait ~2s, refresh Adminer → `jobs.status = completed`, exactly **one** row each in `results`/`worker_executions`.

**SAY:** "Completed, exactly one row, zero duplicates. No loss, no duplication — proven live."

---

## 5. Observability (45s)

→ Flash a log line from a worker terminal, point at `job_id` and `trace_id`.

**SAY:** "Every log line is structured JSON, correlated by job_id and trace_id — one grep reconstructs a job's whole story across processes."

→ Grafana dashboard.

**SAY:** "Live job lifecycle gauges, throughput, retry/DLQ rates, processing latency."

→ Jaeger, open a recent trace.

**SAY:** "Distributed tracing — one request, one trace, across every service it touches."

---

## 6. BONUS: 6-stage AI pipeline (30s)

**SAY:** "One more thing beyond what was asked — the same engine orchestrates multi-stage pipelines, zero engine changes to add stages."

```powershell
curl.exe -X POST http://localhost:8000/jobs -H "Content-Type: application/json" -H "Idempotency-Key: final-4" -d '{"workflow": ["frame_extraction", "grounding_dino", "sam2", "tracking", "propainter", "rendering"], "payload": {}}'
```

→ Adminer `results`, filter to job, sort by `stage`. Point at `propainter.output_reference` == `rendering.based_on_output_reference`.

**SAY:** "Stage 6 is quoting stage 5's own output — a real dependency chain, not six independent stubs."

---

## 7. BONUS: it's actually deployed (20s)

→ Switch to EC2 tab (`16.171.32.244:8000/docs`). Submit one job there via Swagger "Try it out", show it complete.

**SAY:** "And this isn't just my laptop — it's live on a public server right now."

---

## Closing (15s)

**SAY:** "So: guaranteed delivery via the outbox pattern, idempotency, exponential backoff retry, dead-letter queue, survives an induced crash with zero loss or duplication, and full observability through logs, metrics, and tracing. Every one of these is backed by an automated test, not just this demo."

---

## Reset between takes (if needed)

Adminer → SQL command tab:
```sql
DELETE FROM worker_executions; DELETE FROM results; DELETE FROM outbox_events; DELETE FROM idempotency_keys; DELETE FROM jobs;
```
