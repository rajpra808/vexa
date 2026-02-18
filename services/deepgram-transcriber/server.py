"""
Deepgram Transcriber — drop-in WebSocket proxy for WhisperLive.

Speaks the same protocol the Vexa Bot already uses:
  1. Bot connects, sends JSON config: {uid, platform, meeting_url, token, meeting_id, language, ...}
  2. Server replies: {"status": "SERVER_READY", "uid": <uid>}
  3. Bot streams Float32Array audio (binary) and JSON control messages
  4. Server pushes transcription segments to Redis (same format as WhisperLive)

Audio conversion: Bot sends float32 PCM → we convert to int16 PCM (linear16) for Deepgram.
"""

import os
import sys
import json
import time
import uuid
import logging
import asyncio
import hashlib
import datetime
import struct
from typing import Optional

import numpy as np
import redis
import websockets
from deepgram import (
    DeepgramClient,
    LiveTranscriptionEvents,
    LiveOptions,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("deepgram-transcriber")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "9090"))
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")

REDIS_URL = os.getenv("REDIS_STREAM_URL") or os.getenv("REDIS_URL") or "redis://redis:6379/0"
REDIS_STREAM_KEY = os.getenv("REDIS_STREAM_KEY", "transcription_segments")
REDIS_SPEAKER_EVENTS_KEY = os.getenv("REDIS_SPEAKER_EVENTS_RELATIVE_STREAM_KEY", "speaker_events_relative")

SAMPLE_RATE = 16000  # must match what the bot sends


# ---------------------------------------------------------------------------
# Redis helper (synchronous; runs in executor from async context)
# ---------------------------------------------------------------------------
class RedisPublisher:
    """Thin wrapper around Redis for publishing transcription & speaker events."""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._client: Optional[redis.Redis] = None
        self._connect()
        # Track session starts to avoid duplicates
        self._session_starts: set = set()
        # Dedupe repeated identical transcript payloads per session_uid
        self._last_digest: dict = {}

    def _connect(self):
        try:
            self._client = redis.from_url(self.redis_url, decode_responses=True)
            self._client.ping()
            logger.info(f"Connected to Redis at {self.redis_url}")
        except Exception as exc:
            logger.error(f"Redis connection failed: {exc}")
            self._client = None

    def _ensure_connected(self) -> bool:
        if self._client:
            try:
                self._client.ping()
                return True
            except Exception:
                self._client = None
        self._connect()
        return self._client is not None

    # -- session lifecycle ---------------------------------------------------

    def publish_session_start(self, token: str, platform: str, meeting_id: str, session_uid: str):
        if session_uid in self._session_starts:
            return
        if not self._ensure_connected():
            return
        now_iso = datetime.datetime.utcnow().isoformat() + "Z"
        payload = {
            "type": "session_start",
            "token": token,
            "platform": platform,
            "meeting_id": meeting_id,
            "uid": session_uid,
            "start_timestamp": now_iso,
        }
        try:
            self._client.xadd(REDIS_STREAM_KEY, {"payload": json.dumps(payload)})
            self._session_starts.add(session_uid)
            logger.info(f"Published session_start for {session_uid}")
        except Exception as exc:
            logger.error(f"Failed to publish session_start: {exc}")

    def publish_session_end(self, token: str, platform: str, meeting_id: str, session_uid: str):
        if not self._ensure_connected():
            return
        now_iso = datetime.datetime.utcnow().isoformat() + "Z"
        payload = {
            "type": "session_end",
            "token": token,
            "platform": platform,
            "meeting_id": meeting_id,
            "uid": session_uid,
            "end_timestamp": now_iso,
        }
        try:
            self._client.xadd(REDIS_STREAM_KEY, {"payload": json.dumps(payload)})
            self._session_starts.discard(session_uid)
            self._last_digest.pop(session_uid, None)
            logger.info(f"Published session_end for {session_uid}")
        except Exception as exc:
            logger.error(f"Failed to publish session_end: {exc}")

    # -- transcription -------------------------------------------------------

    def publish_transcription(self, token: str, platform: str, meeting_id: str,
                              segments: list, session_uid: str):
        if not self._ensure_connected():
            return
        # Auto-publish session_start if not yet done
        if session_uid not in self._session_starts:
            self.publish_session_start(token, platform, meeting_id, session_uid)

        payload = {
            "type": "transcription",
            "token": token,
            "platform": platform,
            "meeting_id": meeting_id,
            "segments": segments,
            "uid": session_uid,
        }
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha1(payload_json.encode()).hexdigest()
        if self._last_digest.get(session_uid) == digest:
            return  # dedupe
        self._last_digest[session_uid] = digest

        try:
            self._client.xadd(REDIS_STREAM_KEY, {"payload": payload_json})
            logger.debug(f"Published transcription ({len(segments)} segs) for {session_uid}")
        except Exception as exc:
            logger.error(f"Failed to publish transcription: {exc}")

    # -- speaker events ------------------------------------------------------

    def publish_speaker_event(self, event_payload: dict):
        if not self._ensure_connected():
            return
        now_iso = datetime.datetime.utcnow().isoformat() + "Z"
        data = {**event_payload, "server_received_timestamp_iso": now_iso}
        try:
            self._client.xadd(REDIS_SPEAKER_EVENTS_KEY, data)
        except Exception as exc:
            logger.error(f"Failed to publish speaker event: {exc}")


