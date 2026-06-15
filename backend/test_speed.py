import wave, os, time
from faster_whisper import WhisperModel

# Check audio
fname = 'received_audio/audio_185910.wav'
with wave.open(fname, 'rb') as f:
    dur = f.getnframes() / f.getframerate()
    print(f'Audio duration: {dur:.2f}s, rate={f.getframerate()}Hz')

# Test small model
print('\nLoading small model...')
t0 = time.perf_counter()
model = WhisperModel('small', device='cpu', compute_type='int8')
print(f'Loaded in {time.perf_counter()-t0:.2f}s')

print('Transcribing...')
t0 = time.perf_counter()
segments, info = model.transcribe(fname, beam_size=3, language='en')
st = time.perf_counter() - t0
text = ' '.join(s.text.strip() for s in segments if s.text.strip())
print(f'small: {st:.3f}s | text: {text[:80]}')

# Compare large-v3-turbo
print('\nLoading large-v3-turbo model...')
t0 = time.perf_counter()
model2 = WhisperModel('large-v3-turbo', device='cpu', compute_type='int8')
print(f'Loaded in {time.perf_counter()-t0:.2f}s')

print('Transcribing...')
t0 = time.perf_counter()
segments2, info2 = model2.transcribe(fname, beam_size=3, language='en')
st2 = time.perf_counter() - t0
text2 = ' '.join(s.text.strip() for s in segments2 if s.text.strip())
print(f'large-v3-turbo: {st2:.3f}s | text: {text2[:80]}')
print(f'\nSpeedup: {st2/st:.1f}x faster with small')