"""Audio stream constants (extracted from main.py)."""

CHANNELS = 1

SEND_SAMPLE_RATE = 16000

RECEIVE_SAMPLE_RATE = 24000

# 640 samples at 16 kHz = 40 ms, inside the Live API's recommended 20-40 ms
# chunk range (docs: live-api/best-practices). It was 1024 (64 ms): longer
# frames mean more words handed to the model per packet and a coarser
# end-of-speech boundary, which is one of the ways a short word got clipped.
# The gate, Vosk and the wake verifier all derive duration from len(frame), so
# they follow this value rather than assuming it.
CHUNK_SIZE = 640
