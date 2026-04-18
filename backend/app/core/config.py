from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    DATABASE_URL: str = "sqlite+aiosqlite:///./pod_system.db"
    REDIS_URL: str = "redis://localhost:6379"
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "qwen2.5:3b"
    UPS_CLIENT_ID: Optional[str] = None
    UPS_CLIENT_SECRET: Optional[str] = None
    UPS_SANDBOX: bool = True
    SECRET_KEY: str = "supersecretkey_change_in_production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 240   # 4 hours (was 8)
    CORS_ALLOWED_ORIGINS: str = "http://localhost:5173,http://localhost:3000,http://localhost"
    ENVIRONMENT: str = "development"
    CONFIDENCE_THRESHOLD: float = 75.0
    DOCUMENTS_PATH: str = "./storage/documents"
    PACKING_SLIPS_PATH: str = "./storage/packing_slips"
    INVOICES_PATH: str = "./storage/invoices"
    POD_STORAGE_PATH: str = "./storage/pod_storage"
    ORDER_IMPORT_PATH: str = "./storage/order_import"
    # First-run admin seed (set in .env, cleared after first login)
    ADMIN_SEED_EMAIL: str = "fasttrack842001@gmail.com"
    ADMIN_SEED_PASSWORD: str = "Welcome01!"
    # License signing secret — override in .env for production
    LICENSE_SECRET: str = "change-me-license-secret"
    # Vendor / super-admin account — override in .env to change which account gets super_admin role
    VENDOR_EMAIL: str = "fasttrack842001@gmail.com"

    @property
    def ups_base_url(self) -> str:
        return "https://wwwcie.ups.com" if self.UPS_SANDBOX else "https://onlinetools.ups.com"

    class Config:
        env_file = ".env"


settings = Settings()
