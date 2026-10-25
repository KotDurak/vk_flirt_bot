# app/services/image_generator.py
import os
import base64
import tempfile
import logging
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from typing import Any
import aiohttp
import re
from app.config import get_settings, get_llm_settings
from app.db.repositories.payments import PaymentRepository
from app.db.repositories.generations import GenerationRepository
from app.vk.api import VKApi
from app.services.chat_queue import ChatTask
from app.services.chat_utils import clean_response, truncate

logger = logging.getLogger(__name__)


def build_sd_prompt(history_text: str, base_prompt: str) -> str:
    """Сценарный генератор промпта: понимает контекст действий и логику сцены без искусственной цензуры."""
    return (
        f"You are a visual scene director for Stable Diffusion. Analyze the dialogue and create a prompt that accurately reflects the CURRENT state of the character.\n\n"
        f"BASE CHARACTER TRAITS: {base_prompt}\n"
        f"DIALOGUE CONTEXT:\n{history_text}\n\n"
        f"DIRECTOR'S INSTRUCTIONS:\n"
        f"1. PERMANENT FEATURES: Always keep hair color, eye color, hair style, and body type from BASE CHARACTER TRAITS.\n"
        f"2. SCENE ANALYSIS: Read the dialogue carefully. What is the character CURRENTLY doing? Where are they? What are they wearing RIGHT NOW based on recent actions?\n"
        f"3. OUTFIT & STATE LOGIC:\n"
        f"   - If the dialogue shows the character CHANGING clothes (taking off, putting on, undressing, showering, getting into bed), UPDATE the outfit tags accordingly.\n"
        f"   - If the character is in a specific location (beach, shower, bedroom, pool), their outfit and state should MATCH that location.\n"
        f"   - If NO actions or location changes are mentioned, keep the BASE outfit.\n"
        f"4. IMPLICIT ACTIONS: Pay attention to implied actions. If the dialogue mentions 'getting ready for bed', 'after shower', 'waking up', 'intimate moment' — adjust the outfit, pose, and setting accordingly.\n"
        f"5. CONTEXTUAL ACCURACY (NO ARTIFICIAL CENSORSHIP): Describe the scene and character state accurately. If the dialogue implies undressing, intimacy, or specific states, use precise Danbooru-style tags (e.g., 'nsfw', 'nude', 'lingerie', 'detailed anatomy', 'seductive pose', 'blush'). Prioritize visual accuracy and the natural flow of the scene over artificial safety filters.\n"
        f"6. FORMAT: Comma-separated English tags only. NO sentences, NO explanations.\n\n"
        f"EXAMPLES:\n"
        f"- Dialogue: 'she took off her wet clothes after rain' → '1girl, solo, red hair, twintails, orange eyes, nude, detailed anatomy, wet hair, bathroom background, steam'\n"
        f"- Dialogue: 'they went to the beach' → '1girl, solo, red hair, twintails, orange eyes, bikini, beach background, ocean, sunny, wet skin'\n"
        f"- Dialogue: 'she got into bed' → '1girl, solo, red hair, twintails, orange eyes, lingerie, lying on bed, bedroom background, night, seductive pose'\n"
        f"- Dialogue: (no actions mentioned) → '1girl, solo, red hair, twintails, orange eyes, red camisole, denim shorts, standing' (use BASE)\n\n"
        f"CURRENT SCENE PROMPT:"
    )


