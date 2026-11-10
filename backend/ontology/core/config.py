from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ONTOLOGY_", env_file=".env", extra="ignore")
    database_url: str = "sqlite:///var/ontology.db"
    data_dir: Path = Path("var")
    allowed_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    session_hours: int = 12
    login_tenant_slug: str | None = None
    secure_cookies: bool = False
    environment: str = "development"
    encryption_key: str = ""
    allowed_source_hosts: list[str] = []
    allow_private_source_addresses: bool = False
    max_upload_bytes: int = 25 * 1024 * 1024
    feishu_frontend_origin: str = "http://127.0.0.1:5173"

    def check(self) -> None:
        if self.environment == "production":
            if not self.database_url.startswith("postgresql+"):
                raise ValueError("Production requires PostgreSQL")
            if not self.secure_cookies or not self.encryption_key:
                raise ValueError("Production requires secure cookies and an encryption key")
