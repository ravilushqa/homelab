// μ-law <-> PCM conversion + resampling

export function mulawDecode(u) {
  u = ~u & 0xFF;
  const sign = u & 0x80;
  const exp = (u >> 4) & 0x07;
  const mant = u & 0x0F;
  let sample = ((mant << 3) + 0x84) << exp;
  return sign ? 0x84 - sample : sample - 0x84;
}

export function mulawEncode(sample) {
  const BIAS = 0x84;
  const sign = sample < 0 ? 0x80 : 0;
  if (sample < 0) sample = -sample;
  if (sample > 32767) sample = 32767;
  sample += BIAS;
  const exp = Math.max(0, Math.floor(Math.log2(sample / BIAS)));
  const mant = (sample >> (exp + 3)) & 0x0F;
  return ~(sign | (Math.min(exp, 7) << 4) | mant) & 0xFF;
}

// Buffer of μ-law bytes → PCM 16-bit LE Buffer (8kHz)
export function mulawBufToPcm(buf) {
  const out = Buffer.alloc(buf.length * 2);
  for (let i = 0; i < buf.length; i++) {
    out.writeInt16LE(mulawDecode(buf[i]), i * 2);
  }
  return out;
}

// PCM 16-bit LE Buffer → μ-law bytes Buffer
export function pcmBufToMulaw(buf) {
  const out = Buffer.alloc(buf.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = mulawEncode(buf.readInt16LE(i * 2));
  }
  return out;
}

// Upsample PCM 8kHz → 16kHz (linear interpolation)
export function upsample8to16(pcm8k) {
  const samples = pcm8k.length / 2;
  if (samples === 0) return Buffer.alloc(0);
  const out = Buffer.alloc(samples * 4);
  for (let i = 0; i < samples - 1; i++) {
    const s0 = pcm8k.readInt16LE(i * 2);
    const s1 = pcm8k.readInt16LE((i + 1) * 2);
    out.writeInt16LE(s0, i * 4);
    out.writeInt16LE(Math.round((s0 + s1) / 2), i * 4 + 2);
  }
  // Handle last sample
  const lastSample = pcm8k.readInt16LE((samples - 1) * 2);
  out.writeInt16LE(lastSample, (samples - 1) * 4);
  out.writeInt16LE(lastSample, (samples - 1) * 4 + 2);
  return out;
}

// Downsample PCM 16kHz → 8kHz (drop every other sample)
export function downsample16to8(pcm16k) {
  const samples = pcm16k.length / 2;
  const out = Buffer.alloc(Math.floor(samples / 2) * 2);
  for (let i = 0; i < out.length / 2; i++) {
    out.writeInt16LE(pcm16k.readInt16LE(i * 4), i * 2);
  }
  return out;
}
