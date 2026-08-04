# Media-prep — pipeline stage 0. CPU only: no CUDA base, no GPU request.
# Deliberately the one stage that can run on the existing cluster, so a capture
# gets normalized and georeferenced even while the GPU reconstruction path is
# still blocked on capacity.
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# ffmpeg decodes uploaded video to frames; libheif lets Pillow read iPhone HEIC.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libheif1 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir boto3 "pillow>=10.3.0" pillow-heif

WORKDIR /app
COPY geo.py entrypoint.py ./

ENV OUTPUT_DIR=/work
ENV DOWNLOAD_DIR=/tmp/media-prep
ENV S3_ENDPOINT=http://minio:9000
ENV S3_BUCKET=eido-raw
ENV S3_PREFIX=raw/
ENV FRAME_INTERVAL_S=0.5
ENV MAX_FRAMES=600
ENV MIN_IMAGES=3

ENTRYPOINT ["python3", "entrypoint.py"]
