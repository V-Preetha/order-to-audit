from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables or a local .env file.

    The defaults are dummy, local-development-only values. Real deployments must override
    them through the environment; nothing secret is committed.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://app:app@localhost:5432/order_to_audit"
    jwt_secret: str = "dev-only-not-a-secret"
    app_env: str = "dev"
