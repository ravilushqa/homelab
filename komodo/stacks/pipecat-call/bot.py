"""
Pipecat Call Agent — AI phone caller using Gemini Live S2S + Telnyx.

Bot receives a task via custom body data and has a conversation.
Result is captured from the conversation transcript after the call ends.
"""

import asyncio
import os

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

# Shared state for result reporting
# Keyed by call_control_id so multiple concurrent calls are supported
_results: dict[str, asyncio.Future] = {}

# Track conversation transcripts
_transcripts: dict[str, list[str]] = {}


def register_result_future(call_control_id: str, future: asyncio.Future):
    """Register a future that will be resolved when the call ends."""
    _results[call_control_id] = future


async def run_bot(transport: BaseTransport, task: str, call_control_id: str, handle_sigint: bool):
    """Run the Gemini Live S2S pipeline for an outbound call."""

    if not task:
        task = "Have a brief, friendly conversation. Introduce yourself and ask how you can help."

    # Track conversation for result extraction
    transcript = []
    _transcripts[call_control_id] = transcript

    system_instruction = f"""You are a friendly AI phone assistant making an outbound call on behalf of Ravil.

YOUR TASK:
{task}

RULES:
1. Start by greeting warmly in German: "Hallo! Guten Tag, ich rufe im Namen von Ravil an."
2. Speak German unless the person prefers another language.
3. Be warm, natural, and conversational — this is a real phone call.
4. Have a genuine conversation — ask questions, listen, respond.
5. When the task is complete, say goodbye warmly and let the call end naturally.
6. Do NOT mention you are an AI unless directly asked.
7. Keep responses concise — phone conversations should be efficient.
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
            idle_timeout_secs=300,  # 5 min max idle
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"Call connected [{call_control_id}] — starting conversation")
        await task_obj.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"Call disconnected [{call_control_id}]")
        # Build result from transcript
        result = "; ".join(transcript) if transcript else "Call ended without conversation"
        logger.info(f"Call transcript [{call_control_id}]: {result}")

        future = _results.get(call_control_id)
        if future and not future.done():
            future.set_result(result)

        # Cleanup
        _transcripts.pop(call_control_id, None)
        await task_obj.cancel()

    # Timeout: after 2 minutes, end the call and report
    async def call_timeout():
        await asyncio.sleep(120)
        result = "; ".join(transcript) if transcript else "Call timed out after 2 minutes"
        logger.info(f"Call timeout [{call_control_id}]: {result}")

        future = _results.get(call_control_id)
        if future and not future.done():
            future.set_result(result)

        _transcripts.pop(call_control_id, None)
        await task_obj.cancel()

    timeout_task = asyncio.create_task(call_timeout())

    runner = PipelineRunner(handle_sigint=handle_sigint)
    try:
        await runner.run(task_obj)
    finally:
        timeout_task.cancel()
        try:
            await timeout_task
        except asyncio.CancelledError:
            pass


async def bot(runner_args: WebSocketRunnerArguments):
    """Main bot entry point — called by the WebSocket handler in server.py."""
    transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
    logger.info(f"Transport: {transport_type}, call_data keys: {list(call_data.keys())}")

    stream_id = call_data.get("stream_id", "unknown")
    call_control_id = call_data.get("call_control_id", stream_id)
    body = runner_args.body or {}
    task = body.get("task", "")

    # Register the result future using call_control_id (same key as server.py /start)
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
