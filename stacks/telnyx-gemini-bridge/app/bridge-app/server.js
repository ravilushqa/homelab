import express from 'express';
import { createServer } from 'http';
import { WebSocketServer, WebSocket } from 'ws';
import fetch from 'node-fetch';
import { mulawBufToPcm, pcmBufToMulaw, upsample8to16, downsample16to8 } from './audio.js';

const {
  GEMINI_API_KEY,
  TELNYX_API_KEY,
  PUBLIC_HOST = 'bridge.ravil.space',
  PORT = 3000,
  SYSTEM_PROMPT = 'You are Claw, an AI assistant making calls on behalf of Ravil. Be concise and professional. Speak in the language the other person uses.',
} = process.env;

const app = express();
app.use(express.json());

// callControlId -> { geminiWs, telnyxWs }
const sessions = new Map();

// ── Telnyx API ──────────────────────────────────────────────────
async function telnyxAction(callControlId, action, params = {}) {
  const res = await fetch(`https://api.telnyx.com/v2/calls/${callControlId}/actions/${action}`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${TELNYX_API_KEY}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(params),
  });
  const json = await res.json();
  if (!res.ok) console.error(`[telnyx] ${action} error:`, JSON.stringify(json));
  return json;
}

// ── Gemini Live Session ─────────────────────────────────────────
function startGemini(callControlId) {
  const url = `wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=${GEMINI_API_KEY}`;
  const gws = new WebSocket(url);

  gws.on('open', () => {
    console.log(`[${callControlId}] Gemini connected`);
    gws.send(JSON.stringify({
      setup: {
        model: 'models/gemini-2.5-flash-native-audio-latest',
        generation_config: {
          response_modalities: ['AUDIO'],
          speech_config: {
            voice_config: { prebuilt_voice_config: { voice_name: 'Aoede' } },
          },
        },
        system_instruction: { parts: [{ text: SYSTEM_PROMPT }] },
      },
    }));
  });

  gws.on('message', (raw) => {
    try {
      const msg = JSON.parse(raw);
      const parts = msg.serverContent?.modelTurn?.parts ?? [];
      for (const part of parts) {
        if (part.inlineData?.mimeType?.startsWith('audio/pcm')) {
          const pcm16k = Buffer.from(part.inlineData.data, 'base64');
          const pcm8k = downsample16to8(pcm16k);
          const mulaw = pcmBufToMulaw(pcm8k);
          const session = sessions.get(callControlId);
          if (session?.telnyxWs?.readyState === WebSocket.OPEN) {
            session.telnyxWs.send(JSON.stringify({
              event: 'media',
              media: { payload: mulaw.toString('base64') },
            }));
          }
        }
      }
    } catch (e) {
      console.error(`[${callControlId}] Gemini msg error:`, e.message);
    }
  });

  gws.on('error', (e) => console.error(`[${callControlId}] Gemini error:`, e.message));
  gws.on('close', () => console.log(`[${callControlId}] Gemini closed`));

  return gws;
}

// ── Telnyx Webhook ──────────────────────────────────────────────
app.post('/webhook', async (req, res) => {
  const eventType = req.body?.data?.event_type;
  const payload = req.body?.data?.payload;
  console.log('[webhook]', eventType);

  if (eventType === 'call.initiated' && payload?.direction === 'incoming') {
    await telnyxAction(payload.call_control_id, 'answer');
  }

  if (eventType === 'call.answered') {
    const { call_control_id } = payload;
    sessions.set(call_control_id, { geminiWs: null, telnyxWs: null });
    await telnyxAction(call_control_id, 'streaming_start', {
      stream_url: `wss://${PUBLIC_HOST}/media`,
      stream_track: 'inbound_track',
    });
  }

  if (eventType === 'call.hangup') {
    const { call_control_id } = payload;
    const s = sessions.get(call_control_id);
    if (s?.geminiWs) s.geminiWs.close();
    sessions.delete(call_control_id);
  }

  res.sendStatus(200);
});

app.get('/health', (_, res) => res.json({ ok: true, sessions: sessions.size }));

// ── Telnyx Media WebSocket ──────────────────────────────────────
const server = createServer(app);
const wss = new WebSocketServer({ server, path: '/media' });

wss.on('connection', (ws) => {
  let callControlId = null;

  ws.on('message', (raw) => {
    try {
      const msg = JSON.parse(raw);

      if (msg.event === 'start') {
        callControlId = msg.start?.call_control_id;
        console.log(`[${callControlId}] Media stream started`);
        const geminiWs = startGemini(callControlId);
        const session = sessions.get(callControlId) ?? {};
        session.geminiWs = geminiWs;
        session.telnyxWs = ws;
        sessions.set(callControlId, session);
      }

      if (msg.event === 'media' && callControlId) {
        const session = sessions.get(callControlId);
        const gws = session?.geminiWs;
        if (gws?.readyState === WebSocket.OPEN) {
          const mulaw = Buffer.from(msg.media.payload, 'base64');
          const pcm8k = mulawBufToPcm(mulaw);
          const pcm16k = upsample8to16(pcm8k);
          gws.send(JSON.stringify({
            realtimeInput: {
              audio: {
                data: pcm16k.toString('base64'),
                mimeType: 'audio/pcm;rate=16000',
              },
            },
          }));
        }
      }

      if (msg.event === 'stop') {
        const session = sessions.get(callControlId);
        if (session?.geminiWs) session.geminiWs.close();
      }
    } catch (e) {
      console.error('Media msg error:', e.message);
    }
  });

  ws.on('close', () => {
    if (callControlId) {
      const s = sessions.get(callControlId);
      if (s?.geminiWs) s.geminiWs.close();
    }
  });
});

server.listen(PORT, () => console.log(`Bridge listening on :${PORT}`));