# ---------------------------------------------------------------------------
# Per-client session (one bot WebSocket ↔ one Deepgram live stream)
# ---------------------------------------------------------------------------
class ClientSession:
    """Manages one bot connection and its paired Deepgram live stream."""

    def __init__(self, bot_ws, config: dict, redis_pub: RedisPublisher):
        self.bot_ws = bot_ws
        self.config = config
        self.redis_pub = redis_pub

        self.uid = config.get("uid") or str(uuid.uuid4())
        self.platform = config.get("platform", "unknown")
        self.meeting_url = config.get("meeting_url", "")
        self.meeting_id = config.get("meeting_id", "")
        self.token = config.get("token", "")
        self.language = config.get("language") or "en"

        # Deepgram live connection handle
        self.dg_connection = None
        # Accumulated segments to send back to bot & publish to Redis
        self._segments: list = []
        self._segment_id = 0

    async def start(self):
        """Open Deepgram live stream, send SERVER_READY to bot."""
        try:
            dg_client = DeepgramClient(DEEPGRAM_API_KEY)
            self.dg_connection = dg_client.listen.websocket.v("1")

            # Register event handlers
            self.dg_connection.on(LiveTranscriptionEvents.Transcript, self._on_transcript)
            self.dg_connection.on(LiveTranscriptionEvents.Error, self._on_error)

            options = LiveOptions(
                model="nova-2",
                language=self.language,
                encoding="linear16",
                sample_rate=SAMPLE_RATE,
                channels=1,
                interim_results=True,
                utterance_end_ms="1000",
                smart_format=True,
                punctuate=True,
            )

            if not self.dg_connection.start(options):
                logger.error(f"[{self.uid}] Failed to start Deepgram connection")
                return False

            logger.info(f"[{self.uid}] Deepgram live stream started")

            # Publish session_start to Redis
            self.redis_pub.publish_session_start(
                self.token, self.platform, self.meeting_id, self.uid
            )

            # Tell the bot we're ready (matches WhisperLive protocol)
            await self.bot_ws.send(json.dumps({
                "status": "SERVER_READY",
                "uid": self.uid,
            }))
            return True

        except Exception as exc:
            logger.error(f"[{self.uid}] Error starting Deepgram: {exc}")
            return False

    def send_audio(self, audio_bytes: bytes):
        """Forward raw linear16 audio bytes to Deepgram."""
        if self.dg_connection:
            try:
                self.dg_connection.send(audio_bytes)
            except Exception as exc:
                logger.error(f"[{self.uid}] Error sending audio to Deepgram: {exc}")

    def _on_transcript(self, _self_dg, result, **kwargs):
        """Called by Deepgram SDK when a transcript result arrives."""
        try:
            channel = result.channel
            if not channel or not channel.alternatives:
                return

            alt = channel.alternatives[0]
            text = alt.transcript
            if not text or not text.strip():
                return

            is_final = result.is_final
            speech_final = result.speech_final

            start_time = result.start if hasattr(result, "start") else 0.0
            duration = result.duration if hasattr(result, "duration") else 0.0
            end_time = start_time + duration

            segment = {
                "id": self._segment_id,
                "start": round(start_time, 3),
                "end": round(end_time, 3),
                "text": text,
                "completed": is_final,
                "language": self.language,
            }

            if is_final:
                self._segment_id += 1

            # Update or append segment
            if is_final or speech_final:
                self._segments.append(segment)
                # Keep only last 10 segments (same as WhisperLive)
                if len(self._segments) > 10:
                    self._segments = self._segments[-10:]
            else:
                # Interim result — replace last segment if it wasn't completed
                if self._segments and not self._segments[-1].get("completed"):
                    self._segments[-1] = segment
                else:
                    self._segments.append(segment)

            # Send segments back to bot (same format WhisperLive uses)
            bot_message = json.dumps({
                "uid": self.uid,
                "segments": self._segments,
            })
            # Schedule send on the event loop (SDK callback is from a thread)
            asyncio.get_event_loop().call_soon_threadsafe(
                asyncio.ensure_future,
                self._safe_send_to_bot(bot_message),
            )

            # Publish completed segments to Redis
            if is_final:
                self.redis_pub.publish_transcription(
                    self.token, self.platform, self.meeting_id,
                    self._segments, self.uid,
                )

        except Exception as exc:
            logger.error(f"[{self.uid}] Error processing Deepgram transcript: {exc}")

    async def _safe_send_to_bot(self, message: str):
        try:
            await self.bot_ws.send(message)
        except Exception:
            pass  # connection may have closed

    def _on_error(self, _self_dg, error, **kwargs):
        logger.error(f"[{self.uid}] Deepgram error: {error}")

    async def close(self):
        """Shut down Deepgram stream and publish session_end."""
        try:
            if self.dg_connection:
                self.dg_connection.finish()
                logger.info(f"[{self.uid}] Deepgram connection closed")
        except Exception as exc:
            logger.error(f"[{self.uid}] Error closing Deepgram: {exc}")

        self.redis_pub.publish_session_end(
            self.token, self.platform, self.meeting_id, self.uid
        )


