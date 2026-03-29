# Application
APP_NAME=ai-chip-design-platform
APP_ENV=development
APP_VERSION=0.1.0
DEBUG=true
SECRET_KEY=your-secret-key-here

# API
API_HOST=0.0.0.0
API_PORT=8000
API_WORKERS=4

# PostgreSQL
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=chip_design_user
POSTGRES_PASSWORD=secure_password_here
POSTGRES_DB=chip_design_db

# MongoDB
MONGO_HOST=localhost
MONGO_PORT=27017
MONGO_USER=chip_design_user
MONGO_PASSWORD=secure_password_here
MONGO_DB=chip_design_artifacts

# TimescaleDB
TIMESCALE_HOST=localhost
TIMESCALE_PORT=5433
TIMESCALE_USER=chip_design_user
TIMESCALE_PASSWORD=secure_password_here
TIMESCALE_DB=chip_design_metrics

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=
REDIS_DB=0

# Celery
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1

# S3 / MinIO
S3_ENDPOINT=http://localhost:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET=chip-design-artifacts

# LLM Services
OPENAI_API_KEY=your-openai-key
ANTHROPIC_API_KEY=your-anthropic-key
# Or for local LLM
LLM_MODEL_PATH=/models/qwen2.5-coder-14b
LLM_API_BASE=http://localhost:8001/v1

# EDA Tools
VERILATOR_PATH=/usr/bin/verilator
YOSYS_PATH=/usr/bin/yosys
OPENROAD_PATH=/usr/bin/openroad

# Logging
LOG_LEVEL=INFO
LOG_FILE=logs/app.log
