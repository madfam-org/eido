FROM nvidia/cuda:12.2.2-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y python3-pip python3-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir \
    open3d \
    trimesh \
    numpy \
    boto3

WORKDIR /app
COPY entrypoint.py .

ENV INPUT_PLY=/work/splat/point_cloud.ply
ENV OUTPUT_GLB=/work/mesh/output.glb

ENTRYPOINT ["python3", "entrypoint.py"]