# ---------------------------------------------------------------------------
# WebSocket server handler
# ---------------------------------------------------------------------------
redis_publisher = RedisPublisher(REDIS_URL)


async def handler(websocket):
    """Handle one bot WebSocket connection."""
    client_addr = websocket.remote_address
    logger.info(f"New connection from {client_addr}")

    session: Optional[ClientSession] = None

    try:
        # Step 1: Receive config message (first message is always JSON)
        raw_config = await websocket.recv()
        config = json.loads(raw_config)
        logger.info(
            f"Config received: uid={config.get('uid')}, "
            f"platform={config.get('platform')}, "
            f"meeting_id={config.get('meeting_id')}"
        )

        # Validate required fields
        required = ["uid", "platform", "meeting_url", "token", "meeting_id"]
        missing = [f for f in required if not config.get(f)]
        if missing:
            await websocket.send(json.dumps({
                "uid": config.get("uid", "unknown"),
                "status": "ERROR",
                "message": f"Missing required fields: {', '.join(missing)}",
            }))
            return

        # Step 2: Create session and start Deepgram
        session = ClientSession(websocket, config, redis_publisher)
        if not await session.start():
            await websocket.send(json.dumps({
                "uid": config.get("uid", "unknown"),
                "status": "ERROR",
                "message": "Failed to connect to Deepgram",
            }))
            return

        # Step 3: Receive audio / control messages in a loop
        async for message in websocket:
            # Binary → audio data
            if isinstance(message, bytes):
                if message == b"END_OF_AUDIO":
                    logger.info(f"[{session.uid}] Received END_OF_AUDIO")
                    break

                # Convert float32 PCM → int16 PCM for Deepgram
                try:
                    audio_f32 = np.frombuffer(message, dtype=np.float32)
                    audio_i16 = (np.clip(audio_f32, -1.0, 1.0) * 32767).astype(np.int16)
                    session.send_audio(audio_i16.tobytes())
                except Exception as exc:
                    logger.error(f"[{session.uid}] Audio conversion error: {exc}")

            # Text → JSON control message
            elif isinstance(message, str):
                try:
                    control = json.loads(message)
                    msg_type = control.get("type", "unknown")

                    if msg_type in ("speaker_activity", "speaker_activity_update"):
                        payload = control.get("payload", {})
                        if payload:
                            redis_publisher.publish_speaker_event(payload)

                    elif msg_type == "session_control":
                        payload = control.get("payload", {})
                        event = payload.get("event")
                        if event == "LEAVING_MEETING":
                            logger.info(f"[{session.uid}] Bot signalled LEAVING_MEETING")

                    elif msg_type == "audio_chunk_metadata":
                        pass  # logged at debug if needed

                    else:
                        logger.debug(f"[{session.uid}] Unknown control: {msg_type}")

                except json.JSONDecodeError:
                    logger.warning(f"[{session.uid}] Non-JSON text message received")

    except websockets.exceptions.ConnectionClosed:
        uid = session.uid if session else "unknown"
        logger.info(f"[{uid}] Connection closed by client")
    except Exception as exc:
        uid = session.uid if session else "unknown"
        logger.error(f"[{uid}] Handler error: {exc}", exc_info=True)
    finally:
        if session:
            await session.close()
        logger.info(f"Connection from {client_addr} cleaned up")


# ---------------------------------------------------------------------------
# Health check HTTP endpoint (simple inline handler)
# ---------------------------------------------------------------------------
async def health_handler(path, request_headers):
    """Respond to /health with 200 OK (used by Docker healthcheck / load-balancer)."""
    if path == "/health":
        return (200, [("Content-Type", "text/plain")], b"OK\n")
    return None  # let the WebSocket handler deal with it


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    if not DEEPGRAM_API_KEY:
        logger.error("DEEPGRAM_API_KEY environment variable is required")
        sys.exit(1)

    logger.info(f"Starting Deepgram Transcriber on port {LISTEN_PORT}")
    logger.info(f"Redis URL: {REDIS_URL}")
    logger.info(f"Deepgram API key: {DEEPGRAM_API_KEY[:4]}...{DEEPGRAM_API_KEY[-4:]}")

    async with websockets.serve(
        handler,
        "0.0.0.0",
        LISTEN_PORT,
        subprotocols=None,
        process_request=health_handler,
        max_size=10 * 1024 * 1024,  # 10 MB max message
    ):
        logger.info(f"WebSocket server listening on ws://0.0.0.0:{LISTEN_PORT}")
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
