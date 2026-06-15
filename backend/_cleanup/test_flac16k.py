import time, os, numpy as np, soundfile as sf
from scipy.signal import resample_poly
from piper.voice import PiperVoice

MODEL = 'models/en_US-lessac-medium.onnx'
CONFIG = 'models/en_US-lessac-medium.onnx.json'
TARGET_SR = 16000

voice = PiperVoice.load(MODEL, config_path=CONFIG)

test_text = "1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100!"

t0 = time.perf_counter()
chunks = []
sample_rate = None
for chunk in voice.synthesize(test_text):
    chunks.append(chunk.audio_float_array)
    sample_rate = chunk.sample_rate
audio = np.concatenate(chunks)
native_duration = len(audio) / sample_rate
print(f'Generated {native_duration:.2f}s audio at {sample_rate}Hz in {time.perf_counter()-t0:.3f}s')

# Downsample
if sample_rate != TARGET_SR:
    gcd = np.gcd(sample_rate, TARGET_SR)
    audio = resample_poly(audio, TARGET_SR // gcd, sample_rate // gcd)
    print(f'Downsampled to {TARGET_SR}Hz')

duration = len(audio) / TARGET_SR
print(f'Final: {duration:.2f}s at {TARGET_SR}Hz')

# FLAC
sf.write('test_piper_16k.flac', audio, TARGET_SR, format='FLAC')
size = os.path.getsize('test_piper_16k.flac')
print(f'FLAC size: {size} bytes ({size/1024:.1f}KB)')
wav_size = int(duration * TARGET_SR * 2)
print(f'WAV would be: {wav_size} bytes ({wav_size/1024/1024:.1f}MB)')
print(f'Compression: {(1-size/wav_size)*100:.0f}% smaller')