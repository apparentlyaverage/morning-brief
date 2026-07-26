# Morning Brief

**A radio show that runs at 06:30 and only has one listener.**

Your music starts. It plays for three minutes while the briefing is built behind
it. Then it fades down, and a voice tells you what the weather's doing, whether
you have lectures, what's coming up, where the markets closed, and what's in the
news. Then the music comes back — louder than before, because the point is to
get you out of bed.

Everything is local: a Python script, a PowerShell sequence, and a small web
server on `127.0.0.1`. No account, no cloud, no telemetry.

![The Morning Brief dashboard: a month calendar with the day's lectures, an
editable to-do list, live weather, and the music player along the
bottom](docs/dashboard.png)

## What it sounds like

Not a list read out. A local model rewrites the whole thing in a voice you
define, and this is real output from a Sunday in July:

> Morning Uthando, hope you slept well on this fine Sunday in Makhanda. The sky
> is clear, it's sitting at sixteen degrees right now, and we'll climb to a high
> of twenty-three before dropping to a chilly eight later on. Rain is unlikely,
> so any outdoor plans can go ahead without a plan B. No classes for you today,
> so take it easy and enjoy the morning with us.

Turn the model off and you get the same facts in plain sentences. It never
depends on the model being there.

Scripture is the one thing the model never sees — a verse is appended verbatim
after the rewrite, because quoting it exactly matters and paraphrasing it
doesn't.

## What it does

| | |
|---|---|
| **Weather** | Open-Meteo, no API key |
| **Classes** | your timetable, gated on term dates, exam blocks and public holidays |
| **Calendar** | import an `.ics`, or add events by hand |
| **Markets** | shares, indices and crypto, including JSE tickers |
| **News** | SA and world RSS, deduplicated and round-robined across sources |
| **Listen in** | click a story and it reads you the *article*, not the headline |
| **Verse of the day** | Bible, Qur'an or Bhagavad Gita, quoted exactly |
| **Voice** | neural TTS, degrading to two offline engines |
| **Dashboard** | month calendar, to-do list, news cards, player with queue and seek, and a document reader that remembers your place |
| **PWA** | installable, works as a desktop app |

### Listen in

The briefing gives you headlines. If one matters, click **🎧 Listen in** on the
card: the server fetches the article, has the model talk you through it, and
reads it aloud — while the music keeps playing underneath, ducked, and returns
to exactly the volume you had it at.

It's on demand rather than part of the morning routine, so a story that won't
extract costs nothing and the three-minute intro budget is untouched.

## How it works

The design constraint throughout was **plug and play**: standard library only,
apart from `edge-tts` and `pypdf`, which setup installs for you. No pip wall to
climb, no build step. That constraint is where most of the interesting problems
came from.

**Speech falls back twice.** Windows keeps its voices in three registries that
cannot see each other — SAPI, OneCore, and Speech Server. The good "Natural"
voices are Narrator-only and register no usable token at all, so the briefing
uses Microsoft's online neural voices and degrades to OneCore, then to SAPI, if
the network is down. It is never silent.

**PowerShell 5.1 can't call the modern speech engine**, because its async WinRT
calls come back as unprojected COM objects and `Add-Type` won't consume a
`.winmd`. So `TtsHelper.cs` is a small C# shim compiled against the Windows SDK,
which also sets the system master volume through Core Audio — something
PowerShell has no way to do beyond simulating keystrokes.

**The `.ics` importer is hand-rolled** (`calendar_ics.py`): line unfolding,
escapes, all-day events, and `RRULE` expansion for daily, weekly with `BYDAY`,
monthly and yearly, with `COUNT`, `UNTIL`, `INTERVAL` and `EXDATE`. It reports
what it can't do — `TZID` conversion, `BYSETPOS`, per-occurrence edits — as
warnings rather than failing quietly.

**Article extraction is three heuristics, best first** (`article.py`):
schema.org `articleBody`, then the `<article>` element with the most prose, then
paragraph density. Scoring on prose rather than markup size matters more than it
sounds — on one publisher every `<article>` on the page is a reader comment, so
picking the largest block returned a comment thread instead of the story.

**Everything degrades.** A failed market call falls back to the last cached
price rather than a blank row. If the briefing fails to build, the previous
one is *not* read out — confidently reading yesterday's weather as though it
were today's is worse than admitting the fetch failed, so it apologises
instead. Volume restoration sits in a `finally`, so a speech crash can't leave
your music stuck at 10%.

### Notes from building it

A few things that cost real time and might save someone else some:

- **`localhost` resolves to `::1` first.** Cider and Ollama bind IPv4 only, so
  every call wasted about two seconds failing over. Measured 2184 ms vs 4 ms.
  One endpoint went from 8.4 s to 179 ms by writing `127.0.0.1`.
- **Reasoning models need a huge output budget or they return nothing.** They
  spend it thinking before writing a word. Measured 0 of 4 usable responses at
  600 tokens, 5 of 5 at 16000. The symptom is an empty response with
  `done_reason: "length"` — it looks like a broken model, it's a starved one.
- **PowerShell 5.1's `Set-Content -Encoding utf8` writes a BOM**, and a BOM in
  front of JSON is a hard parse error. Setup wrote a config the app couldn't
  read, so a brand-new install crashed on its first run — invisible here,
  because this machine's config was written by Python.
