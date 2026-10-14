# Proposal: `folivox ui` — a local web interface

Status: **v1 implemented** (Starlette + vanilla JS, as proposed). What exists: book upload and chapter
selection, voice catalog with audition and upload/removal of your own voices, preview, render with live
progress (SSE), stop/resume, playback of built chapters, provider settings. Not done yet: voice design in
the UI, queued jobs, a generated Options screen (the options are hand-written in `static/index.html`),
and the "audition on a sentence from the book" render. Code: `src/folivox/ui/`, tests: `tests/test_ui.py`.

## Goal

A browser UI for people who do not want to type options: pick a book, hear a voice, try a short
preview, start the long render and watch it. Everything stays on the user's computer — the server binds
to `127.0.0.1`, there is no account, no telemetry, no cloud component. The CLI stays the primary
interface and keeps working unchanged; the UI is a second front end over the same code.

```bash
uv tool install "folivox[all,ui]"
folivox ui                  # opens http://127.0.0.1:8765
folivox ui --port 9000 --no-open
folivox ui --restart        # replace the running studio by this version, on the same port
folivox ui --stop
folivox ui --host 0.0.0.0 --allow-host nas.lan   # serve the network (a server; see docker.md)
```

One studio per port: the uploads, the session and the voices are shared, and only one render runs at a time. A second
`folivox ui` on the same port finds out whether the occupant is the Folivox studio (`GET /api/ping`, answered with the
saved access token: `app`, `api` level, version, pid, whether it is rendering) and says so instead of "port in use".
`--restart` and `--stop` ask it to exit (`POST /api/shutdown`; `409` while rendering unless forced) and wait until the
port is free. The page asks `/api/ping` too: a server that predates the page (lower `api` level, or no `/api/ping`)
gets a banner telling the user to restart it. Bump `API_LEVEL` in `ui/app.py` when the page starts using a new route.

## Architecture

```
browser (static HTML + small JS)  ⇄  HTTP + SSE  ⇄  folivox.ui (Starlette)  →  folivox.pipeline.render()
```

- **Server:** [Starlette](https://www.starlette.io/) + uvicorn (two small dependencies, behind the optional
  extra `folivox[ui]`; the CLI and CI do not need them). FastAPI would add pydantic and auto-docs we
  do not need for ~12 endpoints; Starlette keeps the surface small. (Alternatives: stdlib `http.server`
  — no dependencies but hand-written SSE/multipart; Gradio/NiceGUI — fastest to build but heavy and
  hard to make look like an audiobook tool.)
- **Front end:** plain HTML/CSS + a few hundred lines of vanilla JS or htmx, shipped inside the package
  (`folivox/ui/static/`). No Node build step, works offline.
- **Jobs:** one render job at a time (the TTS model occupies the GPU/RAM). A job runs
  `pipeline.render()` in a worker thread. The UI implements the existing `Events` callbacks and
  pushes them to the browser as JSON over **Server-Sent Events** (`GET /api/jobs/{id}/events`):
  `stage`, `warning`, `plan`, `chunk_done` (with retakes/RTF), `chapter_built`, `done`, `error`.
  A late-connecting browser gets the current state first, so a page reload does not lose the job.
- **Resume:** the work directory and SQLite journal are already the source of truth. Starting a job on
  the same book + options simply continues; the UI shows "resume (63 % done)" by reading the journal.
- **Cancel:** needs one small core change — a cooperative check. `Events` gets an optional
  `cancelled() -> bool` polled between fragments (default `False`); `render()` stops cleanly with the
  journal consistent, exactly as after Ctrl+C. The CLI's `KeyboardInterrupt` path is unchanged.
- **Security:** bind to loopback only, refuse non-loopback `Host`/`Origin` headers (DNS-rebinding
  guard), a persistent random token (private file `ui_token` in the user data dir) in the link the CLI prints, traded for an HttpOnly SameSite=Strict cookie on first open so the address bar stays clean, uploads limited to book formats and
  stored in a temp dir, no path parameters taken from the browser (files are addressed by id).
  API keys are never accepted or stored by the UI — it only shows which environment variables are set.

## Screens (first version, one page with sections)

1. **Book** — drop a file (or pick a path). Shows title, author, detected language (editable), cover,
   estimated duration, the chapter tree — the output of `folivox inspect`.
