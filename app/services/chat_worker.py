#app/services/chat_worker.py
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
import random
from app.services.chat_queue import ChatTask
from app.services.llm import create_llm_client
from app.services.memory import maybe_generate_summary, build_llm_context
from app.db.repositories.payments import PaymentRepository
from app.vk.api import VKApi
from app.config import get_settings
from difflib import SequenceMatcher
import copy

logger = logging.getLogger(__name__)

MAX_REGEN_ATTEMPTS = 5  # Максимум попыток перегенерации при обнаружении повтора


def _truncate(value: str, limit: int = 1500) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _is_duplicate_response(new_response: str, recent_assistant_msgs: list[str]) -> tuple[bool, list[str]]:
    if len(recent_assistant_msgs) < 2:
        return False, []

    window = recent_assistant_msgs[-5:]
    bad_phrases = set()

    # 1. Проверка на полное совпадение (ПОВЫШАЕМ ПОРОГ с 0.65 до 0.75)
    for old_msg in window:
        if len(old_msg) > 80 and len(new_response) > 80:
            similarity = SequenceMatcher(None, new_response, old_msg).ratio()
            if similarity > 0.75:  # 🐾 Было 0.65. Теперь ловим только очень похожие тексты
                logger.warning(f"🚨 TEMPLATE LOOP DETECTED (sim={similarity:.2f})")
                return True, ["Полное структурное сходство с предыдущим ответом"]

    # 2. Поиск повторяющихся длинных фраз (ПОВЫШАЕМ ТРЕБОВАНИЯ)
    def extract_clauses(text: str) -> set[str]:
        # Ищем более длинные и осмысленные куски (20-60 символов)
        clauses = re.findall(r'[^.,!?;—\-]{20,60}', text.lower())
        return set(c.strip() for c in clauses if len(c.strip().split()) >= 4) # 🐾 Минимум 4 слова

    new_clauses = extract_clauses(new_response)

    for old_msg in window:
        old_clauses = extract_clauses(old_msg)
        common_clauses = new_clauses & old_clauses

        # 🐾 Было >= 2. Стало >= 3. Пусть совпадет хотя бы 3 длинных куска, прежде чем мы забьем тревогу
        if len(common_clauses) >= 3:
            bad_phrases.update(common_clauses)

    if bad_phrases:
        logger.warning(f"🚨 CLICHE LOOP DETECTED! Shared phrases: {bad_phrases}")
        return True, list(bad_phrases)

    # 3. Проверка на повторяющиеся вопросы в конце (ПОВЫШАЕМ ПОРОГ с 0.75 до 0.85)
    new_ends_q = new_response.strip().endswith('?')
    if new_ends_q:
        for old_msg in window:
            if old_msg.strip().endswith('?'):
                new_q = re.search(r'(.{15,40})\?$', new_response.strip())
                old_q = re.search(r'(.{15,40})\?$', old_msg.strip())
                if new_q and old_q:
                    q_sim = SequenceMatcher(None, new_q.group(1).lower(), old_q.group(1).lower()).ratio()
                    if q_sim > 0.85:  # 🐾 Было 0.75. Теперь вопросы должны быть почти идентичны
                        logger.warning("🚨 REPEATING QUESTION PATTERN.")
                        return True, ["Повторяющийся вопрос в конце сообщения"]

    # 4. Проверка действий (уже была улучшена, оставляем порог 0.75 для надежности)
    new_actions = re.findall(r'\*([^*]+)\*', new_response)
    if new_actions:
        for old_msg in window:
            old_actions = re.findall(r'\*([^*]+)\*', old_msg)
            for new_act in new_actions:
                for old_act in old_actions:
                    if len(new_act) > 8 and len(old_act) > 8:
                        act_sim = SequenceMatcher(None, new_act.lower(), old_act.lower()).ratio()
                        if act_sim > 0.75:  # 🐾 Было 0.70. Даем модели чуть больше свободы в вариациях действий
                            logger.warning(f"🚨 ACTION PHRASE LOOP: '{new_act}' ~ '{old_act}' (sim={act_sim:.2f})")
                            return True, [f"Повтор действия: {new_act[:30]}..."]

    return False, []


