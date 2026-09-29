# Voice agent

Talk to your Windows PC and it does things: open apps and sites, search, type,
press keys, click buttons by name, control videos and volume -- often while you
are still talking. Anything bigger ("reply to Sam that I'm running late", "look
up the best budget mics and compare them") goes to Claude, which can work your
screen and the web and saves research to `voice-finds\`.

It is silent: a small bar at the top of the screen shows what it heard and what
it did.

## What you need

- Windows 10 or 11 and a microphone
- [Python 3.11+](https://www.python.org/downloads/) -- tick **Add python.exe to PATH** when installing
- Optional but recommended: [Claude Code](https://claude.com/claude-code), logged in to your
  Claude account. Without it, the instant commands work; multi-step requests don't.
- Optional: a Jev key (see below). Without it, commands fire when you pause instead of mid-sentence.

## Set up (once)

1. Download or clone this folder anywhere.
2. Double-click **`setup.cmd`**. It installs everything into this folder (nothing system-wide)
   and downloads the speech models (~540 MB). Takes a few minutes.
3. If you use Claude Code, open a terminal once, run `claude`, and log in.

## Use it

Double-click **`Start Voice Agent.cmd`** and wait for the window to say it's ready.

- **Double-tap Alt** -> rising beep, the bar appears. Talk.
- **Double-tap Alt again** -> falling beep, the mic is off. A sentence you were in the middle of still runs.
- Changed your mind mid-sentence? Say **"never mind"**, **"scratch that"** or **"hold on"** --
  everything before it is dropped.

| Say | What happens |
|---|---|
| "open Chrome", "open YouTube", "open Reddit on Chrome", "new tab" | opens it |
| "theater mode", "full screen", "pause", "next video", "skip ad" | presses the key / button |
| "volume to 30", "turn it down", "mute" | sets the volume |
| "search for cheap flights to Miami" | searches (on YouTube, searches YouTube) |
| "type hello world", "press ctrl t", "click the settings button" | types / presses / clicks |
| "click the third video", "reply to Sam that I'm late" | Claude does it on screen |
| "look for new AI video tools and compare them" | Claude researches and saves a file in `voice-finds\` |

Add names it mishears to **`voice-agent\vocab.txt`**.

## Know what it can do

This is software that controls your computer from your voice. Read this once:

- **While the mic is on, it acts on what it hears -- including a video, a stream or someone
  else in the room.** Turn the mic off (double-tap Alt) when you're not talking to it.
- The **instant commands** refuse a list of risky keys and texts (close window, delete, shell
  commands) and do nothing while a trading, password or sign-in window is in front.
- **Claude** can click, type and open programs in any window and browse the web, without asking
  each time. It is told to ask before sending, posting, paying or deleting anything and to stay
  out of chat apps and trading or password windows -- but those are instructions, not locks. Keep
  sensitive windows closed while you use it for bigger tasks.

## Privacy -- what goes where

- Your voice is turned into text **on your PC** (nothing is recorded or uploaded as audio).
- With a Jev key, the **text of what you say** is sent to Jev (TypeSafe, via Vercel) to decide
  what you meant.
- Requests that go to Claude -- plus whatever Claude reads on your screen to do them -- go to
  **Anthropic** through your Claude Code account.
- On your PC it keeps a timeline log and caches in `voice-agent\` (`.voice.log`, `decisions.jsonl`,
  `.jev_cache\`). Delete them any time.

## Costs

- Claude requests use **your own Claude plan** (or your API key if `ANTHROPIC_API_KEY` is set --
  then they are billed to that API account).
- Jev costs fractions of a cent per command.

## Optional: a Jev key

Jev is a fast decision model that lets commands fire mid-sentence. Get a key from the
[Vercel AI Gateway](https://vercel.com/ai-gateway) (it serves Jev as `typesafe-ai/jev`) or
from TypeSafe when their sign-ups are open, then in a terminal:

    setx AI_GATEWAY_API_KEY "your-vercel-key"
    rem or, for a key from TypeSafe:
    setx TYPESAFE_API_KEY "your-typesafe-key"

Restart the voice agent afterwards. Tip: set a monthly spend limit on the key.

## Settings

Set these as environment variables (`setx NAME value`, then restart the agent):

| Setting | Default | What |
|---|---|---|
| `VOICE_DEVICE` | auto | part of your mic's name (e.g. `headset`) if it picks the wrong one |
| `VOICE_SPEAK` | 0 | `1` = speak replies out loud |
| `VOICE_BRAIN_MODEL` | haiku | Claude model for quick screen work |
| `VOICE_BRAIN_SMART` | sonnet | Claude model for research and multi-step work |
| `VOICE_ROUTER` | 1 | `0` = always use the quick model |
| `VOICE_HANDS` | 1 | `0` = Claude can answer and research but not touch your screen |
| `VOICE_HOTKEY` | 1 | `0` = no double-tap Alt |
| `VOICE_OVERLAY` | 1 | `0` = no bar at the top of the screen |
| `VOICE_DRY_RUN` | 0 | `1` = show what it would do without doing it (or run `start.py dry`) |

## Troubleshooting

- **It hears nothing** -- the wrong mic was picked. Set `VOICE_DEVICE` to part of your mic's name.
- **Slow to start** -- first start loads the speech models; with little free memory it can take a minute.
- **Claude says it isn't logged in** -- run `claude` in a terminal and log in.
- The full log is at http://127.0.0.1:8420 while it runs.

## For developers

    .venv\Scripts\python -m pytest -q voice-agent\tests      # offline, no keys needed