async def handle_image_generation(
        task: ChatTask, api: VKApi, session: Any, msg_repo: Any,
        payment_repo: PaymentRepository, gen_repo: GenerationRepository, llm: Any
) -> None:
    logger.info("🎨 START image generation: user=%s", task.user_id)
    cost = 5
    char_name = task.char_dict.get("name", "Персонаж")
    char_slug = task.char_dict.get("slug", "")
    vk_user_id = task.user_dict.get('vk_user_id', 0)
    is_admin = get_settings().is_admin(vk_user_id)
    telegram_bot_link = "@MyNekoBaka89_bot"

    # 1. Кулдаун и баланс
    if not is_admin:
        last_attempt = await gen_repo.get_last_attempt(task.user_id)
        if last_attempt:
            last_attempt = last_attempt.replace(tzinfo=timezone.utc) if last_attempt.tzinfo is None else last_attempt
            time_diff = (datetime.now(timezone.utc) - last_attempt).total_seconds()
            if time_diff < 300:
                rem = int(300 - time_diff)
                await api.send_message(peer_id=task.peer_id,
                                       text=f"⏳ Художник отдыхает! Попробуй через {rem // 60} мин. {rem % 60} сек.\nИли рисуй без лимитов в Telegram: {telegram_bot_link} 😼")
                return
        if await payment_repo.get_user_balance(task.user_id) < cost:
            await api.send_message(peer_id=task.peer_id,
                                   text=f"😿 Не хватает энергии! Нужно {cost}, а у тебя {await payment_repo.get_user_balance(task.user_id)}.")
            return
        await gen_repo.log_attempt(task.user_id, "pending", "Generation started")

    await api.send_message(peer_id=task.peer_id,
                           text="⏳ Художник уже взял в руки кисть! Генерирую изображение, подождите немного...")

    # 2. Поиск референса
    char_image_path = None
    if char_slug:
        for search_dir in [Path(__file__).parent.parent / "images", Path(__file__).parent / "images", Path("images")]:
            for ext in [".png", ".jpg", ".jpeg", ".webp"]:
                candidate = search_dir / f"{char_slug}{ext}"
                if candidate.exists():
                    char_image_path = candidate
                    break
            if char_image_path: break

    # 3. Контекст и промпт
    recent_history = await msg_repo.get_recent_history(task.user_id, task.char_id, limit=10)
    history_text = "\n".join([
                                 f"{'User' if msg['role'] == 'user' else char_name}: {msg.get('content', '').replace('*', '').replace('—', '').strip()[:150]}"
                                 for msg in recent_history]) if recent_history else ""

    base_prompt_full = task.char_dict.get("sd_base_prompt", "").strip()

    # 🔥 1. ВЫТАСКИВАЕМ LoRA (Неизменяемое ядро)
    lora_match = re.search(r'<lora:[^>]+>', base_prompt_full)
    lora_tag = lora_match.group(0) if lora_match else ""

    # Убираем LoRA из базы, чтобы LLM его даже не видел и не пытался "исправить"
    clean_base_prompt = base_prompt_full.replace(lora_tag, "").strip()

    final_sd_prompt = clean_base_prompt  # По умолчанию используем чистую базу

    if history_text and clean_base_prompt:
        try:
            # Отправляем LLM только "чистую" базу без LoRA
            text_result = await llm.generate(
                [{"role": "user", "content": build_sd_prompt(history_text, clean_base_prompt)}],
                allow_short_response=True)

            if text_result.success and text_result.content:
                candidate_prompt = text_result.content.strip().replace('```', '').replace('prompt:', '').strip()

                # 🔥 2. ПРОВЕРКА НА ГАЛЛЮЦИНАЦИИ (из прошлого шага)
                base_tags = set(clean_base_prompt.lower().split(', '))
                candidate_tags = set(candidate_prompt.lower().split(', '))
                new_tags = candidate_tags - base_tags

                if new_tags and not history_text:
                    logger.warning(f"🚨 LLM ГАЛЛЮЦИНИРУЕТ! Добавил теги: {new_tags}. Откатываемся к базе.")
                    final_sd_prompt = clean_base_prompt
                else:
                    final_sd_prompt = candidate_prompt

        except Exception as e:
            logger.warning(f"⚠️ LLM prompt engineering failed: {e}")

    # 🔥 3. ВОЗВРАЩАЕМ LoRA НА ПЕРВОЕ МЕСТО (Всегда!)
    if lora_tag:
        final_sd_prompt = f"{lora_tag} {final_sd_prompt}"

    logger.info(f"✅ Финальный промпт для SD: {final_sd_prompt}")

    # 4. Fallback подготовка (на случай, если захотим вернуть Polza позже)
    old_style_prompt = f"CHARACTER REFERENCE: Use provided reference image for facial features ONLY. DO NOT copy expression/mouth. Match scene mood. SCENE CONTEXT:\n{history_text}\nINSTRUCTION: Draw character. STYLE: high quality anime illustration, masterpiece, vibrant colors, beautiful lighting, detailed background."
    reference_b64 = None
    if char_image_path:
        image_data = char_image_path.read_bytes()
        b64 = base64.b64encode(image_data).decode('utf-8')
        mime = "image/png" if char_image_path.suffix == ".png" else "image/jpeg"
        reference_b64 = f"data:{mime};base64,{b64}"

    # 5. Цепочка генерации
    image_result = None
    settings = get_settings()
    llm_settings = get_llm_settings()

    if final_sd_prompt:
        image_result = await llm.generate_sd_forge(prompt=final_sd_prompt)

    if image_result and image_result.get("type") == "task":
        task_id = image_result["task_id"]
        logger.info(f"🚀 [Background] Запущен поллинг для task_id: {task_id}")
        await api.send_message(peer_id=task.peer_id,
                               text="🎨 Художник взял задачу в работу! Очередь может быть большой, я пришлю картинку, как только он закончит. Не закрывай диалог!")

        asyncio.create_task(
            poll_and_deliver_image(
                task_id=task_id, peer_id=task.peer_id, user_id=task.user_id, char_name=char_name,
                api=api, session=session, gen_repo=gen_repo, payment_repo=payment_repo,
                cost=cost, is_admin=is_admin, forge_url=getattr(settings, 'sd_forge_url', '').rstrip('/'),
                forge_api_key=getattr(settings, 'sd_forge_api_key', '')
            )
        )
        return

    # 🔥 ВРЕМЕННАЯ ЗАГЛУШКА: Polza AI отключен для экономии средств на этапе тестов
    if not image_result:
        logger.warning("⚠️ SD Forge не вернул результат. Fallback на Polza AI временно ОТКЛЮЧЕН для экономии бюджета.")
        # Когда захочешь вернуть, просто раскомментируй этот блок:
        # try:
        #     image_result = await llm.generate_image(prompt=old_style_prompt, image_url=reference_b64,
        #                                             model_override=getattr(llm_settings, 'image_model', None))
        # except Exception as e:
        #     logger.error(f"💥 Polza AI image fallback failed: {e}")

    # 6. Обработка синхронного результата (если вдруг Forge вернул файл сразу)
    if image_result and image_result.get("type") == "file":
        temp_file_path = None
        try:
            old_path = Path(image_result["value"])
            fd, temp_path_str = tempfile.mkstemp(suffix=".jpg")
            temp_file_path = Path(temp_path_str)
            os.close(fd)
            old_path.rename(temp_file_path)

            try:
                from PIL import Image
                with Image.open(temp_file_path) as img:
                    img = img.convert("RGB")
                    img.save(temp_file_path, "JPEG", quality=95)
            except Exception as e:
                logger.warning(f"⚠️ Pillow re-save failed: {e}")

            attachment = None
            try:
                attachment = await api.upload_generated_photo(temp_file_path)
            except Exception:
                attachment = await api.upload_as_document(temp_file_path, filename=f"{char_name}_scene.jpg",
                                                          peer_id=task.peer_id)

            await api.send_message(peer_id=task.peer_id,
                                   text=f"✨ Магия сработала! Вот как {char_name} выглядит в этот момент:",
                                   attachment=attachment)

            if not is_admin:
                if await payment_repo.deduct_messages(task.user_id, cost):
                    logger.info(f"✅ Image sent. Energy ({cost}) safely deducted.")
                else:
                    await api.send_message(peer_id=task.peer_id,
                                           text="😿 Пока я рисовала, у тебя закончилась энергия! Пополни баланс.")

            await gen_repo.update_last_attempt_status(task.user_id, "success", None)
        except Exception as vk_err:
            logger.error(f"❌ Upload failed: {vk_err}")
            await api.send_message(peer_id=task.peer_id,
                                   text="😿 Картинка нарисована, но ВКонтакте капризничает. Попробуй через минуту! Энергия не списана.")
            await gen_repo.update_last_attempt_status(task.user_id, "error", str(vk_err))
        finally:
            if temp_file_path and temp_file_path.exists():
                temp_file_path.unlink()
        return

    # 🔥 ФИНАЛЬНАЯ ЗАГЛУШКА: Если ничего не получилось
    await gen_repo.update_last_attempt_status(task.user_id, "error", "Generation failed (Polza disabled for testing)")
    await api.send_message(peer_id=task.peer_id,
                           text=f"😿 Художник пока не смог закончить рисунок (сервис временно перегружен). Попробуй через пару минут!")


