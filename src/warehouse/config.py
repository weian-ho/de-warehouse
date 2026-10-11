"""Load settings from .env (secrets) and config.yaml (everything else).

Precedence, highest first:
    1. real environment variables   (CI, or `export POSTGRES_PORT=...`)
    2. .env file                    (your local secrets, never committed)
    3. config.yaml                  (non-secret defaults, committed)
    4. defaults written below
"""

from pydantic import BaseModel, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)
##we got the http settings in the config.yaml
class HttpSettings(BaseModel):
    connect_timeout_s: float = 3.05
    read_timeout_s: float =10.0
    max_attempts: int = 5

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        yaml_file="config.yaml",
        extra="ignore", # .env also holds PGADMIN_* for docker-compose; ignore them(email,password)
    )

        # --- from .env (the same variables docker-compose already uses) ---
    postgres_user: str
    postgres_password: SecretStr
    postgres_db: str
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    # --- from config.yaml ---
    symbols: list[str] = ["BTCUSDT"]
    base_url: str = "https://data-api.binance.vision"
    page_limit: int = 1000
    log_level: str = "INFO"
    http: HttpSettings = HttpSettings()

    @property
    def dsn(self) -> SecretStr:
        """Postgres connection string, kept masked until .get_secret_value()."""
        return SecretStr(
            f"postgresql://{self.postgres_user}:"
            f"{self.postgres_password.get_secret_value()}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
    @classmethod ##Tell Pydantic which configuration sources to use, and in which priority order.
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Order = priority. Earlier sources win.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
        )


def get_settings() -> Settings:
    return Settings()

