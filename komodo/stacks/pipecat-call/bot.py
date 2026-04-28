"""
Pipecat Call Agent — AI phone caller using Gemini Live S2S + Telnyx.

Bot receives a task via custom body data and has a conversation.
Uses a single "goodbye" function call so the bot can end the call gracefully.
Also auto-ends after prolonged silence.
"""

import asyncio
import os
import time

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndTaskFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import WebSocketRunnerArguments
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.telnyx import TelnyxFrameSerializer
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.tools_schema import FunctionSchema, ToolsSchema
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

load_dotenv(override=True)

# Shared state for result reporting
_results: dict[str, asyncio.Future] = {}

# Silence tracking
_last_activity: dict[str, float] = {}


def register_result_future(call_control_id: str, future: asyncio.Future):
    _results[call_control_id] = future


async def run_bot(transport: BaseTransport, task: str, call_control_id: str, handle_sigint: bool):
    """Run the Gemini Live S2S pipeline for an outbound call."""

    if not task:
        task = "Have a brief, friendly conversation."

    _last_activity[call_control_id] = time.time()
    min_exchanges = 2  # at least this many user turns before allowing goodbye
    user_turn_count = [0]  # mutable counter
    bot_ended = [False]  # flag to prevent double-ending

    system_instruction = f"""Du bist ein freundlicher KI-Telefonassistent, der im Namen von Ravil anruft.

DEINE AUFGABE:
{task}

REGELN:
1. Begrüße die Person herzlich auf Deutsch: "Hallo! Guten Tag, ich rufe im Namen von Ravil an."
2. Sprich Deutsch, es sei denn, die Person bevorzugt eine andere Sprache.
3. Sei natürlich, warmherzig und gesprächig — dies ist ein echtes Telefonat.
4. Führe ein echtes Gespräch — stelle Fragen, höre zu, antworte.
5. Erwähne nicht, dass du eine KI bist, außer du wirst direkt gefragt.
6. Halte Antworten kurz — Telefongespräche sollten effizient sein.
7. Wenn du die Aufgabe erledigt hast oder das Gespräch sich natürlich dem Ende zuneigt, verabschiede dich herzlich und rufe die Funktion "goodbye" auf.
8. Rufe "goodbye" auf, wenn:
   - Die Aufgabe abgeschlossen ist
   - Die Person sich verabschiedet oder auflegt
   - Es nach mehreren Austauschen keine weiteren Fragen gibt
   - Du mindestens 2-3 Austausche hatten hast
"""

    # Goodbye function — the ONLY way for the bot to end the call
    async def goodbye_handler(params: FunctionCallParams):
        summary = params.arguments.get("summary", "Gespräch beendet")
        logger.info(f"Goodbye [{call_control_id}]: {summary}")

        await params.result_callback({"status": "goodbye"})

        # Resolve the future
        future = _results.get(call_control_id)
        if future and not future.done():
            future.set_result(summary)

        bot_ended[0] = True
        # Let goodbye TTS play out, then hang up
        await asyncio.sleep(5)
        await params.llm.push_frame(EndTaskFrame())

    goodbye_tool = FunctionSchema(
        name="goodbye",
        description="Verabschiede dich herzlich und beende das Gespräch. Rufe dies auf, wenn die Aufgabe erledigt ist oder das Gespräch zu Ende geht.",
        properties={
            "summary": {
                "type": "string",
                "description": "Kurze Zusammenfassung des Gesprächs: Was wurde besprochen, welche Informationen wurden gesammelt.",
            }
        },
        required=["summary"],
    )

    tools = ToolsSchema(standard_tools=[goodbye_tool])

    llm = GeminiLiveLLMService(
        api_key=os.getenv("GOOGLE_API_KEY"),
        settings=GeminiLiveLLMService.Settings(
            model="gemini-2.5-flash-native-audio-preview-09-2025",
            voice="Charon",
            system_instruction=system_instruction,
        ),
        tools=tools,
        inference_on_context_initialization=True,
    )

    llm.register_function("goodbye", goodbye_handler)

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    pipeline = Pipeline([
        transport.input(),
        user_aggregator,
        llm,
        transport.output(),
        assistant_aggregator,
    ])

    task_obj = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=8000,
            audio_out_sample_rate=8000,
            enable_metrics=True,
            enable_usage_metrics=True,
            idle_timeout_secs=300,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Call connected [{call_control_id}]")
        await task_obj.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"Call disconnected [{call_control_id}]")
        future = _results.get(call_control_id)
        if future and not future.done():
            future.set_result("Call ended — person hung up")
        _last_activity.pop(call_control_id, None)
        await task_obj.cancel()

    # Silence detector: if no activity for 30s after min exchanges, end gracefully
    async def silence_monitor():
        while True:
            await asyncio.sleep(10)
            if bot_ended[0]:
                break
            elapsed = time.time() - _last_activity.get(call_control_id, time.time())
            if user_turn_count[0] >= min_exchanges and elapsed > 30:
                logger.info(f"Silence timeout [{call_control_id}] after {user_turn_count[0]} exchanges")
                future = _results.get(call_control_id)
                if future and not future.done():
                    future.set_result(f"Call ended after {user_turn_count[0]} exchanges (silence timeout)")
                _last_activity.pop(call_control_id, None)
                await task_obj.cancel()
                break

    # Track user activity from aggregator
    original_on_user_turn_started = user_aggregator._on_user_turn_started

    async def tracked_on_user_turn_started(*args, **kwargs):
        user_turn_count[0] += 1
        _last_activity[call_control_id] = time.time()
        logger.info(f"User turn #{user_turn_count[0]} [{call_control_id}]")
        if hasattr(original_on_user_turn_started, '__wrapped__'):
            return await original_on_user_turn_started(*args, **kwargs)

    # Also track assistant (bot) speaking as activity
    original_on_bot_started = assistant_aggregator._on_bot_started if hasattr(assistant_aggregator, '_on_bot_started') else None

    silence_task = asyncio.create_task(silence_monitor())

    runner = PipelineRunner(handle_sigint=handle_sigint)
    try:
        await runner.run(task_obj)
    finally:
        silence_task.cancel()
        try:
            await silence_task
        except asyncio.CancelledError:
            pass
        _last_activity.pop(call_control_id, None)


async def bot(runner_args: WebSocketRunnerArguments):
    """Main bot entry point."""
    transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
    logger.info(f"Transport: {transport_type}, call_data keys: {list(call_data.keys())}")

    stream_id = call_data.get("stream_id", "unknown")
    call_control_id = call_data.get("call_control_id", stream_id)
    body = runner_args.body or {}
    task = body.get("task", "")

    existing = _results.get(call_control_id)
    if existing and not existing.done():
        result_future = existing
    else:
        result_future = asyncio.get_running_loop().create_future()
        register_result_future(call_control_id, result_future)

    logger.info(f"Bot started [{call_control_id} / stream={stream_id}]: task={task[:100]}...")

    serializer = TelnyxFrameSerializer(
        stream_id=stream_id,
        call_control_id=call_data.get("call_control_id", ""),
        api_key=os.getenv("TELNYX_API_KEY"),
        outbound_encoding=call_data.get("outbound_encoding", "PCMU"),
        inbound_encoding=call_data.get("inbound_encoding", "PCMU"),
    )

    transport = FastAPIWebsocketTransport(
        websocket=runner_args.websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    handle_sigint = getattr(runner_args, 'handle_sigint', False)
    await run_bot(transport, task, call_control_id, handle_sigint)
