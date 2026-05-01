"""Eido API configuration."""
import os
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_version: str = "0.1.0"
    debug: bool = os.getenv("EIDO_ENV", "development") != "production"
    cors_origins: list[str] = ["http://localhost:3000", "https://eido.cam"]

    database_url: str = "postgresql+asyncpg://eido:eido@localhost:5432/eido"
    redis_url: str = "redis://localhost:6379"

    s3_endpoint: str = "http://localhost:9000"
    s3_access_key_id: str = "eido"
    s3_secret_access_key: str = "eido1234"
    s3_bucket_raw: str = "eido-raw"
    s3_bucket_cdn: str = "eido-cdn"
    s3_region: str = "us-east-1"
    cdn_base_url: str = "http://localhost:9000/eido-cdn"

    janua_url: str = "http://localhost:8080"

    # Ecosystem handoff URLs
    blueprint_harvester_url: str = "http://blueprint-harvester-api:8000"
    yantra4d_url: str = "http://yantra4d-api:8000"
    factlas_url: str = "http://factlas-api:8000"
    ceq_url: str = "http://ceq-api:8000"
    selva_url: str = "http://autoswarm-api:8000"   # LLM inference router (Selva)
    dhanam_url: str = "http://dhanam-api:8000"      # Billing + entitlements

    # GPU orchestration
    gpu_provider: str = "vast"
    vast_api_key: str = ""
    gpu_min_vram_gb: float = 24.0
    gpu_max_hourly_spend: float = 8.0
    gpu_worker_image: str = "eido/worker:latest"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
