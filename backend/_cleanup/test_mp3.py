from pydub import AudioSegment
import numpy as np
import os

# Test MP3 export
audio = np.sin(2 * np.pi * 440 * np.linspace(0, 1, 22050)).astype(np.float32)
seg = AudioSegment(audio.tobytes(), frame_rate=22050, sample_width=2, channels=1)
seg.export('test.mp3', format='mp3', bitrate='64k')
print(f'MP3 size: {os.path.getsize("test.mp3")} bytes')

# Test with longer audio (simulate count-to-100)
long_audio = np.sin(2 * np.pi * 440 * np.linspace(0, 85, 85 * 22050)).astype(np.float32)
seg2 = AudioSegment(long_audio.tobytes(), frame_rate=22050, sample_width=2, channels=1)
seg2.export('test_long.mp3', format='mp3', bitrate='64k')
print(f'Long MP3 (85s) size: {os.path.getsize("test_long.mp3")} bytes')
print(f'WAV would be: {85 * 22050 * 2} bytes')