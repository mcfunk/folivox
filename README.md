<div align="center">

# Folivox

**The audiobook workhorse. A powerful tool in a simple interface, free.**

Drop in a book, pick a voice, press Render. Get a chaptered audiobook read in any voice you like,
on your own computer.

FB2 · EPUB · DOCX · PDF · TXT &nbsp;→&nbsp; `.m4b` / `.mp3` with chapters, cover and metadata

![Python 3.10–3.12](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue)
![License Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)
![Platforms](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey)
![Status early development](https://img.shields.io/badge/status-early%20development-orange)

<img src="docs/img/studio-progress.png" alt="Folivox studio rendering a book: progress, chapter lanes, live fragments" width="820">

</div>

## What is this

Folivox turns a whole book, many hours of it, into a finished audiobook. Out of the box it reads with
[CosyVoice 3](https://github.com/FunAudioLLM/CosyVoice), an open model that clones a voice from a short sample,
running entirely on your machine: no account, no subscription, no per-character fee, nothing uploaded.

It is one application with many engines behind it. The book can be read by the free local model or by
ElevenLabs, OpenAI, Gemini or Cartesia with your own API key. A narrator voice can come from the bundled
catalog, from your own recording, or be designed for this particular book. Everything around the voice,
from text clean-up to quality control, chapters and a crash-safe journal, is the same whichever engine you choose.

Use the **web studio** (`folivox ui`, in 9 interface languages) or the **command line**: same pipeline, same
results.

## Quick start

### 1. Install

Pick one way. Each needs no administrator rights and is safe to run again.

**One command** (recommended). Open a terminal (macOS: **Terminal** in Applications → Utilities; Windows:
**PowerShell** from the Start menu; Linux: any terminal), paste the line for your system and press Enter.

macOS and Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/vatavale/folivox/main/scripts/install.sh | bash
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/vatavale/folivox/main/scripts/install.ps1 | iex
```

<details>
<summary><b>With git</b> (if you want the source code)</summary>

macOS and Linux:

```bash
git clone https://github.com/vatavale/folivox.git
cd folivox
bash scripts/install.sh
```

Windows (PowerShell):

```powershell
git clone https://github.com/vatavale/folivox.git
cd folivox
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

</details>

<details>
<summary><b>From a ZIP</b> (no terminal commands to download)</summary>

1. On this page click **Code → Download ZIP** and unpack it.
2. macOS: open **Terminal**, type `cd ` (with a space), drag the unpacked folder into the window, press Enter,
   then run `bash scripts/install.sh`. Linux: open a terminal in the folder and run the same.
3. Windows: open the unpacked folder in Explorer, type `powershell` in the address bar and press Enter, then run
   `powershell -ExecutionPolicy Bypass -File scripts\install.ps1`.

</details>

### 2. Start

Open a new terminal window and run:

```bash
folivox ui
```

Your browser opens the studio. Drop a book, choose a voice, press **Render audiobook**. To stop the studio,
press `Ctrl+C` in the terminal.

The installer sets up everything for you: `uv`, ffmpeg, the PyTorch build that fits your hardware (NVIDIA GPU,
Apple Silicon or CPU) and Folivox. Then it checks your computer and downloads the models (about 6 GB for the
voice and 1.6 GB for the quality check) with a progress bar; an interrupted download continues where it
stopped. Prefer the terminal to the studio? `folivox render book.fb2` puts the audiobook next to the book.

### 3. Update

```bash
folivox update
```

Whichever way you installed, this fetches the latest version and keeps your models, voices, settings and
unfinished renders. Check the version with `folivox --version`.

### 4. Uninstall

```bash
folivox uninstall               # delete the downloaded models (asks first; --dry-run only shows what would go)
folivox uninstall --everything  # also delete your voices and settings
uv tool uninstall folivox       # last step: remove the program itself
```

It removes only what Folivox created. Your books, audiobooks, ffmpeg and uv are never touched.

Details, options, manual install and troubleshooting: [docs/install.md](docs/install.md).

## Choose how your book is read

| | Free, on your computer | With an online engine (your API key) |
|---|---|---|
| **Who reads** | CosyVoice 3, local and offline | ElevenLabs, OpenAI, Gemini or Cartesia |
| **Voice** | Clones any sample: bundled, recorded, uploaded or generated | The provider's voices, or a voice designed or localised for the book |
| **Cost** | Free | Estimated before you start, confirmed by you, with an optional spending cap |
| **Your text** | Never leaves your computer | Sent to that provider |

Paste a key for any of the four providers in the studio (it is stored in your OS keychain) and they appear
next to the local engine. With the model set to `auto`, Folivox asks the provider which models your key can
use and takes the newest one, so new releases are picked up without updating Folivox.

## A voice for every book

- **Ready to use:** a bundled catalog of public-domain narrator voices for the book languages, with audition.
- **Your own voice:** record it with the microphone right in the studio or upload a 6–15 second sample;
  optional gentle noise reduction.
- **Designed for this book:** describe the narrator, or press **Suggest from my book** and a text LLM writes
  the description from the book itself. An online provider (or the local Qwen3 voice-design model) reads a short
  text, Whisper ranks the takes, and you keep the best one as a cloning prompt for the free local engine.
- **Your library:** every voice you add is kept on your computer, with its source and licence, searchable by
  name and language; star your favourites.

Full guide: [docs/voices.md](docs/voices.md).

### Recommended: the almost-free workflow

The best results we get come from combining one cheap online sample with the free local engine:

1. **Generate a voice sample with Gemini 3.8.** In the studio: voice gallery → **Generate a voice sample** →
   Gemini, describe the narrator (or press **Suggest from my book**), generate a few short takes. With a paid
   Gemini key this costs well under one cent (about $0.0023 per 10 s of audio on the paid tier); Google also has
   a free tier.
2. **Keep the best take** (Whisper ranks them) as a voice in your library.
3. **Render the whole book locally** with CosyVoice 3 cloning that voice: free, unlimited, offline.

You pay once for about ten seconds of a top-quality voice, and the open model reads the remaining hours
with it. CLI equivalent:

```bash
export GEMINI_API_KEY=…
folivox voices design narrator -g gemini --mode design --lang en --describe "calm, warm audiobook narrator" --keep takes/
folivox render book.epub --voice narrator
```

### The very best quality: a full online render

Reading the whole book with a commercial engine still sounds better than local cloning. For that we recommend
**Gemini 3.8 Flash TTS**: the best narration in our experience and at the same time one of the cheapest engines,
about $0.8 per hour of audio at list prices (October 2026; a ten-hour book is roughly $8). Folivox shows the
estimate before you start and can stop at a spending cap:

```bash
folivox render book.epub --tts gemini --tts-mode design --tts-describe "calm, warm audiobook narrator" --max-cost 10
```

Prices, limits and terms of all four engines: [docs/cloud-engines.md](docs/cloud-engines.md).

## Features

<table>
<tr>
<td width="33%" valign="top">

**Many formats in**<br>
FB2 (also `.fb2.zip`), EPUB, DOCX, PDF (text layer, no OCR) and TXT. Chapters, nested sections, poems and
epigraphs are kept; PDF headers, footers and page numbers are removed and hyphenation is undone.

</td>
<td width="33%" valign="top">

**9 book languages**<br>
Russian, English, German, French, Spanish, Italian, Chinese, Japanese, Korean. Detected from the book's
metadata or its text; `--lang` overrides it.

</td>
<td width="33%" valign="top">

**Text prepared for speech**<br>
Numbers, dates, times, money, units, roman numerals and abbreviations read in full, with case and gender
agreement where the language has it (Russian cases, Spanish `veintiuna`, French `une`, German `eine`, Korean
native counters, Chinese `两`); sentences split and packed into fragments; dialogue pauses.
[Per-language table](docs/how-it-works.md#what-the-normaliser-reads-out-per-language).

</td>
</tr>
<tr>
<td valign="top">

**Stress, `ё` and your own pronunciations**<br>
For Russian, `ё` is restored automatically with ruaccent, and full stress marks are available
(`--stress all`, experimental). Names and invented words: a plain-text dictionary next to the book
(`Kvothe = Quothe`), also editable in the studio. [Details](docs/pronunciation.md).

</td>
<td valign="top">

**Quality control**<br>
Every fragment is listened back with Whisper. If it does not match the text it is read again, and the best
take is kept (`--takes`, default 3).

</td>
<td valign="top">

**Real audiobook files**<br>
One `.m4b` with chapter markers, cover and metadata (title, author, narrator, year, description, series), or
one `.mp3` per chapter. Loudness-normalised (−18 LUFS).

</td>
</tr>
<tr>
<td valign="top">

**Built for 10-hour books**<br>
Every finished fragment is journalled. Stop, crash or lose power, then run again and it continues. Edit the
text and only changed fragments are redone. The previous file is never replaced until the new one is complete.

</td>
<td valign="top">

**Studio with live progress**<br>
Chapter lanes, the fragment being read, elapsed and remaining time, retakes, playback of finished chapters,
a quick preview before the long run. Local only, token-protected.

</td>
<td valign="top">

**Runs on what you have**<br>
Apple Silicon (GPU via Metal, Whisper via MLX), NVIDIA CUDA, or a plain CPU. If the GPU fails, the CPU takes
over. `folivox doctor` reviews your hardware and gives advice; `folivox bench` measures the real speed.

</td>
</tr>
<tr>
<td valign="top">

**Interface in 9 languages**<br>
The studio speaks English, Russian, German, French, Spanish, Italian, Chinese, Japanese and Korean (menu in
the header).

</td>
<td valign="top">

**Free, with no limits**<br>
The local engine reads any number of books of any length at no cost. Only if you choose a paid online
engine: a cost estimate first, your confirmation, a clean stop at `--max-cost`, no fragment paid for twice.

</td>
<td valign="top">

**Sturdy and open**<br>
Early ffmpeg and disk checks, resumable downloads, retries with backoff, waiting out network outages, a
redacted support bundle. Pluggable providers via entry points.

</td>
</tr>
</table>

### A closer look

<table>
<tr>
<td width="50%"><img src="docs/img/studio-book.png" alt="A loaded book with its chapters and estimated length"><br><sub>Drop a book: chapters, language, cover, details and estimated length.</sub></td>
<td width="50%"><img src="docs/img/studio-voices.png" alt="Reading engine and voice gallery"><br><sub>The voice for your book: the best match is picked automatically; audition others, record your own or generate one.</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/img/studio-done.png" alt="The audiobook is ready card"><br><sub>Done: the file, its length and how long it took.</sub></td>
<td width="50%"><img src="docs/img/studio-gallery.png" alt="Voice gallery with 64 bundled voices filtered by language"><br><sub>The bundled voice library: filter by language and sex, audition, star favourites.</sub></td>
</tr>
</table>

<sub>Demo render on a public-domain text (<i>Alice's Adventures in Wonderland</i>); timings are illustrative.</sub>

## Why Folivox

- **Free for real.** The local engine has no limits and no bill; online engines are an option, not a requirement.
- **A workhorse for long books.** Resumable, crash-safe, quality-checked, with a time forecast: start a
  ten-hour book and walk away.
- **Speaks every book's language properly.** A text normaliser for each of the 9 languages reads numbers,
  dates, money, units and roman numerals the way a narrator would, with gender and case agreement.
- **Simple outside, deep inside.** A few clicks in the studio; every option is still there in the CLI.
- **Any voice, consistent to the last page.** Yours, a bundled one, or one designed for the book.
- **Honest about what leaves your computer.** Online use is opt-in, labelled, estimated and capped.
- **Open and replaceable.** Apache-2.0 and small provider interfaces: when a better model appears, plug it in.

## Usage in short

```bash
folivox inspect book.epub                     # chapter tree, size, estimated duration
folivox book.fb2 -v me.wav -o book.m4b        # render the whole book with your voice
folivox book.fb2 -c 1-2 --preview 600         # a quick test: chapters 1–2, first 600 characters each
folivox book.pdf --mp3 -o out/                # one mp3 per chapter
folivox render book.epub --tts openai --tts-voice coral --max-cost 5   # an online engine with a cap
folivox voices                                # your voice library
folivox doctor --bundle                       # check the setup / write a redacted support bundle
folivox bench                                 # speed test: characters per second on each device (CPU, CUDA, MPS)
```

`folivox bench` synthesises one short fragment on every device it finds, each in its own process, and prints
characters per second, RTF and peak GPU memory, so you can see what actually helps on your machine and compare
machines (`--devices cpu,cuda` picks devices; CUDA is tried even if `auto` would skip a small GPU).

`folivox --help`, `folivox render --help` and `folivox help <topic>` (`voices`, `providers`, `config`,
`languages`, `resuming`, `quality`, `speed`) explain every option. Full list: [docs/cli.md](docs/cli.md).

## FAQ

**What hardware do I need?** Python 3.10–3.12, ffmpeg and about 7.6 GB for the models. A Mac with Apple
Silicon, or Windows/Linux with an NVIDIA GPU or just a CPU. The CPU works but is slower; `folivox setup` and
`folivox doctor` look at your machine and tell you what to expect.

**How long does a book take?** It depends on your hardware, so we do not quote a number. The studio shows the
audio length before you start, estimates the render time from your own previous runs, and shows the remaining
time while it works. Try a preview first to hear the voice on a few hundred characters.

**Is my book uploaded anywhere?** Not with the local engine (the default): everything runs on your computer
and the studio listens on `127.0.0.1` only; the network is used to download the models. Online engines,
online voice generation and OpenAI-compatible recognition or LLM providers **send text (or audio) to that
provider and may cost money**; Folivox says so and asks first. Keys live in environment variables or your OS
keychain, never in a config file.

**Can I stop and continue?** Yes, at any time, even after a crash or a power cut: run the same command (or
press Render) again.

**Whose voice may I clone?** Your own, or one you have explicit permission to use. Folivox ships no celebrity
or third-party voices and has no feature for imitating real people.
The bundled voices are real readers' public-domain recordings from LibriVox; a reader can ask for their recording to be removed, and the voice will then be dropped.

**How do I remove it?** `folivox uninstall` deletes the models Folivox downloaded (it shows the list and asks first;
your voices and settings stay unless you add `--everything`), then `uv tool uninstall folivox` removes the program.
Your books, audiobooks, ffmpeg and anything you had before are never touched. Details: [docs/install.md](docs/install.md).

## Documentation

| Page | What is in it |
|---|---|
| [docs/install.md](docs/install.md) | One-command install, starting the studio, update and removal |
| [docs/cli.md](docs/cli.md) | All `folivox render` options and examples |
| [docs/pronunciation.md](docs/pronunciation.md) | Custom pronunciations: names, invented words, Russian stress, phonemes |
| [docs/voices.md](docs/voices.md) | Voice library, your own voice, the voice sample, designing and generating voices |
| [docs/cloud-engines.md](docs/cloud-engines.md) | Online reading engines: prices, limits, terms, spending cap |
| [docs/providers.md](docs/providers.md) | Pluggable providers and `config.toml` |
| [docs/ui.md](docs/ui.md) | The web studio: design, screens and API |
| [docs/docker.md](docs/docker.md) | Running the studio on a server: Docker, Compose, Docker Swarm |
| [docs/ui-languages.md](docs/ui-languages.md) | Interface languages and translations |
| [docs/how-it-works.md](docs/how-it-works.md) | The pipeline in five steps |
| [docs/support.md](docs/support.md) | Resuming, retries, `folivox doctor` and the support bundle |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup and code structure |

## Development

```bash
uv sync --all-extras
uv run pytest -q
uv run ruff check src tests
```

The CI workflow (`.github/workflows/ci.yml`) runs lint and the unit tests on macOS, Windows and Linux without
model inference. Notes for working on the code: [CONTRIBUTING.md](CONTRIBUTING.md).

## Feedback

Found a bug, a mispronounced word or a book that does not open? Please
[open an issue](https://github.com/vatavale/folivox/issues) with the steps to reproduce; for crashes attach
the redacted bundle from `folivox doctor --bundle`. Ideas and feature requests are welcome there too.

Folivox is maintained by one person, so before writing code for a larger change, please open an issue
first to talk it over.

## Acknowledgements

Folivox stands on the work of many people who shared it openly. Thank you:

- [CosyVoice](https://github.com/FunAudioLLM/CosyVoice) by the FunAudioLLM team (Alibaba Tongyi Lab): the
  voice-cloning model and inference code at the heart of Folivox.
- [Matcha-TTS](https://github.com/shivammehta25/Matcha-TTS) and [Whisper](https://github.com/openai/whisper)
  (OpenAI), parts of which are vendored; [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and
  [mlx-whisper](https://github.com/ml-explore/mlx-examples) for the quality check.
- [LibriVox](https://librivox.org/) and its volunteer readers, who record public-domain books and give the
  recordings to the public domain; the bundled voices come from LibriVox recordings.
- [Qwen3-TTS](https://github.com/QwenLM) for local voice design.
- [ruaccent](https://github.com/Den4ikAI/ruaccent) (stress and `ё`), [razdel](https://github.com/natasha/razdel)
  (Natasha project, sentence splitting) and [num2words](https://github.com/savoirfairelinux/num2words).
- [FFmpeg](https://ffmpeg.org/), [PyTorch](https://pytorch.org/), [Hugging Face](https://huggingface.co/),
  [PyMuPDF](https://pymupdf.readthedocs.io/), [EbookLib](https://github.com/aerkalov/ebooklib),
  [python-docx](https://github.com/python-openxml/python-docx), [noisereduce](https://github.com/timsainb/noisereduce),
  [Starlette](https://www.starlette.io/), [Typer](https://typer.tiangolo.com/), [Rich](https://github.com/Textualize/rich)
  and [uv](https://github.com/astral-sh/uv).
- [Project Gutenberg](https://www.gutenberg.org/) for the public-domain text in the screenshots.

## License and status

Status: early development. Folivox is licensed under **Apache-2.0**. It vendors a trimmed copy of the
CosyVoice inference code (Apache-2.0) and parts of Matcha-TTS and Whisper (MIT); see
[`src/folivox/vendor/README.md`](src/folivox/vendor/README.md) and [NOTICE](NOTICE). The CosyVoice 3 weights are
Apache-2.0 ([model card](https://huggingface.co/FunAudioLLM/Fun-CosyVoice3-0.5B-2512)) and are downloaded, not
redistributed. Audio produced by commercial APIs is never bundled; its use is governed by the provider's terms.
