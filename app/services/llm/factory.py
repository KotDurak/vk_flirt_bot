from __future__ import annotations

import logging

import aiohttp

from app.config import get_settings
from app.services.llm.base import LLMBase

logger = logging.getLogger(__name__)


def create_llm_client(session: aiohttp.ClientSession) -> LLMBase:
    """
    Фабрика для создания LLM-клиента на основе конфигурации.

    Читает LLM_PROVIDER из .env и возвращает соответствующий клиент.

    Args:
        session: aiohttp.ClientSession для HTTP-запросов

    Returns:
        Экземпляр LLMBase (LLMPolza, LLMYandex и т.д.)
    """
    settings = get_settings()
    provider = settings.llm_provider.lower()

    if provider == "polza":
        from app.services.llm.polza import LLMPolza
        logger.info("🏭 Creating LLMPolza client")
        return LLMPolza(session)

    elif provider == "routerai":
        from app.services.llm.routerai import LLMRouterAI
        logger.info("🏭 Creating LLMRouterAI client (Cydonia fallback ready)")
        return LLMRouterAI(session)

    elif provider == "vsegpt":
        from app.services.llm.vsegpt import LLMVseGPT
        logger.info("🏭 Creating LLMVseGPT client (Qwen/EVA ready)")
        return LLMVseGPT(session)

    # Здесь можно добавить другие провайдеры:
    # elif provider == "yandex":
    #     from app.services.llm.yandex import LLMYandex
    #     return LLMYandex(session)
    #
    # elif provider == "openrouter":
    #     from app.services.llm.openrouter import LLMOpenRouter
    #     return LLMOpenRouter(session)

    else:
        raise ValueError(f"Unknown LLM provider: {provider}")