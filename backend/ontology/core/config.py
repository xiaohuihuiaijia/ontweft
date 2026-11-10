from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ONTOLOGY_", env_file=".env", extra="ignore")
    database_url: str = "sqlite:///var/ontology.db"
    data_dir: Path = Path("var")
    frontend_dir: Path | None = None
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
    extensions: list[Literal["knowledge", "integrations", "automation"]] = []
    model_approval: Literal["auto", "single", "separate"] = "auto"

    @property
    def separate_model_approval(self) -> bool:
        return self.model_approval == "separate" or (
            self.model_approval == "auto" and self.environment == "production"
        )

    @property
    def source_kinds(self) -> set[str]:
        core = {"tabular", "postgres", "postgresql", "sqlite", "file"}
        return core | (
            {"mysql", "mongo", "mongodb", "neo4j", "rdf", "http", "feishu"}
            if "integrations" in self.extensions
            else set()
        )

    def check(self) -> None:
        if self.environment == "production":
            if not self.database_url.startswith("postgresql+"):
                raise ValueError("Production requires PostgreSQL")
            if not self.secure_cookies or not self.encryption_key:
                raise ValueError("Production requires secure cookies and an encryption key")
