# Чертежи ЕСКД

Оконная программа для Windows x64 читает основные надписи на сканах и программных PDF, сверяет обозначение с именем файла и собирает полные комплекты по номерам листов. В отчёте видны пропуски, конкурирующие версии листа и ошибки чтения. Спорные страницы можно проверить по изображению штампа и исправить в окне программы.

## Запуск готовой программы

1. Скачайте Windows ZIP из [GitHub Releases](https://github.com/ttethh-pixel/pdf-combining/releases/latest), распакуйте целиком и откройте **Чертежи ЕСКД.exe**.
2. Выберите отдельные папки входа и вывода. Укажите адрес API, визуальную модель и ключ, если его требует сервер.
3. Нажмите **Проверить API**, затем **Обработать и сшить**. В папке вывода появятся готовые комплекты, текстовый отчёт и две таблицы CSV.

Доступны Ollama, LM Studio, LiteLLM Proxy, OpenAI API и другие серверы с поддержкой изображений через Chat Completions. При локальной обработке укажите локальный адрес. При использовании удалённого сервера программа отправляет на него изображения страниц и штампов. Подробности в [инструкции](Как_запустить.txt).

## Запуск из исходников

Из корня проекта в PowerShell на Windows с Python 3.12 x64 и Tcl/Tk:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app/drawing_app.py
```

Графический интерфейс и API-клиент находятся в `app/drawing_app.py`. Обработка PDF, проверки комплектности, ручные решения и консольный режим находятся в `app/eskd_qwen.py`. В каталоге `app` также лежит инструкция распознавания, которую получает модель.

## Сборка и проверка

Для сборки нужны полный дистрибутив CPython 3.12 x64, зависимости из requirements.txt и системный компилятор .NET Framework 4.x. После создания окружения выполните:

```powershell
.\tools\build.ps1 -Python .\.venv\Scripts\python.exe
```

Скрипт компилирует Windows-загрузчик из `tools/Launcher.cs` и формирует переносимый ZIP в `dist`. Для интеграционных тестов:

```powershell
.\.venv\Scripts\python.exe -m pip install reportlab
.\.venv\Scripts\python.exe tests/check_pipeline.py
$archive = (Get-ChildItem dist/*.zip | Select-Object -First 1).FullName
Expand-Archive -LiteralPath $archive -DestinationPath build/unpacked -Force
.\build\unpacked\runtime\python.exe tests/check_portable.py
```

Тесты используют синтетические PDF и локальный тестовый API. Они проверяют пропуски, конфликт имени, возобновление, ручное подтверждение, порядок страниц и контроль результата. Точность чтения конкретной модели следует оценивать на образцах своего архива.
