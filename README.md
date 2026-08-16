# VK Flirt Bot 🤖💕

Ролевой бот для ВКонтакте с AI-персонажами. Использует LLM для генерации ответов в характере.

## 🚀 Быстрый старт

### Установка зависимостей
```bash
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install -r requirements.txt


Настройка
Скопируй .env.example в .env
Заполни API-ключи:
VK_BOT_TOKEN — токен группы ВК
LLM_API_KEY — ключ от Polza.ai
LLM_BASE_URL — URL API провайдера

# Локально
python -m app.main

# Через PM2 (продакшен)
pm2 start ecosystem.config.js

# Статус всех приложений
pm2 status

# Логи в реальном времени
pm2 logs vk-flirt-bot

# Только ошибки
pm2 logs vk-flirt-bot --err

# Перезапуск
pm2 restart vk-flirt-bot

# Остановка
pm2 stop vk-flirt-bot

# Удаление из PM2
pm2 delete vk-flirt-bot

# Сохранить конфигурацию (для автозапуска)
pm2 save

# Настроить автозапуск при загрузке системы
pm2 startup

app/
├── main.py                    # Точка входа
├── config.py                  # Конфигурация
├── handlers/
│   └── messages.py           # Обработка сообщений ВК
├── services/
│   ├── llm/                  # LLM-клиенты (Polza, Yandex и т.д.)
│   │   ├── base.py          # Абстрактный класс
│   │   ├── polza.py         # Реализация для Polza.ai
│   │   └── factory.py       # Фабрика клиентов
│   ├── chat_queue.py        # Очередь сообщений
│   ├── chat_worker.py       # Обработчик задач из очереди
│   └── memory.py            # Суммаризация истории
├── vk/
│   ├── api.py               # VK API клиент
│   └── longpoll.py          # LongPoll для получения событий
└── db/
    ├── connection.py        # Подключение к SQLite
    └── repositories/        # Репозитории для работы с БД

Сделано с ❤️ и нейросетями