2. **Chapters** — checkboxes per chapter (select all / range), live estimate of the selected length.
   Depth selector.
3. **Voice** — list of catalog voices (filtered by book language) with ▶ audition of the stored
   sample; upload your own sample (+ transcript field); link to voice design later. A short
   "read this sentence from your book" audition renders ~10 s with the selected voice through the
   normal TTS path (needs the model loaded once; cached for the session).
4. **Options** — the same groups as `folivox render --help` (Text / Quality control / Output …),
   collapsed by default, defaults and descriptions generated from the CLI option definitions so
   the two never drift apart.
5. **Preview** — renders the first N characters of the first selected chapter (`--preview`) and plays
   it in the browser. The fast loop to check voice, speed and pronunciation before a 10-hour run.
6. **Render** — Start / Pause (= cancel, resumable) / Resume. Progress bar (characters done / total),
   elapsed, remaining, RTF, retakes, a log of stages and warnings, finished chapters playable as soon
   as they are built. On completion: path to the `.m4b`, "show in folder".
7. **Settings** — providers per kind (`tts`, `asr`, `llm`, `voicegen`) from the registry
   (`folivox providers`), local vs cloud badge with the "your text leaves this computer" warning, model
   directory, device. Writes the same `config.toml` (never keys).

## API sketch

| Method | Path | |
|---|---|---|
| POST | `/api/books` | upload → `{book_id, title, author, lang, chapters[], estimated_seconds}` |
| GET | `/api/gen/providers` | cloud voice providers: capabilities, price/terms notes, key state (masked) |
| POST | `/api/gen/suggest`, `/api/gen/texts`, `/api/gen/estimate`, `/api/gen/start` | design prompt (LLM), sample texts, cost estimate, start a generation job (events and cancel via `/api/jobs/{id}/…`) |
| GET | `/api/tts/engines` | reading engines: local + paid cloud ones with models, price per hour, limits, key state |
| POST | `/api/tts/estimate` | `{book_id, chapters, kind, options}` → approximate cost of the render or the preview |
| GET | `/api/gen/voices/{provider}`, `/api/gen/{job}/takes/{name}` | preset voices; audio of a take |
| POST | `/api/gen/{job}/add` | add a take to the catalog (marked commercial API) |
| GET | `/api/voices` | catalog (id, name, lang, license) |
| GET | `/api/voices/{id}/sample` | audio for audition |
| POST | `/api/preview` | `{book_id, voice, options}` → `{audio_url}` (synchronous, short) |
| POST | `/api/jobs` | start render `{book_id, voice, chapters, options, acknowledged}` → `{job_id}` (409 if busy). A cloud `options.tts` needs `acknowledged: true` (the text is sent and billed), the key, and takes optional `tts_model`, `tts_voice`, `tts_mode`, `tts_style`, `max_cost`; events include `spend` and a `cancelled` with `reason: "cost_cap"` |
| GET | `/api/jobs/{id}/events` | SSE stream of pipeline events |
| POST | `/api/jobs/{id}/cancel` | cooperative stop |
| GET | `/api/jobs/{id}/chapters/{n}.mp3` | built chapter audio |
| GET/PUT | `/api/settings` | providers and paths (no secrets) |

## Required core changes (small, CLI-neutral)

- `Events.cancelled()` hook (above).
- Option metadata shared between the Typer CLI and the UI (one list of option name, type, default,
  help, group) so the Options screen is generated, not copied.
- A function returning book structure as plain data (already what `inspect` prints).

## Scope and phasing

- **v0 (minimal first version):** Book → Chapters → Voice (catalog + audition) → Preview → Render with
  live progress, cancel/resume. Providers shown read-only. ~600 lines Python + ~400 lines JS, 8–10 tests
  (API with Starlette's `TestClient`, a fake `render` that emits events).
- **v1:** voice upload and design, settings editor, playing finished chapters, multiple queued jobs.
- **Out of scope:** accounts, remote access, hosted deployment, editing the book text in the UI.

## Open questions

1. Starlette (recommended) vs. FastAPI vs. a heavier all-in-one toolkit (Gradio/NiceGUI)?
2. Is the v0 scope above right, or should it start smaller (inspect + render progress only)?
3. Should `folivox ui` open the browser automatically (default) or just print the URL?
