"""
Author: Trịnh Thiên Quang
Đo thời gian tiếp nhận và hoàn tất tác vụ, ghi nhận lỗi và timeout đầy đủ.
"""

import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request


def _derive_history_url(chat_url):
    parsed = urllib.parse.urlsplit(chat_url)
    path = (
        parsed.path[:-5] + "/history"
        if parsed.path.endswith("/chat")
        else parsed.path.rstrip("/") + "/history"
    )
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _poll_result(url, token, action_id, deadline):
    parsed = urllib.parse.urlsplit(url)
    while time.perf_counter() < deadline:
        cursor, seen = None, set()
        while time.perf_counter() < deadline:
            query = dict(urllib.parse.parse_qsl(parsed.query))
            query["limit"] = "100"
            if cursor:
                query["cursor"] = cursor
            target = urllib.parse.urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    urllib.parse.urlencode(query),
                    "",
                )
            )
            req = urllib.request.Request(
                target, headers={"Authorization": f"Bearer {token}"}, method="GET"
            )
            with urllib.request.urlopen(
                req, timeout=min(10, max(0.1, deadline - time.perf_counter()))
            ) as reply:
                data = json.loads(reply.read())
            for item in data.get("items", []):
                if item.get("action_id") == action_id:
                    return item
            cursor = data.get("cursor")
            if not cursor:
                break
            if cursor in seen:
                raise ValueError("History returned a repeated cursor")
            seen.add(cursor)
        time.sleep(min(0.4, max(0, deadline - time.perf_counter())))
    return None


def benchmark_live_endpoint(
    endpoint_url, *, jwt_token="", history_url="", iterations=5, poll_timeout_s=25.0
):
    """Report accepted, completed, failed and timed-out samples alongside latency."""
    if iterations < 1 or poll_timeout_s < 0:
        raise ValueError(
            "iterations must be positive and polling timeout must be non-negative"
        )
    token = (jwt_token or os.environ.get("SATIBOT_BENCHMARK_JWT", "")).strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise ValueError(
            "Provide Cognito JWT via --jwt-token or SATIBOT_BENCHMARK_JWT."
        )
    history = (
        history_url
        or os.environ.get("SATIBOT_BENCHMARK_HISTORY_URL")
        or _derive_history_url(endpoint_url)
    )
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    result = {
        "endpoint": endpoint_url,
        "submitted": iterations,
        "accepted": 0,
        "completed": 0,
        "succeeded": 0,
        "failed": 0,
        "timed_out": 0,
        "samples": [],
    }
    accept, completed = [], []
    for index in range(iterations):
        sample = {"iteration": index + 1}
        start = time.perf_counter()
        payload = json.dumps(
            {"message": f"Liệt kê các hàm Lambda, lần {index + 1}"}
        ).encode()
        try:
            request = urllib.request.Request(
                endpoint_url, data=payload, headers=headers, method="POST"
            )
            with urllib.request.urlopen(request, timeout=20) as reply:
                status, data = reply.status, json.loads(reply.read())
            sample["accept_ms"] = round((time.perf_counter() - start) * 1000, 2)
            accept.append(sample["accept_ms"])
            if status != 202 or not data.get("action_id"):
                raise ValueError(
                    "Expected HTTP 202 with action_id for the benchmark task"
                )
            result["accepted"] += 1
            sample["action_id"] = data["action_id"]
            item = _poll_result(
                history, token, data["action_id"], time.perf_counter() + poll_timeout_s
            )
            if item is None:
                sample["outcome"] = "timeout"
                result["timed_out"] += 1
            else:
                latency = round((time.perf_counter() - start) * 1000, 2)
                sample.update(workflow_e2e_ms=latency, status=item.get("status"))
                completed.append(latency)
                result["completed"] += 1
                success = item.get("status") == "SUCCESS"
                sample["outcome"] = "success" if success else "failed"
                result["succeeded" if success else "failed"] += 1
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
            sample["outcome"] = "failed"
            sample["error"] = (
                f"HTTP {exc.code}"
                if isinstance(exc, urllib.error.HTTPError)
                else type(exc).__name__
            )
            result["failed"] += 1
        result["samples"].append(sample)
        time.sleep(0.3)
    result["accept_initial_ms"] = result["samples"][0].get("accept_ms")
    result["accept_warm_avg_ms"] = (
        round(statistics.mean(accept[1:] or accept), 2) if accept else None
    )
    if completed:
        result["workflow_e2e_initial_ms"] = result["samples"][0].get("workflow_e2e_ms")
        result["workflow_e2e_avg_ms"] = round(statistics.mean(completed), 2)
    return result
