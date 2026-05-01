FROM nvidia/cuda:12.2.2-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    python3-pip python3-dev git cmake ninja-build \
    libglfw3-dev libgl1-mesa-dev libglu1-mesa-dev \
    && rm -rf /var/lib/apt/lists/*

# Install gsplat (nerfstudio's 3DGS implementation)
RUN pip3 install --no-cache-dir \
    torch torchvision --index-url https://download.pytorch.org/whl/cu122 \
    gsplat \
    boto3 \
    pillow \
    open3d

WORKDIR /app
COPY entrypoint.py .

ENV ITERATIONS=30000
ENV INPUT_DIR=/work/sparse
ENV OUTPUT_DIR=/work/splat

ENTRYPOINT ["python3", "entrypoint.py"]
