# Vexa Architecture and Anti‑Detection Notes

## Overview
- Self-hosted meeting intelligence: bots join Google Meet and Microsoft Teams, capture audio, and stream real-time multilingual transcripts.
- Interfaces: REST (API Gateway), WebSocket (WhisperLive), internal Redis for command/control, PostgreSQL for persistence.
- Deployments: Vexa Lite (single container, GPU-free via external transcriber) and full Docker Compose stack.

## System Components
- API Gateway
  - Forwards public API calls to internal services with header pass‑through.
  - Entrypoint for create/stop bots, status, etc.
  - Code: /services/api-gateway [main.py](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/api-gateway/main.py)

- Bot Manager
  - Meeting lifecycle owner; mints MeetingToken (HS256 JWT) per meeting.
  - Starts/stops bot containers via orchestrator façade (Docker/Nomad).
  - Publishes status via Redis; persists transitions to DB.
  - Code:
    - Orchestrator façade: [docker.py](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/bot-manager/app/orchestrators/docker.py)
    - Start container with BOT_CONFIG: [orchestrator_utils.py](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/bot-manager/app/orchestrator_utils.py#L130-L225)
    - Token minting: [main.py](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/bot-manager/app/main.py#L227-L279)

- Vexa Bot (Playwright runtime)
  - Automates browser to join meeting, mute, handle admission/removal, and capture audio in-page.
  - Platform-specific flows coordinated by a shared meeting flow.
  - Code:
    - Runtime entry: [index.ts](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/index.ts)
    - Shared flow: [meetingFlow.ts](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/platforms/shared/meetingFlow.ts)
    - Google Meet flow: [googlemeet/index.ts](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/platforms/googlemeet/index.ts)

- WhisperLive
  - WebSocket edge for real-time transcription; fronts local transcription-service or external service.
  - Bot sends config + PCM chunks; receives segments/language.
  - Code (client): [whisperlive.ts](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/services/whisperlive.ts)

- Transcription Service
  - Whisper-compatible service with Nginx load balancer and worker replicas (GPU/CPU).
  - Code: [README.md](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/transcription-service/README.md)

- Transcription Collector
  - Merges Redis partials with DB-finalized segments; de-duplicates overlaps; exposes APIs.
  - Code:
    - API endpoints: [endpoints.py](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/transcription-collector/api/endpoints.py)
    - Service entry: [main.py](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/transcription-collector/main.py)

- Shared Models
  - Meeting status FSM, platform helpers, schemas.
  - Code: [models.py](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/libs/shared-models/shared_models/models.py), [schemas.py](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/libs/shared-models/shared_models/schemas.py)

## Control & Data Flow
1. Client → API Gateway POST /bots; forwarded to Bot Manager.
2. Bot Manager validates, mints MeetingToken, starts bot container with BOT_CONFIG (meeting_id, platform, meetingUrl, botName, language, task, token, redisUrl, connectionId).
3. Bot launches Playwright and joins meeting using platform strategies; sends status callbacks: joining → awaiting_admission → active.
4. In-page Web Audio pipeline captures 16k PCM; sends via WhisperLive WebSocket.
5. WhisperLive → transcription-service (local or external) → streams segments back.
6. Transcription Collector merges Redis partials + DB finalized; provides GET transcript.
7. Status updates and exit: unified callback with retries/backoff, graceful leave paths.

## Deployment Modes
- Vexa Lite (single container)
  - Stateless API + Bot Manager; uses your Postgres and external transcription.
  - Guide: [docs/vexa-lite-deployment.md](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/docs/vexa-lite-deployment.md)
- Full stack (compose)
  - API Gateway, Bot Manager, bots, WhisperLive, transcription-service, DB.
  - Compose: [docker-compose.yml](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/docker-compose.yml)

## Anti‑Detection Strategies
- Stealth plugin (non‑Teams)
  - puppeteer-extra-plugin-stealth with Playwright Extra; selective evasion disabling.
  - Code: [index.ts:L442-L447](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/index.ts#L442-L447)
- Navigator/window overrides
  - addInitScript sets navigator.webdriver undefined, languages/plugins, hardwareConcurrency/deviceMemory, viewport sizes.
  - Code: [index.ts:L477-L492](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/index.ts#L477-L492)
- UA and launch flags
  - Modern Chrome UA; flags for stable media capture and reduced detection.
  - Code: [constans.ts](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/constans.ts)
- CSP/script injection
  - Bypass CSP contexts; pre-inject utils; Trusted Types / Blob URL fallback.
  - Code: [index.ts:L424-L437](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/index.ts#L424-L437), [injection.ts:L23-L72](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/utils/injection.ts#L23-L72)
- Platform-aligned browser choice
  - Teams uses Edge channel + permissive flags; Meet uses Chrome with stealth.
  - Code: [index.ts:L404-L421](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/index.ts#L404-L421)
- Human-like behavior
  - Random delays, targeted selectors, settle waits, minimal DOM disturbance.
  - Code: [googlemeet/join.ts](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/vexa-bot/core/src/platforms/googlemeet/join.ts)

## Status Model & Storage
- MeetingStatus FSM with validated transitions and sources (user, bot_callback, validation_error).
  - Code: [models.py](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/libs/shared-models/shared_models/models.py)
- Platform helpers construct meeting URLs and validate native IDs.
  - Code: [models.py platform](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/libs/shared-models/shared_models/models.py#L120-L220)
- Transcript assembly merges Redis partials + DB finalized with overlap dedupe.
  - Code: [endpoints.py:L160-L220](file:///Users/rajendra/Desktop/personal_v2/scrum/POC/vexa/services/transcription-collector/api/endpoints.py#L160-L220)

## Extensibility
- New platforms: implement strategies (join, waitForAdmission, prepare, startRecording, startRemovalMonitor, leave) and register with shared flow.
- Alternate orchestrators: implement the orchestrator shim methods and export via app.orchestrators.*.
- Alternate transcription: point WhisperLive to external Whisper-compatible service.