- **Piping a long-running script into `Select-Object -First N` kills it**, which
  produces a confusing non-zero exit from a process that actually succeeded.
- **Audit a page while driving it, not at rest.** The settings page looked
  clean until a search rendered eight keyboard-unreachable rows.

## Accessibility

Both pages are keyboard navigable with visible focus, labelled controls, and
live regions for anything that changes without a click. Contrast was measured
in-browser rather than eyeballed: all text passes WCAG AA, and anything whose
border identifies it as a control meets the 3:1 required by 1.4.11.
`prefers-reduced-motion` is honoured.

## Requirements

- **Windows 10/11**
- **Python 3.9+** on your PATH
- **[Cider](https://cider.sh)** — this is what plays the music. Turn on its
  External API in *Settings → Connectivity*.
- Internet at run time

## Setup

Double-click **`START HERE.cmd`**.

It checks Python and Cider, installs `edge-tts` and `pypdf`, writes a starter
config (never overwriting an existing one), registers the scheduled task, and
opens the settings page. Then set your town, paste your Cider token, and name a
playlist — everything else has a working default.

Prefer to do it by hand? `powershell -ExecutionPolicy Bypass -File setup.ps1`.

### Running it

| | |
|---|---|
| `settings.ps1` | settings and dashboard (`http://127.0.0.1:8765`) |
| `morning-routine.ps1` | the full sequence — music, briefing, dashboard |
| `run-briefing.ps1` | briefing only, no music |
| `install-task.ps1 -Time 06:30` | change the daily time |
| `install-task.ps1 -Uninstall` | remove the scheduled task |

Useful for testing: `-IntroSeconds 5`, `-NoMusic`, `-NoSpeak`, `-NoDashboard`.

### Sending it to someone

```powershell
.\make-release.ps1
```

Builds `morning-brief.zip` from `git archive`, so nothing untracked — your
config, timetable, tasks, diary, uploaded documents, caches — can be included by
accident. They unzip it and double-click `START HERE.cmd`.

## Tests

```powershell
.\run-tests.ps1
```

229 tests, about a second and a half, standard-library `unittest` so the project
keeps running on a clean Python install. No network: the market API is stubbed
and the library and calendar paths are redirected at a temp directory.
`-Detailed` names each test, `-Only stocks` runs one file.

They target the things that fail quietly rather than loudly — `RRULE` expansion,
deleted-event bookkeeping that has to survive a re-import, JSE prices arriving
in cents, reading-position clamping, article extraction, and the academic
calendar. That last one runs against a real university calendar in
`tests/fixtures` and checks that every public holiday suppresses lectures,
including one that falls on a Sunday and is observed on the Monday.

The suite was checked by mutation: seven deliberate breakages — dropping the
cents conversion, skipping the holiday check, ignoring `EXDATE`, unclamping the
reading position — were each caught by a failing test.

## Your data stays yours

`config.json`, your timetable, tasks, diary and uploaded documents are all
git-ignored and never leave the machine. The only outbound traffic is the text
sent to Microsoft's voice service to be spoken, and the requests to the news,
weather and market sources.

Setting up by hand: copy `config.example.json` → `config.json` and
`timetable.example.json` → `timetable.json`.

## Layout

```
briefing.py          builds the briefing (screen, spoken, and JSON for the UI)
ui_server.py         local web server for the settings page and dashboard
ui/                  the two pages, service worker, manifest, icons
morning-routine.ps1  the morning sequence
speak.ps1            text to speech, online with two offline fallbacks
say_online.py        neural voice via edge-tts
cider.ps1            Cider control (playlists, playback, volume)
article.py           pulls the readable body out of a news page
calendar_ics.py      .ics importer with RRULE expansion
events_store.py      hand-added calendar events
documents.py         document library and reading position
scripture.py         verse of the day
stocks.py            market quotes
llm.py               Ollama client
TtsHelper.cs         C# shim: modern Windows voices + system volume
tests/               stdlib unittest suite (run-tests.ps1)
make-release.ps1     builds the zip you send to someone else
```

`TtsHelper.dll` is committed so the app runs without a build step. Rebuild with
`build-ttshelper.ps1` only if you change the C# (needs the Windows SDK).

Roughly 3,400 lines of Python, 2,100 of HTML/CSS/JS, 1,100 of PowerShell and 130
of C#, plus 1,700 lines of tests.

## Known limits

- **Windows only.** It leans on PowerShell, Task Scheduler, WinRT and Cider's
  local API.
- **Cider is required** for music. Spotify's API is effectively closed to new
  products and YouTube Music has no official API, so neither is supported.
- **You supply your own term dates.** `timetable.example.json` ships blank on
  purpose: one university's calendar is wrong for everyone else, and being told
  about a term you aren't in is worse than being told nothing. Leave `_calendar`
  empty and classes are assumed to run every weekday.
- **"Listen in" can't read every story.** Extraction is heuristic and roughly
  three quarters of stories work. Some publishers render the body without
  paragraph tags; short photo pieces have nothing to extract. You get a message
  saying so rather than a broken segment — and it reads whatever the page gives
  it, so on a paywalled story that's the teaser, not the article.
- **Several data sources are free, undocumented endpoints.** Fine for personal
  use; a commercial version would need licensed replacements for the voice, the
  market data and the scripture sources.

---

Built for one person in Makhanda who wanted one glance in the morning instead of
three apps.
