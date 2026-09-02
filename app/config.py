from functools import lru_cache
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
import app.models_list

class Settings(BaseSettings):
    group_token: str
    group_id: int
    api_version: str = "5.199"
    log_level: str = "INFO"
    longpoll_wait: int = 25
    db_path: str = "data/bot.db"
    llm_provider: str = os.getenv("LLM_PROVIDER", "polza")
    payment_provider: str = "test"
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    yookassa_return_url: str = "https://vk.com/"
    admin_ids: str = ""

    model_config = SettingsConfigDict(
        env_prefix="VK_BOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def is_admin(self, user_id: int) -> bool:
        if not self.admin_ids:
            return False
        admin_list = [int(x.strip()) for x in self.admin_ids.split(",")]
        return user_id in admin_list


@lru_cache
def get_settings() -> Settings:
    """
    Возвращает настройки приложения.

    Переменные окружения читаются с префиксом VK_BOT_:
    - VK_BOT_GROUP_TOKEN
    - VK_BOT_GROUP_ID
    - VK_BOT_API_VERSION
    - VK_BOT_LOG_LEVEL
    - VK_BOT_LONGPOLL_WAIT
    """
    return Settings()

# app/config.py

# ... (импорты и класс Settings остаются)

class LLMSettings(BaseSettings):
    api_key: str
    base_url: str = "https://api.polza.ai/v1"
    model: str = "thedrummer/skyfall-36b-v2"
    model_sfw: str = "meta-llama/llama-3.1-70b-instruct"
    max_tokens: int = 800
    temperature: float = 1.1
    llm_provider: str = os.getenv("LLM_PROVIDER", "polza")
    model_summary: str = "sao10k/l3.3-euryale-70b"

    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_llm_settings() -> LLMSettings:
    """Возвращает настройки LLM."""
    return LLMSettings()


MODELS_LIST = app.models_list.MODELS_LIST