def _clean_response(text: str) -> str:
    """Убирает системный мусор, предотвращает создание вертикальных простынь текста."""
    if not text:
        return ""

    # 1. ЖЁСТКОЕ УДАЛЕНИЕ ВСЕХ HTML-ТЕГОВ И MARKDOWN
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'```(?:markdown|json|text)?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'```', '', text)

    # 2. УДАЛЕНИЕ УТЕЧЕК ПРОМПТА
    leak_patterns = [
        r'\+{2,}\s*диалог\.md.*',
        r'\[.*?(?:ФОРМАТ|ПРАВИЛО|КОНТЕКСТ|СИСТЕМНАЯ|АНКЕТА|ГРАНИЦЫ).*?\]',
        r'(?:Вот мой ответ|Как персонаж|Отыгрыш|Резюме):',
    ]
    for pattern in leak_patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.MULTILINE)

    # 3. УДАЛЕНИЕ АНГЛИЙСКИХ ВСТАВОК
    replacements = {
        r'\bhandsome\b': 'красавчик', r'\bbaby\b': 'малыш',
        r'\bsweetheart\b': 'милый', r'\bhoney\b': 'солнце',
        r'\bdarling\b': 'дорогой', r'\bcute\b': 'милый',
        r'\bhey\b': 'привет', r'\bhi\b': 'привет',
        r'\bhello\b': 'привет', r'\bokay\b': 'хорошо',
        r'\bwow\b': 'вау', r'\bsorry\b': 'прости', r'\byeah\b': 'да',
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    text = re.sub(r'\([^)]*Примечание[^)]*\)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\([^)]*Note[^)]*\)', '', text, flags=re.IGNORECASE)

    # 4. УМНАЯ РАБОТА С АБЗАЦАМИ (БЕЗ СОЗДАНИЯ ПРОСТЫНЕЙ)
    # Сначала заменяем одиночные переносы строк на пробелы, чтобы склеить разорванный текст
    text = re.sub(r'(?<!\n)\n(?!\n)', ' ', text)

    # Теперь разбиваем только по двойным переносам (настоящие абзацы)
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]

    # Если модель слила всё в один огромный абзац, аккуратно выделяем только ПЕРВЫЕ действие и речь
    if len(paragraphs) == 1 and len(paragraphs[0]) > 100:
        # Ищем первое действие (*) и первую речь (—)
        match = re.search(r'(\*[^*]{5,50}\*)\s*(—\s*.+)', paragraphs[0])
        if match:
            action = match.group(1).strip()
            speech = match.group(2).strip()
            # Отбрасываем всё, что модель попыталась приписать дальше (защита от простыни)
            paragraphs = [action, speech]

    # Собираем обратно, максимум 2-3 абзаца
    clean_text = '\n\n'.join(paragraphs)

    # 5. Финальная зачистка множественных пробелов внутри строк
    lines = clean_text.split('\n\n')
    lines = [re.sub(r'\s+', ' ', line).strip() for line in lines]
    clean_text = '\n\n'.join(lines)

    return clean_text.strip()


async def process_chat_task(
        task: ChatTask,
        api: VKApi,
        session: Any,
        msg_repo: Any,
        summary_repo: Any,
        payment_repo: PaymentRepository,
) -> None:
    logger.info("🎯 START task: user=%s char=%s text='%s'",
                task.user_id, task.char_id, task.text[:50])

    is_start_message = task.text.startswith("[Начало диалога") or task.text.startswith("[СИСТЕМНАЯ КОМАНДА")
    is_regeneration = getattr(task, 'is_regeneration', False)

    # Проверка баланса (ПРОПУСКАЕМ, если это регенерация)
    if not is_start_message and not is_regeneration:
        balance = await payment_repo.get_user_balance(task.user_id)
        if balance <= 0:
            logger.warning("⚠️ User %s has no messages left", task.user_id)
            await api.send_message(
                peer_id=task.peer_id,
                text="😿 У тебя закончились сообщения! Купи новый пакет в главном меню."
            )
            return

    llm = create_llm_client(session)

    ERROR_MESSAGES = [
        "Ой, я немного задумалась... Попробуй написать ещё раз? 😊",
        "Хм, что-то меня отвлекло. Повтори, пожалуйста?",
        "Прости, я на секунду потеряла мысль. Что ты говорил?",
    ]

    answer = random.choice(ERROR_MESSAGES)
    is_real_answer = False

    try:
        await maybe_generate_summary(
            session=session, llm=llm,
            msg_repo=msg_repo, summary_repo=summary_repo,
            user_id=task.user_id, character_id=task.char_id,
            model_override=getattr(task, 'model_name', None)
        )

        await asyncio.sleep(1.0)

        messages = await build_llm_context(
            msg_repo=msg_repo, summary_repo=summary_repo,
            user_id=task.user_id, character_id=task.char_id,
            system_prompt=task.char_dict["system_prompt"],
        )

        logger.info("📜 Last 3 messages in context:")
        for msg in messages[-3:]:
            logger.info("   [%s] %s", msg["role"], msg.get("content", "")[:100])

        # 🆕 Собираем последние ответы бота из контекста для проверки на дубли
        recent_assistant_msgs = [
            msg["content"] for msg in messages
            if msg.get("role") == "assistant"
        ]


        # 🔄 ЦИКЛ ПЕРЕГЕНЕРАЦИИ (без удаления из истории)

        base_settings = getattr(llm, '_settings', None)
        base_temperature = getattr(base_settings, 'temperature', 0.75) if base_settings else 0.75

        for attempt in range(MAX_REGEN_ATTEMPTS + 1):
            # При перегенерации увеличиваем temperature, чтобы избежать шаблонов
            if attempt > 0 and base_settings is not None:
                increased_temp = min(base_temperature + 0.05, 0.85)
                logger.info(f"🌡️ Increasing temperature to {increased_temp} for attempt {attempt + 1}")
                # Создаём копию настроек с увеличенной temperature
                new_settings = copy.deepcopy(base_settings)
                new_settings.temperature = increased_temp
                llm._settings = new_settings

            try:
                result = await llm.generate(messages, model_override=getattr(task, 'model_name', None))
            finally:
                # Восстанавливаем оригинальные настройки после каждой генерации
                if attempt > 0 and base_settings is not None:
                    llm._settings = base_settings

            if not result.success:
                logger.error("❌ LLM failed: code=%s msg=%s", result.error_code, result.error_message)
                break

            candidate_answer = _clean_response(result.content)
            candidate_answer = _truncate(candidate_answer)

            # 🛡️ ПРОВЕРКА НА СИСТЕМНЫЙ СРЫВ РОЛИ (НЕ ТРОГАЕТ IC-ОТКАЗЫ!)
            if _is_ai_refusal(candidate_answer):
                logger.warning("🚫 AI SAFETY REFUSAL DETECTED! Preventing energy deduction.")

                answer = (
                    "⚠️ Нейросеть наложила внутренний фильтр на эту конкретную формулировку. "
                    "Это не ошибка бота! Попробуй перефразировать свое сообщение или немного сменить тему. "
                    "⚡ Энергия за этот ответ НЕ списана."
                )
                is_real_answer = False  # КЛЮЧЕВОЕ: блокирует списание энергии
                break  # Выходим из цикла, перегенерировать системный отказ бессмысл

            # Проверяем кандидат
            is_dup, bad_phrases = _is_duplicate_response(candidate_answer, recent_assistant_msgs)

            if is_dup:
                if attempt < MAX_REGEN_ATTEMPTS:
                    logger.warning(
                        f"🔄 Duplicate detected (attempt {attempt + 1}/{MAX_REGEN_ATTEMPTS}). Purging bad context and regenerating...")

                    # 🚨 КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ: Удаляем последнее сообщение ассистента из контекста!
                    # Иначе модель продолжает видеть свой же плохой паттерн и копирует его, игнорируя запреты.
                    for i in range(len(messages) - 1, -1, -1):
                        if messages[i].get("role") == "assistant":
                            bad_msg = messages.pop(i)
                            logger.warning(
                                f"🗑️ Purged looping assistant message from context: {bad_msg['content'][:50]}...")
                            break

                    # Формируем жёсткий запрет на конкретные слова
                    banned_list = ", ".join([f'"{p}"' for p in bad_phrases[:3]])

                    # Находим последнее сообщение пользователя
                    last_user_msg = ""
                    for msg in reversed(messages):
                        if msg["role"] == "user":
                            last_user_msg = msg["content"]
                            break

                    # 🐾 МЯГКИЙ, НО ЧЕТКИЙ ВЫГОВОР (вместо "КРИТИЧЕСКОЙ ОШИБКИ")
                    # Мы не запрещаем слова, мы просим сменить тактику. Это проще для LLM.
                    messages.append({
                        "role": "system",
                        "content": (
                            f"[ВНИМАНИЕ ПЕРСОНАЖА]: Ты замечаешь, что твои последние реакции стали слишком похожи друг на друга. "
                            f"Не повторяй свои предыдущие действия или фразы. "
                            f"Смени тактику: опиши совершенно новое действие, эмоцию или задай неожиданный вопрос, "
                            f"реагируя на это сообщение пользователя: '{last_user_msg[:100]}'. "
                            f"Оставайся в характере, но будь оригинальнее."
                        )
                    })
                    continue  # Пробуем снова с увеличенной temperature
            # 🆕 НОВАЯ ПРОВЕРКА: Многословность
            if _is_too_verbose(candidate_answer):
                if attempt < MAX_REGEN_ATTEMPTS:
                    logger.warning(f"📜 Response too verbose (attempt {attempt + 1}). Regenerating...")

                    # Находим последнее сообщение пользователя
                    last_user_msg = ""
                    for msg in reversed(messages):
                        if msg["role"] == "user":
                            last_user_msg = msg["content"]
                            break

                    # Добавляем "ругалку" в стиле Барсика
                    messages.append({
                        "role": "system",
                        "content": (
                            f"[ВНИМАНИЕ]: Ты слишком многословна! "
                            f"Сократи ответ до 2-3 ёмких абзацев. Оставь только самые важные действия и реплики, "
                            f"говори по делу, реагируя на это сообщение пользователя: '{last_user_msg[:100]}'. "
                        )
                    })
                    continue
                else:
                    # 🚨 FALLBACK: Модель полностью сломана
                    logger.warning("⚠️ All regen attempts exhausted. Activating SAFE FALLBACK.")
                    answer = "..."
                    is_real_answer = True
                    break

            # Если дубликата нет, принимаем ответ
            answer = candidate_answer
            is_real_answer = True
            logger.info("✅ Answer accepted (clean): '%s'", answer[:100])
            break

        # Сохраняем в БД
        if is_real_answer:
            await msg_repo.add_message(task.user_id, task.char_id, "assistant", answer)

    except Exception:
        logger.exception("💥 CRITICAL error in chat worker")

    # Отправляем ответ пользователю
    try:
        await api.send_message(
            peer_id=task.peer_id,
            text=answer,
            keyboard=getattr(task, 'keyboard', None),
        )

        # Списываем сообщение ТОЛЬКО если это не старт, был реальный ответ и НЕ регенерация
        if not is_start_message and is_real_answer and not is_regeneration and not get_settings().is_admin(
                task.user_dict['vk_user_id']):
            success = await payment_repo.use_message(task.user_id)
            if success:
                new_balance = await payment_repo.get_user_balance(task.user_id)
                logger.info("💰 Energy used. User %s balance: %d", task.user_id, new_balance)
            else:
                logger.warning("⚠️ Failed to use energy for user %s", task.user_id)

        elif not is_start_message and not is_real_answer and not is_regeneration:
            logger.info("💰 Energy NOT charged (LLM failed). User %s can retry", task.user_id)

        elif is_regeneration and is_real_answer:
            logger.info("♻️ Regeneration completed successfully. Energy was already deducted in handle_update.")

    except Exception:
        logger.exception("Failed to send message to user %s", task.user_id)

    logger.info("🏁 FINISHED task for user=%s", task.user_id)


def _is_ai_refusal(text: str) -> bool:
    """
    Отличает отказ нейросети (срыв роли) от ролевого отказа персонажа.
    Возвращает True ТОЛЬКО если это системный сбой.
    """
    if not text or len(text) < 30:
        return False  # Слишком коротко для стандартного системного отказа

    text_lower = text.lower()

    # 1. ЖЕСТКИЕ МАРКЕРЫ (Нейросеть почти всегда использует эти фразы)
    hard_markers = [
        "языковая модель", "искусственный интеллект", "ИИ",
        "политика использования", "правила безопасности",
        "не могу участвовать в таких обсуждениях", "не могу предоставить такую информацию"
    ]
    if any(marker in text_lower for marker in hard_markers):
        return True

    # 2. МЯГКИЕ МАРКЕРЫ + ПРОВЕРКА ФОРМАТИРОВАНИЯ
    # Нейросеть пишет: "Извините, но я не могу продолжить этот разговор."
    # Персонаж пишет: "*краснея, отводит взгляд* Извини, но я не могу этого сделать."
    soft_markers = [
        "не могу продолжить этот разговор",
        "не могу выполнить этот запрос",
        "извините, но я не могу",
        "я не могу помочь с этим"
    ]

    has_soft_marker = any(marker in text_lower for marker in soft_markers)

    # Проверяем наличие ролевых маркеров: звездочки для действий или тире для речи
    has_roleplay_format = ("*" in text) or ("—" in text) or ("–" in text)

    # Если есть мягкий маркер отказа, НО нет ролевого форматирования -> это 99% сбой нейросети
    if has_soft_marker and not has_roleplay_format:
        return True

    # 3. ПРОВЕРКА НА ПОЛНЫЙ ОТСУТСТВИЕ КОНТЕКСТА (Сухой шаблон)
    # Если ответ состоит только из извинения и отказа, без единого эмоционального слова
    if has_soft_marker and len(text.split()) < 15:
        return True

    return False


def _is_too_verbose(text: str) -> bool:
    """Проверяет, не слишком ли многословен ответ."""
    if not text:
        return False

    paragraphs = [p for p in text.split('\n\n') if p.strip()]

    # Больше 5 абзацев ИЛИ больше 1500 символов (мягче, чем 2000)
    return len(paragraphs) > 5 or len(text) > 1500