"""
Pipecat Call Agent — AI phone caller using Gemini Live S2S + Telnyx.

Bot receives a task via custom body data and completes it during the call,
then reports the result via report_result() function call.
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
from pipecat.services.llm_service import FunctionCallParams

load_dotenv(override=True)

# Shared state for result reporting
# Keyed by stream_id so multiple concurrent calls are supported
_results: dict[str, asyncio.Future] = {}


def register_result_future(stream_id: str, future: asyncio.Future):
    """Register a future that will be resolved when the bot reports its result."""
    _results[stream_id] = future


async def run_bot(transport: BaseTransport, task: str, call_control_id: str, handle_sigint: bool):
    """Run the Gemini Live S2S pipeline for an outbound call."""

    if not task:
        task = "Have a brief, friendly conversation. Introduce yourself and ask how you can help."

    system_instruction = f"""You are an AI phone agent making an outbound call.

YOUR TASK:
{task}

INSTRUCTIONS:
- You are speaking over the phone. Be natural, concise, and polite.
- Speak in the language appropriate for the phone number's country (German for German numbers, English otherwise), unless the task specifies otherwise.
- Listen carefully to the other person's responses.
- When you have completed the task or gathered the needed information, call the `report_result` function with a clear summary.
- If you cannot complete the task (e.g., no answer, wrong number, closed), call `report_result` explaining what happened.
- Do NOT mention that you are an AI unless directly asked.
- Keep responses brief — phone conversations should be efficient.
"""

    # Result callback — sets the future so /start endpoint gets the result
    async def report_result_handler(params: FunctionCallParams):
        summary = params.arguments.get("summary", "No summary provided")
        logger.info(f"Call result [{call_control_id}]: {summary}")

        await params.result_callback({"status": "completed"})

        # Resolve the waiting future
        future = _results.get(call_control_id)
        if future and not future.done():
            future.set_result(summary)

        # Allow ~4 s for TTS to finish playing the goodbye before hanging up
        await asyncio.sleep(4)
        await params.llm.push_frame(EndTaskFrame())

    report_result_schema = {
        "type": "function",
        "function": {
            "name": "report_result",
            "description": "Report the final result of the phone call. Call this ONCE when the task is complete or cannot be completed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Summary of the call outcome: what was accomplished, information gathered, or why the task failed.",
                    }
                },
                "required": ["summary"],
            },
        },
    }

    llm = GeminiLiveLLMService(
        api_key=os.getenv("GOOGLE_API_KEY"),
        settings=GeminiLiveLLMService.Settings(
            model="gemini-2.5-flash-native-audio-preview-09-2025",
            voice="Charon",
            system_instruction=system_instruction,
            # Disable thinking for lower latency in phone calls
            thinking={"thinking_budget": 0},
        ),
        tools=[report_result_schema],
    )

    llm.register_function("report_result", report_result_handler)

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
        # If no result was reported, set a default
        future = _results.get(call_control_id)
        if future and not future.done():
            future.set_result("Call ended without a result (caller disconnected)")
        await task_obj.cancel()

    runner = PipelineRunner(handle_sigint=handle_sigint)
    await runner.run(task_obj)


async def bot(runner_args: WebSocketRunnerArguments):
    """Main bot entry point — called by the WebSocket handler in server.py."""
    transport_type, call_data = await parse_telephony_websocket(runner_args.websocket)
    logger.info(f"Transport: {transport_type}, call_data keys: {list(call_data.keys())}")

    stream_id = call_data.get("stream_id", "unknown")
    call_control_id = call_data.get("call_control_id", stream_id)
    body = runner_args.body or {}
    task = body.get("task", "")

    # Register the result future using call_control_id (same key as server.py /start)
    # The future may already exist if server.py created it in /start
    existing = _results.get(call_control_id)
    if existing and not existing.done():
        result_future = existing  # use the one server.py is waiting on
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
