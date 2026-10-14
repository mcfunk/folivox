"""Starlette app behind ``folivox ui``: a thin JSON/SSE layer over the same code the CLI uses."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import secrets
import shutil
import urllib.parse
import uuid
from pathlib import Path

from platformdirs import user_data_dir
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from folivox import config
from folivox import voices as catalog
from folivox.pipeline import QA_LEVELS, RenderOptions
from folivox.text import pronounce as pronunciations
from folivox.ui.jobs import JobManager, chapter_files

STATIC = Path(__file__).parent / "static"
# Bumped when the page starts using a route that servers started before the update do not have: the page asks
# /api/ping and, if the server is older, tells the user to restart it.
API_LEVEL = 3
MAX_UPLOAD = 200 * 1024 * 1024
PREVIEW_CHARS = 600


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status, self.extra = status, extra


class State:
    def __init__(self, data_dir: Path, render=None):
        self.data_dir = data_dir
        self.books: dict[str, dict] = {}
        self.manager = JobManager(render)
        self.gen_jobs: dict = {}

    def book(self, book_id: str) -> dict:
        """The uploaded book; re-read from disk after a server restart."""
        if book_id not in self.books:
            from folivox.readers import read_book

            folder = self.data_dir / "uploads" / Path(book_id).name
            files = [p for p in folder.iterdir() if p.is_file()] if folder.is_dir() else []
            if not files:
                raise ApiError("Unknown book; upload it again", 404)
            self.books[book_id] = {"book": read_book(files[0]), "path": files[0]}
        return self.books[book_id]

    @property
    def session_file(self) -> Path:
        return self.data_dir / "session.json"


def _chapters(book, depth: int) -> list[dict]:
    cps = book.chars / book.estimated_seconds if book.estimated_seconds else 14.0
    return [{"n": i, "title": ch.title, "chars": ch.chars, "seconds": ch.chars / cps}
            for i, ch in enumerate(book.chapters(depth), 1)]


def _book_summary(book_id: str, book, depth: int = 2, cover: str = "") -> dict:
    from folivox import lang as languages

    try:
        lang_name = languages.get(book.lang).native
    except ValueError:
        lang_name = book.lang
    return {"book_id": book_id, "title": book.title, "author": book.author, "lang": book.lang,
            "lang_name": lang_name, "format": book.source_format, "year": book.year, "has_cover": bool(book.cover),
            "cover": cover,
            "chars": book.chars, "estimated_seconds": book.estimated_seconds, "chapters": _chapters(book, depth)}


_PEAKS: dict[tuple[str, float], dict] = {}


def _wave(path: Path, bars: int = 64) -> dict:
    """Waveform thumbnail of a voice sample: ``{peaks: [0..100]*bars, seconds}`` (empty without soundfile)."""
    try:
        key = (str(path), path.stat().st_mtime)
        if key not in _PEAKS:
            import numpy as np
            import soundfile as sf

            data, sr = sf.read(str(path), dtype="float32", always_2d=True)
            mono = np.abs(data).max(axis=1)
            chunks = np.array_split(mono, bars)
            peaks = np.array([c.max() if c.size else 0.0 for c in chunks])
            peaks = peaks / (peaks.max() or 1.0)
            _PEAKS[key] = {"peaks": [int(round(100 * p)) for p in peaks], "seconds": round(len(mono) / sr, 1)}
        return _PEAKS[key]
    except Exception:  # noqa: BLE001 — a thumbnail is a nicety
        return {"peaks": [], "seconds": 0}


def _voice_json(v) -> dict:
    return {**_wave(v.path), "id": v.id, "name": v.name, "gender": v.gender, "lang": v.lang, "license": v.license,
            "bundled": v.bundled, "commercial": v.commercial, "default": v.id == catalog.DEFAULT_VOICE,
            "transcript": v.transcript}


def _registries():
    from folivox.llm import LLMS
    from folivox.qa.asr import ASR_PROVIDERS
    from folivox.tts import ENGINES
    from folivox.voicegen import GENERATORS

    return {r.kind: r for r in (ENGINES, ASR_PROVIDERS, GENERATORS, LLMS)}


def create_app(token: str | None = None, port: int | None = None, data_dir: Path | None = None,
               render=None, services: dict | None = None, allow_hosts: tuple[str, ...] | list[str] = ()) -> Starlette:
    """``services`` (tests): ``generator(name)``, ``llm(name)`` and ``asr(lang)`` factories replacing the real ones.
    ``allow_hosts``: more names the studio answers to besides localhost (a server, a reverse proxy), each with or
    without ``:port``; ``*`` turns the Host check off (the token still guards the API)."""
    state = State(data_dir or Path(user_data_dir("folivox", appauthor=False)) / "ui", render)
    services = services or {}
    # what the cookie holds: derived from the token, so it survives restarts but the token itself never sits in it
    session = (hmac.new(token.encode(), b"folivox-ui-cookie", "sha256").hexdigest() if token
               else secrets.token_urlsafe(24))
    cookie_name = f"folivox_session_{port or 0}"
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"} if port else None
    if allowed_hosts is not None:
        for h in allow_hosts:
            if h == "*":
                allowed_hosts = None
                break
            has_port = re.fullmatch(r"(\[[^\]]+\]|[^:\[\]]+):\d+", h)
            allowed_hosts |= {h} if has_port else {h, f"{h}:{port}"}

    # ---- handlers -------------------------------------------------------------------------------
    async def index(request: Request):
        given = request.query_params.get("token")
        if token and given is not None:  # the link printed by `folivox ui`: trade the token for a cookie, hide it
            if not secrets.compare_digest(given, token):
                return JSONResponse({"error": "Wrong token"}, status_code=401)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(cookie_name, session, httponly=True, samesite="strict", path="/", max_age=90 * 86400)
            return resp
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))

    async def ping(request: Request):
        """Who is on this port: lets `folivox ui` tell its own studio from another program, and the page tell an
        outdated server from a current one."""
        import os

        from folivox import __version__

        return JSONResponse({"app": "folivox-studio", "api": API_LEVEL, "version": __version__, "pid": os.getpid(),
                             "busy": state.manager.busy()})

    async def shutdown(request: Request):
        """Stop this server (`folivox ui --restart` / `--stop`); a running job is not killed without ``force``."""
        from starlette.background import BackgroundTask

        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        if state.manager.busy() and not (isinstance(body, dict) and body.get("force")):
            raise ApiError("A render is running; stopping the studio would interrupt it", 409, busy=True)
        request_exit = getattr(app.state, "request_exit", None)
        if request_exit is None:
            raise ApiError("This server cannot be stopped from outside", 501)
        return JSONResponse({"stopping": True}, background=BackgroundTask(request_exit))

    async def i18n_catalog(request: Request):
        """UI message catalogs (static JSON next to index.html); the code is checked against the shipped files."""
        code = request.path_params["code"]
        path = STATIC / "i18n" / f"{code}.json"
        if not re.fullmatch(r"[a-z]{2}", code) or not path.is_file():
            return JSONResponse({"error": "Unknown language"}, status_code=404)
        return Response(path.read_text(encoding="utf-8"), media_type="application/json",
                        headers={"Cache-Control": "no-cache"})

    async def upload_book(request: Request):
        from folivox.readers import UnsupportedFormat, read_book

        form = await request.form()
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise ApiError("No file in the request")
        data = await f.read()
        if len(data) > MAX_UPLOAD:
            raise ApiError("File is too large", 413)
        book_id = uuid.uuid4().hex[:12]
        folder = state.data_dir / "uploads" / book_id
        folder.mkdir(parents=True)
        path = folder / (Path(f.filename or "book").name or "book")
        path.write_bytes(data)
        try:
            book = await asyncio.to_thread(read_book, path)
        except UnsupportedFormat as e:
            shutil.rmtree(folder, ignore_errors=True)
            raise ApiError(str(e), 415) from None
        except Exception as e:  # noqa: BLE001 — a broken book must not kill the server
            shutil.rmtree(folder, ignore_errors=True)
            raise ApiError(f"Cannot read the book: {e}") from None
        state.books[book_id] = {"book": book, "path": path}
        write_session({"book_id": book_id})  # the last uploaded book survives a reload even before any other change
        return JSONResponse(_book_summary(book_id, book, cover=cover_kind(book_id, book)))

    # ---- book details shown in the player (title, author, narrator, series…) -------------------------
    META_FIELDS = {"title": 300, "author": 300, "narrator": 300, "series": 300, "series_part": 20, "year": 20,
                   "description": 2000, "genre": 100}

    def meta_file(book_id: str) -> Path:
        return state.data_dir / "meta" / f"{Path(book_id).name}.json"

    def meta_defaults(book) -> dict:
        return {"title": book.title or "", "author": book.author or "", "narrator": "", "series": "",
                "series_part": "", "year": str(book.year or ""), "description": book.annotation or "",
                "genre": "Audiobook"}

    def book_meta(book_id: str, book) -> dict:
        try:
            saved = json.loads(meta_file(book_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        return {**meta_defaults(book), **{k: v for k, v in saved.items() if k in META_FIELDS and isinstance(v, str)}}

    async def get_meta(request: Request):
        bid = request.path_params["book_id"]
        book = state.book(bid)["book"]
        return JSONResponse({"meta": book_meta(bid, book), "defaults": meta_defaults(book)})

    async def put_meta(request: Request):
        bid = request.path_params["book_id"]
        book = state.book(bid)["book"]
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("Book details must be an object")
        if body.get("reset"):
            meta_file(bid).unlink(missing_ok=True)
        else:
            current = book_meta(bid, book)
            for k, limit in META_FIELDS.items():
                if k in body:
                    current[k] = " ".join(str(body[k]).split()) if k != "description" else str(body[k]).strip()
                    if len(current[k]) > limit:
                        raise ApiError(f"{k} is longer than {limit} characters")
            meta_file(bid).parent.mkdir(parents=True, exist_ok=True)
            meta_file(bid).write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        return JSONResponse({"meta": book_meta(bid, book), "defaults": meta_defaults(book)})

    # ---- pronunciations: the lexicon of this book (see folivox.text.pronounce) ---------------------------
    PRONOUNCE_LIMIT = 100_000
    SAMPLE_LIMIT = 600

    def pronounce_file(book_id: str) -> Path:
        return state.data_dir / "pronounce" / f"{Path(book_id).name}.txt"

    def pronounce_state(book_id: str, text: str | None = None) -> dict:
        f = pronounce_file(book_id)
        if text is None:
            text = f.read_text(encoding="utf-8") if f.is_file() else ""
        lex = pronunciations.Lexicon.load([f]) if f.is_file() else pronunciations.Lexicon()
        warnings = [w.replace(f"{f.name}:", "line ", 1) for w in lex.warnings]  # the file is internal: say «line 3»
        return {"text": text, "count": len(lex.entries), "warnings": warnings}

    async def get_pronounce(request: Request):
        bid = request.path_params["book_id"]
        state.book(bid)
        return JSONResponse(pronounce_state(bid))

    async def put_pronounce(request: Request):
        bid = request.path_params["book_id"]
        state.book(bid)
        body = await request.json()
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise ApiError("The pronunciations must be text")
        if len(text) > PRONOUNCE_LIMIT:
            raise ApiError(f"The pronunciations are longer than {PRONOUNCE_LIMIT} characters")
        f = pronounce_file(bid)
        if text.strip():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        else:
            f.unlink(missing_ok=True)
        return JSONResponse(pronounce_state(bid, text))

    def lexicon_of(book_id: str):
        f = pronounce_file(book_id)
        return pronunciations.Lexicon.load([f]) if f.is_file() else pronunciations.Lexicon()

    async def pronounce_sample(request: Request):
        """Sentences of the book that contain the first entries of the lexicon, in the order of the file."""
        from folivox import lang as languages

        bid = request.path_params["book_id"]
        book = state.book(bid)["book"]
        lex = lexicon_of(bid)
        paragraphs = [b.text for ch in book.chapters() for b in ch.blocks]
        text, missing = lex.sample(paragraphs, languages.get(book.lang or "ru").sentences)
        return JSONResponse({"text": text, "missing": missing})

    # ---- cover: the book's own, an image you upload, or none ----------------------------------------
    IMAGE_TYPES = ((b"\xff\xd8\xff", "jpg", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
                   (b"GIF8", "gif", "image/gif"))

    def sniff_image(data: bytes) -> tuple[str, str] | None:
        for magic, ext, mime in IMAGE_TYPES:
            if data.startswith(magic):
                return ext, mime
        return ("webp", "image/webp") if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None

    def custom_cover(book_id: str) -> Path | None:
        d = state.data_dir / "covers"
        found = [p for p in d.glob(f"{Path(book_id).name}.*") if p.suffix != ".none"] if d.is_dir() else []
        return found[0] if found else None

    def cover_kind(book_id: str, book) -> str:
        """custom | none | book | '' (no cover at all)"""
        if custom_cover(book_id):
            return "custom"
        if (state.data_dir / "covers" / f"{Path(book_id).name}.none").exists():
            return "none"
        return "book" if book.cover else ""

    async def get_cover(request: Request):
        bid = request.path_params["book_id"]
        book = state.book(bid)["book"]
        mine = custom_cover(bid)
        if mine:
            return FileResponse(mine, headers={"Cache-Control": "no-cache"})
        if cover_kind(bid, book) == "book":
            return Response(book.cover, media_type=book.cover_mime or "image/jpeg")
        raise ApiError("No cover", 404)

    async def put_cover(request: Request):
        bid = request.path_params["book_id"]
        book = state.book(bid)["book"]
        folder = state.data_dir / "covers"
        folder.mkdir(parents=True, exist_ok=True)
        key = Path(bid).name
        if "multipart" in request.headers.get("content-type", ""):
            f = (await request.form()).get("file")
            if f is None or not hasattr(f, "read"):
                raise ApiError("No image in the request")
            data = await f.read()
            if len(data) > 15 * 1024 * 1024:
                raise ApiError("The image is larger than 15 MB", 413)
            kind = sniff_image(data)
            if kind is None:
                raise ApiError("Use a JPEG, PNG, WebP or GIF image", 415)
            for old in folder.glob(f"{key}.*"):
                old.unlink()
            (folder / f"{key}.{kind[0]}").write_bytes(data)
        else:  # {"mode": "book"} back to the book's own cover, {"mode": "none"} no cover
            mode = (await request.json()).get("mode")
            if mode not in ("book", "none"):
                raise ApiError("mode must be book or none")
            for old in folder.glob(f"{key}.*"):
                old.unlink()
            if mode == "none":
                (folder / f"{key}.none").write_bytes(b"")
        return JSONResponse({"cover": cover_kind(bid, book)})

    async def book_chapters(request: Request):
        entry = state.book(request.path_params["book_id"])
        book = entry["book"]
        if lang := request.query_params.get("lang"):
            from folivox import lang as languages

            try:
                book.lang = languages.get(lang).code
            except ValueError as e:
                raise ApiError(str(e)) from None
        depth = int(request.query_params.get("depth", 2))
        bid = request.path_params["book_id"]
        return JSONResponse(_book_summary(bid, book, depth, cover_kind(bid, book)))

    def write_session(body: dict) -> None:
        """Atomic, so a crash or two writers never leave a half-written file that would reset the page."""
        state.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = state.session_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(body), encoding="utf-8")
        os.replace(tmp, state.session_file)

    def uploads_newest_first() -> list[str]:
        root = state.data_dir / "uploads"
        dirs = [d for d in root.iterdir() if d.is_dir() and any(d.iterdir())] if root.is_dir() else []
        return [d.name for d in sorted(dirs, key=lambda d: d.stat().st_mtime, reverse=True)]

    async def get_session(request: Request):
        """What the page had selected, so a reload (or a restart) shows the same screen. If the saved session is
        missing or its book is gone, the most recently uploaded book is selected."""
        try:
            data = json.loads(state.session_file.read_text(encoding="utf-8"))
            state.book(data.get("book_id", ""))  # drop a session whose book is gone
        except (OSError, ValueError, ApiError, AttributeError):
            data = None
        if data is None:
            for bid in uploads_newest_first()[:5]:
                try:
                    state.book(bid)
                except Exception:  # noqa: BLE001 — an unreadable upload must not block the others
                    continue
                data = {"book_id": bid}
                break
        return JSONResponse(data)

    async def put_session(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ApiError("Session must be an object")
        write_session(body)
        return JSONResponse({"ok": True})

    def read_favorites() -> list[str]:
        try:
            ids = json.loads((state.data_dir / "favorites.json").read_text(encoding="utf-8"))
            return [i for i in ids if isinstance(i, str)]
        except (OSError, ValueError):
            return []

    async def put_favorites(request: Request):
        ids = (await request.json()).get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) and len(i) <= 64 for i in ids):
            raise ApiError("ids must be a list of voice ids")
        state.data_dir.mkdir(parents=True, exist_ok=True)
        (state.data_dir / "favorites.json").write_text(json.dumps(sorted(set(ids))), encoding="utf-8")
        return JSONResponse({"ok": True})

    async def list_voices(request: Request):
        lang = request.query_params.get("lang")
        voices = [v for v in catalog.all_voices() if not lang or v.lang == lang]
        auto = {code: catalog.default_for(code).id for code in sorted({v.lang for v in catalog.all_voices()})}
        return JSONResponse({"voices": [_voice_json(v) for v in voices], "auto": auto, "favorites": read_favorites(),
                             "user_dir": str(catalog.user_voices_dir())})

    async def voice_sample(request: Request):
        v = catalog.find(request.path_params["voice_id"])
        if v is None or not v.path.exists():
            raise ApiError("Unknown voice", 404)
        return FileResponse(v.path, media_type="audio/flac")

    async def add_voice(request: Request):
        from folivox import lang as languages
        from folivox.tts.voice import VoiceError, load_voice

        form = await request.form()
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise ApiError("No audio file in the request")
        voice_id = str(form.get("id", "")).strip()
        try:
            code = languages.get(str(form.get("lang") or "en")).code
        except ValueError as e:
            raise ApiError(str(e)) from None
        tmp = state.data_dir / "tmp" / uuid.uuid4().hex
        tmp.mkdir(parents=True)
        try:
            audio = tmp / (Path(f.filename or "voice.wav").name or "voice.wav")
            audio.write_bytes(await f.read())
            transcribe = None
            text = str(form.get("transcript") or "").strip()
            if not text:
                from folivox.qa.asr import Transcriber

                transcribe = Transcriber(language=code).transcribe_file
            try:
                v = await asyncio.to_thread(load_voice, audio, text or None, transcribe)
                entry = await asyncio.to_thread(
                    catalog.add, voice_id, audio, v.transcript, name=str(form.get("name") or ""),
                    gender=str(form.get("gender") or ""), lang=code, overwrite=False)
            except (VoiceError, ValueError, FileExistsError) as e:
                raise ApiError(str(e)) from None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return JSONResponse({**_voice_json(entry), "warnings": v.warnings})

    async def denoise(request: Request):
        """WAV in → WAV out with steady background noise reduced (for the A/B check before adding a voice)."""
        import io

        import soundfile as sf

        from folivox.audio.denoise import reduce_noise

        form = await request.form()
        f = form.get("file")
        if f is None or not hasattr(f, "read"):
            raise ApiError("No audio file in the request")
        data, sr = sf.read(io.BytesIO(await f.read()), dtype="float32", always_2d=True)
        try:
            cleaned = await asyncio.to_thread(reduce_noise, data.mean(axis=1), sr)
        except RuntimeError as e:
            raise ApiError(str(e), 501) from None
        out = io.BytesIO()
        sf.write(out, cleaned, sr, format="WAV", subtype="PCM_16")
        return Response(out.getvalue(), media_type="audio/wav")

    async def delete_voice(request: Request):
        voice_id = request.path_params["voice_id"]
        v = next((x for x in catalog.user() if x.id == voice_id), None)
        if v is None:
            raise ApiError("Only your own voices can be removed", 404)
        catalog.remove(voice_id)
        return JSONResponse({"ok": True})

    def regs_tts_names() -> list[str]:
        return _registries()["tts"].names()

    def cloud_options(body: dict, o: dict, tts: str, voice) -> dict:
        """Options of a paid cloud engine; refuses to start without the user's explicit agreement."""
        from folivox.tts.cloud import generator_class, voice_modes_of

        cls = generator_class(tts)
        if body.get("acknowledged") is not True:
            raise ApiError("Confirm that the book text is sent to the provider and billed to your account", 400)
        ks = key_state(cls)
        if not (ks["key_in_env"] or ks["key_saved"]):
            raise ApiError(f"No API key for {cls.label}: paste it under Advanced settings → API keys, "
                           f"or set {ks['key_env']}", 412, missing_key=ks["key_env"], provider=tts)
        model = str(o.get("tts_model") or "auto").strip()
        if not re.fullmatch(r"[\w.\-]{1,80}", model):
            raise ApiError("Bad model name")
        from folivox.tts import saved

        speaker = str(o.get("tts_voice") or "").strip()
        mine = saved.find(tts, speaker)  # a voice kept from an earlier book: reused as it is
        mode = mine["mode"] if mine else str(o.get("tts_mode") or "preset")
        if mode not in voice_modes_of(cls):
            raise ApiError(f"{cls.label} cannot use the voice mode {mode!r} for a book")
        description = " ".join(str(o.get("tts_description") or "").split())[:1000]
        ref = None
        if mode == "clone" and not mine:
            if o.get("own_voice") is not True:
                raise ApiError("Confirm that this is your own voice or you have the speaker's permission")
            if voice is None or not any(x.id == voice.id for x in catalog.user()):
                raise ApiError("Choose one of your own voices to clone")
            ref = voice.path
        if mode == "design" and not mine and len(description) < 10:
            raise ApiError("Describe the voice (at least a few words)")
        if mode in ("preset", "localize") and not speaker and tts in ("elevenlabs", "cartesia"):
            raise ApiError("Pick one of the provider's voices")
        cap = o.get("max_cost")
        try:
            cap = float(cap) if cap not in (None, "") else None
        except (TypeError, ValueError):
            raise ApiError("The spending cap must be a number of dollars") from None
        if cap is not None and cap <= 0:
            raise ApiError("The spending cap must be more than zero")
        style = o.get("tts_style")
        return dict(accept_cloud=True, tts_model=model, tts_voice=speaker, tts_mode=mode, tts_description=description,
                    tts_style=str(style) if style not in (None, "") else None, voice_ref=ref, max_cost=cap,
                    keep_voice=bool(o.get("keep_voice", False)) and not mine,
                    tts_voice_name=" ".join(str(o.get("tts_voice_name") or "").split())[:60])

    def build_options(body: dict, book_entry: dict, kind: str) -> RenderOptions:
        from folivox.text.stress import MODES

        o = body.get("options") or {}
        book = book_entry["book"]
        voice_id = body.get("voice") or ""
        from folivox.pipeline import is_cloud

        tts = config.provider("tts", o.get("tts") or None) or "cosyvoice3"
        cloud = tts in regs_tts_names() and is_cloud(tts)
        v = catalog.find(voice_id) if voice_id else catalog.default_for(book.lang)
        if v is None and not cloud:
            raise ApiError(f"Unknown voice {voice_id!r}")
        stress = o.get("stress", "yo")
        if stress not in MODES:
            raise ApiError(f"stress must be one of {', '.join(MODES)}")
        regs = _registries()
        tts = config.provider("tts", o.get("tts") or None) or "cosyvoice3"
        asr = config.provider("asr", o.get("asr") or None) or "auto"
        for kind_name, name in (("tts", tts), ("asr", asr)):
            if name not in regs[kind_name].names():
                raise ApiError(f"Unknown {kind_name} provider {name!r}")
        chapters = body.get("chapters") or []
        spec = ",".join(str(int(n)) for n in chapters) or None
        cloud_opts = cloud_options(body, o, tts, v) if cloud else {}
        lexicon = pronounce_file(str(body.get("book_id", "")))
        common = dict(voice_wav=v.path if v else None,
                      voice_text=body.get("voice_text") or (v.transcript if v else None), lang=book.lang, **cloud_opts,
                      depth=int(o.get("depth", 2)), max_chars=int(o.get("max_chars", 300)), stress=stress,
                      speed=float(o.get("speed", 1.0)), qa=bool(o.get("qa", True)),
                      qa_threshold=QA_LEVELS.get(str(o.get("qa_level")), QA_LEVELS["standard"]),
                      max_takes=int(o.get("takes") or (2 if cloud else 3)),
                      tts=tts, asr=asr, device=str(o.get("device", "auto")),
                      pronounce=(lexicon,) if lexicon.is_file() else (),
                      cover=custom_cover(str(body.get("book_id", ""))),
                      no_cover=cover_kind(str(body.get("book_id", "")), book) == "none",
                      meta=book_meta(str(body.get("book_id", "")), book))
        if kind == "preview":
            first = [int(chapters[0])] if chapters else [1]
            work = state.data_dir / "previews" / uuid.uuid4().hex[:8]
            return RenderOptions(output=work / "preview.m4b", work_dir=work / "work", chapters=str(first[0]),
                                 preview=int(o.get("preview_chars", PREVIEW_CHARS)), **common)
        out_dir = Path(str(o.get("output_dir") or Path.home() / "Audiobooks")).expanduser()
        mp3 = bool(o.get("mp3", False))
        stem = Path(book_entry["path"]).stem
        return RenderOptions(output=out_dir / stem if mp3 else out_dir / f"{stem}.m4b", chapters=spec, mp3=mp3,
                             bitrate=str(o.get("bitrate", "64k")), **common)

    async def start_job(request: Request):
        body = await request.json()
        kind = body.get("kind", "render")
        if kind not in ("render", "preview"):
            raise ApiError("kind must be render or preview")
        entry = state.book(body.get("book_id", ""))
        if state.manager.busy():
            raise ApiError("Another job is running; cancel it or wait", 409)
        opts = build_options(body, entry, kind)
        book = entry["book"]
        if body.get("sample_text") is not None:  # listen to a short text of your own (or picked from the book)
            from folivox.book import Block, BlockKind, Book, Section

            text = " ".join(str(body["sample_text"]).split())
            if kind != "preview" or not text or len(text) > SAMPLE_LIMIT:
                raise ApiError(f"A test text is 1–{SAMPLE_LIMIT} characters, played as a preview")
            book = Book(title=book.title, lang=book.lang, sections=[Section(blocks=[Block(BlockKind.PARAGRAPH, text)])])
        opts.output.parent.mkdir(parents=True, exist_ok=True)
        try:
            job = state.manager.start(kind, body["book_id"], book, opts)
        except RuntimeError:  # another request won the race between the busy() check and start()
            raise ApiError("Another job is running; cancel it or wait", 409) from None
        return JSONResponse(job.summary(), status_code=202)

    def get_job(request: Request):
        job = state.manager.jobs.get(request.path_params["job_id"])
        if job is None:
            raise ApiError("Unknown job", 404)
        return job

    async def job_info(request: Request):
        job = get_job(request)
        if job.opts is None:
            return JSONResponse({**job.summary(), "takes": getattr(job, "takes", [])})
        chapters = sorted(chapter_files(job.opts.resolved_work_dir()))
        return JSONResponse({**job.summary(), "chapters_built": chapters})

    async def current_job(request: Request):
        job = state.manager.current
        return JSONResponse(job.summary() if job else None)

    async def job_events(request: Request):
        job = get_job(request)

        async def stream():
            idx = int(request.headers.get("last-event-id", -1)) + 1
            while True:
                new, finished = await asyncio.to_thread(job.wait_events, idx)
                for ev in new:
                    yield f"id: {idx}\ndata: {json.dumps(ev)}\n\n"
                    idx += 1
                if finished:
                    return
                if not new:
                    yield ": keep-alive\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def job_cancel(request: Request):
        job = get_job(request)
        job.cancel_requested.set()
        return JSONResponse(job.summary())

    async def job_reveal(request: Request):
        """Show the finished file in the system file manager (only the result of a job of this server)."""
        import subprocess
        import sys

        job = get_job(request)
        if job.state != "done" or not job.result or not Path(job.result).exists():
            raise ApiError("Nothing to show yet", 404)
        target = Path(job.result)
        if sys.platform == "darwin":
            cmd = ["open", "-R", str(target)]
        elif sys.platform == "win32":
            cmd = ["explorer", f"/select,{target}"]
        else:
            cmd = ["xdg-open", str(target if target.is_dir() else target.parent)]
        try:
            subprocess.Popen(cmd)  # noqa: S603 — fixed argv, path comes from our own job result
        except OSError:  # a server or container: no file manager to show it in
            raise ApiError("No file manager on the machine running the studio: use Download", 409) from None
        return JSONResponse({"ok": True})

    async def job_result(request: Request):
        """The finished audiobook as a download (the studio may run on another machine): the .m4b itself, or a zip
        of the MP3 folder."""
        import tempfile
        import zipfile

        from starlette.background import BackgroundTask

        job = get_job(request)
        if job.state != "done" or not job.result or not Path(job.result).exists():
            raise ApiError("Nothing to download yet", 404)
        target = Path(job.result)
        if target.is_file():
            return FileResponse(target, filename=target.name)

        def pack() -> Path:
            state.data_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(suffix=".zip", dir=state.data_dir)
            os.close(fd)
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:  # MP3s are already compressed
                for f in sorted(target.rglob("*")):
                    if f.is_file():
                        z.write(f, Path(target.name) / f.relative_to(target))
            return Path(tmp)

        tmp = await asyncio.to_thread(pack)
        return FileResponse(tmp, filename=f"{target.name}.zip", media_type="application/zip",
                            background=BackgroundTask(tmp.unlink, missing_ok=True))

    async def job_chapter(request: Request):
        job = get_job(request)
        path = chapter_files(job.opts.resolved_work_dir()).get(int(request.path_params["n"]))
        if path is None:
            raise ApiError("Chapter is not built yet", 404)
        return FileResponse(path, media_type="audio/flac")

    def review_list(work: Path) -> list[dict]:
        from folivox.journal import Journal

        if not (work / "journal.sqlite").exists():
            return []
        j = Journal(work)
        try:
            return [{"key": c.key, "chapter": c.chapter + 1, "idx": c.idx, "text": c.source, "heard": t.asr or "",
                     "score": round(t.score, 3), "takes": t.takes, "duration": round(t.duration, 1)}
                    for c, t in j.flagged()]
        finally:
            j.close()

    async def job_review(request: Request):
        """Fragments whose kept take still fails QA after every retake: listed for a human to check."""
        job = get_job(request)
        if job.opts is None:
            raise ApiError("This job has no fragments", 404)
        items = await asyncio.to_thread(review_list, job.opts.resolved_work_dir())
        return JSONResponse({"items": items})

    async def job_fragment(request: Request):
        job = get_job(request)
        key = request.path_params["key"]
        if job.opts is None or not re.fullmatch(r"[0-9a-f]{24}", key):
            raise ApiError("Unknown fragment", 404)
        path = job.opts.resolved_work_dir() / "audio" / f"{key}.flac"
        if not path.exists():
            raise ApiError("Unknown fragment", 404)
        return FileResponse(path, media_type="audio/flac")

    def diag_work_dir() -> Path | None:
        job = state.manager.current
        return job.opts.resolved_work_dir() if job is not None and job.opts is not None else None

    async def diagnostics(request: Request):
        from folivox import diag

        data = await asyncio.to_thread(diag.report, diag_work_dir())
        return JSONResponse({"report": data, "text": diag.render_report_text(data)})

    async def support_bundle(request: Request):
        """A redacted zip (no keys, no book text) for a bug report; built in memory-safe temp space."""
        import tempfile

        from folivox import diag

        def build() -> bytes:
            with tempfile.TemporaryDirectory() as tmp:
                return diag.support_bundle(Path(tmp) / "bundle.zip", diag_work_dir()).read_bytes()

        data = await asyncio.to_thread(build)
        return Response(data, media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="folivox-support.zip"', "Cache-Control": "no-store"})

    async def get_settings(request: Request):
        kinds = {}
        for kind, reg in _registries().items():
            kinds[kind] = {"default": reg.default, "selected": config.provider(kind) or reg.default,
                           "providers": [{"name": p.name, "description": p.description, "local": p.local,
                                          "needs": p.needs} for p in reg.providers()]}
        return JSONResponse({"kinds": kinds, "config_path": str(config.config_path()),
                             "output_dir": str(Path.home() / "Audiobooks")})

    def key_names() -> list[dict]:
        """Env var names the cloud providers read: the defaults plus any ``api_key_env`` in the config."""
        owners: dict[str, list[str]] = {}
        for kind, reg in _registries().items():
            for p in reg.providers():
                if not p.local and p.needs.isupper():
                    owners.setdefault(config.section(f"{kind}.{p.name}").get("api_key_env", p.needs), []).append(
                        f"{kind}/{p.name}")
        return [{"name": n, "used_by": u} for n, u in sorted(owners.items())]

    def mask(value: str) -> str:
        return "•" * 8 + value[-4:] if len(value) > 8 else "•" * 8

    async def get_keys(request: Request):
        out = []
        for k in key_names():
            stored = config.keychain_get(k["name"])
            out.append({**k, "env": bool(os.environ.get(k["name"])), "saved": bool(stored),
                        "masked": mask(stored) if stored else ""})
        return JSONResponse({"keys": out})  # the full value never leaves the server

    async def put_key(request: Request):
        name = request.path_params["name"]
        if name not in {k["name"] for k in key_names()}:
            raise ApiError("Unknown key name", 404)
        value = str((await request.json()).get("value", "")).strip()
        if not value:
            raise ApiError("Empty key")
        try:
            config.keychain_set(name, value)
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"Cannot use the OS keychain: {type(e).__name__}") from None
        return await get_keys(request)

    async def delete_key(request: Request):
        name = request.path_params["name"]
        try:
            config.keychain_delete(name)
        except Exception as e:  # noqa: BLE001
            raise ApiError(f"Cannot use the OS keychain: {type(e).__name__}") from None
        return await get_keys(request)

    async def put_settings(request: Request):
        import tomli_w

        body = await request.json()
        chosen = body.get("providers") or {}
        regs = _registries()
        for kind, name in chosen.items():
            if kind not in regs or name not in regs[kind].names():
                raise ApiError(f"Unknown {kind} provider {name!r}")
        path = config.config_path()
        data = dict(config.load())
        data["providers"] = {**data.get("providers", {}), **chosen}
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))  # comments are lost on rewrite
        path.write_text(tomli_w.dumps(data), encoding="utf-8")
        config.load.cache_clear()
        return await get_settings(request)


    # ---- cloud voice generation -------------------------------------------------------------------
    GEN_TAKE = re.compile(r"^take_t\d+_\d+\.wav$")

    def gen_class(name: str):
        from folivox.voicegen.cloud import cloud_providers

        cls = cloud_providers().get(name)
        if cls is None:
            raise ApiError(f"Unknown cloud voice provider {name!r}", 404)
        return cls

    def key_state(cls) -> dict:
        env = config.section(f"voicegen.{cls.name}").get("api_key_env", cls.key_env)
        stored = config.keychain_get(env)
        return {"key_env": env, "key_in_env": bool(os.environ.get(env)), "key_saved": bool(stored),
                "key_masked": mask(stored) if stored else ""}

    def make_generator(name: str, model: str = ""):
        from folivox.providers import ProviderError

        cls = gen_class(name)
        ks = key_state(cls)
        if not (ks["key_in_env"] or ks["key_saved"]):
            raise ApiError(f"No API key for {cls.label}: paste it below or set {ks['key_env']} in the environment",
                           412, missing_key=ks["key_env"], provider=name)
        try:
            if "generator" in services:
                return services["generator"](name, model=model)
            return GENERATORS_create(name, model)
        except ProviderError as e:
            raise ApiError(str(e), 502) from None
        except ValueError as e:
            raise ApiError(str(e), 412, missing_key=ks["key_env"], provider=name) from None

    def GENERATORS_create(name: str, model: str = ""):
        from folivox.voicegen import GENERATORS

        return GENERATORS.create(name, model=model or None)

    async def gen_call(fn, *args):
        """Run a blocking provider call; a provider failure becomes a clear API error (never the key)."""
        from folivox.providers import ProviderError

        try:
            return await asyncio.to_thread(fn, *args)
        except ProviderError as e:
            raise ApiError(str(e), 429 if getattr(e, "kind", "") == "rate_limit" else 502) from None

    def book_text(book, limit: int = 60000) -> str:
        parts, n = [], 0
        for ch in book.chapters():
            for b in ch.blocks:
                parts.append(b.text)
                n += len(b.text)
                if n > limit:
                    return "\n".join(parts)
        return "\n".join(parts)

    def make_llm(body: dict):
        from folivox.llm import LLMS
        from folivox.providers import ProviderError

        name = config.provider("llm", body.get("llm") or None) or LLMS.default
        try:
            if "llm" in services:
                return services["llm"](name)
            return LLMS.create(name, model=body.get("model") or None)
        except (ProviderError, ValueError) as e:
            raise ApiError(str(e), 412) from None

    async def gen_providers(request: Request):
        from folivox.voicegen.cloud import COMMERCIAL_NOTICE, cloud_providers

        def sample_usd(cls):  # a typical voice sample: ~160 characters, 3 takes
            best = cls.models[0]["id"] if cls.models else ""
            return {m: cls.estimate_for(160, 3, m, best).get("usd") for m in cls.modes}

        out = [{**cls.capabilities(), **key_state(cls), "sample_usd": sample_usd(cls)}
               for cls in cloud_providers().values()]
        order = ["gemini", "cartesia", "elevenlabs", "openai"]  # by value for money
        tags = {"gemini": "cheap and best", "openai": "low quality for non-English"}
        for p in out:
            p["tag"] = tags.get(p["name"], "")
        out.sort(key=lambda p: order.index(p["name"]) if p["name"] in order else len(order))
        return JSONResponse({"providers": out, "notice": COMMERCIAL_NOTICE})

    async def tts_engines(request: Request):
        """Engines that can read the book: the local one and the paid cloud ones, with prices and limits."""
        from folivox.tts.cloud import CHARS_PER_SECOND, voice_modes_of
        from folivox.voicegen.cloud import COMMERCIAL_NOTICE, cloud_providers

        hour = CHARS_PER_SECOND * 3600
        out = [{"name": "cosyvoice3", "label": "On this computer (CosyVoice 3)", "local": True, "cloud": False,
                "blurb": "Free, private, works offline. Clones a voice from a short recording. Slower and "
                         "the quality depends on your hardware.", "models": [], "per_hour": 0}]
        for cls in cloud_providers().values():
            prices = {m["id"]: cls.estimate_for(hour, 1, "preset", m["id"]).get("usd") for m in cls.models}
            out.append({**cls.capabilities(), **key_state(cls), "local": False, "cloud": True,
                        "voice_modes": list(voice_modes_of(cls)), "per_hour": prices.get(cls.models[0]["id"])
                        if cls.models else None, "prices": prices})
        order = ["cosyvoice3", "gemini", "cartesia", "elevenlabs", "openai"]  # free first, then by value for money
        tags = {"gemini": "cheap and best", "openai": "low quality for non-English"}
        for e in out:
            e["tag"] = tags.get(e["name"], "")
        out.sort(key=lambda e: order.index(e["name"]) if e["name"] in order else len(order))
        return JSONResponse({"engines": out, "notice": COMMERCIAL_NOTICE})

    async def tts_estimate(request: Request):
        from folivox.tts import cost
        from folivox.tts.cloud import generator_class
        from folivox.voicegen.cloud import COMMERCIAL_NOTICE

        body = await request.json()
        entry = state.book(body.get("book_id", ""))
        o = body.get("options") or {}
        try:
            cls = generator_class(str(o.get("tts") or ""))
        except Exception:  # noqa: BLE001
            raise ApiError("Pick a cloud engine", 400) from None
        from folivox.pipeline import parse_selection

        depth = int(o.get("depth", 2))
        total = len(entry["book"].chapters(depth))
        chapters = body.get("chapters") or []
        chosen = [i + 1 for i in parse_selection(",".join(str(int(n)) for n in chapters) or None, total)]
        preview = int(o.get("preview_chars", PREVIEW_CHARS)) if body.get("kind") == "preview" else None
        if preview:
            chosen = chosen[:1]
        chars = cost.book_chars(entry["book"], depth, chosen, preview)
        model = str(o.get("tts_model") or "auto")
        if model in ("auto", "latest"):
            model = cls.models[0]["id"] if cls.models else ""
        est = cost.estimate(cls.name, chars, model, int(o.get("takes") or 2), bool(o.get("qa", True)),
                            str(o.get("tts_mode") or "preset"))
        return JSONResponse({**est, "summary": cost.describe(est), "notice": COMMERCIAL_NOTICE,
                             "label": cls.label, "price_note": cls.price_note})

    async def tts_saved_list(request: Request):
        from folivox.tts import saved

        return JSONResponse({"voices": saved.listing(request.path_params["provider"])})

    async def tts_saved_rename(request: Request):
        from folivox.tts import saved

        name = " ".join(str((await request.json()).get("name", "")).split())[:60]
        if not name:
            raise ApiError("Give the voice a name")
        if not saved.rename(request.path_params["provider"], request.path_params["voice"], name):
            raise ApiError("Unknown saved voice", 404)
        return JSONResponse({"ok": True})

    async def tts_saved_delete(request: Request):
        """Forget a kept voice and delete it from the provider's account."""
        from folivox.providers import ProviderError
        from folivox.tts import saved

        name, vid = request.path_params["provider"], request.path_params["voice"]
        if saved.find(name, vid) is None:
            raise ApiError("Unknown saved voice", 404)
        gen = make_generator(name)
        try:
            await asyncio.to_thread(gen.teardown_voices, [vid])
        except ProviderError as e:
            if getattr(e, "kind", "") != "bad_request":  # "not found": it is already gone, forget it
                raise ApiError(str(e), 502) from None
        saved.remove(name, vid)
        return JSONResponse({"ok": True})

    async def gen_voices(request: Request):
        name = request.path_params["provider"]
        gen = make_generator(name)
        if "preset" not in gen.modes:
            return JSONResponse({"voices": []})
        speakers = await gen_call(gen.speakers, request.query_params.get("lang"))
        lang = request.query_params.get("lang")
        names, details = getattr(gen, "names", {}), getattr(gen, "details", {})
        rows = [{"id": k, "lang": v[0], "gender": v[1], "name": names.get(k, k), **details.get(k, {})}
                for k, v in speakers.items() if not lang or v[0] in (lang, "")]
        for r in rows:  # some providers serve samples only to the key holder: the browser plays them through us
            if r.get("preview_url"):
                sample_urls[(name, r["id"])] = r["preview_url"]
                if name in AUTH_SAMPLES:
                    r["preview_url"] = f"/api/tts/{name}/sample/{urllib.parse.quote(r['id'], safe='')}"
        return JSONResponse({"voices": rows, "note": getattr(gen, "voices_note", "")})

    sample_urls: dict[tuple[str, str], str] = {}
    AUTH_SAMPLES = {"cartesia"}  # sample files that answer 403 without the account's key

    async def tts_sample(request: Request):
        """A provider's free voice sample, fetched with the key (never exposed). Only URLs the provider
        itself listed for that voice are fetched."""
        name, voice = request.path_params["provider"], request.path_params["voice"]
        url = sample_urls.get((name, voice))
        host = urllib.parse.urlparse(url or "").hostname or ""
        if name not in AUTH_SAMPLES or not url or not url.startswith("https://") or not host.endswith(".cartesia.ai"):
            raise ApiError("No sample for this voice", 404)
        gen = make_generator(name)

        def fetch():
            resp = gen.transport("GET", url, {**gen.headers(), "User-Agent": "Mozilla/5.0"}, None, 60)
            if resp.status >= 300:
                raise gen.map_error(resp)
            return resp

        resp = await gen_call(fetch)
        return Response(resp.body, media_type=resp.headers.get("content-type", "audio/wav"),
                        headers={"Cache-Control": "private, max-age=3600"})

    async def gen_suggest(request: Request):
        from folivox.voices.suggest import suggest_design_prompt

        body = await request.json()
        entry = state.book(body.get("book_id", ""))
        book = entry["book"]
        llm = make_llm(body)
        try:
            prompt = await asyncio.to_thread(suggest_design_prompt, book_text(book, 12000), book.lang, llm,
                                             title=book.title or "", author=book.author or "")
        except ValueError as e:
            raise ApiError(str(e), 502) from None
        except Exception as e:  # noqa: BLE001 — LLM server down, bad key…
            raise ApiError(f"The text LLM failed: {e}", 502) from None
        return JSONResponse({"prompt": prompt, "llm": getattr(llm, "name", "")})

    async def gen_texts(request: Request):
        from folivox import lang as languages
        from folivox.voices.design import pick_book_texts, safe_texts

        body = await request.json()
        try:
            code = languages.get(str(body.get("lang") or "en")).code
        except ValueError as e:
            raise ApiError(str(e)) from None
        if body.get("source") == "book":
            book = state.book(body.get("book_id", ""))["book"]
            llm = make_llm(body)
            try:
                found = await asyncio.to_thread(pick_book_texts, book_text(book), book.lang, llm, 3)
            except Exception as e:  # noqa: BLE001
                raise ApiError(f"The text LLM failed: {e}", 502) from None
            if not found:
                raise ApiError("No easy passage found in the book; use the built-in text or write your own", 404)
            return JSONResponse({"texts": found, "lang": book.lang})
        texts = safe_texts(code)
        if not texts:
            raise ApiError(f"No built-in text for {code!r}; write your own", 404)
        return JSONResponse({"texts": texts, "lang": code})

    def gen_params(body: dict) -> dict:
        from folivox import lang as languages

        cls = gen_class(str(body.get("provider", "")))
        mode = body.get("mode") or cls.modes[0]
        if mode not in cls.modes:
            raise ApiError(f"{cls.label} supports: {', '.join(cls.modes)}")
        try:
            lang = languages.get(str(body.get("lang") or "en")).code
        except ValueError as e:
            raise ApiError(str(e)) from None
        if lang not in cls.languages:
            raise ApiError(f"{cls.label} does not list {lang!r}; supported: {', '.join(cls.languages)}")
        texts = [" ".join(str(t).split()) for t in (body.get("texts") or [])]
        texts = [t for t in texts if t]
        if not 1 <= len(texts) <= 5:
            raise ApiError("Give one to five texts to be spoken")
        if any(len(t) > cls.max_chars for t in texts):
            raise ApiError(f"A text is longer than {cls.max_chars} characters")
        takes = int(body.get("takes", 3))
        if not 1 <= takes <= 6:
            raise ApiError("takes must be between 1 and 6")
        model = str(body.get("model") or "auto").strip()
        if not re.fullmatch(r"[\w.\-]{1,80}", model):
            raise ApiError("Bad model name")
        description = " ".join(str(body.get("description") or "").split())
        speaker = str(body.get("speaker") or "").strip()
        reference = None
        if mode == "clone":
            mine = next((v for v in catalog.user() if v.id == body.get("reference")), None)
            if mine is None or not mine.path.exists():
                raise ApiError("Choose one of your own voices to clone")
            if body.get("own_voice") is not True:
                raise ApiError("Confirm that this is your own voice or you have the speaker's permission")
            reference = mine.path
        if mode == "localize" and not speaker:
            raise ApiError("Pick the voice whose accent should be localised")
        if mode == "design" and len(description) < 10:
            raise ApiError("Describe the voice (at least a few words), or press Suggest")
        if mode == "preset" and not speaker:
            raise ApiError("Pick one of the provider's voices")
        if len(description) > 1000:
            raise ApiError("The description is too long (1000 characters at most)")
        return {"cls": cls, "mode": mode, "lang": lang, "texts": texts, "takes": takes, "description": description,
                "speaker": speaker, "model": model, "reference": reference}

    async def gen_estimate(request: Request):
        from folivox.voicegen.cloud import COMMERCIAL_NOTICE

        prm = gen_params(await request.json())
        cls = prm["cls"]
        model = cls.models[0]["id"] if prm["model"] in ("auto", "latest") and cls.models else prm["model"]
        est = cls.estimate_for(sum(len(t) for t in prm["texts"]), prm["takes"], prm["mode"], model)
        return JSONResponse({**est, "notice": COMMERCIAL_NOTICE, "terms_url": cls.terms_url})

    async def gen_start(request: Request):
        from folivox.ui.gen import GenJob, run_generation

        body = await request.json()
        prm = gen_params(body)
        if body.get("acknowledged") is not True:
            raise ApiError("Confirm that the text is sent to the provider and billed to your account", 400)
        if state.manager.busy():
            raise ApiError("Another job is running; cancel it or wait", 409)
        name = prm["cls"].name
        gen = make_generator(name, prm["model"])
        gen.mode = prm["mode"]
        out_dir = state.data_dir / "gen" / uuid.uuid4().hex[:10]
        out_dir.mkdir(parents=True)
        job = GenJob(name, str(body.get("book_id") or ""), out_dir,
                     {k: v for k, v in prm.items() if k != "cls"})
        texts = prm["texts"]

        def asr_factory(lang):
            if "asr" in services:
                return services["asr"](lang)
            from folivox.qa.asr import Transcriber

            return Transcriber(language=lang)

        try:
            state.manager.start_task(job, lambda j: run_generation(j, gen, texts, asr_factory=asr_factory, wave=_wave))
        except RuntimeError as e:
            raise ApiError(str(e), 409) from None
        state.gen_jobs[job.id] = job
        return JSONResponse(job.summary(), status_code=202)

    def get_gen_job(request: Request):
        job = state.gen_jobs.get(request.path_params["job_id"])
        if job is None:
            raise ApiError("Unknown generation job", 404)
        return job

    async def gen_take(request: Request):
        job = get_gen_job(request)
        name = request.path_params["name"]
        path = job.out_dir / name
        if not GEN_TAKE.match(name) or not path.is_file():
            raise ApiError("Unknown take", 404)
        return FileResponse(path, media_type="audio/wav")

    async def gen_add(request: Request):
        from folivox import lang as languages

        job = get_gen_job(request)
        body = await request.json()
        take = next((t for t in job.takes if t["name"] == body.get("take")), None)
        if take is None:
            raise ApiError("Pick one of the generated takes", 404)
        cls = gen_class(job.provider)
        try:
            code = languages.get(str(body.get("lang") or job.params["lang"])).code
            entry = await asyncio.to_thread(
                catalog.add, str(body.get("id", "")).strip(), job.out_dir / take["name"], take["text"],
                name=str(body.get("name") or ""), gender=str(body.get("gender") or ""), lang=code,
                source=f"{cls.label} {job.params['model']} (commercial API): "
                       f"{job.params['description'] or job.params['speaker'] or job.params['mode']}",
                license=cls.license, url=cls.terms_url, commercial=True)
        except (ValueError, FileExistsError) as e:
            raise ApiError(str(e)) from None
        return JSONResponse(_voice_json(entry))

    # ---- plumbing -------------------------------------------------------------------------------
    def check(request: Request) -> JSONResponse | None:
        if allowed_hosts is not None and request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"error": "Bad Host header"}, status_code=403)  # DNS-rebinding guard
        if token and request.url.path.startswith("/api/"):
            given = request.headers.get("x-folivox-token") or request.query_params.get("token") or ""
            cookie = request.cookies.get(cookie_name) or ""
            if not (secrets.compare_digest(given, token) or secrets.compare_digest(cookie, session)):
                return JSONResponse({"error": "Missing or wrong token"}, status_code=401)
        return None

    async def api_error(request: Request, exc: ApiError):
        return JSONResponse({"error": str(exc), **exc.extra}, status_code=exc.status)

    async def bad_request(request: Request, exc: Exception):
        return JSONResponse({"error": f"Bad request: {exc}"}, status_code=400)

    routes = [
        Route("/", index),
        Route("/i18n/{code}.json", i18n_catalog),
        Route("/api/ping", ping),
        Route("/api/shutdown", shutdown, methods=["POST"]),
        Route("/api/books", upload_book, methods=["POST"]),
        Route("/api/books/{book_id}/chapters", book_chapters),
        Route("/api/books/{book_id}/meta", get_meta),
        Route("/api/books/{book_id}/meta", put_meta, methods=["PUT"]),
        Route("/api/books/{book_id}/pronounce", get_pronounce),
        Route("/api/books/{book_id}/pronounce", put_pronounce, methods=["PUT"]),
        Route("/api/books/{book_id}/pronounce/sample", pronounce_sample),
        Route("/api/books/{book_id}/cover", get_cover),
        Route("/api/books/{book_id}/cover", put_cover, methods=["PUT", "POST"]),
        Route("/api/session", get_session),
        Route("/api/session", put_session, methods=["PUT"]),
        Route("/api/favorites", put_favorites, methods=["PUT"]),
        Route("/api/voices", list_voices),
        Route("/api/voices", add_voice, methods=["POST"]),
        Route("/api/denoise", denoise, methods=["POST"]),
        Route("/api/voices/{voice_id}/sample", voice_sample),
        Route("/api/voices/{voice_id}", delete_voice, methods=["DELETE"]),
        Route("/api/jobs", start_job, methods=["POST"]),
        Route("/api/jobs/current", current_job),
        Route("/api/jobs/{job_id}", job_info),
        Route("/api/jobs/{job_id}/events", job_events),
        Route("/api/jobs/{job_id}/cancel", job_cancel, methods=["POST"]),
        Route("/api/jobs/{job_id}/reveal", job_reveal, methods=["POST"]),
        Route("/api/jobs/{job_id}/result", job_result),
        Route("/api/jobs/{job_id}/chapters/{n:int}", job_chapter),
        Route("/api/jobs/{job_id}/review", job_review),
        Route("/api/jobs/{job_id}/fragments/{key}", job_fragment),
        Route("/api/diagnostics", diagnostics),
        Route("/api/support-bundle", support_bundle),
        Route("/api/settings", get_settings),
        Route("/api/settings", put_settings, methods=["PUT"]),
        Route("/api/gen/providers", gen_providers),
        Route("/api/tts/engines", tts_engines),
        Route("/api/tts/{provider}/sample/{voice}", tts_sample),
        Route("/api/tts/{provider}/saved", tts_saved_list),
        Route("/api/tts/{provider}/saved/{voice}", tts_saved_delete, methods=["DELETE"]),
        Route("/api/tts/{provider}/saved/{voice}", tts_saved_rename, methods=["PUT"]),
        Route("/api/tts/estimate", tts_estimate, methods=["POST"]),
        Route("/api/gen/suggest", gen_suggest, methods=["POST"]),
        Route("/api/gen/texts", gen_texts, methods=["POST"]),
        Route("/api/gen/estimate", gen_estimate, methods=["POST"]),
        Route("/api/gen/start", gen_start, methods=["POST"]),
        Route("/api/gen/voices/{provider}", gen_voices),
        Route("/api/gen/{job_id}/takes/{name}", gen_take),
        Route("/api/gen/{job_id}/add", gen_add, methods=["POST"]),
        Route("/api/keys", get_keys),
        Route("/api/keys/{name}", put_key, methods=["PUT"]),
        Route("/api/keys/{name}", delete_key, methods=["DELETE"]),
    ]
    app = Starlette(routes=routes, exception_handlers={ApiError: api_error, ValueError: bad_request,
                                                      TypeError: bad_request, KeyError: bad_request})
    app.add_middleware(_Guard, check=check)
    app.state.ui = state
    app.state.request_exit = None  # set by `folivox ui`: asks uvicorn to stop
    return app


class _Guard:
    """Pure-ASGI middleware (keeps SSE streaming intact): Host and token checks before routing."""

    def __init__(self, app, check):
        self.app, self.check = app, check

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            denied = self.check(Request(scope, receive))
            if denied is not None:
                return await denied(scope, receive, send)
        await self.app(scope, receive, send)
