"""
Обёртка для запуска бота через PM2.
PM2 не умеет запускать `python -m app.main`, поэтому используем этот файл.
"""
from app.main import main

if __name__ == "__main__":
    main()