"""
Author: Trịnh Thiên Quang
Kiểm thử đóng gói Lambda và benchmark.
"""

import hashlib
import sys
import time
from unittest.mock import Mock
import zipfile
import pytest
from scripts.benchmark_coldstart import (
    benchmark_cloud_lambdas,
    benchmark_live_endpoint,
    benchmark_local_dispatcher,
    benchmark_subprocess_coldstart,
    benchmark_tool_registry,
)
from scripts.package_backend import package
from .helpers import ROOT


def test_slim_per_lambda_packaging_and_coldstart_benchmark_suite(tmp_path):
    package(tmp_path, with_dependencies=False)
    for slim_name in ("formatter", "connector", "history", "auth_handler"):
        with zipfile.ZipFile(tmp_path / f"{slim_name}.zip") as zf:
            names = set(zf.namelist())
            assert f"backend/functions/{slim_name}/index.py" in names
            assert "backend/shared/tool_registry.py" not in names
    for rich_name in ("chatbot", "dispatcher"):
        with zipfile.ZipFile(tmp_path / f"{rich_name}.zip") as zf:
            assert "backend/shared/tool_registry.py" in set(zf.namelist())

    local_metrics = benchmark_local_dispatcher(iterations=3)
    reg_metrics = benchmark_tool_registry(iterations=4)
    cloud_metrics = benchmark_cloud_lambdas(iterations=3)
    assert local_metrics["cold_ms"] > 0 and local_metrics["warm_avg_ms"] >= 0
    assert reg_metrics["cache_stats"]["hits"] >= 3
    assert set(cloud_metrics) == {
        "chatbot",
        "dispatcher",
        "formatter",
        "connector",
        "history",
    }


def test_deterministic_zip_hashes_and_subprocess_coldstart(tmp_path):
    dir_1 = tmp_path / "build1"
    dir_2 = tmp_path / "build2"
    package(dir_1, with_dependencies=False)
    time.sleep(1.1)
    package(dir_2, with_dependencies=False)
    for name in ("chatbot", "dispatcher", "formatter", "connector", "history", "auth_handler"):
        h1 = hashlib.sha256((dir_1 / f"{name}.zip").read_bytes()).hexdigest()
        h2 = hashlib.sha256((dir_2 / f"{name}.zip").read_bytes()).hexdigest()
        assert h1 == h2

    # Use this test's source packages with the current interpreter's dependencies.
    # Linux deployment wheels in dist/backend cannot run on a Windows interpreter.
    # The CI artifact smoke check separately verifies the vendored Linux packages.
    cold_report = benchmark_subprocess_coldstart(dir_1, warm_runs=2)
    assert set(cold_report) == {
        "chatbot", "dispatcher", "formatter", "connector", "history",
    }
    assert all(item["import_init_ms"] > 0 for item in cold_report.values())


def test_live_benchmark_enforces_jwt_and_tracks_async_workflow_completion(monkeypatch):
    monkeypatch.delenv("SATIBOT_BENCHMARK_JWT", raising=False)
    with pytest.raises(ValueError, match="JWT"):
        benchmark_live_endpoint(
            "https://example.execute-api.ap-southeast-1.amazonaws.com/api/chat",
            iterations=1,
        )

    seen_headers = []

    def _fake_urlopen(req, timeout=20):
        seen_headers.append(dict(req.headers))
        resp = Mock()
        resp.__enter__ = Mock(return_value=resp)
        resp.__exit__ = Mock(return_value=False)
        if req.full_url.endswith("/chat"):
            resp.status = 202
            resp.read.return_value = b'{"status":"ACCEPTED","action_id":"act_live_1"}'
        else:
            resp.status = 200
            resp.read.return_value = (
                b'{"items":[{"action_id":"act_live_1","status":"SUCCESS"}]}'
            )
        return resp

    monkeypatch.setattr("scripts.benchmark_live.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("scripts.benchmark_live.time.sleep", lambda *_a: None)
    metrics = benchmark_live_endpoint(
        "https://example.execute-api.ap-southeast-1.amazonaws.com/api/chat",
        jwt_token="eyJ.demo.token",
        iterations=2,
    )
    assert metrics["accept_initial_ms"] >= 0
    assert "workflow_e2e_avg_ms" in metrics
    assert all(h.get("Authorization") == "Bearer eyJ.demo.token" for h in seen_headers)
