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

    # Pydantic будет искать VK_BOT_LLM_PROVIDER (или просто оставь дефолт)
    llm_provider: str = "polza"

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
    return Settings()


class LLMSettings(BaseSettings):
    # 🔥 ЕДИНЫЙ КЛЮЧ ДЛЯ ВСЕХ ПРОВАЙДЕРОВ
    api_key: str

    # Дефолты для Polza
    base_url: str = "https://api.polza.ai/v1"
    model: str = "thedrummer/skyfall-36b-v2"
    model_sfw: str = "meta-llama/llama-3.1-70b-instruct"

    # Дефолты для RouterAI (наша золотая середина)
    routerai_base_url: str = "https://routerai.ru/v1"
    routerai_model: str = "nousresearch/hermes-3-llama-3.1-70b"

    model_summary: str = "nousresearch/hermes-3-llama-3.1-70b"
    fallback_model: str = "nousresearch/hermes-3-llama-3.1-70b"

    max_tokens: int = 800
    temperature: float = 0.85  # 🔥 Безопасный баланс креатива и контроля

    # 🔥 КРИТИЧЕСКИ ВАЖНО: Поле называется 'provider',
    # чтобы Pydantic с префиксом 'LLM_' искал в .env именно 'LLM_PROVIDER',
    # а не 'LLM_LLM_PROVIDER'.
    provider: str = "polza"

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