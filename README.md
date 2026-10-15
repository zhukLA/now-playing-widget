# Now Playing Widget

Десктопный виджет для Windows 10/11: показывает название, исполнителя и обложку
текущего медиа (Spotify, браузеры, VLC и другие приложения, поддерживающие
**System Media Transport Controls**), с кнопками prev / play-pause / next.

Технологии: Python 3.13+, PySide6, `winrt-*` (WinRT-привязки, без `winsdk` и `pycaw`).

---

## Требования

- Windows 10 (1809+) или Windows 11, x64.
- **Python 3.13 или 3.14 (рекомендуется 3.13.x)** — под эти версии существуют
  готовые бинарные колёса пакетов `winrt-*`.

> ⚠️ **Предупреждение о версиях Python.** Начиная с Python 3.15 пакеты `winrt-*`
> пока не публикуют колёса на PyPI, и `pip install` пытается скомпилировать
> расширение из исходников, падая с ошибкой
> *«Microsoft Visual C++ 14.0 or greater is required»* (или ошибками компиляции,
> если Build Tools всё же установлены). Если `pip install -r requirements.txt`
> завершается такой ошибкой — пересоздайте окружение под Python 3.13/3.14.
> PySide6 (abi3-колёса) на новых версиях обычно ставится нормально.

---

## Установка и запуск (из исходников)

Все команды запускаются из корня проекта. Путь содержит пробелы — заключайте
его в кавычки:

```bat
cd "C:\Users\PC\Documents\Project OpenCode"
cd NowPlayingWidget

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Если `python` в системе — версии новее 3.14 (например, 3.15), создавайте
окружение явно от 3.13: `py -3.13 -m venv .venv` (см. предупреждение выше).

Для PowerShell вместо `.venv\Scripts\activate` используйте `.venv\Scripts\Activate.ps1`.

При первом запуске создастся `%APPDATA%\NowPlayingWidget\settings.json`.

### MOCK-режим (без Windows API)

Полезно для отладки интерфейса и на не-Windows-окружении:

```bat
set NOW_PLAYING_MOCK=1
python main.py
```

```powershell
$env:NOW_PLAYING_MOCK = "1"; python main.py
```

В MOCK-режиме имитируется смена треков (каждые 6 секунд), работают кнопки
управления, обложки — цветные заглушки. Заголовок окна помечается как *Now Playing (MOCK)*.

### Тесты и dev-зависимости

```bat
pip install -r requirements-dev.txt
pytest
```

---

## Управление

- **Перетаскивание** — за любую область окна; позиция запоминается.
- **ПКМ** — контекстное меню: «Поверх всех окон» (галочка), «Автозапуск»
  (галочка), «Настройки», «Закрыть».
- **Наведение мыши** — проявляются кнопки prev / play-pause / next (fade).
- **Трей** — левый клик: показать/скрыть; ПКМ: «Показать/Скрыть», «Выход».
- Если медиа нет: виджет либо скрывается (настройка «Прятать виджет, когда нет
  медиа»), либо показывает заглушку.
- Повторный запуск приложения не создаёт вторую копию — уже запущенное окно
  поднимается поверх (QLocalServer/QLocalSocket).

### Настройки

Файл: `%APPDATA%\NowPlayingWidget\settings.json`

| Ключ | Значение по умолчанию | Описание |
|---|---|---|
| `always_on_top` | `true` | поверх всех окон |
| `hide_when_no_media` | `false` | прятать виджет без медиа |
| `autostart_enabled` | `false` | автозапуск |
| `x`, `y` | `null` | позиция окна |

---

## Сборка

Сборка выполняется в окружении проекта с dev-зависимостями:

```bat
cd "C:\Users\PC\Documents\Project OpenCode"
cd NowPlayingWidget
.venv\Scripts\activate
pip install -r requirements-dev.txt
```

### 1. Exe (PyInstaller, onedir)

```bat
pyinstaller build.spec
```

Результат: `dist\NowPlayingWidget\NowPlayingWidget.exe` (+ `_internal\`).
Иконка и winrt-модули подключаются через `hiddenimports` в `build.spec`;
пути в spec — относительные (`SPECPATH`).

### 2. Установщик (Inno Setup 6)

Если `iscc` есть в PATH:

```bat
iscc installer.iss
```

Иначе — полный путь (у стандартной установки Inno Setup 6 под пользователем):

```bat
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer.iss
```

Результат: `dist\NowPlayingWidgetSetup.exe`.

Установщик: установка на текущего пользователя (без прав администратора),
деинсталлятор, опциональные ярлык на рабочем столе и автозапуск
(значение `NowPlayingWidget` в `HKCU\...\Run`, удаляется при деинсталляции).
Если у компилятора нет `Languages\Russian.isl`, установщик собирается только
на английском (условие в `installer.iss`).

---

## Автозапуск

- В **frozen-сборке** (`exe`): включается в настройках виджета или при установке
  (галочка «Запускать вместе с Windows»). В реестр пишется путь к `sys.executable`.
- В **dev-режиме** (`python main.py`) автозапуск намеренно **не регистрируется**
  в реестре — значение `Run` с командой `python main.py` не создаётся. Флаг в
  настройках хранится, но в реестр не пишется.

---

## Устранение неполадок

| Симптом | Причина / решение |
|---|---|
| `pip install` падает: *Microsoft Visual C++ 14.0 or greater is required* | Python 3.15+ — нет колёс `winrt-*`. Используйте Python 3.13/3.14, пересоздайте `.venv`. |
| Виджет запущен, но нет данных | Приложение-источник не поддерживает SMTC или не играет медиа. Запустите Spotify/браузер/VLC и начните воспроизведение. |
| Данные не обновляются | Перезапустите плеер: часть приложений регистрирует SMTC-сессию только после первого воспроизведения. |
| Обложка отсутствует | Не все приложения отдают thumbnail — виджет покажет заглушку, текст и кнопки работают. |
| Окно не видно | Возможно включено «Прятать виджет, когда нет медиа» или окно скрыто из трея — ПКМ на иконке в трее → «Показать/Скрыть». |
| `QLocalServer`-ошибка при запуске | Устаревший сокет прошлого запуска автоматически снимается (`QLocalServer.removeServer`); если повторяется — завершите висящий процесс `NowPlayingWidget.exe`. |
| `iscc` не найден | Установите Inno Setup 6 или используйте полный путь (см. выше). |
| Настройки «битые» | Удалите `%APPDATA%\NowPlayingWidget\settings.json` — будут созданы значения по умолчанию. |
| Нет иконки в трее | Иконка — `assets\icon.ico`; если файла нет, выполните `.venv\Scripts\python.exe tools\generate_icon.py`. |

---

## Структура проекта

```
NowPlayingWidget/
├── main.py                 # точка входа: single-instance, трей, связка UI и контроллера
├── media_controller.py     # SMTC + asyncio-цикл в QThread + MOCK-режим
├── settings.py             # JSON-настройки в %APPDATA%\NowPlayingWidget\ (+ автозапуск)
├── ui/
│   ├── widget.py           # окно виджета (paintEvent: тень, скругления, кнопки)
│   └── settings_dialog.py  # диалог настроек
├── assets/icon.ico         # иконка (генерируется tools/generate_icon.py)
├── tools/generate_icon.py  # разовый скрипт генерации иконки (PySide6)
├── tests/                  # pytest: settings + MOCK-парсинг
├── requirements.txt        # runtime-зависимости
├── requirements-dev.txt    # + pytest, pyinstaller
├── build.spec              # PyInstaller (onedir, windowed)
├── installer.iss           # Inno Setup
└── .gitignore
```
