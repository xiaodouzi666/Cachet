# Third-party notices

Cachet distributes selected Starlette and FastAPI source lines as prompt text. The excerpts demonstrate ASGI request routing, middleware, custom route handlers and their tests. Each file header identifies its original path and inclusive line range. The code between headers is copied verbatim. Each project retains its own license and attribution.

| Project | Use | Attribution | License |
| --- | --- | --- | --- |
| [Starlette 1.10.0](https://github.com/Kludex/starlette/tree/8aa828ceaff764a2e57932fb42fdf105a47c4c83) | L1 source context | Copyright © 2018, Encode OSS Ltd. | [BSD-3-Clause](contexts/sources/starlette/LICENSE.md) |
| [FastAPI 0.162.2](https://github.com/fastapi/fastapi/tree/3adc94ef0fb4ffd77bace5467baeb758b43e3da5) | L2 source context, layered on Starlette | Copyright (c) 2018 Sebastián Ramírez | [MIT](contexts/sources/fastapi/LICENSE) |
| [HTTPX 0.28.1](https://github.com/encode/httpx/tree/26d48e0634e6ee9cdc0533996db289ce4b430177) | HTTP client dependency | Copyright © 2019, Encode OSS Ltd. | [BSD-3-Clause](contexts/sources/httpx/LICENSE.md) |
| [Qwen2.5-7B-Instruct-AWQ](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-AWQ/tree/b25037543e9394b818fdfca67ab2a00ecc7dd641) | Externally obtained inference model and tokenizer | Copyright 2024 Alibaba Cloud | [Apache-2.0](contexts/sources/qwen/LICENSE) |

BSD-3-Clause permits redistribution subject to retention of its notices and disclaimer and the restriction on using contributor names to endorse derived products. MIT permits redistribution with its copyright and permission notices. Apache-2.0 permits redistribution under its terms, including providing the license, retaining applicable notices and identifying modified files.

## Reproducing the context lengths

The manifest records each source revision and SHA-256 digest, the text and token hashes of each layer, and the pinned tokenizer digest. Tokenize the UTF-8 text of each layer independently, including file headers and the final newline, with special-token insertion disabled. The token hash covers unsigned 32-bit token IDs in little-endian order. [Source manifest](contexts/manifest.json)

The default model configuration allows 32,768 tokens. The layers occupy 25,900 in total. Reserving 4,096 for conversation, generation and prompt framing leaves another 2,772. [Pinned model configuration](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-AWQ/blob/b25037543e9394b818fdfca67ab2a00ecc7dd641/config.json)

Run the following from the repository root in a Python environment with the Hugging Face `tokenizers` package installed. You can also pass the tokenizer JSON from an existing model snapshot.

```sh
curl -fsSL https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-AWQ/resolve/b25037543e9394b818fdfca67ab2a00ecc7dd641/tokenizer.json -o /tmp/cachet-qwen-tokenizer.json
python scripts/verify_contexts.py /tmp/cachet-qwen-tokenizer.json
```
