# Python 3.11: the TTS resampler uses the stdlib `audioop` module, which was
# removed from the stdlib in 3.13. Do NOT bump to 3.13.
FROM python:3.11-slim

# Supertonic runtime: libsndfile1 = audio I/O, libgomp1 for onnxruntime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the Supertonic model assets into the image so runtime needs no network.
RUN python -c "from supertonic import TTS; TTS(auto_download=True)"

COPY pytest.ini conftest.py ./
COPY app ./app
COPY tests ./tests
RUN mkdir -p /app/audio /app/data

ENV AUDIO_DIR=/app/audio
ENV DB_DIR=/app/data
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
