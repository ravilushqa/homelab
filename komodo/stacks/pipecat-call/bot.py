"""
Pipecat Call Agent — AI phone caller using Gemini Live S2S + Telnyx.

No function calling — Gemini has a natural conversation.
Call ends via: silence timeout (30s), max duration (3min), or hangup.
Result is captured from the conversation transcript.
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
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

load_dotenv(override=True)

_results: dict[str, asyncio.Future] = {}
_last_activity: dict[str, float] = {}


def register_result_future(call_control_id: str, future: asyncio.Future):
    _results[call_control_id] = future


async def run_bot(transport: BaseTransport, task: str, call_control_id: str, handle_sigint: bool):
    """Run the Gemini Live S2S pipeline for an outbound call."""

    if not task:
        task = "Have a brief, friendly conversation."

    _last_activity[call_control_id] = time.time()
    user_turn_count = [0]
    conversation_ended = [False]

    system_instruction = f"""Du bist ein freundlicher Assistent von Ravil und rufst jemanden an.

DEINE AUFGABE:
{task}

WICHTIGSTE REGELN:
1. Begrüße die Person WARMHERZIG auf Deutsch: "Hallo! Guten Tag! Ich rufe im Namen von Ravil an. Wie geht es Ihnen?"
2. Sprich Deutsch.
3. Führe ein GESPRÄCH — stelle Fragen, höre zu, reagiere auf Antworten.
4. Erwähne NICHT dass du eine KI bist.
5. Halte Antworten kurz (2-3 Sätze).
6. WICHTIG: Wenn jemand "Hallo" sagt, antworte mit einer Begrüßung UND einer Frage. Beende das Gespräch NICHT nach der ersten Nachricht.
7. Wenn du die Aufgabe erledigt hast, verabschiede dich kurz und höre auf zu sprechen. Das Gespräch endet automatisch.
8. Versuche mindestens 3-4 Nachrichten auszutauschen bevor du dich verabschiedest.
"""

    llm = GeminiLiveLLMService(
        api_key=os.getenv("GOOGLE_API_KEY"),
        settings=GeminiLiveLLMService.Settings(
            model="gemini-2.5-flash-native-audio-preview-09-2025",
            voice="Charon",
            system_instruction=system_instruction,
        ),
        inference_on_context_initialization=True,
    )

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
        if not conversation_ended[0]:
            conversation_ended[0] = True
            future = _results.get(call_control_id)
            if future and not future.done():
                future.set_result(f"Call ended — person hung up after {user_turn_count[0]} exchanges")
        _last_activity.pop(call_control_id, None)
        await task_obj.cancel()

    async def end_call(reason: str):
        """End the call gracefully."""
        if conversation_ended[0]:
            return
        conversation_ended[0] = True
        logger.info(f"Ending call [{call_control_id}]: {reason}")

        future = _results.get(call_control_id)
        if future and not future.done():
            future.set_result(reason)

        _last_activity.pop(call_control_id, None)
        # Give a moment for final audio to play
        await asyncio.sleep(2)
        await task_obj.cancel()

    # Monitor: end after silence or max duration
    async def call_monitor():
        max_duration = 180  # 3 minutes max
        silence_timeout = 30  # 30 seconds of silence
        min_exchanges_for_silence = 2  # don't silence-end before at least 2 user turns
        start_time = time.time()

        while not conversation_ended[0]:
            await asyncio.sleep(5)

            elapsed = time.time() - start_time
            silence = time.time() - _last_activity.get(call_control_id, time.time())

            # Max duration
            if elapsed > max_duration:
                await end_call(f"Call ended — max duration ({max_duration}s) reached after {user_turn_count[0]} exchanges")
                break

            # Silence timeout (only after minimum exchanges)
            if user_turn_count[0] >= min_exchanges_for_silence and silence > silence_timeout:
                await end_call(f"Call ended — {silence_timeout}s silence after {user_turn_count[0]} exchanges")
                break

    monitor_task = asyncio.create_task(call_monitor())

    runner = PipelineRunner(handle_sigint=handle_sigint)
    try:
        await runner.run(task_obj)
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
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
