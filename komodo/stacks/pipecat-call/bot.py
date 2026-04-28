"""
Pipecat Call Agent — AI phone caller using Gemini Live S2S + Telnyx.

Call ends when:
  - Bot finishes speaking and user is silent for 8 seconds (natural goodbye)
  - Person hangs up
  - Max duration (3 minutes)
"""

import asyncio
import os
import time
from typing import Callable

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import BotStoppedSpeakingFrame, EndTaskFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
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


def register_result_future(call_id: str, future: asyncio.Future):
    _results[call_id] = future


class BotSpeechMonitor(FrameProcessor):
    """Detects when bot starts/stops speaking and calls callbacks."""

    def __init__(self, on_bot_stopped: Callable):
        super().__init__()
        self._on_bot_stopped = on_bot_stopped

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStoppedSpeakingFrame):
            await self._on_bot_stopped()
        await self.push_frame(frame, direction)


async def run_bot(transport: BaseTransport, task: str, call_id: str, handle_sigint: bool):

    if not task:
        task = "Have a brief, friendly conversation."

    user_turn_count = [0]
    bot_turn_count = [0]
    conversation_ended = [False]
    last_user_speech_time = [time.time()]
    silence_after_bot_task = [None]

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
7. Wenn du die Aufgabe erledigt hast, verabschiede dich kurz — sage z.B. "Tschüss, bis bald!" und dann STOPPE. Das Gespräch wird automatisch enden.
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

    async def on_bot_stopped_speaking():
        """Called when bot finishes speaking. Start silence timer."""
        bot_turn_count[0] += 1
        logger.info(f"Bot stopped speaking [{call_id}] turn #{bot_turn_count[0]}, waiting for user...")
        if conversation_ended[0] or user_turn_count[0] < 1:
            return
        # Cancel previous silence task
        if silence_after_bot_task[0] and not silence_after_bot_task[0].done():
            silence_after_bot_task[0].cancel()

        async def wait_for_user():
            await asyncio.sleep(8)
            if not conversation_ended[0]:
                elapsed = time.time() - last_user_speech_time[0]
                if elapsed > 6:
                    logger.info(f"No user response after bot speech [{call_id}], ending call")
                    conversation_ended[0] = True
                    future = _results.get(call_id)
                    if future and not future.done():
                        future.set_result(f"Call ended — goodbye ({user_turn_count[0]} user, {bot_turn_count[0]} bot)")
                    await asyncio.sleep(2)
                    await task_obj.cancel()

        silence_after_bot_task[0] = asyncio.create_task(wait_for_user())

    speech_monitor = BotSpeechMonitor(on_bot_stopped=on_bot_stopped_speaking)

    pipeline = Pipeline([
        transport.input(),
        user_aggregator,
        llm,
        speech_monitor,
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
        logger.info(f"Call connected [{call_id}]")
        await task_obj.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"Call disconnected [{call_id}]")
        if not conversation_ended[0]:
            conversation_ended[0] = True
            future = _results.get(call_id)
            if future and not future.done():
                future.set_result(f"Call ended — person hung up ({user_turn_count[0]} user, {bot_turn_count[0]} bot)")
        if silence_after_bot_task[0] and not silence_after_bot_task[0].done():
            silence_after_bot_task[0].cancel()
        await task_obj.cancel()

    # Track user speaking
    original_on_user_turn = user_aggregator._on_user_turn_started

    async def tracked_user_turn(*args, **kwargs):
        user_turn_count[0] += 1
        last_user_speech_time[0] = time.time()
        logger.info(f"User turn #{user_turn_count[0]} [{call_id}]")
        # Cancel silence detector
        if silence_after_bot_task[0] and not silence_after_bot_task[0].done():
            silence_after_bot_task[0].cancel()

    # Max duration safety net
    async def max_duration_monitor():
        await asyncio.sleep(180)
        if not conversation_ended[0]:
            conversation_ended[0] = True
            future = _results.get(call_id)
            if future and not future.done():
                future.set_result(f"Call ended — max duration ({user_turn_count[0]} user, {bot_turn_count[0]} bot)")
            await task_obj.cancel()

    max_task = asyncio.create_task(max_duration_monitor())

    runner = PipelineRunner(handle_sigint=handle_sigint)
    try:
        await runner.run(task_obj)
    finally:
        max_task.cancel()
        if silence_after_bot_task[0] and not silence_after_bot_task[0].done():
            silence_after_bot_task[0].cancel()


async def bot(runner_args: WebSocketRunnerArguments):
    transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
    logger.info(f"Transport: {transport_type}, call_data keys: {list(call_data.keys())}")

    stream_id = call_data.get("stream_id", "unknown")
    call_control_id = call_data.get("call_control_id", stream_id)
    body = runner_args.body or {}
    task = body.get("task", "")
    call_id = body.get("call_id", call_control_id)

    existing = _results.get(call_id)
    if existing and not existing.done():
        result_future = existing
    else:
        result_future = asyncio.get_running_loop().create_future()
        register_result_future(call_id, result_future)

    logger.info(f"Bot started [call_id={call_id} / call_control_id={call_control_id} / stream={stream_id}]: task={task[:100]}...")

    serializer = TelnyxFrameSerializer(
        stream_id=stream_id,
        call_control_id=call_control_id,
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
    await run_bot(transport, task, call_id, handle_sigint)
