#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

model_id=Qwen/Qwen2.5-7B-Instruct-AWQ
revision=b25037543e9394b818fdfca67ab2a00ecc7dd641
model_path=.cache/models/Qwen2.5-7B-Instruct-AWQ
if [[ ${1:-} == --install ]]; then
  uv=${UV:-uv}
  if [[ ! -x .venv-sglang/bin/python ]]; then
    "$uv" venv --python 3.11 .venv-sglang
  fi
  "$uv" pip install --prerelease=allow --python .venv-sglang/bin/python \
    'sglang==0.5.23' python-dotenv
  .venv-sglang/bin/hf download "$model_id" --revision "$revision" --local-dir "$model_path"
  exit 0
fi

if [[ ! -x .venv-sglang/bin/python ]]; then
  echo "Install the inference environment and model with scripts/start_sglang.sh --install." >&2
  exit 1
fi
if ! .venv-sglang/bin/python scripts/serve_sglang.py --check-model "$model_path"; then
  echo "Run scripts/start_sglang.sh --install to complete the model and environment." >&2
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "Copy .env.example to .env and set SGLANG_API_KEY before starting the server." >&2
  exit 1
fi

umask 077
mkdir -p logs
if [[ ${1:-} == --serve ]]; then
  if [[ -z ${CUDA_HOME:-} && -d /usr/local/cuda ]]; then
    export CUDA_HOME=/usr/local/cuda
  fi
  if [[ -n ${CUDA_HOME:-} ]]; then
    export PATH="$CUDA_HOME/bin:$PATH"
  fi
  exec .venv-sglang/bin/python -u scripts/serve_sglang.py >> logs/sglang.log 2>&1
fi
if [[ $# -ne 0 ]]; then
  echo "Usage: scripts/start_sglang.sh [--install]" >&2
  exit 2
fi
if tmux has-session -t '=cachet-sglang' 2>/dev/null; then
  echo "tmux session cachet-sglang already exists. See logs/sglang.log."
  exit 0
fi
tmux new-session -d -s cachet-sglang -c "$PWD" 'exec scripts/start_sglang.sh --serve'
echo "Started tmux session cachet-sglang. See logs/sglang.log for readiness."
