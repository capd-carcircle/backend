from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # PostgreSQL
    DATABASE_URL: str = "postgresql://capd_user:capd_pass@db:5432/capd"

    # JWT
    SECRET_KEY: str = "dev-secret-key-change-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # Google Cloud
    GOOGLE_CLOUD_PROJECT: str = ""
    GEMINI_MODEL: str = "gemini-1.5-pro"

    class Config:
        env_file = ".env"
        extra = "allow"


settings = Settings()
