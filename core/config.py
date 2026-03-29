"""
Configuration management for AI Chip Design Platform
"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""
    
    # Application
    app_name: str = "ai-chip-design-platform"
    app_env: str = "development"
    app_version: str = "0.1.0"
    debug: bool = True
    secret_key: str
    
    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_workers: int = 4
    
    # PostgreSQL
    postgres_host: str
    postgres_port: int = 5432
    postgres_user: str
    postgres_password: str
    postgres_db: str
    
    # MongoDB
    mongo_host: str
    mongo_port: int = 27017
    mongo_user: str
    mongo_password: str
    mongo_db: str
    
    # TimescaleDB
    timescale_host: str
    timescale_port: int = 5433
    timescale_user: str
    timescale_password: str
    timescale_db: str
    
    # Redis
    redis_host: str
    redis_port: int = 6379
    redis_password: Optional[str] = None
    redis_db: int = 0
    
    # Celery
    celery_broker_url: str
    celery_result_backend: str
    
    # S3/MinIO
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str
    
    # LLM
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    llm_model_path: Optional[str] = None
    llm_api_base: Optional[str] = None
    
    # EDA Tools
    verilator_path: str = "/usr/bin/verilator"
    yosys_path: str = "/usr/bin/yosys"
    openroad_path: str = "/usr/bin/openroad"
    
    # Logging
    log_level: str = "INFO"
    log_file: str = "logs/app.log"
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False
    )
    
    @property
    def postgres_url(self) -> str:
        """Build PostgreSQL connection URL"""
        return f"postgresql://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
    
    @property
    def mongo_url(self) -> str:
        """Build MongoDB connection URL"""
        return f"mongodb://{self.mongo_user}:{self.mongo_password}@{self.mongo_host}:{self.mongo_port}"
    
    @property
    def timescale_url(self) -> str:
        """Build TimescaleDB connection URL"""
        return f"postgresql://{self.timescale_user}:{self.timescale_password}@{self.timescale_host}:{self.timescale_port}/{self.timescale_db}"
    
    @property
    def redis_url(self) -> str:
        """Build Redis connection URL"""
        if self.redis_password:
            return f"redis://:{self.redis_password}@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()