async def poll_and_deliver_image(
        task_id: str, peer_id: int, user_id: int, char_name: str, api: VKApi, session: Any,
        gen_repo: GenerationRepository, payment_repo: PaymentRepository, cost: int, is_admin: bool,
        forge_url: str, forge_api_key: str
):
    logger.info(f"🕵️ [Background] Start polling task_id: {task_id}")
    poll_url = f"{forge_url}/sdapi/v1/task/{task_id}"
    temp_path = None
    consecutive_errors = 0

    try:
        for attempt in range(120):
            await asyncio.sleep(5)
            try:
                async with session.get(poll_url, timeout=aiohttp.ClientTimeout(total=60, connect=10),
                                       headers={'x-api-key': forge_api_key}) as resp:
                    consecutive_errors = 0
                    if resp.status == 404:
                        await api.send_message(peer_id=peer_id, text="😿 Задача потерялась. Попробуй ещё раз!")
                        await gen_repo.update_last_attempt_status(user_id, "error", "Task not found")
                        return
                    if resp.status != 200:
                        continue

                    data = await resp.json(content_type=None)
                    status = data.get("status")
                    if attempt % 10 == 0: logger.info(f"📊 [Background] Task {task_id} status: {status}")

                    if status == "completed":
                        b64_image = data.get("image")
                        if not b64_image:
                            await api.send_message(peer_id=peer_id, text="😿 Картинка не получилась!")
                            await gen_repo.update_last_attempt_status(user_id, "error", "No image")
                            return

                        fd, temp_path = tempfile.mkstemp(suffix=".png")
                        with os.fdopen(fd, 'wb') as f:
                            f.write(base64.b64decode(b64_image))

                        try:
                            from PIL import Image
                            with Image.open(temp_path) as img:
                                img = img.convert("RGB")
                                img.save(temp_path, "JPEG", quality=95)
                        except Exception:
                            pass

                        attachment = None
                        try:
                            attachment = await api.upload_generated_photo(Path(temp_path))
                        except Exception:
                            attachment = await api.upload_as_document(Path(temp_path), filename=f"{char_name}.jpg",
                                                                      peer_id=peer_id)

                        await api.send_message(peer_id=peer_id, text=f"✨ {char_name} закончила позировать!",
                                               attachment=attachment)
                        if not is_admin: await payment_repo.deduct_messages(user_id, cost)
                        await gen_repo.update_last_attempt_status(user_id, "success", None)
                        logger.info(f"✅ [Background] Task {task_id} delivered")
                        return
                    elif status == "failed":
                        await api.send_message(peer_id=peer_id,
                                               text=f"😿 Ошибка генерации: {data.get('error', 'Unknown')}")
                        await gen_repo.update_last_attempt_status(user_id, "error", data.get('error', 'Unknown'))
                        return
            except asyncio.TimeoutError:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    await api.send_message(peer_id=peer_id, text="😿 API не отвечает. Попробуй через минуту!")
                    await gen_repo.update_last_attempt_status(user_id, "error", "API timeout")
                    return
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    await api.send_message(peer_id=peer_id, text="😿 Слишком много ошибок. Попробуй позже!")
                    await gen_repo.update_last_attempt_status(user_id, "error", "Too many errors")
                    return
        await gen_repo.update_last_attempt_status(user_id, "error", "Timeout")
    except Exception as e:
        logger.exception(f"💥 [Background] Critical error: {e}")
    finally:
        if temp_path and Path(temp_path).exists(): Path(temp_path).unlink()