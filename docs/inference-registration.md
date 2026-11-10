# Register a self-hosted GPU

The API on Render uses `INFERENCE_ENDPOINT_MODE=registered`. Set its `SGLANG_API_KEY` to the GPU host's engine key. Keep both this key and the backend administration token on the server. Set `PUBLIC_API_URL` on the GPU machine to the HTTPS origin of the Render API. If the deployed API uses a separate administration credential, configure it on the GPU host as `PRODUCTION_ADMIN_TOKEN`.

Run these commands on the GPU host from the repository root:

```sh
scripts/start_sglang.sh
.venv/bin/python scripts/gpu_tunnel.py install
.venv/bin/python scripts/gpu_agent.py start
```

The agent runs under tmux as `cachet-gpu-agent` and reports the model, GPU and SGLang version every 30 seconds. A new public origin triggers an immediate registration. The API probes the announced origin over HTTPS. A healthy probe and a heartbeat younger than 90 seconds enable live inference. After loss of the endpoint, the API selects its configured and verified hosted fallback, or recorded sessions if one is available. Check `/api/health` for the active mode.

The Quick Tunnel forwards to the authenticated loopback proxy on port 18765. It carries only Cachet's inference and cache operations to the SGLang server on port 30000. The agent derives a bearer token for each public origin. TLS verification uses the announced hostname, including when Cloudflare's HTTPS DNS resolver supplies a newly published address. The native engine and its administration routes stay on loopback. A managed tunnel can be used by configuring `TUNNEL_FIXED_URL` and `CLOUDFLARE_TUNNEL_TOKEN` on the GPU host, provisioning that origin to forward to the same loopback proxy, and setting the matching `TUNNEL_FIXED_URL` on the API.

To test a live deployment's physical tunnel loss and restoration, run this on its GPU host during a planned test window. It stops only the inference tunnel, polls the actual API mode, then restarts the connector. The resulting JSON records the observed times and the model identity after recovery.

```sh
.venv/bin/python scripts/check_gpu_registration.py --output logs/inference-recovery.json
```

For local development, `INFERENCE_ENDPOINT_MODE=direct` connects to the configured `SGLANG_BASE_URL` on loopback. Local and recorded demos do not need a public tunnel.
