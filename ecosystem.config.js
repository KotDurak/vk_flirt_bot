module.exports = {
  apps: [
    {
      name: 'vk-flirt-bot',

      // 🐾 ХИТРОСТЬ: Указываем сам Python из venv как "скрипт"
      script: '/root/vk_flirt_bot/venv/bin/python',

      // 🐾 А флаг -m и имя модуля передаем как аргументы
      args: '-m app.main',

      // Рабочая директория (ОБЯЗАТЕЛЬНО абсолютный путь из шага 1)
      cwd: '/root/vk_flirt_bot',

      // Перезапуск при падении
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000,

      // Переменные окружения (PYTHONPATH эмулирует поведение флага -m)
      env: {
        PYTHONIOENCODING: 'utf-8',
        PYTHONUNBUFFERED: '1',
        PYTHONPATH: '/root/vk_flirt_bot/'
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