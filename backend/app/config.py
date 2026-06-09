"""Application configuration."""

from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    app_name: str = "lmWorld"
    debug: bool = False

    # Database
    database_url: str = "sqlite+aiosqlite:///./lmworld.db"

    # Ollama
    ollama_gpu_host: str = "http://localhost:11434"
    ollama_cpu_host: str = "http://localhost:11435"

    # HuggingFace
    hf_token: str = ""
    models_dir: str = "../models"

    # Resource limits
    max_ram_usage_percent: float = 80.0
    max_vram_usage_percent: float = 90.0

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @property
    def models_path(self) -> Path:
        return Path(self.models_dir).resolve()

    @property
    def root_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent


settings = Settings()
