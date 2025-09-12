from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    d360_api_key: str
    base_url: str
    phone_number_id: str

    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_host: str
    postgres_port: str

    redis_url: str = "redis://redis:6379/0"

    class Config:
        env_file = ".env"

settings = Settings()
