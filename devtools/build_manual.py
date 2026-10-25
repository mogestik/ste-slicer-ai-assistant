"""Собирает PDF-руководство пользователя (plugin/AIAssistant/docs/AI_Assistant_user_guide_ru.pdf).

    python devtools/build_manual.py [--html-only] [--browser PATH]

Текст руководства лежит в этом файле, а справочник инструментов собирается из живого реестра —
так в руководстве всегда ровно те инструменты, что есть в плагине. HTML печатается в PDF через
Edge или Chrome в режиме headless, без PDF-библиотек для Python. Запускайте после изменений
инструментов или их описаний.
"""

import argparse
import html
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HERE)
PLUGIN_DIR = os.path.join(REPO_DIR, "plugin", "AIAssistant")
OUT_PDF = os.path.join(PLUGIN_DIR, "docs", "AI_Assistant_user_guide_ru.pdf")
sys.path.insert(0, PLUGIN_DIR)

from agent import __version__  # noqa: E402
from agent.tools import EXTRA_TOOLS, build_registry  # noqa: E402

BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/usr/bin/chromium", "/usr/bin/google-chrome",
)

GROUP_TITLES = (
    ("app", "Служебные"),
    ("settings", "Принтер, режим, материал, профиль и настройки"),
    ("scene", "Модели и сцена"),
    ("geometry", "Анализ геометрии"),
    ("knowledge", "База знаний"),
    ("slicing", "Слайсинг и сохранение"),
    ("verify", "Самопроверка"),
)

RU_LABELS = {
    "ping": "Проверка связи", "get_app_info": "Сведения о среде", "list_attachments": "Вложения диалога",
    "get_current_config": "Текущая конфигурация", "select_machine": "Выбор принтера", "get_settings": "Чтение настроек",
    "set_settings": "Изменение настроек", "reset_settings": "Сброс настроек", "search_settings": "Поиск настроек",
    "list_profiles": "Список профилей", "select_profile": "Выбор профиля", "create_profile": "Создание профиля",
    "save_profile": "Сохранение профиля", "list_materials": "Список материалов", "set_material": "Выбор материала",
    "set_support_material": "Поддержки другим материалом", "set_print_mode": "Режим печати 3D/5D",
    "load_model": "Загрузка модели", "list_objects": "Объекты на столе", "get_object_dimensions": "Габариты модели",
    "move_object": "Перемещение", "rotate_object": "Поворот", "scale_object": "Масштаб",
    "center_for_5d": "Центровка для 5D", "arrange_all": "Авторасстановка", "duplicate_object": "Копии объекта (партия)",
    "delete_objects": "Удаление объектов", "start_slicing": "Слайсинг", "get_slicing_status": "Статус слайсинга",
    "cancel_slicing": "Остановка слайсинга", "get_slicing_result": "Итог слайсинга", "export_gcode": "Экспорт G-code",
    "save_project": "Сохранение проекта 3MF", "analyze_model": "Анализ геометрии",
    "recommend_print_mode": "Рекомендация режима", "suggest_orientation": "Подбор ориентации",
    "get_print_guidelines": "Правила печати", "recommend_material": "Подбор материала",
    "get_print_preset": "Пресет настроек", "verify_project": "Проверка проекта по ТЗ",
}

CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm; }
body { font-family: "Segoe UI", Arial, sans-serif; font-size: 10.5pt; line-height: 1.45; color: #1f2328; }
h1 { font-size: 24pt; margin: 0 0 4mm 0; color: #0b4f9c; }
h2 { font-size: 15pt; margin: 9mm 0 3mm 0; color: #0b4f9c; border-bottom: 1px solid #cfd8e3; padding-bottom: 1.5mm; }
h3 { font-size: 12pt; margin: 6mm 0 2mm 0; }
p { margin: 0 0 2.5mm 0; }
ul, ol { margin: 0 0 3mm 0; padding-left: 6mm; }
li { margin: 0 0 1mm 0; }
code { font-family: Consolas, monospace; font-size: 9.5pt; background: #f2f4f7; padding: 0 1mm; border-radius: 2px; }
table { border-collapse: collapse; width: 100%; margin: 2mm 0 4mm 0; font-size: 9.5pt; }
th, td { border: 1px solid #d4dae2; padding: 1.5mm 2mm; vertical-align: top; text-align: left; }
th { background: #eef3f9; }
.tool td:first-child { width: 33%; }
.tag { display: inline-block; font-size: 8pt; color: #5b6474; border: 1px solid #c9d1dc; border-radius: 3px; padding: 0 1mm; }
.note { background: #fff7e6; border-left: 3px solid #d29922; padding: 2mm 3mm; margin: 3mm 0; }
.tip { background: #eaf4ff; border-left: 3px solid #3794ff; padding: 2mm 3mm; margin: 3mm 0; }
.cover { margin-top: 30mm; }
.cover .sub { font-size: 13pt; color: #5b6474; }
.toc li { margin: 0; }
.pb { page-break-before: always; }
.params { color: #5b6474; font-size: 8.8pt; }
"""

BODY = """
<div class="cover">
  <h1>AI Assistant для STE Slicer</h1>
  <p class="sub">Руководство пользователя · версия плагина {version}</p>
  <p>Ассистент-технолог внутри слайсера: по описанию задачи, модели, фото и ТЗ сам выбирает режим 3D/5D,
  материал, профиль и настройки, ставит модель на стол, нарезает её и объясняет решения.
  Принтер — Stereotech Hybrid 530 V5.2.4.</p>
  <h3>Содержание</h3>
  <ol class="toc">
    <li>Установка</li><li>Подключение к LLM (настройки)</li><li>Панель чата</li><li>Как поставить задачу</li>
    <li>Что делает ассистент</li><li>Режимы 3D и 5D</li><li>Материалы и поддержки другим материалом</li>
    <li>Партии деталей</li><li>Результаты: G-code, проект 3MF, проверка</li><li>Подключение внешних агентов (MCP)</li>
    <li>Неполадки</li><li>Справочник инструментов</li>
  </ol>
</div>

<h2 class="pb">1. Установка</h2>
<ol>
  <li>Закройте STE Slicer.</li>
  <li>Скопируйте папку <code>AIAssistant</code> в каталог плагинов пользователя:
  <code>%APPDATA%\\steslicer\\7.10\\plugins\\</code> (номер версии — как у вашего слайсера).</li>
  <li>Запустите слайсер. У правого края окна появится синий язычок <b>AI Assistant</b>,
  а в меню <b>Расширения</b> — пункт <b>AI Assistant</b> с командами <i>Open chat</i>, <i>Settings</i> и
  <i>User guide (PDF)</i>.</li>
</ol>
<p>Плагин работает на Python слайсера и не требует установки дополнительных библиотек.</p>
<div class="note">Для печати в режимах 5D нужна активированная лицензия ATSS: без неё внешняя программа
нарезки glicer зависает примерно на 50–60 %. Ассистент заметит это и подскажет, что делать.</div>

<h2>2. Подключение к LLM (настройки)</h2>
<p>Ассистенту нужна языковая модель с поддержкой вызова инструментов (tools) через OpenAI-совместимый API.
Откройте настройки: шестерёнка в заголовке панели или <b>Расширения → AI Assistant → Settings</b>.</p>
<table>
  <tr><th>Поле</th><th>Что указать</th></tr>
  <tr><td>Адрес API</td><td>Адрес с <code>/v1</code> на конце, например <code>https://api.example.ai/v1</code>.</td></tr>
  <tr><td>Ключ API</td><td>Ключ доступа. Хранится в настройках слайсера; в форме показывается маска — если её
  не трогать, ключ не меняется.</td></tr>
  <tr><td>Модель</td><td>Имя модели. Кнопка <i>Загрузить список моделей</i> покажет доступные — щёлкните нужную.</td></tr>
  <tr><td>Температура</td><td>0–2. Для стабильных вызовов инструментов — 0.1–0.3.</td></tr>
  <tr><td>Макс. шагов агента</td><td>Сколько обращений к модели допускается на одно сообщение (по умолчанию 30).</td></tr>
  <tr><td>Таймаут, повторы</td><td>Ожидание ответа модели и число повторов при сбоях сети или сервера.</td></tr>
  <tr><td>Макс. токенов ответа</td><td>0 — без ограничения.</td></tr>
  <tr><td>Размер фото</td><td>Фото уменьшаются до этого размера по большей стороне перед отправкой модели.</td></tr>
  <tr><td>Порт API и MCP</td><td>Локальный порт (по умолчанию 8765); если занят — берётся свободный.</td></tr>
  <tr><td>Флажки</td><td>Все инструменты сразу; потоковый ответ; параллельные вызовы; показ рассуждений;
  проверка SSL-сертификата.</td></tr>
</table>
<p>Нажмите <b>Проверить подключение</b> — ассистент отправит модели короткий запрос и покажет результат
и время ответа. Затем <b>Сохранить</b>.</p>

<h2>3. Панель чата</h2>
<p>Панель встроена в главное окно справа. Щелчок по язычку <b>AI Assistant</b> раскрывает её, 3D-вид сдвигается
влево. Ширину меняют, потянув за левый край панели. Пока курсор над панелью, мышь не управляет сценой.</p>
<table>
  <tr><th>Элемент</th><th>Назначение</th></tr>
  <tr><td>Точка у заголовка</td><td>Зелёная — связь с плагином есть, красная — нет (панель переподключится сама).</td></tr>
  <tr><td>＋</td><td>Новый диалог.</td></tr>
  <tr><td>Часы</td><td>История диалогов текущего сеанса слайсера.</td></tr>
  <tr><td>Солнце / луна</td><td>Светлая или тёмная тема (запоминается).</td></tr>
  <tr><td>Шестерёнка</td><td>Настройки подключения и работы агента.</td></tr>
  <tr><td>?</td><td>Это руководство.</td></tr>
  <tr><td>✕</td><td>Свернуть панель в язычок.</td></tr>
  <tr><td>Скрепка</td><td>Прикрепить файлы: модели STL/OBJ/3MF, фото JPG/PNG, ТЗ DOCX/TXT/MD. Пока файл
  загружается, на нём крутится индикатор; готовый отмечается галочкой.</td></tr>
  <tr><td>Кнопка ↑ / ■</td><td>Отправить сообщение (Enter) или остановить работу ассистента. Shift+Enter — новая строка.</td></tr>
</table>
<p>Каждое действие ассистента показывается строкой: название инструмента, значок результата (✓ — выполнено,
✕ — ошибка) и время. Щелчок по строке раскрывает подробности: аргументы и ответ. Во время нарезки над полем
ввода видна полоса прогресса, по окончании — карточка с временем печати и расходом материала.</p>
<div class="tip">Пока ассистент работает, не меняйте сцену и настройки вручную и не отправляйте новые сообщения —
иначе его действия и ваши перемешаются. Нужно вмешаться — сначала нажмите ■.</div>

<h2>4. Как поставить задачу</h2>
<p>Опишите деталь так, как описали бы технологу: назначение, нагрузки, температура и среда эксплуатации
(вода, масло, бензин, щёлочь, солнце), требования к гибкости, точности и внешнему виду, количество.
Приложите модель и, если есть, фото и ТЗ.</p>
<p><b>Примеры запросов:</b></p>
<ul>
  <li>«Ведущее колесо конвейера, 450 об/мин, нагрузка до 18 кг, нужна сцепляемость с бумагой. Печатаем в 5D.» + STL.</li>
  <li>«Пыльник ШРУСа, эластичный от 0 до +75 °C, стойкий к смазке и бензину.» + STL.</li>
  <li>«Кронштейн из PETG, поддержки из растворимого PVA.» + STL.</li>
  <li>«Партия из 6 фигурок на один стол.» + STL.</li>
  <li>«Покажи текущую конфигурацию принтера», «Почему ты выбрал этот материал?», «Сделай заполнение 40 %».</li>
</ul>
<p>Можно уточнять по ходу: «поменяй материал на PA6», «поверни деталью вниз», «нарежь заново». Если данных
не хватает, ассистент задаст один короткий вопрос или назовёт допущения, с которыми работает.</p>

<h2>5. Что делает ассистент</h2>
<ol>
  <li>Разбирает задачу и вложения (фото описывает, ТЗ читает).</li>
  <li>Анализирует геометрию модели: ось вращения, осевые отверстия, свесы, толщину стенок, габариты.</li>
  <li>Проверяет конфигурацию слайсера, сбрасывает правки прошлой задачи, выбирает режим 3D/5D.</li>
  <li>Подбирает материал из библиотеки Hybrid 530 по требованиям ТЗ.</li>
  <li>Берёт пресет настроек под задачу и материал, выбирает профиль качества, применяет настройки одним шагом.</li>
  <li>Загружает модель, подбирает ориентацию и ставит её на стол (в 5D — осью вращения на ось стола).</li>
  <li>Нарезает модель, сохраняет G-code и проект 3MF.</li>
  <li>Проверяет сохранённые файлы на соответствие своим решениям и исправляет расхождения.</li>
  <li>Выдаёт карточку: таблица параметров с источниками, «Почему так», выполнимость каждого требования ТЗ,
  оговорки (паспорт филамента, калибровка, ручные операции).</li>
</ol>
<p>Ассистент честен: если ни один доступный материал или режим не закрывает ТЗ целиком, он прямо называет
невыполненные требования и нужные испытания. Время печати и масса берутся только из результата нарезки.</p>

<h2>6. Режимы 3D и 5D</h2>
<table>
  <tr><th>Режим</th><th>Когда</th></tr>
  <tr><td><b>3D</b> (classic)</td><td>Обычная послойная печать на столе: плоские детали, зубчатые колёса, гибкие изделия,
  декор, детали без оси вращения.</td></tr>
  <tr><td><b>5D Spiral</b> (cylindrical)</td><td>Тело вращения со сквозным осевым отверстием: печать спиральными
  цилиндрическими слоями на оснастке. Base Diameter — диаметр отверстия (оправки).</td></tr>
  <tr><td><b>5D</b> (cylindrical_full)</td><td>Тело вращения со сплошной ступицей или валом: сердечник плоскими
  слоями, затем цилиндрические слои вокруг оси.</td></tr>
</table>
<h3>Выбор по прочности</h3>
<p>Если в задаче есть нагрузка (изгиб, кручение, обороты, давление, зуб, обод), ассистент вызывает расчёт
<code>compare_print_strategies</code> и показывает, как понял нагрузки. FDM-деталь анизотропна: вдоль слоя прочность 1,
поперёк слоёв — k<sub>z</sub> (0,55 для PLA/PETG/ABS/PC; 0,45 для наполненных волокном; 0,80 для TPU), межслойный сдвиг
k<sub>s</sub> (0,6 / 0,5 / 0,8). По Цаю–Хиллу считается индекс прочности для лучших поз 3D и для 5D (слои цилиндрические,
повторяют линии нагрузки). Разница индексов от 0,15 решает выбор; меньше — решает геометрия (поддержки, свесы), а цена
компромисса называется прямо. В карточке — строка «Прочность» с индексами. Это <b>[оценка]</b> по типичным данным
анизотропии FDM, а не измерение; режим, названный вами, главнее, но ассистент предупредит, если он заметно слабее.</p>
<p>Режим выбирается по геометрии, если вы не указали его сами. В 5D модель автоматически ставится осью вращения
на ось стола; стол и подложку ассистент не трогает — их задаёт профиль.</p>

<h2>7. Материалы и поддержки другим материалом</h2>
<p>Ассистент выбирает только материалы, доступные в слайсере для Hybrid 530 (предпочитает Stereotech:
Enduse, Fiberpart, Sealant, Proto) и сравнивает их по требованиям: влага, масло, бензин, щёлочь, УФ, нагрев,
жёсткость, ударная вязкость, гибкость, износ, трение, сцепление, пищевой контакт, плотность.
Температуры, обдув и ретракт берутся из профиля материала.</p>
<h3>Поддержки вторым материалом</h3>
<p>Если вы прямо попросите («поддержки из PVA», «растворимые поддержки», «поддержки вторым экструдером»),
ассистент в режиме 3D:</p>
<ul>
  <li>оставит материал детали на основном экструдере (0);</li>
  <li>назначит материал поддержек второму экструдеру (1) и переключит на него поддержки;</li>
  <li>включит черновую башню для прочистки сопла при смене материала;</li>
  <li>оценит пару: растворимые PVA (вода) — к PLA, PETG, TPU; HIPS (D-лимонен) — к ABS; остальное — отрывные
  поддержки; предупредит о разнице температур стола.</li>
</ul>
<div class="note">В 5D второй экструдер для поддержек не используется. Материалы серии Sealant печатаются
только основным экструдером. Перед печатью заправьте материал поддержек во второй экструдер и откалибруйте
смещение сопел.</div>

<h2>8. Партии деталей</h2>
<p>Для запроса вида «партия из N деталей» ассистент в 3D добавляет копии и расставляет их на столе, сообщает,
сколько поместилось и сколько столов понадобится; время и расход умножаются на число столов.
В 5D копирование недоступно: на оси стоит одна деталь.</p>

<h2>9. Результаты: G-code, проект 3MF, проверка</h2>
<p>После нарезки ассистент сохраняет G-code и проект 3MF в папку результатов
<code>%TEMP%\\steslicer_ai_assistant\\results\\</code> (или по пути, который вы назовёте) и проверяет их:
режим, материал, профиль, изменённые настройки, положение модели, наличие и корректность G-code
(для 5D — системы координат и поворотных осей). Проект открывается в STE Slicer обычным способом
(<b>Файл → Открыть</b>).</p>

<h2>10. Подключение внешних агентов (MCP)</h2>
<p>Все инструменты ассистента доступны внешним клиентам по протоколу MCP (JSON-RPC 2.0 через HTTP) на
<code>http://127.0.0.1:8765/mcp</code> (порт — из настроек). Сервер слушает только локальный адрес.
Через этот же интерфейс можно управлять слайсером из других AI-агентов.</p>

<h2>11. Неполадки</h2>
<table>
  <tr><th>Признак</th><th>Что делать</th></tr>
  <tr><td>Баннер «Не задан ключ API»</td><td>Откройте настройки, укажите адрес, ключ, модель; проверьте подключение.</td></tr>
  <tr><td>«Ключ API не принят», «Модель не найдена»</td><td>Проверьте ключ и имя модели (кнопка списка моделей).</td></tr>
  <tr><td>Красная точка «Нет связи с плагином»</td><td>Панель переподключается сама; если долго — перезапустите слайсер.</td></tr>
  <tr><td>5D-нарезка зависла на 50–60 %</td><td>Не активирована лицензия ATSS: активируйте и повторите или выберите 3D.</td></tr>
  <tr><td>«Достигнут лимит шагов»</td><td>Нажмите <i>Продолжить</i> или увеличьте «Макс. шагов агента».</td></tr>
  <tr><td>Ответ обрезан</td><td>Увеличьте «Макс. токенов ответа» (0 — без ограничения) и нажмите <i>Продолжить</i>.</td></tr>
</table>
<p>Журнал плагина пишется в журнал слайсера <code>%APPDATA%\\steslicer\\7.10\\steslicer.log</code>, строки с
префиксом <code>[AIAssistant]</code>.</p>

<h2 class="pb">12. Справочник инструментов</h2>
<p>Инструменты, которые ассистент вызывает сам (их же видят внешние MCP-клиенты). Помеченные
<span class="tag">по запросу</span> подключаются, когда нужны: модель запрашивает их сама.
Параметры со звёздочкой обязательны.</p>
{tools}
"""


def _param_text(schema):
    props = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    parts = []
    for name, spec in props.items():
        kind = spec.get("type")
        if isinstance(kind, list):
            kind = "/".join(kind)
        if spec.get("enum"):
            kind = " | ".join(str(v) for v in spec["enum"])
        parts.append("<code>%s%s</code> %s" % (html.escape(name), "*" if name in required else "",
                                               html.escape(str(kind or ""))))
    return ", ".join(parts) or "—"


def tools_html():
    registry = build_registry()
    by_group = {}
    for tool in registry.tools():
        by_group.setdefault(tool.group, []).append(tool)
    known = [g for g, _t in GROUP_TITLES]
    groups = list(GROUP_TITLES) + [(g, g) for g in by_group if g not in known]
    chunks = []
    for group, title in groups:
        tools = by_group.get(group)
        if not tools:
            continue
        rows = []
        for tool in tools:
            spec = tool.to_mcp()
            extra = ' <span class="tag">по запросу</span>' if tool.name in EXTRA_TOOLS else ""
            rows.append("<tr><td><code>%s</code>%s<br>%s</td><td>%s<div class=\"params\">Параметры: %s</div></td></tr>"
                        % (html.escape(tool.name), extra, html.escape(RU_LABELS.get(tool.name, "")),
                           html.escape(spec.get("description") or ""), _param_text(spec.get("inputSchema"))))
        chunks.append("<h3>%s</h3><table class=\"tool\"><tr><th>Инструмент</th><th>Что делает</th></tr>%s</table>"
                      % (html.escape(title), "".join(rows)))
    return "\n".join(chunks)


def build_html():
    body = BODY.replace("{version}", html.escape(__version__)).replace("{tools}", tools_html())
    return ("<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\"><title>AI Assistant — руководство"
            "</title><style>%s</style></head><body>%s</body></html>" % (CSS, body))


def find_browser(explicit=None):
    for path in ([explicit] if explicit else []) + list(BROWSERS):
        if path and os.path.isfile(path):
            return path
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--html-only", action="store_true", help="только собрать HTML (путь напечатается) и выйти")
    parser.add_argument("--browser", help="путь к msedge.exe / chrome.exe")
    args = parser.parse_args(argv)
    os.makedirs(os.path.dirname(OUT_PDF), exist_ok=True)
    work = tempfile.mkdtemp(prefix="aiassistant_manual_")
    html_path = os.path.join(work, "guide.html")
    with open(html_path, "w", encoding="utf-8") as handle:
        handle.write(build_html())
    if args.html_only:
        print(html_path)
        return 0
    browser = find_browser(args.browser)
    if browser is None:
        print("Edge/Chrome not found: pass --browser", file=sys.stderr)
        return 2
    url = "file:///" + html_path.replace("\\", "/")
    subprocess.check_call([browser, "--headless", "--disable-gpu", "--no-pdf-header-footer",
                           "--print-to-pdf-no-header", "--user-data-dir=" + os.path.join(work, "profile"),
                           "--print-to-pdf=" + OUT_PDF, url], timeout=120)
    print("PDF: %s (%d KB)" % (OUT_PDF, os.path.getsize(OUT_PDF) // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
