import logging
import sys


def setup_logging(level_name: str = "INFO") -> None:
    """
    Настраивает базовое логирование приложения.
    """
    level = getattr(logging, level_name.upper(), logging.INFO)

    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        force=True,
    )

    # Немного приглушаем слишком болтливые библиотеки
    logging.getLogger("aiohttp").setLevel(logging.WARNING)