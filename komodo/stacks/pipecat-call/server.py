"""
Pipecat Call Agent — FastAPI server for outbound phone calls via Telnyx.

Endpoints:
  POST /start    — initiate outbound call, blocks until result
  POST /answer   — TeXML webhook (called by Telnyx when call is answered)
  POST /status   — Telnyx status callback
  WS   /ws       — media streaming WebSocket (called by Telnyx)
  GET  /health   — health check
"""

import asyncio
import base64
import json
import os
from contextlib import asynccontextmanager

import aiohttp
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from loguru import logger

load_dotenv(override=True)

# ─── Configuration ───────────────────────────────────────────────────────────

TELNYX_API_KEY = os.getenv("TELNYX_API_KEY", "")
TELNYX_ACCOUNT_SID = os.getenv("TELNYX_ACCOUNT_SID", "")
TELNYX_APPLICATION_SID = os.getenv("TELNYX_APPLICATION_SID", "2948190653835642016")
TELNYX_PHONE_NUMBER = os.getenv("TELNYX_PHONE_NUMBER", "")

API_KEY = os.getenv("API_KEY", "")

REQUIRED_ENV = {
    "TELNYX_API_KEY": TELNYX_API_KEY,
    "TELNYX_ACCOUNT_SID": TELNYX_ACCOUNT_SID,
    "TELNYX_APPLICATION_SID": TELNYX_APPLICATION_SID,
    "GOOGLE_API_KEY": os.getenv("GOOGLE_API_KEY", ""),
    "API_KEY": API_KEY,
}

missing = [k for k, v in REQUIRED_ENV.items() if not v]
if missing:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

WS_URL = "wss://call.ravil.space/ws"


# ─── Telnyx API helpers ─────────────────────────────────────────────────────

async def make_telnyx_call(
    session: aiohttp.ClientSession,
    to_number: str,
    from_number: str,
    texml_url: str,
) -> dict:
    """Make an outbound call via Telnyx TeXML API."""
    url = f"https://api.telnyx.com/v2/texml/Accounts/{TELNYX_ACCOUNT_SID}/Calls"
    headers = {
        "Authorization": f"Bearer {TELNYX_API_KEY}",
        "Content-Type": "application/json",
    }
    data = {
        "To": to_number,
        "From": from_number,
        "Url": texml_url,
        "ApplicationSid": TELNYX_APPLICATION_SID,
    }

    async with session.post(url, headers=headers, json=data) as resp:
        result = await resp.json()
        if resp.status >= 400:
            logger.error(f"Telnyx API error: {resp.status} — {result}")
            raise Exception(f"Telnyx call failed: {result}")
        return result


# ─── Lifespan ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.session = aiohttp.ClientSession()
    logger.info("Pipecat Call Agent server started")
    yield
    await app.state.session.close()
    logger.info("Server shut down")


# ─── App ─────────────────────────────────────────────────────────────────────

app = FastAPI(title="Pipecat Call Agent", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Health ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "pipecat-call-agent"}


# ─── Outbound call initiation ───────────────────────────────────────────────

@app.post("/start")
async def start_call(request: Request, x_api_key: str = Header(None)):
    """
    Initiate an outbound call.

    Request body:
      {
        "phone_number": "+491234567890",  (required)
        "task": "Забронировать столик на 2 в 20:00",  (required)
        "language": "de"  (optional)
      }

    Blocks until the call completes and returns the result.
    Timeout: 5 minutes.
    """
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    phone_number = body.get("phone_number")
    task = body.get("task", "")

    if not phone_number:
        return JSONResponse({"error": "phone_number is required"}, status_code=400)
    if not task:
        return JSONResponse({"error": "task is required"}, status_code=400)

    # Create a future for the result
    result_future = asyncio.get_running_loop().create_future()

    # Build custom data to pass through the call
    custom_data = {"task": task}
    body_b64 = base64.urlsafe_b64encode(json.dumps(custom_data).encode()).decode()

    texml_url = f"https://call.ravil.space/answer?body={body_b64}"

    logger.info(f"Initiating call to {phone_number}: {task[:80]}...")

    try:
        result = await make_telnyx_call(
            app.state.session,
            to_number=phone_number,
            from_number=TELNYX_PHONE_NUMBER,
            texml_url=texml_url,
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    call_control_id = result.get("data", {}).get("call_control_id") or result.get("call_control_id", "unknown")
    logger.info(f"Call initiated: {call_control_id} (response: {json.dumps(result)[:300]})")

    # Register the future — bot.py will resolve it via report_result()
    # The WebSocket handler will use call_control_id as key
    from bot import _results as _bot_results
    _bot_results[call_control_id] = result_future

    # Wait for the result (timeout 5 minutes)
    try:
        call_result = await asyncio.wait_for(result_future, timeout=300)
        return JSONResponse({
            "status": "completed",
            "call_control_id": call_control_id,
            "result": call_result,
        })
    except asyncio.TimeoutError:
        return JSONResponse({
            "status": "timeout",
            "call_control_id": call_control_id,
            "result": "Call timed out after 5 minutes",
        })
    finally:
        _bot_results.pop(call_control_id, None)


# ─── TeXML webhook (Telnyx calls this when call is answered) ────────────────

@app.post("/answer")
async def answer(request: Request):
    """Return TeXML to connect the call to our WebSocket."""
    params = await request.form()
    body_param = params.get("body", "")

    ws_url = WS_URL
    if body_param:
        ws_url = f"{ws_url}?body={body_param}"

    texml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws_url}" bidirectionalMode="rtp"></Stream>
  </Connect>
  <Pause length="40"/>
</Response>"""

    return PlainTextResponse(texml, media_type="text/xml")


# ─── Status callback ────────────────────────────────────────────────────────

@app.post("/status")
async def status(request: Request):
    """Telnyx status callback — log call events."""
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        body = await request.json()
    else:
        try:
            body = await request.json()
        except Exception:
            raw = await request.body()
            logger.warning(f"/status: non-JSON body: {raw[:200]}")
            return JSONResponse({"status": "ok"})

    event_type = body.get("data", {}).get("event_type", "unknown")
    call_control_id = body.get("data", {}).get("payload", {}).get("call_control_id", "unknown")
    logger.info(f"Call status: {event_type} [{call_control_id}]")

    # If call ended without connecting, resolve the future with an error
    if event_type in ("call.hangup", "call.speak.ended"):
        from bot import _results
        future = _results.get(call_control_id)
        if future and not future.done():
            future.set_result(f"Call ended: {event_type}")

    return JSONResponse({"status": "ok"})


# ─── WebSocket handler (Telnyx media stream) ────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    body_encoded: str = Query(None, alias="body"),
    serviceHost: str = Query(None),
):
    """Handle Telnyx media stream WebSocket connection."""
    await websocket.accept()

    # Import and run the bot
    from bot import bot
    from pipecat.runner.types import WebSocketRunnerArguments

    # Decode body
    body_data = {}
    if body_encoded:
        try:
            # Pad to a multiple of 4 before decoding
            padded = body_encoded + "=" * (4 - len(body_encoded) % 4)
            body_data = json.loads(base64.urlsafe_b64decode(padded).decode())
        except Exception:
            try:
                body_data = json.loads(body_encoded)
            except Exception:
                logger.warning(f"Could not decode body: {body_encoded[:100]}")

    logger.info(f"WebSocket connected, body: {body_data}")

    runner_args = WebSocketRunnerArguments(
        websocket=websocket,
        body=body_data,
    )

    try:
        await bot(runner_args)
    except Exception as e:
        logger.error(f"Bot error: {e}")
    finally:
        logger.info("WebSocket session ended")


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)
