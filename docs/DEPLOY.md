# Deploying to Hugging Face Spaces

The repository is already a valid **Docker Space**. The YAML header at the top of `README.md` declares `sdk: docker` and `app_port: 7860`, and the `Dockerfile` runs the app as uid 1000, which is what Spaces expects.

## Option A: push directly

```bash
# 1. Create a new Space at https://huggingface.co/new-space and pick "Docker" -> "Blank".
# 2. Push this repository to it:
git remote add space https://huggingface.co/spaces/<your-hf-username>/harness2-lab
git push space main
```

The Space builds the image and starts `uvicorn app.server:app` on port 7860. The offline simulator needs no secrets.

## Option B: keep GitHub as the source of truth

`.github/workflows/sync-to-hf.yml` mirrors every push to `main` into the Space. To set it up, go to the GitHub repo's **Settings → Secrets and variables → Actions** and add:

- the secret `HF_TOKEN`: a Hugging Face access token with write permission
- the variable `HF_SPACE`: `<your-hf-username>/harness2-lab`

## Optional settings (Space → Settings → Variables and secrets)

| Name | Default | Meaning |
|---|---|---|
| `H2_MAX_ACTIVE_RUNS` | 4 | Concurrent runs the Space accepts |
| `H2_MAX_REAL_TASKS` | 3 | Maximum tasks per real-model run |
| `H2_MAX_REAL_K` | 3 | Maximum recursion budget per real-model run |
| `H2_ALLOW_SERVER_KEYS` | unset | Set to `1` to let visitors run on **your** key |
| `ANTHROPIC_API_KEY` | | Used only when `H2_ALLOW_SERVER_KEYS=1` |
| `OPENAI_API_KEY` / `HF_TOKEN` | | Used only when `H2_ALLOW_SERVER_KEYS=1` (OpenAI-compatible backend) |

> **Before you set a server-side key**, remember that the agent executes code it writes. The code runs in a sandboxed subprocess with no environment variables, but treat that as defence in depth. Only allow server keys on a private Space, or one where you accept that visitors spend your quota.

## Run locally

```bash
pip install -r requirements.txt
uvicorn app.server:app --port 7860       # http://localhost:7860
# or the same image the Space builds:
docker build -t h2lab . && docker run -p 7860:7860 h2lab
```
