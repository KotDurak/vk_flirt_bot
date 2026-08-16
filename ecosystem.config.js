module.exports = {
  apps: [
    {
      name: 'vk-flirt-bot',

      // 🆕 Запускаем обёртку вместо -m
      script: './run.py',

      // Интерпретатор из .venv
      interpreter: 'D:\\vk_flirt_bot\\.venv\\Scripts\\python.exe',

      // Рабочая директория
      cwd: 'D:\\vk_flirt_bot',

      // Перезапуск при падении
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,

      // Переменные окружения
      env: {
        PYTHONIOENCODING: 'utf-8',
        PYTHONPATH: 'D:\\vk_flirt_bot',
        PYTHONUNBUFFERED: '1',
      },

      // Логи
      output: './logs/out.log',
      error: './logs/error.log',
      merge_logs: true,

      // Один инстанс
      instances: 1,
      exec_mode: 'fork',
    }
  ]
}