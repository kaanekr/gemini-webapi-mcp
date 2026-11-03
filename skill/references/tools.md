# MCP Tools Reference

## Model arguments (all tools)

Every `model` parameter accepts:

- a built-in alias — `gemini-3.0-flash` (chat default), `gemini-3.0-pro`,
  `gemini-3.0-flash-thinking` (image default), plus the current web modes
  `gemini-3.5-flash-lite`, `gemini-3.8-flash`, `gemini-3.1-pro`, `gemini-4-argon`;
- an alias registered through `GEMINI_MODEL_ALIASES`
  (`'{"g4-argon": "0123456789abcdef"}'`);
- a raw 16-hex backend model id.

`GEMINI_CHAT_MODEL` / `GEMINI_IMAGE_MODEL` change the defaults. Unknown aliases
fail with the current list — the registry never guesses an id. Ask
`gemini_models` what is registered before inventing a name.

## gemini_chat

Text prompt to Gemini. Supports multi-turn and thinking mode.

**Parameters:**
| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `prompt` | yes | — | Text prompt |
| `model` | no | `gemini-3.0-flash` | Model to use |
| `session_id` | no | — | Session ID for multi-turn |

```
gemini_chat(prompt="Explain quantum computing")
```

Multi-turn:
```
gemini_chat(prompt="Now simpler", session_id="abc123")
```

Thinking mode (shows reasoning):
```
gemini_chat(prompt="Solve step by step: ...", model="gemini-3.0-flash-thinking")
```

## gemini_start_chat

Start multi-turn session. Returns `session_id` for use in `gemini_chat`.

**Parameters:**
| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `model` | no | `gemini-3.0-flash` | Model for session |

```
gemini_start_chat()
gemini_start_chat(model="gemini-3.0-pro")
```

## gemini_generate_image

Generate new images or edit existing ones. Watermark removed automatically (built-in,
deterministic — see Output).

**Parameters:**
| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `prompt` | yes | — | Image description or editing instruction |
| `files` | no | — | List of file paths for image editing |
| `model` | no | `gemini-3.0-flash-thinking` | Image model (Nano Banana 2, supports aspect ratios) |
| `conversation_id` | no | — | `[cid, rid, rcid]` from previous call for iterative refinement |

**Generate:**
```
gemini_generate_image(prompt="a serene mountain lake at sunset, oil painting style")
```

**Edit existing image:**
```
gemini_generate_image(prompt="make the sky purple", files=["/path/to/image.png"])
```

**Iterative refinement** (continue from previous generation):
```
gemini_generate_image(prompt="make it more dramatic", conversation_id=["c_xxx", "r_xxx", "rc_xxx"])
```

**Editing your OWN previous output — prefer `conversation_id` over re-passing `files`.**
To change something in an image Gemini just generated, pass back its `conversation_id`
instead of re-uploading the PNG via `files`. The model edits its own last output in place
(better fidelity). Use `files` only when the source is an ORIGINAL photo Gemini has not
produced.

**Output:** PNG saved to `~/Pictures/gemini/`, 2x upscaled resolution. Returns
`conversation_id` for continuation. The Gemini ✦ sparkle is removed automatically by the
server (built-in deterministic reverse alpha-blend; clean on any background). Both fixed
corner anchors (96 px and 32 px from the bottom-right) are processed every time with a
per-image opacity fit and a self-check that reverts any subtraction which does not reduce
the star correlation — no modes, no detection thresholds, no per-conversation state.
Set `GEMINI_WM_KEEP=1` to keep the watermark for diagnostics.

**Resolution (2x upscale):**
- 16:9: 2816x1536 (native 1408x768, auto-upscaled 2x)
- 9:16: 1536x2816 (native 768x1408, auto-upscaled 2x)
- 1:1: 2048x2048 (native 1024x1024, auto-upscaled 2x)
- Fallback to native if 2x unavailable

**Aspect ratio:** Include in prompt naturally: "wide landscape 16:9", "tall portrait 9:16". Do NOT specify model explicitly — the server picks the best image model automatically.

## gemini_upload_file

Upload file and ask Gemini about it. Supports video, images, PDF, documents.

