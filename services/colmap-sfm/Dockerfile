FROM nvidia/cuda:12.2.2-base-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    python3-pip cmake git wget \
    libgoogle-glog-dev libgflags-dev \
    libatlas-base-dev libeigen3-dev \
    libsuitesparse-dev libfreeimage-dev \
    libmetis-dev libboost-all-dev \
    libcgal-dev libflann-dev \
    colmap \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir boto3 numpy pillow

WORKDIR /app
COPY entrypoint.py .

ENV INPUT_DIR=/work/images
ENV OUTPUT_DIR=/work/sparse
ENV S3_ENDPOINT=http://minio:9000
ENV S3_BUCKET=eido-raw
ENV S3_PREFIX=raw/

ENTRYPOINT ["python3", "entrypoint.py"]
