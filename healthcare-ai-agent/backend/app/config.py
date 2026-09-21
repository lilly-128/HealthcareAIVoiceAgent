import os


class Settings:
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "postgresql+psycopg2://postgres:postgres@db:5432/healthcare"
    )
    JWT_SECRET: str = os.getenv("JWT_SECRET", "dev-secret-change-me")
    JWT_ALGO: str = "HS256"

    # External AI provider (LLM used by the LangGraph agent via Groq)
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

    # Default to a high-TPM Groq model to prevent "413 Payload Too Large / Rate Limit Exceeded"
    LLM_MODEL: str = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")

    # Allowed frontend CORS origins
    CORS_ORIGINS: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # Mock EHR
    EHR_BASE_URL: str = os.getenv("EHR_BASE_URL", "http://mock-ehr:9000")
    EHR_TIMEOUT_SECONDS: float = float(os.getenv("EHR_TIMEOUT_SECONDS", "4"))
    EHR_MAX_RETRIES: int = int(os.getenv("EHR_MAX_RETRIES", "2"))

    SLOT_MINUTES: int = int(os.getenv("SLOT_MINUTES", "30"))
    SEED_ON_START: bool = os.getenv("SEED_ON_START", "true").lower() == "true"


settings = Settings()