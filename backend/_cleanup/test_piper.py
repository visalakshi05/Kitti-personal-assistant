import time, os, wave, numpy as np
from piper.voice import PiperVoice

MODEL = 'models/en_US-lessac-medium.onnx'
CONFIG = 'models/en_US-lessac-medium.onnx.json'

print('Loading Piper voice...')
t0 = time.perf_counter()
voice = PiperVoice.load(MODEL, config_path=CONFIG)
print(f'Loaded in {time.perf_counter()-t0:.2f}s')

print('Generating audio...')
t0 = time.perf_counter()
audio_chunks = []
sample_rate = None
for chunk in voice.synthesize('Hello, I am Kitti, your personal assistant.'):
    audio_chunks.append(chunk.audio_float_array)
    sample_rate = chunk.sample_rate
    print(f'  chunk: {len(chunk.audio_float_array)} samples at {chunk.sample_rate}Hz')

audio = np.concatenate(audio_chunks)
elapsed = time.perf_counter() - t0
duration = len(audio) / sample_rate
print(f'Generated {duration:.2f}s audio in {elapsed:.3f}s (RTF: {elapsed/duration:.2f})')

# Save as WAV
with wave.open('test_piper.wav', 'wb') as f:
    f.setnchannels(1)
    f.setsampwidth(2)
    f.setframerate(sample_rate)
    f.writeframes(audio.astype(np.int16).tobytes())
size = os.path.getsize('test_piper.wav')
print(f'Saved to test_piper.wav ({size} bytes, {size/1024:.1f}KB)')

audio = np.concatenate(audio_chunks)
elapsed = time.perf_counter() - t0
duration = len(audio) / chunk.sample_rate
print(f'Generated {duration:.2f}s audio in {elapsed:.3f}s (RTF: {elapsed/duration:.2f})')

# Save as WAV
with wave.open('test_piper.wav', 'wb') as f:
    f.setnchannels(1)
    f.setsampwidth(2)
    f.setframerate(chunk.sample_rate)
    f.writeframes(audio.tobytes())
size = os.path.getsize('test_piper.wav')
print(f'Saved to test_piper.wav ({size} bytes, {size/1024:.1f}KB)')