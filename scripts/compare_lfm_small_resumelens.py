"""Compare smaller Liquid models with Qwen on the deployment VM.

Run with ~/resumelens/.venv/bin/python. Uses synthetic bundled examples,
temporarily pauses inference, and restores the service even if a candidate
fails. Model files are resolved to immutable Hugging Face revisions before
download and results are written under .deploy/.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from huggingface_hub import HfApi, hf_hub_download

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.home() / "resumelens"
MODELS = [
    (
        "LiquidAI/LFM2-700M-GGUF",
        "LFM2-700M-Q4_0.gguf",
        "main",
    ),
    (
        "LiquidAI/LFM2.5-350M-GGUF",
        "LFM2.5-350M-QAD-Q4_0.gguf",
        "main",
    ),
    (
        "Qwen/Qwen3-0.6B-GGUF",
        "Qwen3-0.6B-Q8_0.gguf",
        "23749fefcc72300e3a2ad315e1317431b06b590a",
    ),
]

with socket.socket() as check:
    check.bind(("127.0.0.1", 8081))

subprocess.run(["systemctl", "is-active", "--quiet", "resumelens-inference"], check=True)
sys.path.insert(0, str(ROOT))
import app  # noqa: E402
from src.examples import EXAMPLES  # noqa: E402

app.LOCAL_BASE_URL = "http://127.0.0.1:8081"
token = app.require_hf_token()
api = HfApi(token=token)
resolved_models = []
for repo, filename, requested_revision in MODELS:
    revision = requested_revision
    if revision == "main":
        revision = api.model_info(repo, revision="main").sha
    started = time.perf_counter()
    path = hf_hub_download(
        repo_id=repo,
        filename=filename,
        revision=revision,
        local_dir=ROOT / "models",
        token=token,
    )
    resolved_models.append((repo, path, revision))
    print(
        json.dumps(
            {
                "download": repo,
                "revision": revision,
                "filename": filename,
                "bytes": Path(path).stat().st_size,
                "seconds": round(time.perf_counter() - started, 2),
            }
        ),
        flush=True,
    )

for repo, path, _ in resolved_models:
    started = time.perf_counter()
    with open(path, "rb", buffering=0) as model_file:
        os.posix_fadvise(model_file.fileno(), 0, 0, os.POSIX_FADV_SEQUENTIAL)
        while model_file.read(8 * 1024 * 1024):
            pass
    print(
        json.dumps({"model": repo, "cache_warm_seconds": round(time.perf_counter() - started, 2)}),
        flush=True,
    )

output = ROOT / ".deploy" / "comparison-lfm-small-2048.jsonl"
if output.exists():
    output = output.with_name(f"{output.stem}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
report = output.open("w")


def emit(value):
    report.write(json.dumps(value) + "\n")
    report.flush()
    print(json.dumps(value), flush=True)


scenarios = list(EXAMPLES) + [[EXAMPLES[0][0], kind, ""] for kind in ["General Resume Review", "Skills Analysis"]]
original_post = httpx.Client.post
last = {}


def capture(self, url, **kwargs):
    response = original_post(self, url, **kwargs)
    if url == "/v1/chat/completions" and response.is_success:
        data = response.json()
        last.update(
            {
                "timings": data.get("timings", {}),
                "usage": data.get("usage", {}),
                "finish_reason": data["choices"][0].get("finish_reason"),
            }
        )
    return response


httpx.Client.post = capture
subprocess.run(["sudo", "systemctl", "stop", "resumelens-inference"], check=True)
try:
    for repo, path, revision in resolved_models:
        started = time.perf_counter()
        log_path = ROOT / ".deploy" / ("comparison-small-" + repo.split("/")[1] + ".log")
        with log_path.open("w") as log:
            process = subprocess.Popen(
                [
                    str(ROOT / ".runtime/bin/llama-server"),
                    "--model",
                    path,
                    "--alias",
                    repo,
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8081",
                    "--threads",
                    "2",
                    "--threads-batch",
                    "2",
                    "--ctx-size",
                    "4096",
                    "--parallel",
                    "1",
                    "--batch-size",
                    "128",
                    "--ubatch-size",
                    "64",
                    "--n-gpu-layers",
                    "0",
                    "--jinja",
                    "--chat-template-kwargs",
                    '{"enable_thinking": false}',
                    "--no-context-shift",
                    "--cache-ram",
                    "0",
                ],
                stdout=log,
                stderr=log,
            )
            done = threading.Event()
            metrics = {"peak_service_rss_mib": 0, "min_available_mib": 99999}

            def counters():
                return dict(line.split() for line in Path("/proc/vmstat").read_text().splitlines())

            initial = counters()

            def sample():
                while not done.is_set():
                    mem = {
                        key: int(value.split()[0])
                        for key, value in (
                            line.split(":", 1)
                            for line in Path("/proc/meminfo").read_text().splitlines()
                        )
                    }
                    metrics["min_available_mib"] = min(
                        metrics["min_available_mib"], mem["MemAvailable"] / 1024
                    )
                    pids = [
                        str(process.pid),
                        subprocess.check_output(
                            ["systemctl", "show", "resumelens-app", "-p", "MainPID", "--value"],
                            text=True,
                        ).strip(),
                    ]
                    rss = 0
                    for pid in pids:
                        try:
                            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                                if line.startswith("VmRSS:"):
                                    rss += int(line.split()[1]) / 1024
                        except FileNotFoundError:
                            pass
                    metrics["peak_service_rss_mib"] = max(metrics["peak_service_rss_mib"], rss)
                    done.wait(0.25)

            thread = threading.Thread(target=sample, daemon=True)
            thread.start()
            try:
                for _ in range(900):
                    if process.poll() is not None:
                        raise RuntimeError("server exited; inspect the sanitized model log")
                    try:
                        if httpx.get("http://127.0.0.1:8081/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(1)
                else:
                    raise RuntimeError("model load timed out")
                emit(
                    {
                        "model": repo,
                        "revision": revision,
                        "load_seconds": round(time.perf_counter() - started, 2),
                    }
                )
                for resume, kind, job in scenarios:
                    last.clear()
                    started = time.perf_counter()
                    try:
                        response = app._run_local_model(
                            repo,
                            app._validated_prompt(resume, kind, job),
                            2048,
                            0.3,
                        )
                        emit(
                            {
                                "model": repo,
                                "review": kind,
                                "max_tokens": 2048,
                                "seconds": round(time.perf_counter() - started, 2),
                                "response": response,
                                **last,
                            }
                        )
                    except Exception as error:
                        emit(
                            {
                                "model": repo,
                                "review": kind,
                                "seconds": round(time.perf_counter() - started, 2),
                                "error": app._safe_error(error),
                            }
                        )
            except Exception as error:
                emit({"model": repo, "error": app._safe_error(error)})
            finally:
                done.set()
                thread.join()
                final = counters()
                for key in ["pswpin", "pswpout", "oom_kill"]:
                    metrics[key + "_delta"] = int(final[key]) - int(initial[key])
                emit({"model": repo, "resources": metrics})
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

finally:
    subprocess.run(["sudo", "systemctl", "start", "resumelens-inference"], check=True)
    report.close()
    httpx.Client.post = original_post
    print(json.dumps({"results_path": str(output)}), flush=True)
