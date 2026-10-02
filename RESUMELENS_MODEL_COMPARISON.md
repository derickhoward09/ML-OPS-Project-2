# CPU model comparison with a 2,048-token output ceiling

Measured October 1, 2026 on the existing two-core Ubuntu 22 CPU VM.
The smaller-model comparison below was run October 2, 2026.
The app default is now 2,048 output tokens and was verified through live Gradio configuration.
Context remains 4,096, with two threads, batch 128, microbatch 64, and no GPU.
Inputs are synthetic bundled examples; shared prompts and temperature 0.3 were unchanged.

## Collected measurements

| Model | Review | Seconds | Generated tokens | Prompt tokens/s | Generation tokens/s | Finish |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Qwen | Bullet Point Strength | 12.64 | 241 | 150.50 | 33.73 | stop |
| Qwen | Job Description Match | 17.48 | 187 | 128.99 | 27.38 | stop |
| Qwen | General Resume Review | 12.34 | 272 | 145.06 | 33.43 | stop |
| Qwen | Skills Analysis | 8.15 | 128 | 141.28 | 34.30 | stop |
| LiquidAI | Bullet Point Strength | 11.87 | 110 | 101.19 | 37.68 | stop |
| LiquidAI | Job Description Match | 20.84 | 113 | 97.54 | 32.45 | stop |
| LiquidAI | General Resume Review | 12.69 | 136 | 98.31 | 37.88 | stop |
| LiquidAI | Skills Analysis | 11.38 | 89 | 101.05 | 37.11 | stop |
| HuggingFaceTB | Bullet Point Strength | 62.60 | 595 | 65.94 | 12.34 | stop |
| HuggingFaceTB | Job Description Match | 207.68 | 1487 | 60.87 | 8.12 | stop |

## Quality assessment

- Qwen: invented leadership and numeric accomplishments (3 projects, 2 hyperparameters, 4 models, and a 20% accuracy claim). Job matching overlooked explicitly supplied W&B, distributed training, and vector search and incorrectly claimed AWS/GCP experience.
- Liquid: faster generation than Qwen, but slower prompt processing and no prefix reuse in this run. Its bullet rewrite invented deployment and changed text/audio inputs to audio-visual clips. Job matching omitted requested gap analysis; general feedback referenced an absent job description; skills feedback overlooked explicit projects. It is a viable CPU candidate, not a demonstrated factual-quality upgrade.
- Tested SmolLM2 GGUF: bullet feedback mostly echoed input and selected a project bullet despite the experience-only instruction. Job matching only echoed the supplied material. General review and skills analysis reached the full 2,048-token ceiling. The server chat template was checked and contains standard system/user/assistant ChatML boundaries. This tested configuration is unsuitable for the app; these results do not establish how every SmolLM2 checkpoint or prompt behaves.

## Resources and storage limits

- Qwen combined app/inference peak RSS: 1,268.16 MiB; minimum available memory: 3,179.72 MiB.
- Liquid combined app/inference peak RSS: 1,449.45 MiB; minimum available memory: 3,091.86 MiB.
- Those runs had zero OOM and zero swap-out pages. Small global swap-in deltas were recorded (Qwen 64 pages, Liquid 5); these counters include other VM processes and do not demonstrate sustained swapping.
- Severe host I/O pressure caused initial two-minute candidate load failures. Download times: Liquid 299.75 s, SmolLM2 359.24 s. Sequential file-cache warming took 10.99 s and 0.10 s on retry. Retry loading: Liquid 1.32 s, SmolLM2 143.02 s. These are storage-affected observations, not representative cold-start benchmarks.
- The completed SmolLM2 retry peaked at 2,667.93 MiB combined app/inference RSS and 2,218.37 MiB minimum available memory. It recorded 11,929 system-wide swap-out pages (about 46.6 MiB) and 7,650 swap-in pages (about 29.9 MiB), with no OOM kill. Swap counters are host-wide, but this run shows materially more memory pressure than the smaller candidates.
- One run per review/model at temperature 0.3; these samples are not a statistical quality evaluation. Models may stop before the output ceiling.

## Smaller-model comparison

Measured October 2 on the same two-core VM with llama.cpp b11324, two CPU threads, 4,096 context, 2,048-token output cap, temperature 0.3, and the same four synthetic review prompts. Each model was loaded separately and run once. Liquid used official Q4_0 GGUFs; Qwen used the existing Q8_0 file. Warm-cache load times were about one second.

| Model | Bullet (tokens / sec) | Job match (tokens / sec) | General (tokens / sec) | Skills (tokens / sec) | Mean review time | Weighted decode | Peak app/inference RSS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LFM2 700M Q4_0 | 365 / 13.22 | 677 / 23.92 | 631 / 15.96 | 547 / 15.05 | 17.04 s | 50.57 tok/s | 968.37 MiB |
| LFM2.5 350M QAD Q4_0 | 8 / 2.75 | 206 / 7.72 | 8 / 2.75 | 8 / 2.75 | 3.99 s | 84.39 tok/s* | 561.51 MiB |
| Qwen3 0.6B Q8_0 | 122 / 8.89 | 244 / 19.16 | 224 / 11.03 | 132 / 8.30 | 11.85 s | 31.53 tok/s | 1,233.00 MiB |

\*The 350M decode rate is not a useful quality/speed score: three responses stopped after eight tokens with only “Not evident from the resume.” Its job-match response also invented cloud, GPU, and Kubernetes qualifications and did not follow the requested structure.

- **LFM2 700M:** fastest token generation, but slower end-to-end review time than Qwen because it generated much longer answers. It invented unsupported leadership, impact metrics, TensorFlow, and cloud claims in these samples. Not a factual-quality improvement.
- **LFM2.5 350M:** smallest and fastest by wall time, but failed three review types with near-empty answers; the fourth included unsupported qualifications. Unsuitable for this resume-review prompt set.
- **Qwen3 0.6B:** faster end-to-end than LFM2 700M on these prompts; the 350M's shorter times came from near-empty replies and are not useful-speed wins. Qwen still misread the supplied resume and task in this run: it treated a role heading as a bullet and said the resume lacked technical skills despite the listed skills.
- No candidate hit the output ceiling. All three had zero OOM kills and zero swap-out pages in this run; minimum available memory was 3,345.98 MiB (700M), 3,544.99 MiB (350M), and 3,267.68 MiB (Qwen). LFM2 700M recorded six global swap-in pages; the others recorded none.
- Model revisions: LFM2 700M `fd39e80d7a5ac61494ffff577e61bbbfddbd0d02`; LFM2.5 350M `657e078c94084481950a2d555a941481f715536b`; Qwen3 0.6B `23749fefcc72300e3a2ad315e1317431b06b590a`.
- The direct benchmark script is `scripts/compare_lfm_small_resumelens.py`; raw results are in `benchmarks/comparison-lfm-small-2048.jsonl`.

## Retrieval and final service check

On October 2, the completed retry JSONL was retrieved and the local partial snapshot was replaced. Both production systemd services were active afterward; Gradio and the inference health endpoint returned HTTP 200, and temporary benchmark port 8081 was free. The benchmark used the direct SSH connection.

After the comparison, the local model order was changed to Qwen3 0.6B Q8_0 primary and LiquidAI LFM2.5 1.2B Instruct QAD Q4_0 backup. Deployment verification is recorded in `RESUMELENS_VERIFICATION.md`. The comparison script and pinned candidates are in case study 2. The response-token default is deployed. All 32 app/deployment tests passed before this model-order update, including jump-host defaults, explicit direct-connection override, known-host checking, and secret-safe transport.