**Parameters:**
| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `file_path` | yes | — | Absolute path to file |
| `prompt` | no | `"Describe this file."` | Question or instruction |
| `model` | no | `gemini-3.0-flash` | Model to use |

```
gemini_upload_file(file_path="/path/to/image.png", prompt="What is in this image?")
```

> For **image editing**, use `gemini_generate_image` with the `files` parameter instead.

## gemini_analyze_url

Analyze URL — YouTube videos, webpages, articles.

**Parameters:**
| Param | Required | Default | Description |
|-------|----------|---------|-------------|
| `url` | yes | — | URL to analyze |
| `prompt` | no | `"Summarize this content."` | Question about the content |
| `model` | no | `gemini-3.0-flash` | Model to use |

```
gemini_analyze_url(url="https://youtube.com/watch?v=...", prompt="Summarize this video")
```

## gemini_reset

Re-initialize client when cookies expire. No parameters.

```
gemini_reset()
```

**When to use:** Auth errors, 401/403 responses, "cookie expired" messages.

## Models

| Model | Best for | Notes |
|-------|----------|-------|
| `gemini-3.0-flash` | Text chat, analysis | Fast, default for chat |
| `gemini-3.0-flash-thinking` | Image generation, editing | Nano Banana 2, supports aspect ratios, default for images |
| `gemini-3.0-pro` | Complex reasoning | Slower, higher quality text |

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Auth error / cookies expired | `gemini_reset` |
| Stream interrupted / timeout | Server auto-retries (watchdog 45s); file editing may need 2-3 retries |
| No images in response | Prompt may violate content policy, rephrase. For file editing: update server (`git pull && uv sync`) |
| Square image despite aspect ratio | Include "16:9" or "9:16" in prompt text |
| Error 1052 / image generation fails | Google rotated model IDs. Register the new id (`GEMINI_MODEL_ALIASES='{"my-alias":"<16-hex-id>"}'` or `GEMINI_IMAGE_MODEL_ID`), or update the server: `git pull && uv sync` |
| "Gemini cookies not found" / `RequiresAdminError` while Chrome is open | Chrome locks its cookie DB. Write the JSON store (`GEMINI_COOKIE_FILE`), start Chrome with `--remote-debugging-port=9222`, or pin a source with `GEMINI_COOKIE_SOURCE` |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_PSID` | — | Override `__Secure-1PSID` cookie (instead of reading from Chrome) |
| `GEMINI_PSIDTS` | — | Override `__Secure-1PSIDTS` cookie |
| `GEMINI_ACCOUNT_INDEX` | `0` | Google account index (for multi-account Chrome profiles) |
| `GEMINI_LANGUAGE` | `en` | Language for Gemini responses |
| `GEMINI_CHAT_MODEL` | `gemini-3.0-flash` | Default chat/analysis alias or raw 16-hex id |
| `GEMINI_IMAGE_MODEL` | `gemini-3.0-flash-thinking` | Default image-generation alias or raw id |
| `GEMINI_MODEL_ALIASES` | — | JSON map of extra aliases → ids, e.g. `{"g4-argon": "0123456789abcdef"}` |
| `GEMINI_COOKIE_FILE` | `~/.gemini-webapi-mcp/cookies.json` | JSON cookie store (`{"psid": "...", "psidts": "..."}`) |
| `GEMINI_COOKIE_SOURCE` | `auto` | `auto`, `env`, `store`, `browser` or `cdp` |
| `GEMINI_COOKIE_STORE_WRITE` | `0` | `1` = rewrite the cookie store after every successful read |
| `GEMINI_CDP_URL` | — | Chrome DevTools endpoint (`http://127.0.0.1:9222`) for cookie reading |
| `GEMINI_IMAGE_MODEL_ID` | `56fdd199312815e2` | Header id forced during image generation |

**Cookie auth on headless/remote servers:**

By default, cookies are read automatically from a local Chrome browser via `browser-cookie3`. On headless servers (no Chrome), set `GEMINI_PSID` and `GEMINI_PSIDTS` manually:

1. Open Chrome on any machine → go to `gemini.google.com` → sign in
2. DevTools (F12) → Application → Cookies → `https://gemini.google.com`
3. Copy values of `__Secure-1PSID` and `__Secure-1PSIDTS`
4. Set as environment variables for the MCP server
