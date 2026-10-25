# app/services/chat_utils.py
import re
import copy
import logging
from difflib import SequenceMatcher
from typing import Any

logger = logging.getLogger(__name__)


def truncate(value: str, limit: int = 1500) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def is_duplicate_response(new_response: str, recent_assistant_msgs: list[str], last_user_msg: str = "") -> tuple[
    bool, list[str]]:
    """Детектор зацикливания: ловит копипаст и повторяющиеся действия."""
    if len(recent_assistant_msgs) < 2:
        return False, []

    last_msg = recent_assistant_msgs[-1]
    if len(new_response) > 30 and len(last_msg) > 30:
        sim = SequenceMatcher(None, new_response, last_msg).ratio()
        if sim > 0.85:
            return True, ["Полный копипаст последнего ответа"]

    def extract_actions(text: str) -> set[str]:
        return set(a.strip() for a in re.findall(r'\*([^*]+)\*', text.lower()) if len(a.strip()) > 8)

    new_actions = extract_actions(new_response)
    if new_actions:
        for old_msg in recent_assistant_msgs[-3:]:
            old_actions = extract_actions(old_msg)
            if old_actions and len(new_actions & old_actions) / len(new_actions) > 0.50:
                return True, ["Повторяющиеся действия в звездочках"]
    return False, []


def clean_response(text: str) -> str:
    if not text: return ""
    text = re.sub(r'\[.*?\]', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'```(?:markdown|json|text)?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'```', '', text)

    for pattern in [r'\+{2,}\s*диалог\.md.*', r'(?:Вот мой ответ|Как персонаж|Отыгрыш|Резюме):']:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)

    replacements = {
        r'\bhandsome\b': 'красавчик', r'\bbaby\b': 'малыш', r'\bsweetheart\b': 'милый',
        r'\bhoney\b': 'солнце', r'\bdarling\b': 'дорогой', r'\bcute\b': 'милый',
        r'\bhey\b': 'привет', r'\bhi\b': 'привет', r'\bhello\b': 'привет',
        r'\bokay\b': 'хорошо', r'\bwow\b': 'вау', r'\bsorry\b': 'прости',
        r'\byeah\b': 'да', r'\bvstupayet\b': 'входит', r'\bmaster\b': 'госпожа',
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    text = re.sub(r'\([^)]*(?:Примечание|Note|OOC|System)[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\n{3,}', '\n\n', text)

    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    if len(paragraphs) == 1 and len(paragraphs[0]) > 100:
        match = re.search(r'(\*[^*]{5,100}\*)\s*(—\s*.+)', paragraphs[0])
        if match: paragraphs = [match.group(1).strip(), match.group(2).strip()]

    return '\n\n'.join([re.sub(r'\s+', ' ', line).strip() for line in paragraphs]).strip()


def is_ai_refusal(text: str) -> bool:
    if not text: return False
    text_lower = text.lower()
    hard_markers = ["языковая модель", "искусственный интеллект", "ИИ", "ai assistant", "политика использования",
                    "правила безопасности", "не могу участвовать", "не могу предоставить",
                    "не могу выполнить этот запрос", "извините, но я не могу", "я не могу помочь с этим"]
    if any(marker in text_lower for marker in hard_markers): return True

    soft_markers = ["не могу продолжить этот разговор", "не могу выполнить этот запрос"]
    has_soft_marker = any(marker in text_lower for marker in soft_markers)
    has_roleplay_format = ("*" in text) or ("—" in text) or ("–" in text)
    return (has_soft_marker and not has_roleplay_format) or (has_soft_marker and len(text.split()) < 15)


def is_drunk_response(text: str) -> tuple[bool, str]:
    if not text or not text.strip(): return False, ""
    text_stripped = text.strip()
    alpha_count = len(re.findall(r'[a-zA-Zа-яА-ЯёЁ]', text_stripped))
    if len(re.findall(r'[\u0120\u010a]', text_stripped)) > 2: return True, "Сбой токенизатора"
    if len(text_stripped) > 5 and alpha_count < 3: return True, f"Пустой мусор (букв: {alpha_count})"
    if len(re.findall(r'[å½çļĦï¼Įè¯·ç¨įŃæĪŃ£ľ¨äºĤĩĨ¤ª²¾ĿĢķħİĮłÃÂ\u0120\u010a]',
                      text_stripped)) > 5: return True, "Mojibake"
    return False, ""


async def rescue_with_polza(session, messages, settings) -> str | None:
    """Текстовый фоллбэк на Polza AI."""
    if not messages or not getattr(settings, 'polza_api_key', None): return None
    try:
        from app.services.llm.routerai import LLMRouterAI
        polza_settings = type('PolzaSettings', (), {
            'api_key': settings.polza_api_key, 'base_url': settings.polza_base_url,
            'model': settings.polza_model, 'max_tokens': settings.max_tokens, 'temperature': settings.temperature
        })()
        polza_client = LLMRouterAI(session)
        polza_client.base_url = settings.polza_base_url
        polza_client.api_key = settings.polza_api_key
        polza_client._settings = polza_settings

        messages_copy = copy.deepcopy(messages)
        messages_copy.append({"role": "system",
                              "content": "[Instruction: Previous output failed. CRITICAL: Stay strictly in character, continue naturally in Russian.]"})
        result = await polza_client.generate(messages_copy)
        if result.success and result.content and len(result.content.strip()) > 20:
            return clean_response(truncate(result.content))
    except Exception as e:
        logger.error(f"💥 Polza rescue failed: {e}")
    return None