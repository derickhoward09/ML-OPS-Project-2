# ResumeLens deployment verification

Verified October 1, 2026, on `student-admin@paffenroth-23.dyn.wpi.edu:23001`.
Ubuntu 22.04.5, x86_64, two CPUs, 4 GiB RAM, 2 GiB swap. The application target
remains a conservative 3.5 GB memory budget.

## Deployment and service checks

| Check | Result |
| --- | --- |
| UI | Healthy at `0.0.0.0:7860` |
| Local CPU router | Healthy at `127.0.0.1:8080` |
| systemd | Both services active and enabled for startup |
| App and backend restart | Recovered; subsequent local review completed in 11.52 s |
| Repeat deployment | Passed; cached binary deployments took 36 s and 52 s |
| Dependency reuse | Repeat uv sync checked 50 packages in 0.69 ms |
| Python dependencies, initial install | Download/preparation 2.17 s; installation 9.52 s |
| Package cleanup | Initial apt install finished; paused controller retired automatically; dpkg audit clean |
| Credentials | Deployed `.env` mode 0600; Gradio file endpoint returned 403 |
| Tests | 25 app tests plus 5 deployment tests passed; shell syntax and diff checks passed |

Runtime: uv 0.11.16, Python 3.11.15, Gradio 6.29.0, official CPU llama.cpp
build b11324 / `32dd62ee6dfa80ada846551fefec215cefc5ae1c`. The runtime archive
SHA-256 is checked before installation. The GGUF revisions are pinned in
`configure_resumelens.py`. No PyTorch, Transformers, CUDA, Spaces, or OAuth is
used. Source-based cold provisioning hit severe filesystem journal stalls;
there is no representative completed cold deployment time. The final deployer
uses the verified CPU binary, with a source fallback only if compatibility fails.

## Inference behavior

- The live router generated `Ready.` with Bonsai, then switched to Qwen.
  `/models` confirmed Bonsai unloaded and Qwen loaded: only one resident model.
- Two concurrent Gradio submissions both completed using Qwen. At most one was
  processing at a time; response times were 11.27 s and 9.02 s.
- With the CPU backend deliberately stopped, Local-first failed over to
  GPT-OSS through the server-side HF token. The response reported the mode
  switch and completed in 0.55 s. The backend was restarted afterward.
- Both remote model defaults, GPT-OSS and GLM 5.3 Flash, returned nonempty
  responses through the supplied deployment credentials.
- Live switching exposed a dropped keep-alive connection. The app now disables
  local HTTP keep-alive; the full live integration check passed after deployment.

## CPU measurements

The following Qwen measurements ran sequentially on the VM, with no competing
inference tests, a 512-token response ceiling, two threads, context 4096,
batch 128, microbatch 64, and thinking disabled. Matching prompt prefixes were
reused (210 cached tokens on later review types).

| Qwen3 0.6B Q8_0 review | Total response time | Prompt tokens/sec | Output tokens/sec |
| --- | ---: | ---: | ---: |
| Bullet Point Strength | 10.43 s | 159.48 | 36.03 |
| Job Description Match | 18.50 s | 128.49 | 27.55 |
| General Resume Review | 12.30 s | 144.02 | 33.68 |
| Skills Analysis | 8.39 s | 133.73 | 35.82 |

Ternary Bonsai 1.7B's group-64 model is 490,163,968 bytes and its downloaded
file matched the publisher's recorded SHA-256. It loads and generates on CPU.
A 15-token prompt / 3-token answer took 5.12 s: 3.42 prompt tokens/sec and
2.74 output tokens/sec. The live router smoke test measured 2.98 and 2.68
respectively. Full resume attempts reached the configured 300-second read
timeout. This compact quantization is unsuitable for quick full resume reviews
on the tested x86 CPU configuration; Qwen is much faster. At the time of this
October 1 verification, the Bonsai-first default remained. The current model
order is recorded below.

## Memory acceptance and quality limits

The final live service test measured peak combined app/router/model RSS of
1,292.73 MiB (1.26 GiB), minimum available OS memory of 3,289.66 MiB, zero
swap-in pages, zero swap-out pages, and zero OOM kills during the test. This
passes the 3.5 GB budget and 500 MB OS headroom requirements for the exercised
loads. The separate sequential model tests peaked at 1,268.03 MiB RSS.

Qwen produced feedback for all four review types, but content quality is limited:

- One run invented a 15% accuracy improvement that was not in the supplied resume.
- Another suggested changing an assistant role to a leadership role without evidence.
- Skills feedback incorrectly claimed Python and SQL were not explicitly listed.

Shared prompt constraints remain in place, but these small-model responses
require human review. Deployment/functionality and memory checks passed;
Bonsai full-resume latency and small-model factual reliability are explicit
limitations, not successful quality/latency acceptance results.

## Current local model order — October 2, 2026

The deployed local-first order is Qwen3 0.6B Q8_0 primary, followed by
LiquidAI LFM2.5 1.2B Instruct QAD Q4_0. The pinned IDs are
`Qwen/Qwen3-0.6B-GGUF` and `LiquidAI/LFM2.5-1.2B-Instruct-GGUF`. One model
remains resident at a time. This replaces the historical Bonsai/Qwen order
described above.

The direct-SSH deployment completed successfully. Readback confirmed both
model presets and GGUF files, the app's primary/backup IDs, active app and
inference services, HTTP 200 from Gradio and router health, and both model IDs
in the router catalog. The router reports them unloaded until a request selects
one. The model quality and speed samples are in
`RESUMELENS_MODEL_COMPARISON.md`; both local models still require human review.
