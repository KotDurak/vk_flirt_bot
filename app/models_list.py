MODELS_LIST = {
    # === Мелкие/бюджетные (8B) ===
    'sao10k/l3-lunaris-8b': {
        'description': 'Щегол, опыта не поднабрался. Для коротких реплик сойдёт, но не более'
    },

    # === Euryale (от sao10k) ===
    'sao10k/l3.1-euryale-70b': {
        'description': 'Ловит 429, как Барсик ловит мух. Красиво пишет, но нестабильна'
    },
    'sao10k/l3.3-euryale-70b': {
        'description': 'Тоже отваливается, любит зациклиться на "*улыбается*... *улыбается*"'
    },

    # === Средние (24-36B) ===
    'thedrummer/skyfall-36b-v2': {
        'description': 'Словесный понос, но иногда красиво. Нестабильный гений'
    },
    'thedrummer/cydonia-24b-v4.1': {
        'description': 'Начинает хорошо, потом всё забывает. Память как у рыбки'
    },

    # === Llama-семейство (цензоры и англицизмы) ===
    'meta-llama/llama-3.3-70b-instruct': {
        'description': '"Здрасьте, до свидания". Цензорка, NSFW не понимает от слова совсем'
    },
    'nousresearch/hermes-3-llama-3.1-70b': {
        'description': 'Дура. Пишет "morning", "presentable", god-moding. Коты недовольны'
    },
    'nousresearch/hermes-3-llama-3.1-405b': {
        'description': 'Умеет в качественный RP, но денег много хочет. Барсик не одобряет'
    },
    'nousresearch/hermes-4-70b': {
        'description': 'Новая версия Hermes. Меньше отказов, но та же беда с англицизмами'
    },
    'nousresearch/hermes-4-405b': {
        'description': 'Умная, но дорогая. Как Hermes 3 405B, только с рассуждениями'
    },

    # === Qwen (наш текущий фаворит!) ===
    'qwen/qwen-2.5-72b-instruct': {
        'description': '⚡ ФАВОРИТ. Чистый русский, NSFW не моралит, держит формат. Бабушка оказалась с характером!'
    },

    # === Специализированные RP-модели (если есть у провайдера) ===
    'neversleep/llama-3-lumimaid-70b': {
        'description': 'Заточена под NSFW-литературу. Отказы на нуле. Искать на Polza/RouterAI'
    },
    'cognitivecomputations/dolphin-2.9-llama3-70b': {
        'description': 'Король uncensored. Вообще не знает слова "нет". Но стиль хромает'
    },
    'sao10k/l3.1-70b-stheno-v3.2': {
        'description': 'Младшая сестра Euryale. Меньше отказов, лучше держит контекст'
    },
}