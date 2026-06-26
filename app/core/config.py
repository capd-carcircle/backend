from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # PostgreSQL — 반드시 .env에 설정 (기본값 없음)
    DATABASE_URL: str

    # JWT — SECRET_KEY는 반드시 .env에 설정 (기본값 없음, 하드코딩 금지)
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # Google Cloud
    GOOGLE_CLOUD_PROJECT: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # AI 서버 URL (로컬/Docker: http://ai:8001, GCP: Cloud Run URL)
    AI_SERVICE_URL: str = "http://ai:8001"

    class Config:
        env_file = ".env"
        extra = "allow"


settings = Settings()
