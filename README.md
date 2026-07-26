# Morning Brief

A spoken morning briefing for Windows. At a set time it opens your music, plays
a playlist for a few minutes, fades it down, and reads you the weather, your
classes, what's coming up, the markets, the headlines and a verse — in a natural
neural voice. Then it opens a dashboard with your calendar, to-do list and the
news as clickable cards.

Built for a student in Makhanda, South Africa, so the defaults lean that way
(SA news feeds, JSE tickers, a South African voice). All of it is configurable.

## What it does

- **Weather** for your town, via Open-Meteo — no API key
- **Classes** from your timetable, aware of terms, exams and public holidays
- **Calendar** — import an `.ics`, or add events by hand
- **Markets** — shares, indices and crypto, including JSE tickers
- **News** — South African and world RSS, deduplicated across sources
- **Verse of the day** — Bible, Qur'an or Bhagavad Gita, quoted verbatim
- **Spoken aloud** in a neural voice, with an offline fallback
- **Optional local model** (via Ollama) rewrites it in a voice you define
- **Dashboard** — month calendar, editable to-do list, news cards, music player
  with queue and seek, and a document reader that remembers your place
- **Installable** as a PWA

## Requirements

- **Windows 10/11**
- **Python 3.9+** on your PATH
- **[Cider](https://cider.sh)** — required, this is what plays the music.
  Turn on its External API in *Settings → Connectivity*.
- Internet at run time (weather, news, markets, the neural voice)

Two Python packages are installed for you by setup: `edge-tts` (voices) and
`pypdf` (reading PDFs aloud).

## Setup

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

It checks Python and Cider, installs what's missing, writes a starter
`config.json` (never overwriting an existing one), registers the scheduled
task, and opens the settings page.

Then, on the settings page: set your town, paste your Cider API token, name a
playlist. Everything else has a working default.

## Running it

| | |
|---|---|
| `settings.ps1` | Settings page and dashboard (`http://127.0.0.1:8765`) |
| `morning-routine.ps1` | The full sequence — music, briefing, dashboard |
| `run-briefing.ps1` | Briefing only, no music |
| `install-task.ps1 -Time 06:30` | Change the daily time |
| `install-task.ps1 -Uninstall` | Remove the scheduled task |

Useful flags on `morning-routine.ps1`: `-IntroSeconds 5` for a quick test,
`-NoMusic`, `-NoSpeak`, `-NoDashboard`.

## Tests

```powershell
.\run-tests.ps1
```

191 tests, about a second, no network and no side effects — they redirect the
library and calendar paths at a temp directory, and stub the market API rather
than calling it. `-Detailed` names each test; `-Only stocks` runs one file.

They cover the parts that fail quietly rather than loudly: the `.ics`
recurrence expander, deleted-event bookkeeping that has to survive a
re-import, JSE prices arriving in cents, reading-position clamping, and the
academic calendar — including a check that every public holiday in the shipped
term dates actually suppresses lectures.

## Your data stays yours

`config.json`, your timetable, tasks, diary and uploaded documents are all
git-ignored. Nothing is uploaded anywhere except the text sent to Microsoft's
voice service to be spoken, and the requests to the news, weather and market
sources.

Copy `config.example.json` → `config.json` and `timetable.example.json` →
`timetable.json` if you'd rather set up by hand than run `setup.ps1`.

## Layout

```
briefing.py         builds the briefing (screen, spoken, and JSON for the UI)
ui_server.py        local web server for the settings page and dashboard
ui/                 the two pages, service worker, manifest, icons
morning-routine.ps1 the morning sequence
speak.ps1           text to speech, online voice with an offline fallback
say_online.py       neural voice via edge-tts
cider.ps1           Cider control (playlists, playback, volume)
scripture.py        verse of the day
stocks.py           market quotes
documents.py        document library and reading position
events_store.py     hand-added calendar events
calendar_ics.py     .ics importer
llm.py              Ollama client
TtsHelper.cs        C# shim: modern Windows voices + system volume
tests/              stdlib unittest suite (run-tests.ps1)
```

`TtsHelper.dll` is committed so the app runs without a build step. Rebuild it
with `build-ttshelper.ps1` if you ever change the C# (needs the Windows SDK).

## Known limits

- **Windows only.** It leans on PowerShell, Task Scheduler, WinRT and Cider's
  local API.
- **Cider is required** for music. Spotify and YouTube Music are not supported.
- **Term dates run out** when the academic year does — the briefing will tell
  you when it needs the next year's added.
- Several data sources are free, undocumented endpoints. Fine for personal
  use; they would need licensed replacements in a commercial product.
