# Устанавливаем UTF-8 для вывода
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = "D:\vk_flirt_bot"

# Переходим в директорию проекта
Set-Location D:\vk_flirt_bot

# Запускаем бота
& "D:\vk_flirt_bot\.venv\Scripts\python.exe" -m app.main