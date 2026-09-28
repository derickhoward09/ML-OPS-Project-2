#!/usr/bin/env bash
# Set OpenCode environment variables for WPI bedrock, then run opencode.

export WPI_LLM_API_KEY="$(cat ~/.private/wpi_llm_token)"
export OPENCODE_CONFIG_CONTENT='{
  "provider": {
    "wpi": {
    "npm": "@ai-sdk/openai-compatible",
    "name": "WPI GGPT",
    "options": {
        "baseURL": "https://ggpt-llm-p-u02.int.wpi.edu/v1",
        "apiKey": "{env:WPI_LLM_API_KEY}"
    },
    "models": {
        "nemotron-3-super-120b-a12b": { "name": "Nemotron 3 Super 120B-A12B" },
        "minimax-m2.5": { "name": "MiniMax M2.5" },
        "qwen3-coder-next": { "name": "Qwen3 Coder Next" },
        "kimi-k2.5": { "name": "Kimi K2.5" },
        "gemma-4-31b": { "name": "Gemma 4 31b" },
        "gemma-4-26b-a4b": { "name": "Gemma 4 26b a4b" },
        "gemma-4-e2b": { "name": "Gemma 4 e2b" },
        "qwen3.8:27b": { "name": "Qwen3.8:27b" }
    }
  }
},
"model": "wpi/minimax-m2.5"
}'

echo " OpenCode configured for WPI GGPT"
echo "  BASE_URL = https://ggpt-llm-p-u02.int.wpi.edu/v1"
echo "  MODEL    = wpi/minimax-m2.5"
opencode
