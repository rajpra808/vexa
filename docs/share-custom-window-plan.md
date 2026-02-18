# Feature Plan: Pre-Open Custom Page and Share Specific Window in Google Meet

## Scope
- Before joining: launch an extra browser page with a configurable URL (e.g., https://www.google.com) and a deterministic window/tab title.
- After joining and becoming ACTIVE: start presenting that specific page in the Google Meet UI.
- Do not regress existing transcription, admission handling, status callbacks, or anti‑detection behavior.
- Default-off; opt-in via new BOT_CONFIG fields.

## Complexity (Qualitative)
- Google Meet + Chromium: Medium
  - Straightforward if `--auto-select-desktop-capture-source=<Title>` works in the deployed Chromium channel.
  - Without auto‑select flag, selecting the tab/window from Meet’s “Present now” dialog requires brittle UI selectors → Medium‑High.
- Microsoft Teams + Edge: Medium‑High
  - Similar mechanism; UI differs, selectors differ, and screen-share constraints in containerized environments are higher.
- Environment constraints: High variance
  - Screen capture inside containers may require a functional display server (Xvfb/Wayland), appropriate caps, and matching Chromium channel.
  - “Tab capture” is typically more reliable than “Entire screen” in headless/virtual displays.

## Preconditions and Risks
- Chromium/Edge must support `--auto-select-desktop-capture-source` in the deployed channel.
- The bot already launches non‑headless. Ensure virtual display is present in containers.
- Meet UI can change; selectors should be resilient with fallbacks.
- Avoid audio loops: present a tab with no audio or ensure tab has no playing audio.

## Proposed Configuration Additions
Extend BOT_CONFIG (and zod schema) with optional fields (all default null/disabled):
- `shareWindowUrl: string | null` — URL to open and later present.
- `shareWindowTitle: string | null` — explicit title to set on the page, used by auto‑select; if empty, derive from URL.
- `shareMode: 'tab' | 'window' | null` — desired presentation type (prefer 'tab' for reliability).
- `shareStart: 'after_active' | 'manual' | null` — when to trigger.

Change points:
- Schema: [types/schema in bot core](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/index.ts)
- Orchestrator: pass through from Bot Manager’s `start_bot_container`: [orchestrator_utils.py](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/bot-manager/app/orchestrator_utils.py)

## High‑Level Flow Changes
1) Bot startup (Node)
   - If `shareWindowUrl` is set:
     - Before meeting join: create a new page, navigate to the URL, and set `document.title = shareWindowTitle` (if provided).
     - Keep a handle/reference to this page.
   - For Chromium launch (non‑Teams), if `shareWindowTitle` is present, append `--auto-select-desktop-capture-source=<shareWindowTitle>` to launch args.

2) Meeting join → admission → ACTIVE
   - After ACTIVE is confirmed (post‑verification), trigger a platform‑specific “start presentation” routine if `shareStart==='after_active'`.

3) Google Meet “Present now” – tab/window
   - Click “Present now”.
   - Prefer “A tab” flow to select our specific tab.
   - If auto‑select flag is present, opening the share dialog should auto‑pick our tab; otherwise, pick from the list using `shareWindowTitle` text.

4) Non‑regression
   - Keep mic/cam muted as before; ensure recording pipeline remains attached to meeting audio, not the presented tab.
   - Maintain status callbacks order and retry logic.

## File‑Level TODOs

### 1. Configuration plumbing
- Bot Schema: add optional fields
  - File: core schema parsing in [index.ts](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/index.ts)
  - Add `shareWindowUrl`, `shareWindowTitle`, `shareMode`, `shareStart` to the zod schema and `BotConfig` type.
- Bot Manager: pass‑through in BOT_CONFIG
  - File: [orchestrator_utils.py](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/bot-manager/app/orchestrator_utils.py)
  - Include the new fields when constructing `bot_config_data` (omit if null).
- API surface (optional extension)
  - If exposing via API: extend POST /bots input schema and validation; ensure backward compatibility.

### 2. Browser launch adjustments (non‑Teams)
- Inject Chrome arg when configured
  - File: [index.ts launch path](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/index.ts#L441-L452)
  - If `shareWindowTitle`, push `--auto-select-desktop-capture-source=${shareWindowTitle}` into args.

### 3. Pre‑open the share page
- Before calling platform join:
  - File: [index.ts] early in `runBot`
  - `const sharePage = await context.newPage(); await sharePage.goto(shareWindowUrl); await sharePage.evaluate(t => { document.title = t }, title);`
  - Store a handle (module‑scope variable or closure) for future reference.

### 4. Platform‑specific “start presentation” (Google Meet)
- Add a `present.ts` under googlemeet with helpers:
  - Click “Present now” button.
  - Choose “A tab” (preferred) or “A window”.
  - If auto‑select flag present, the dialog should accept automatically; otherwise locate and click the entry matching `shareWindowTitle`.
  - Add resilience: wait for buttons, use multiple selectors, add timeouts and fallbacks.
- Wire it into the shared flow after ACTIVE, before or in parallel with recording start:
  - File: [googlemeet/index.ts](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/platforms/googlemeet/index.ts)
  - After `runMeetingFlow` sets ACTIVE and before starting recording, call `startGooglePresentationIfConfigured`.

### 5. Teams (follow‑up)
- Similar module under `msteams/present.ts` using Teams UI selectors.
- Launch with Edge; test whether auto‑select flag behaves identically.

### 6. Guardrails and fallbacks
- If share fails, log and continue transcription; do not abort the meeting.
- Provide a Redis “reconfigure” action to stop/start presenting later (future).

### 7. Tests & Diagnostics
- Add screenshots around key checkpoints: before share, after clicking “Present now”, after share confirmed.
- Log concise events (e.g., `Sharing:STARTED|FAILED`) to avoid noise.
- Manual E2E checklist in dev compose:
  - Start Meet bot with share config; confirm ACTIVE then share started.
  - Verify transcripts still streaming; no echo loops.

## Selector Sketch (Google Meet; indicative)
- “Present now” button: `button[aria-label*="Present now"], button:has-text("Present now")`
- “A tab” option: `text="A tab"`, dialog list item contains `shareWindowTitle`
- Confirmation banners: locate and verify present indicator.

## Backward Compatibility
- All fields optional; default behavior unchanged.
- If unsupported environment (no display/flag ignored), feature is best-effort and non-blocking.

## Rollout Plan
- Phase 1: Google Meet + Tab sharing with auto‑select flag; document tested browser/OS channels.
- Phase 2: Fallback without auto‑select (UI selection), with robust selectors and retries.
- Phase 3: Teams parity.

## References
- Launch logic and anti‑detection: [index.ts](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/index.ts)
- Shared flow: [meetingFlow.ts](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/platforms/shared/meetingFlow.ts)
- Google Meet join/recording: [googlemeet/join.ts](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/platforms/googlemeet/join.ts), [googlemeet/recording.ts](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/platforms/googlemeet/recording.ts)
- Bot Manager start config: [orchestrator_utils.py](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/bot-manager/app/orchestrator_utils.py)

