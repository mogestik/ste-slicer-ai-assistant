// Панель чата AI Assistant, встроенная в главное окно STE Slicer (внешне похожа на чат в VS Code).
// QtWebEngine в слайсере нет, поэтому интерфейс нарисован на QML и общается с плагином через
// локальный HTTP API. `manager` — Python-объект ChatBridge; родительский элемент задаёт Python.
import QtQuick 2.7
import QtQuick.Layouts 1.3

import UM 1.3 as UM

import "markdown_lib.js" as Md

Item
{
    id: win
    z: 1000
    anchors.right: parent ? parent.right : undefined
    anchors.top: parent ? parent.top : undefined
    anchors.bottom: parent ? parent.bottom : undefined
    property real topOffset: 0
    anchors.topMargin: topOffset > 0 ? topOffset : UM.Theme.getSize("sidebar_header").height + 2 * UM.Theme.getSize("line").height

    // Размеры и тема
    property bool expanded: false
    readonly property real k: Math.max(1.0, UM.Theme.getSize("line").height / 11.0)   // масштаб интерфейса слайсера
    property real panelWidth: 460 * k
    readonly property real handleWidth: 26 * k
    readonly property int fs: Math.round(13 * k)
    width: expanded ? panelWidth : handleWidth

    property string theme: "light"
    readonly property var dark: ({
        bg: "#1f1f1f", surface: "#252526", surface2: "#2d2d30", border: "#3c3c3c", text: "#cccccc", text2: "#8b8b8b",
        accent: "#3794ff", accentBtn: "#0e639c", user: "#2a2d2e", ok: "#3fb950", warn: "#d29922", danger: "#f85149",
        dangerBg: "#3a1d1d", okBg: "#16301f", code: "#181818", hover: "#33ffffff", handle: "#0e639c"
    })
    readonly property var light: ({
        bg: "#ffffff", surface: "#f3f3f3", surface2: "#e8e8e8", border: "#d4d4d4", text: "#3b3b3b", text2: "#6e6e6e",
        accent: "#0066b8", accentBtn: "#0066b8", user: "#eef3fb", ok: "#1a7f37", warn: "#9a6700", danger: "#cf222e",
        dangerBg: "#fdecec", okBg: "#e6f4ea", code: "#f0f0f0", hover: "#22000000", handle: "#0066b8"
    })
    readonly property var c: theme === "dark" ? dark : light

    onWidthChanged: applyViewport()
    onExpandedChanged: { updateTop(); applyViewport(); }
    onParentChanged: updateTop()
    function updateTop()
    {
        var y = manager.topOffset();
        if (y > 0) { win.topOffset = y; }
    }
    Connections { target: win.parent; onHeightChanged: win.updateTop(); onWidthChanged: win.updateTop() }
    Timer { interval: 600; running: true; repeat: false; onTriggered: win.updateTop() }
    function applyViewport()
    {
        if (parent && parent.width > 0) { manager.setViewportShare(expanded ? width / parent.width : 0.0); }
    }

    // Состояние
    property string baseUrl: ""
    property string sessionId: ""
    property int lastSeq: 0
    property int pollGeneration: 0
    property bool running: false
    property bool online: true
    property bool keySet: true
    property string usageText: ""
    property string chatTitle: "Новый диалог"
    property string toastText: ""
    property var views: ({})
    property real slicingProgress: -1
    property string slicingLabel: ""
    property bool historyOpen: false
    property bool settingsOpen: false
    property bool settingsBusy: false
    property string settingsStatus: ""
    property bool settingsStatusOk: false
    property bool stick: true
    property bool uploadingNow: false

    readonly property var toolLabels: ({
        get_current_config: "Текущая конфигурация", get_settings: "Чтение настроек", set_settings: "Изменение настроек",
        reset_settings: "Сброс настроек", search_settings: "Поиск настроек", list_profiles: "Список профилей",
        select_profile: "Выбор профиля", create_profile: "Создание профиля", list_materials: "Список материалов",
        set_material: "Выбор материала", set_print_mode: "Режим печати 3D/5D", load_model: "Загрузка модели",
        list_objects: "Объекты на столе", move_object: "Перемещение", rotate_object: "Поворот",
        scale_object: "Масштаб", arrange_all: "Авторасстановка", duplicate_object: "Копии объекта",
        delete_objects: "Удаление объектов", start_slicing: "Слайсинг", get_slicing_status: "Статус слайсинга",
        get_slicing_result: "Итог слайсинга", export_gcode: "Экспорт G-code", save_project: "Сохранение проекта 3MF",
        select_machine: "Выбор принтера", center_for_5d: "Центровка для 5D", analyze_model: "Анализ геометрии",
        recommend_print_mode: "Рекомендация режима", suggest_orientation: "Подбор ориентации",
        get_print_guidelines: "Правила печати", recommend_material: "Подбор материала",
        get_print_preset: "Пресет настроек", verify_project: "Проверка проекта по ТЗ",
        enable_tools: "Подключение инструментов"
    })

    ListModel { id: msgs }           // kind, mid, text, extra, status, duration, expanded
    ListModel { id: atts }           // path, name, attId, status
    ListModel { id: hist }           // sid, title, busy
    ListModel { id: modelChoices }   // name

    // Вспомогательные функции
    function openSettings()
    {
        win.present();
        win.historyOpen = false;
        win.settingsOpen = true;
        win.settingsStatus = "";
        loadSettingsForm();
    }

    function openManual()
    {
        var error = manager.openManual();
        if (error) { toast(error); }
    }

    function loadSettingsForm()
    {
        if (win.baseUrl === "") { settingsRetry.start(); return; }
        api("GET", "/api/settings", null, function (err, data)
        {
            if (err) { win.settingsStatus = "Не удалось прочитать настройки: " + err.message; win.settingsStatusOk = false; return; }
            var st = data.settings;
            fBaseUrl.text = st.base_url;
            fApiKey.text = st.api_key;
            fModel.text = st.model;
            fTemperature.text = String(st.temperature);
            fIterations.text = String(st.max_iterations);
            fTimeout.text = String(st.request_timeout);
            fRetries.text = String(st.max_retries);
            fMaxTokens.text = String(st.max_tokens);
            fImageSide.text = String(st.image_max_side);
            fPort.text = String(st.port);
            cStream.checked = !!st.stream;
            cParallel.checked = !!st.parallel_tool_calls;
            cReasoning.checked = !!st.show_reasoning;
            cSsl.checked = !!st.verify_ssl;
            cAllTools.checked = st.llm_tools === "all";
            win.keySet = !!st.api_key_set;
        });
    }

    function collectSettings()
    {
        return {
            base_url: fBaseUrl.text.trim(), api_key: fApiKey.text.trim(), model: fModel.text.trim(),
            temperature: fTemperature.text.trim(), max_iterations: fIterations.text.trim(),
            request_timeout: fTimeout.text.trim(), max_retries: fRetries.text.trim(), max_tokens: fMaxTokens.text.trim(),
            image_max_side: fImageSide.text.trim(), port: fPort.text.trim(),
            stream: cStream.checked, parallel_tool_calls: cParallel.checked, show_reasoning: cReasoning.checked,
            verify_ssl: cSsl.checked, llm_tools: cAllTools.checked ? "all" : "dynamic"
        };
    }

    function saveSettings()
    {
        win.settingsBusy = true;
        api("POST", "/api/settings", collectSettings(), function (err, data)
        {
            win.settingsBusy = false;
            if (err) { win.settingsStatus = err.message; win.settingsStatusOk = false; return; }
            win.keySet = !!data.settings.api_key_set;
            fApiKey.text = data.settings.api_key;
            win.settingsStatus = "Сохранено.";
            win.settingsStatusOk = true;
        });
    }

    function testSettings()
    {
        win.settingsBusy = true;
        win.settingsStatus = "Проверяю подключение…";
        win.settingsStatusOk = true;
        var form = collectSettings();
        api("POST", "/api/settings/test", { base_url: form.base_url, api_key: form.api_key, model: form.model }, function (err, data)
        {
            win.settingsBusy = false;
            if (err) { win.settingsStatus = err.message; win.settingsStatusOk = false; return; }
            win.settingsStatusOk = !!data.ok;
            win.settingsStatus = data.message || (data.ok ? "Подключение работает." : "Подключение не работает.");
        });
    }

    function loadModels()
    {
        win.settingsBusy = true;
        api("GET", "/api/models", null, function (err, data)
        {
            win.settingsBusy = false;
            modelChoices.clear();
            if (err) { win.settingsStatus = "Список моделей недоступен: " + err.message + " (сохраните адрес и ключ)"; win.settingsStatusOk = false; return; }
            var names = data.models || [];
            for (var i = 0; i < names.length && i < 200; i++) { modelChoices.append({ name: names[i] }); }
            if (!names.length) { win.settingsStatus = "Сервер не вернул список моделей — впишите имя вручную."; win.settingsStatusOk = false; }
        });
    }

    function present()
    {
        win.expanded = true;
        if (win.baseUrl === "") { win.connectToServer(); }
    }

    function toast(text)
    {
        win.toastText = text;
        toastTimer.restart();
    }

    function setTheme(name)
    {
        win.theme = name;
        manager.setTheme(name);
    }

    function api(method, path, body, callback)
    {
        var xhr = new XMLHttpRequest();
        xhr.open(method, win.baseUrl + path);
        xhr.setRequestHeader("X-AI-Assistant", "1");
        if (body !== undefined && body !== null) { xhr.setRequestHeader("Content-Type", "application/json"); }
        xhr.onreadystatechange = function ()
        {
            if (xhr.readyState !== 4) { return; }
            var data = null;
            try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
            if (xhr.status >= 200 && xhr.status < 300) { callback(null, data); }
            else { callback({ message: (data && (data.error || data.message)) || ("HTTP " + xhr.status), status: xhr.status }, data); }
        };
        xhr.send(body !== undefined && body !== null ? JSON.stringify(body) : null);
    }

    function prettyJson(text)
    {
        if (text === undefined || text === null) { return ""; }
        try { return JSON.stringify(JSON.parse(text), null, 1); } catch (e) { return String(text); }
    }

    function appendMsg(kind, mid, text, extra)
    {
        msgs.append({ kind: kind, mid: mid || "", text: text || "", extra: extra || "", status: "", duration: "", expanded: false });
        var index = msgs.count - 1;
        if (mid) { var map = win.views; map[mid] = index; win.views = map; }
        scrollTimer.restart();
        return index;
    }

    function clearMessages()
    {
        msgs.clear();
        win.views = ({});
        win.slicingProgress = -1;
    }

    function durationText(ms)
    {
        if (ms === undefined || ms === null) { return ""; }
        return ms >= 1000 ? (ms / 1000).toFixed(1) + " с" : ms + " мс";
    }

    function resultItems(data)
    {
        var rows = [];
        if (data.print_time_text) { rows.push("Время печати: <b>" + Md.escapeHtml(data.print_time_text) + "</b>"); }
        var mats = data.material || [];
        for (var i = 0; i < mats.length; i++)
        {
            var m = mats[i];
            if (m.weight_g !== undefined) { rows.push("Материал: <b>" + m.weight_g + " г</b>" + (m.name ? " (" + Md.escapeHtml(m.name) + ")" : "")); }
            if (m.length_m !== undefined) { rows.push("Длина нити: <b>" + m.length_m + " м</b>"); }
        }
        if (data.mode) { rows.push("Режим: <b>" + Md.escapeHtml(String(data.mode)) + "</b>"); }
        if (data.layers) { rows.push("Слоёв: <b>" + data.layers + "</b>"); }
        return rows.join("<br>");
    }

    function errorTitle(kind)
    {
        var titles = {
            retry: "Повторный запрос к модели", loop: "Цикл остановлен", empty_answer: "Модель не дала ответа",
            max_iterations: "Достигнут лимит шагов", length: "Ответ обрезан", stream_interrupted: "Поток ответа оборвался",
            timeout: "LLM не ответила вовремя", network: "Нет связи с LLM", rate_limit: "Превышен лимит запросов к LLM",
            server: "Ошибка сервера LLM", auth: "Ключ API не принят", config: "Подключение не настроено",
            not_found: "Модель или адрес не найдены"
        };
        return titles[kind] || "";
    }

    function richStyle()
    {
        return "<style>a{color:" + c.accent + ";} pre,code{font-family:Consolas,monospace;background-color:" + c.code
               + ";} th{background-color:" + c.surface2 + ";} td,th{padding:3px 6px;} h1,h2,h3{margin:6px 0 2px 0;}</style>";
    }

    // События от плагина
    function ensureAssistant(mid)
    {
        if (win.views[mid] !== undefined) { return win.views[mid]; }
        return appendMsg("assistant", mid, "", "");
    }

    function applyEvent(ev)
    {
        var idx;
        switch (ev.type)
        {
        case "user_message":
            win.stick = true;
            appendMsg("user", "", ev.text, "");
            break;
        case "turn_started":
            win.running = true;
            break;
        case "assistant_delta":
            idx = ensureAssistant(ev.message_id);
            msgs.setProperty(idx, "text", msgs.get(idx).text + ev.text);
            scrollTimer.restart();
            break;
        case "tool_call_started":
            idx = appendMsg("tool", ev.call_id, win.toolLabels[ev.name] || ev.name, prettyJson(JSON.stringify(ev.arguments)));
            msgs.setProperty(idx, "status", "running");
            break;
        case "tool_call_result":
            idx = win.views[ev.call_id];
            if (idx !== undefined)
            {
                msgs.setProperty(idx, "status", ev.ok ? "ok" : "error");
                msgs.setProperty(idx, "duration", durationText(ev.duration_ms));
                msgs.setProperty(idx, "extra", prettyJson(ev.result));
            }
            break;
        case "slicing_progress":
            win.slicingProgress = Math.max(0, Math.min(1, ev.progress || 0));
            win.slicingLabel = ev.message || (ev.state === "done" ? "Слайсинг завершён" : "Слайсинг…");
            if (ev.state === "done" || ev.state === "error") { slicingHide.restart(); }
            break;
        case "slicing_result":
            win.slicingProgress = -1;
            appendMsg("result", "", resultItems(ev), "");
            break;
        case "usage":
            win.usageText = ev.total_tokens ? ev.total_tokens + " токенов" : "";
            break;
        case "error":
            appendMsg("error", "", ev.message || "Ошибка", errorTitle(ev.kind) + "|" + (ev.kind || ""));
            break;
        case "done":
            win.running = false;
            if (ev.reason === "cancelled") { appendMsg("notice", "", "Остановлено пользователем.", ""); }
            break;
        case "session_reset":
            clearMessages();
            break;
        }
    }

    function renderTranscript(items)
    {
        clearMessages();
        for (var i = 0; i < items.length; i++)
        {
            var it = items[i], idx;
            if (it.kind === "user") { appendMsg("user", "", it.text, ""); }
            else if (it.kind === "assistant") { if (it.content) { appendMsg("assistant", it.id, it.content, ""); } }
            else if (it.kind === "tool")
            {
                idx = appendMsg("tool", it.id, win.toolLabels[it.name] || it.name, prettyJson(JSON.stringify(it.arguments)));
                msgs.setProperty(idx, "status", it.status === "running" ? "running" : (it.status === "ok" ? "ok" : "error"));
                if (it.status !== "running")
                {
                    msgs.setProperty(idx, "duration", durationText(it.duration_ms));
                    msgs.setProperty(idx, "extra", prettyJson(it.result));
                }
            }
            else if (it.kind === "slicing_result") { appendMsg("result", "", resultItems(it.data || {}), ""); }
            else if (it.kind === "error") { appendMsg("error", "", it.message || "Ошибка", errorTitle(it.error_kind) + "|" + (it.error_kind || "")); }
            else if (it.kind === "notice") { appendMsg("notice", "", it.text, ""); }
        }
        win.stick = true;
        scrollTimer.restart();
    }

    // Обмен с плагином
    function connectToServer()
    {
        var url = manager.ensureServer();
        if (!url) { win.online = false; retryTimer.start(); return; }
        win.baseUrl = url.replace(/\/$/, "");
        win.theme = manager.getTheme();
        refreshSettings();
        api("GET", "/api/sessions", null, function (err, data)
        {
            if (err) { win.online = false; retryTimer.start(); return; }
            win.online = true;
            var best = null;
            var list = data.sessions || [];
            for (var i = 0; i < list.length; i++)
            {
                if (list[i].messages && (!best || list[i].updated > best.updated)) { best = list[i]; }
            }
            if (best) { loadSession(best.id); } else { newSessionRequest(); }
        });
    }

    function refreshSettings()
    {
        api("GET", "/api/settings", null, function (err, data)
        {
            if (!err && data && data.settings) { win.keySet = !!data.settings.api_key_set; }
        });
    }

    function loadHistory()
    {
        api("GET", "/api/sessions", null, function (err, data)
        {
            if (err) { return; }
            hist.clear();
            var list = (data.sessions || []).slice().sort(function (a, b) { return b.updated - a.updated; });
            for (var i = 0; i < list.length; i++)
            {
                if (list[i].messages || list[i].id === win.sessionId) { hist.append({ sid: list[i].id, title: list[i].title, busy: !!list[i].running }); }
            }
        });
    }

    function newSessionRequest()
    {
        if (win.baseUrl === "") { win.present(); return; }
        api("POST", "/api/sessions", {}, function (err, data)
        {
            if (err) { toast("Не удалось создать диалог: " + err.message); return; }
            clearMessages();
            loadSession(data.session.id);
        });
    }

    function loadSession(id)
    {
        win.pollGeneration += 1;
        var generation = win.pollGeneration;
        win.sessionId = id;
        api("GET", "/api/session?session=" + encodeURIComponent(id), null, function (err, data)
        {
            if (generation !== win.pollGeneration) { return; }
            if (err) { win.online = false; retryTimer.start(); return; }
            win.online = true;
            win.lastSeq = data.last_seq;
            renderTranscript(data.items || []);
            win.running = !!data.session.running;
            win.chatTitle = data.session.title || "Новый диалог";
            win.usageText = data.usage && data.usage.total_tokens ? data.usage.total_tokens + " токенов" : "";
            poll(generation);
        });
    }

    function poll(generation)
    {
        if (generation !== win.pollGeneration) { return; }
        api("GET", "/api/events?session=" + encodeURIComponent(win.sessionId) + "&since=" + win.lastSeq + "&timeout=20", null,
            function (err, data)
            {
                if (generation !== win.pollGeneration) { return; }
                if (err)
                {
                    win.online = false;
                    pollRetry.generation = generation;
                    pollRetry.start();
                    return;
                }
                win.online = true;
                if (data.head !== undefined && data.head < win.lastSeq) { loadSession(win.sessionId); return; }
                var events = data.events || [];
                for (var i = 0; i < events.length; i++) { applyEvent(events[i]); }
                win.lastSeq = data.last_seq;
                if (win.running && !data.running) { win.running = false; }
                poll(generation);
            });
    }

    function readyAttachments()
    {
        var ids = [];
        for (var i = 0; i < atts.count; i++) { if (atts.get(i).status === "done") { ids.push(atts.get(i).attId); } }
        return ids;
    }

    function uploading()
    {
        for (var i = 0; i < atts.count; i++) { if (atts.get(i).status === "uploading") { return true; } }
        return false;
    }

    function refreshUploading() { win.uploadingNow = uploading(); }

    function sendMessage(text)
    {
        if (win.sessionId === "") { toast("Подключаюсь к плагину… повторите через пару секунд."); return; }
        if (win.running) { api("POST", "/api/stop", { session: win.sessionId }, function (err) { if (err) { toast(err.message); } }); return; }
        text = (text || "").trim();
        var ids = readyAttachments();
        if (uploading()) { toast("Дождитесь окончания загрузки файлов."); return; }
        if (!text && !ids.length) { return; }
        api("POST", "/api/message", { session: win.sessionId, text: text, attachments: ids }, function (err)
        {
            if (err) { toast(err.message); return; }
            input.text = "";
            atts.clear();
            win.refreshUploading();
            win.running = true;
        });
    }

    function addFiles()
    {
        if (win.sessionId === "") { toast("Подключаюсь к плагину… повторите через пару секунд."); return; }
        var paths = manager.pickFiles();
        for (var i = 0; i < paths.length; i++)
        {
            var path = paths[i];
            var name = path.replace(/\\/g, "/").split("/").pop();
            atts.append({ path: path, name: name, attId: "", status: "uploading" });
            manager.uploadFile(win.sessionId, path);
        }
        refreshUploading();
    }

    Connections
    {
        target: manager
        onFileUploaded: {
            for (var i = 0; i < atts.count; i++)
            {
                if (atts.get(i).path === path && atts.get(i).status === "uploading")
                {
                    if (error) { atts.remove(i); win.toast("Файл не загружен: " + error); }
                    else { atts.setProperty(i, "attId", attachmentId); atts.setProperty(i, "status", "done"); }
                    break;
                }
            }
            win.refreshUploading();
        }
    }

    Timer { id: settingsRetry; interval: 1500; onTriggered: { if (win.baseUrl !== "") { win.loadSettingsForm(); } else { start(); } } }
    Timer { id: retryTimer; interval: 2500; repeat: false; onTriggered: { win.connectToServer(); } }
    Timer { id: pollRetry; property int generation: 0; interval: 2000; onTriggered: { win.poll(generation); } }
    Timer { id: toastTimer; interval: 4000; onTriggered: { win.toastText = ""; } }
    Timer { id: slicingHide; interval: 2500; onTriggered: { win.slicingProgress = -1; } }
    Timer { id: scrollTimer; interval: 50; onTriggered: { if (win.stick) { list.positionViewAtEnd(); } } }

    // Интерфейс
    Rectangle   // свёрнутый язычок
    {
        visible: !win.expanded
        anchors.fill: parent
        color: win.c.handle
        Text
        {
            anchors.centerIn: parent
            rotation: 90
            text: "AI Assistant"
            color: "white"
            font.pixelSize: Math.round(12 * win.k)
            font.bold: true
            width: 140 * win.k
            horizontalAlignment: Text.AlignHCenter
        }
        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; acceptedButtons: Qt.AllButtons
                    onClicked: win.present(); onWheel: wheel.accepted = true }
    }

    Rectangle   // сама панель
    {
        id: panel
        visible: win.expanded
        anchors.fill: parent
        color: win.c.bg
        border.width: 1
        border.color: win.c.border

        MouseArea   // забираем мышь себе, чтобы сцена под панелью не крутилась и не масштабировалась
        {
            anchors.fill: parent
            acceptedButtons: Qt.AllButtons
            hoverEnabled: true
            onWheel: wheel.accepted = true
        }

        MouseArea   // левый край тянется мышью
        {
            x: 0; y: 0; width: 5; height: parent.height
            z: 20
            cursorShape: Qt.SizeHorCursor
            property real startX: 0
            property real startWidth: 0
            onPressed: { startX = mapToItem(win.parent, mouse.x, 0).x; startWidth = win.panelWidth; }
            onPositionChanged:
            {
                if (!pressed) { return; }
                var now = mapToItem(win.parent, mouse.x, 0).x;
                win.panelWidth = Math.max(320 * win.k, Math.min(win.parent.width * 0.7, startWidth + (startX - now)));
            }
        }

        // Заголовок
        Rectangle
        {
            id: header
            anchors { left: parent.left; right: parent.right; top: parent.top }
            height: 38 * win.k
            color: win.c.bg

            Row
            {
                anchors { left: parent.left; leftMargin: 14 * win.k; verticalCenter: parent.verticalCenter }
                spacing: 8 * win.k
                Rectangle { width: 8 * win.k; height: 8 * win.k; radius: width / 2; anchors.verticalCenter: parent.verticalCenter
                            color: win.online ? win.c.ok : win.c.danger }
                Text
                {
                    anchors.verticalCenter: parent.verticalCenter
                    width: panel.width - 230 * win.k
                    text: win.chatTitle
                    elide: Text.ElideRight
                    color: win.c.text
                    font.pixelSize: win.fs
                    font.bold: true
                }
            }
            Row
            {
                anchors { right: parent.right; rightMargin: 8 * win.k; verticalCenter: parent.verticalCenter }
                spacing: 2 * win.k
                IconButton { size: 28 * win.k; glyph: ""; tip: "Новый диалог"; fg: win.c.text; hoverColor: win.c.hover
                             enabled: !win.running; onClicked: { win.historyOpen = false; win.settingsOpen = false; win.newSessionRequest(); } }
                IconButton { size: 28 * win.k; glyph: ""; tip: "История диалогов"; fg: win.c.text; hoverColor: win.c.hover
                             active: win.historyOpen; onClicked: { win.historyOpen = !win.historyOpen; if (win.historyOpen) { win.loadHistory(); } } }
                IconButton { size: 28 * win.k; glyph: win.theme === "dark" ? "" : ""; tip: "Тема"; fg: win.c.text; hoverColor: win.c.hover
                             onClicked: win.setTheme(win.theme === "dark" ? "light" : "dark") }
                IconButton { size: 28 * win.k; glyph: ""; tip: "Настройки подключения"; fg: win.c.text; hoverColor: win.c.hover
                             active: win.settingsOpen; onClicked: { if (win.settingsOpen) { win.settingsOpen = false; } else { win.openSettings(); } } }
                IconButton { size: 28 * win.k; glyph: "\uE897"; tip: "Руководство пользователя (PDF)"; fg: win.c.text; hoverColor: win.c.hover
                             onClicked: win.openManual() }
                IconButton { size: 28 * win.k; glyph: ""; tip: "Свернуть"; fg: win.c.text; hoverColor: win.c.hover
                             onClicked: win.expanded = false }
            }
            Rectangle { anchors { left: parent.left; right: parent.right; bottom: parent.bottom } height: 1; color: win.c.border }
        }

        // Плашка с предупреждением
        Rectangle
        {
            id: banner
            anchors { left: parent.left; right: parent.right; top: header.bottom }
            visible: !win.online || !win.keySet
            height: visible ? bannerText.implicitHeight + 12 * win.k : 0
            color: !win.online ? win.c.dangerBg : win.c.surface2
            Text
            {
                id: bannerText
                anchors { left: parent.left; right: parent.right; margins: 12 * win.k; verticalCenter: parent.verticalCenter }
                wrapMode: Text.Wrap
                color: win.c.text
                font.pixelSize: Math.round(12 * win.k)
                text: !win.online ? "Нет связи с плагином — переподключаюсь…"
                                  : "Не задан ключ API. Откройте настройки (шестерёнка) и укажите адрес, ключ и модель."
            }
        }

        // Сообщения
        ListView
        {
            id: list
            anchors { left: parent.left; right: parent.right; top: banner.bottom; bottom: slicingBar.top }
            clip: true
            spacing: 6 * win.k
            model: msgs
            topMargin: 10 * win.k
            bottomMargin: 10 * win.k
            boundsBehavior: Flickable.StopAtBounds
            onMovementEnded: win.stick = list.atYEnd
            onFlickEnded: win.stick = list.atYEnd

            delegate: Item
            {
                id: row
                width: list.width
                height: content.implicitHeight
                readonly property bool isTool: model.kind === "tool"
                readonly property string errKind: model.kind === "error" ? model.extra.split("|")[1] : ""
                readonly property string errTitle: model.kind === "error" ? model.extra.split("|")[0] : ""

                Item
                {
                    id: content
                    x: 12 * win.k
                    width: parent.width - 24 * win.k
                    implicitHeight: kindColumn.implicitHeight

                    Column
                    {
                        id: kindColumn
                        width: parent.width
                        spacing: 4 * win.k

                        // сообщение пользователя — в рамке
                        Rectangle
                        {
                            visible: model.kind === "user"
                            width: parent.width
                            height: visible ? userText.implicitHeight + 16 * win.k : 0
                            radius: 6 * win.k
                            color: win.c.user
                            border.width: 1
                            border.color: win.c.border
                            TextEdit
                            {
                                id: userText
                                x: 10 * win.k; y: 8 * win.k
                                width: parent.width - 20 * win.k
                                readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap
                                color: win.c.text
                                font.pixelSize: win.fs
                                text: model.kind === "user" ? model.text : ""
                            }
                        }

                        // ответ ассистента — без рамки, markdown
                        TextEdit
                        {
                            visible: model.kind === "assistant"
                            width: parent.width
                            height: visible ? implicitHeight : 0
                            readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap
                            textFormat: TextEdit.RichText
                            color: win.c.text
                            font.pixelSize: win.fs
                            text: model.kind === "assistant" ? win.richStyle() + Md.render(model.text) : ""
                            onLinkActivated: Qt.openUrlExternally(link)
                        }

                        // вызов инструмента — одна строка, по клику раскрывается
                        Item
                        {
                            visible: row.isTool
                            width: parent.width
                            height: visible ? toolLine.implicitHeight + 6 * win.k : 0
                            Row
                            {
                                id: toolLine
                                y: 3 * win.k
                                spacing: 7 * win.k
                                Text
                                {
                                    text: model.expanded ? "" : ""
                                    font.family: "Segoe MDL2 Assets"; font.pixelSize: Math.round(9 * win.k)
                                    color: win.c.text2; anchors.verticalCenter: parent.verticalCenter
                                }
                                Text
                                {
                                    id: statusIcon
                                    property real angle: 0
                                    rotation: model.status === "running" ? angle : 0
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: model.status === "ok" ? "" : model.status === "error" ? "" : ""
                                    font.family: "Segoe MDL2 Assets"; font.pixelSize: Math.round(11 * win.k)
                                    color: model.status === "ok" ? win.c.ok : model.status === "error" ? win.c.danger : win.c.warn
                                    NumberAnimation on angle { running: model.status === "running"; from: 0; to: 360; duration: 1200; loops: Animation.Infinite }
                                }
                                Text { text: model.text; color: win.c.text2; font.pixelSize: Math.round(12 * win.k); anchors.verticalCenter: parent.verticalCenter }
                                Text { text: model.status === "running" ? "" : model.duration; color: win.c.text2; opacity: 0.7
                                       font.pixelSize: Math.round(11 * win.k); anchors.verticalCenter: parent.verticalCenter }
                            }
                            MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor
                                        onClicked: msgs.setProperty(index, "expanded", !model.expanded) }
                        }
                        Rectangle
                        {
                            visible: row.isTool && model.expanded
                            width: parent.width
                            height: visible ? toolDetail.implicitHeight + 12 * win.k : 0
                            radius: 4 * win.k
                            color: win.c.code
                            border.width: 1
                            border.color: win.c.border
                            TextEdit
                            {
                                id: toolDetail
                                x: 8 * win.k; y: 6 * win.k
                                width: parent.width - 16 * win.k
                                readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap
                                font.family: "Consolas"; font.pixelSize: Math.round(11 * win.k)
                                color: win.c.text2
                                text: model.extra.length > 3000 ? model.extra.substring(0, 3000) + "\n…" : model.extra
                            }
                        }

                        // карточка итога нарезки
                        Rectangle
                        {
                            visible: model.kind === "result"
                            width: parent.width
                            height: visible ? resultCol.implicitHeight + 16 * win.k : 0
                            radius: 6 * win.k
                            color: win.c.okBg
                            Rectangle { width: 3 * win.k; height: parent.height; color: win.c.ok; radius: 2 }
                            Column
                            {
                                id: resultCol
                                x: 14 * win.k; y: 8 * win.k
                                width: parent.width - 24 * win.k
                                spacing: 3 * win.k
                                Text { text: "Слайсинг завершён"; font.bold: true; color: win.c.ok; font.pixelSize: win.fs }
                                TextEdit
                                {
                                    width: parent.width
                                    readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap
                                    textFormat: TextEdit.RichText
                                    color: win.c.text
                                    font.pixelSize: win.fs
                                    text: model.kind === "result" ? model.text : ""
                                }
                            }
                        }

                        // карточка ошибки
                        Rectangle
                        {
                            visible: model.kind === "error"
                            width: parent.width
                            height: visible ? errCol.implicitHeight + 16 * win.k : 0
                            radius: 6 * win.k
                            color: win.c.dangerBg
                            Rectangle { width: 3 * win.k; height: parent.height; color: win.c.danger; radius: 2 }
                            Column
                            {
                                id: errCol
                                x: 14 * win.k; y: 8 * win.k
                                width: parent.width - 24 * win.k
                                spacing: 4 * win.k
                                Text { visible: row.errTitle !== ""; text: row.errTitle; font.bold: true; color: win.c.danger; font.pixelSize: win.fs }
                                TextEdit
                                {
                                    width: parent.width
                                    readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap
                                    color: win.c.text
                                    font.pixelSize: win.fs
                                    text: model.kind === "error" ? model.text : ""
                                }
                                TextButton
                                {
                                    visible: row.errKind === "max_iterations" || row.errKind === "length" || row.errKind === "stream_interrupted"
                                    label: "Продолжить"
                                    primary: true
                                    bg: win.c.accentBtn
                                    fontSize: Math.round(12 * win.k)
                                    enabled: !win.running
                                    onClicked: win.sendMessage("продолжай")
                                }
                            }
                        }

                        // служебная пометка
                        Text
                        {
                            visible: model.kind === "notice"
                            width: parent.width
                            height: visible ? implicitHeight : 0
                            horizontalAlignment: Text.AlignHCenter
                            text: model.kind === "notice" ? model.text : ""
                            color: win.c.text2
                            font.pixelSize: Math.round(12 * win.k)
                        }
                    }
                }
            }

            footer: Item
            {
                width: list.width
                height: win.running ? 34 * win.k : 0
                Row
                {
                    visible: win.running
                    x: 14 * win.k
                    y: 12 * win.k
                    spacing: 6 * win.k
                    Repeater
                    {
                        model: 3
                        Rectangle
                        {
                            width: 6 * win.k; height: 6 * win.k; radius: width / 2; color: win.c.text2
                            SequentialAnimation on opacity
                            {
                                running: win.running
                                loops: Animation.Infinite
                                PauseAnimation { duration: index * 180 }
                                NumberAnimation { from: 0.25; to: 1; duration: 350 }
                                NumberAnimation { from: 1; to: 0.25; duration: 350 }
                                PauseAnimation { duration: (2 - index) * 180 }
                            }
                        }
                    }
                }
            }

            // тонкая полоса прокрутки
            Rectangle
            {
                visible: list.contentHeight > list.height
                anchors.right: parent.right
                width: 6 * win.k
                radius: width / 2
                color: win.c.text2
                opacity: 0.35
                y: list.visibleArea.yPosition * list.height
                height: Math.max(24, list.visibleArea.heightRatio * list.height)
            }
        }

        // Пустой диалог
        Column
        {
            visible: msgs.count === 0 && win.online
            anchors { left: parent.left; right: parent.right; top: banner.bottom; topMargin: 56 * win.k; leftMargin: 20 * win.k; rightMargin: 20 * win.k }
            spacing: 10 * win.k
            Text { width: parent.width; text: "Чем помочь с печатью?"; color: win.c.text; font.pixelSize: Math.round(18 * win.k); font.bold: true }
            Text
            {
                width: parent.width
                wrapMode: Text.Wrap
                color: win.c.text2
                font.pixelSize: win.fs
                text: "Опишите деталь и задачу, приложите STL/OBJ/3MF, фото или ТЗ. Ассистент подберёт режим 3D/5D, профиль, материал и настройки, разместит модель и запустит слайсинг."
            }
            Repeater
            {
                model: ["Кронштейн под нагрузку, PETG, без поддержек", "Покажи текущую конфигурацию принтера", "Какие режимы печати есть и когда нужен 5D?"]
                Rectangle
                {
                    width: parent.width
                    height: sug.implicitHeight + 14 * win.k
                    radius: 6 * win.k
                    color: sugArea.containsMouse ? win.c.surface2 : win.c.surface
                    border.width: 1
                    border.color: win.c.border
                    Text { id: sug; x: 12 * win.k; width: parent.width - 24 * win.k; anchors.verticalCenter: parent.verticalCenter
                           wrapMode: Text.Wrap; text: modelData; color: win.c.text; font.pixelSize: win.fs }
                    MouseArea { id: sugArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                onClicked: { input.text = modelData; input.forceActiveFocus(); } }
                }
            }
        }

        // Прогресс нарезки
        Item
        {
            id: slicingBar
            anchors { left: parent.left; right: parent.right; bottom: composer.top }
            height: win.slicingProgress >= 0 ? 34 * win.k : 0
            visible: win.slicingProgress >= 0
            Column
            {
                anchors { left: parent.left; right: parent.right; leftMargin: 14 * win.k; rightMargin: 14 * win.k; verticalCenter: parent.verticalCenter }
                spacing: 4 * win.k
                Text { text: win.slicingLabel + "  " + Math.round(Math.max(0, win.slicingProgress) * 100) + "%"; color: win.c.text2; font.pixelSize: Math.round(12 * win.k) }
                Rectangle
                {
                    width: parent.width; height: 3 * win.k; radius: height / 2; color: win.c.surface2
                    Rectangle { width: parent.width * Math.max(0, win.slicingProgress); height: parent.height; radius: height / 2; color: win.c.accent }
                }
            }
        }

        // Поле ввода
        Item
        {
            id: composer
            anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
            height: box.height + 20 * win.k

            Rectangle
            {
                id: box
                x: 12 * win.k
                y: 6 * win.k
                width: parent.width - 24 * win.k
                height: boxCol.implicitHeight + 16 * win.k
                radius: 8 * win.k
                color: win.c.surface
                border.width: 1
                border.color: input.activeFocus ? win.c.accent : win.c.border

                Column
                {
                    id: boxCol
                    x: 10 * win.k; y: 8 * win.k
                    width: parent.width - 20 * win.k
                    spacing: 6 * win.k

                    Flow
                    {
                        width: parent.width
                        spacing: 6 * win.k
                        visible: atts.count > 0
                        Repeater
                        {
                            model: atts
                            Rectangle
                            {
                                height: 22 * win.k
                                width: chipText.implicitWidth + 40 * win.k
                                radius: 4 * win.k
                                color: win.c.surface2
                                border.width: 1
                                border.color: model.status === "done" ? win.c.accent : win.c.border
                                Spinner { x: 7 * win.k; anchors.verticalCenter: parent.verticalCenter; size: 12 * win.k
                                          visible: model.status === "uploading"; running: visible; color: win.c.accent }
                                Text { x: 7 * win.k; anchors.verticalCenter: parent.verticalCenter; visible: model.status === "done"
                                       text: ""; font.family: "Segoe MDL2 Assets"; font.pixelSize: Math.round(10 * win.k); color: win.c.ok }
                                Text { id: chipText; x: 24 * win.k; anchors.verticalCenter: parent.verticalCenter; color: win.c.text
                                       font.pixelSize: Math.round(11 * win.k); text: model.name }
                                Text { anchors { right: parent.right; rightMargin: 6 * win.k; verticalCenter: parent.verticalCenter }
                                       text: ""; font.family: "Segoe MDL2 Assets"; font.pixelSize: Math.round(8 * win.k); color: win.c.text2 }
                                MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: { atts.remove(index); win.refreshUploading(); } }
                            }
                        }
                    }

                    Flickable
                    {
                        id: inputFlick
                        width: parent.width
                        height: Math.min(Math.max(input.contentHeight, win.fs * 1.5 + 4), 150 * win.k)
                        contentWidth: width
                        contentHeight: input.contentHeight
                        clip: true
                        boundsBehavior: Flickable.StopAtBounds
                        function ensureVisible(r)
                        {
                            if (contentY >= r.y) { contentY = r.y; }
                            else if (contentY + height <= r.y + r.height) { contentY = r.y + r.height - height; }
                        }
                        TextEdit
                        {
                            id: input
                            width: inputFlick.width
                            wrapMode: TextEdit.Wrap
                            color: win.c.text
                            selectionColor: win.c.accent
                            font.pixelSize: win.fs
                            selectByMouse: true
                            onCursorRectangleChanged: inputFlick.ensureVisible(cursorRectangle)
                            Keys.onPressed:
                            {
                                if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) && !(event.modifiers & Qt.ShiftModifier))
                                {
                                    win.sendMessage(input.text);
                                    event.accepted = true;
                                }
                            }
                            Text
                            {
                                visible: input.text.length === 0
                                text: "Опишите задачу…  (Enter — отправить, Shift+Enter — новая строка)"
                                color: win.c.text2
                                font.pixelSize: win.fs
                            }
                        }
                    }

                    Item
                    {
                        width: parent.width
                        height: 28 * win.k
                        IconButton { anchors.left: parent.left; size: 28 * win.k; glyph: ""; tip: "Прикрепить файл"; fg: win.c.text2; hoverColor: win.c.hover
                                     onClicked: win.addFiles() }
                        Text
                        {
                            anchors { left: parent.left; leftMargin: 36 * win.k; verticalCenter: parent.verticalCenter }
                            text: win.usageText
                            color: win.c.text2
                            font.pixelSize: Math.round(11 * win.k)
                        }
                        Rectangle
                        {
                            anchors.right: parent.right
                            width: 28 * win.k; height: 28 * win.k; radius: width / 2
                            color: win.running ? win.c.danger : (sendArea.containsMouse ? Qt.lighter(win.c.accentBtn, 1.15) : win.c.accentBtn)
                            opacity: (win.running || input.text.trim().length > 0 || atts.count > 0) ? 1.0 : 0.45
                            Spinner { anchors.centerIn: parent; size: 14 * win.k; color: "white"; visible: win.uploadingNow; running: visible }
                            Text
                            {
                                visible: !win.uploadingNow
                                anchors.centerIn: parent
                                text: win.running ? "" : ""
                                font.family: "Segoe MDL2 Assets"; font.pixelSize: Math.round(12 * win.k)
                                color: "white"
                            }
                            MouseArea { id: sendArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                        onClicked: win.sendMessage(input.text) }
                        }
                    }
                }
            }
        }

        // Настройки
        Rectangle
        {
            id: settingsView
            visible: win.settingsOpen
            z: 25
            anchors { left: parent.left; right: parent.right; top: header.bottom; bottom: parent.bottom; leftMargin: 1; rightMargin: 1; bottomMargin: 1 }
            color: win.c.bg

            Flickable
            {
                id: settingsFlick
                anchors.fill: parent
                contentWidth: width
                contentHeight: settingsCol.implicitHeight + 32 * win.k
                clip: true
                boundsBehavior: Flickable.StopAtBounds

                Column
                {
                    id: settingsCol
                    x: 16 * win.k
                    y: 14 * win.k
                    width: settingsFlick.width - 32 * win.k
                    spacing: 12 * win.k

                    Row
                    {
                        spacing: 8 * win.k
                        IconButton { size: 26 * win.k; glyph: ""; tip: "Назад к чату"; fg: win.c.text; hoverColor: win.c.hover
                                     onClicked: win.settingsOpen = false }
                        Text { anchors.verticalCenter: parent.verticalCenter; text: "Настройки"; color: win.c.text
                               font.pixelSize: Math.round(16 * win.k); font.bold: true }
                    }

                    Text { text: "ПОДКЛЮЧЕНИЕ К LLM"; color: win.c.text2; font.pixelSize: Math.round(11 * win.k); font.bold: true }
                    FormField { id: fBaseUrl; width: parent.width; c: win.c; k: win.k; label: "Адрес API (OpenAI-совместимый)"
                                placeholder: "https://…/v1"; hint: "С /v1 на конце: запросы идут на …/chat/completions." }
                    FormField { id: fApiKey; width: parent.width; c: win.c; k: win.k; label: "Ключ API"; password: true
                                placeholder: "sk-…"; hint: "Хранится в настройках слайсера. Показана маска — оставьте её, чтобы не менять ключ." }
                    FormField { id: fModel; width: parent.width; c: win.c; k: win.k; label: "Модель"
                                hint: "Модель должна поддерживать вызов инструментов (tools)." }
                    Flow
                    {
                        width: parent.width
                        spacing: 6 * win.k
                        TextButton { label: "Загрузить список моделей"; fontSize: Math.round(12 * win.k); textColor: win.c.text; border2: win.c.border
                                     enabled: !win.settingsBusy; onClicked: win.loadModels() }
                        Repeater
                        {
                            model: modelChoices
                            Rectangle
                            {
                                height: 24 * win.k
                                width: mText.implicitWidth + 16 * win.k
                                radius: 12 * win.k
                                color: model.name === fModel.text ? win.c.accentBtn : (mArea.containsMouse ? win.c.surface2 : win.c.surface)
                                border.width: 1
                                border.color: win.c.border
                                Text { id: mText; anchors.centerIn: parent; text: model.name; font.pixelSize: Math.round(11 * win.k)
                                       color: model.name === fModel.text ? "white" : win.c.text }
                                MouseArea { id: mArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                            onClicked: fModel.text = model.name }
                            }
                        }
                    }

                    Row
                    {
                        spacing: 8 * win.k
                        TextButton { label: "Проверить подключение"; fontSize: Math.round(12 * win.k); textColor: win.c.text; border2: win.c.border
                                     enabled: !win.settingsBusy; onClicked: win.testSettings() }
                        Spinner { anchors.verticalCenter: parent.verticalCenter; size: 14 * win.k; color: win.c.accent; visible: win.settingsBusy; running: visible }
                    }
                    Text
                    {
                        visible: win.settingsStatus !== ""
                        width: parent.width
                        wrapMode: Text.Wrap
                        text: win.settingsStatus
                        color: win.settingsStatusOk ? win.c.ok : win.c.danger
                        font.pixelSize: Math.round(12 * win.k)
                    }

                    Text { text: "РАБОТА АГЕНТА"; color: win.c.text2; font.pixelSize: Math.round(11 * win.k); font.bold: true }
                    Grid
                    {
                        id: numGrid
                        width: parent.width
                        columns: 2
                        columnSpacing: 12 * win.k
                        rowSpacing: 10 * win.k
                        property real cell: (width - columnSpacing) / 2
                        FormField { id: fTemperature; width: numGrid.cell; c: win.c; k: win.k; numeric: true; label: "Температура"; hint: "0–2; ниже — стабильнее вызовы." }
                        FormField { id: fIterations; width: numGrid.cell; c: win.c; k: win.k; numeric: true; label: "Макс. шагов агента"; hint: "Вызовов LLM на сообщение (1–100)." }
                        FormField { id: fTimeout; width: numGrid.cell; c: win.c; k: win.k; numeric: true; label: "Таймаут запроса, с"; hint: "10–1800." }
                        FormField { id: fRetries; width: numGrid.cell; c: win.c; k: win.k; numeric: true; label: "Повторы при сбоях"; hint: "429/5xx/сеть, 0–10." }
                        FormField { id: fMaxTokens; width: numGrid.cell; c: win.c; k: win.k; numeric: true; label: "Макс. токенов ответа"; hint: "0 — без ограничения." }
                        FormField { id: fImageSide; width: numGrid.cell; c: win.c; k: win.k; numeric: true; label: "Размер фото, px"; hint: "Большая сторона уменьшается до него." }
                        FormField { id: fPort; width: numGrid.cell; c: win.c; k: win.k; numeric: true; label: "Порт API и MCP"; hint: "Занят — берётся свободный; после перезапуска." }
                    }
                    FormCheck { id: cAllTools; width: parent.width; c: win.c; k: win.k; label: "Все инструменты сразу"
                                hint: "Выключено — основной набор, остальные модель подключает сама (короче запрос)." }
                    FormCheck { id: cStream; width: parent.width; c: win.c; k: win.k; label: "Потоковый ответ (stream)" }
                    FormCheck { id: cParallel; width: parent.width; c: win.c; k: win.k; label: "Параллельные вызовы инструментов"
                                hint: "Если шлюз не принимает параметр, он отключается автоматически." }
                    FormCheck { id: cReasoning; width: parent.width; c: win.c; k: win.k; label: "Показывать рассуждения модели" }
                    FormCheck { id: cSsl; width: parent.width; c: win.c; k: win.k; label: "Проверять SSL-сертификат"
                                hint: "Отключайте только для серверов с самоподписанным сертификатом." }

                    Row
                    {
                        spacing: 8 * win.k
                        TextButton { label: "Сохранить"; primary: true; bg: win.c.accentBtn; fontSize: Math.round(12 * win.k)
                                     enabled: !win.settingsBusy; onClicked: win.saveSettings() }
                        TextButton { label: "Закрыть"; fontSize: Math.round(12 * win.k); textColor: win.c.text; border2: win.c.border
                                     onClicked: win.settingsOpen = false }
                    }
                    Text
                    {
                        width: parent.width
                        wrapMode: Text.Wrap
                        color: win.c.text2
                        font.pixelSize: Math.round(11 * win.k)
                        text: "MCP-сервер для внешних клиентов: " + (win.baseUrl ? win.baseUrl + "/mcp" : "запустится при открытии чата")
                    }
                }
            }
        }

        // Список диалогов
        Rectangle
        {
            visible: win.historyOpen
            z: 30
            anchors { top: header.bottom; right: parent.right; rightMargin: 8 * win.k }
            width: Math.min(parent.width - 16 * win.k, 340 * win.k)
            height: Math.min(histList.contentHeight + 8 * win.k, 320 * win.k)
            radius: 6 * win.k
            color: win.c.surface
            border.width: 1
            border.color: win.c.border
            ListView
            {
                id: histList
                anchors { fill: parent; margins: 4 * win.k }
                clip: true
                model: hist
                delegate: Rectangle
                {
                    width: histList.width
                    height: 28 * win.k
                    radius: 4 * win.k
                    color: hArea.containsMouse ? win.c.hover : (model.sid === win.sessionId ? win.c.surface2 : "transparent")
                    Text
                    {
                        x: 8 * win.k; width: parent.width - 16 * win.k; anchors.verticalCenter: parent.verticalCenter
                        elide: Text.ElideRight; color: win.c.text; font.pixelSize: Math.round(12 * win.k)
                        text: (model.busy ? "● " : "") + model.title
                    }
                    MouseArea
                    {
                        id: hArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: { win.historyOpen = false; if (model.sid !== win.sessionId) { win.loadSession(model.sid); } }
                    }
                }
            }
        }

        // Всплывающее сообщение
        Rectangle
        {
            visible: win.toastText !== ""
            z: 40
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.bottom: composer.top
            anchors.bottomMargin: 8 * win.k
            width: Math.min(parent.width - 40 * win.k, toastLabel.implicitWidth + 24 * win.k)
            height: toastLabel.implicitHeight + 14 * win.k
            radius: 6 * win.k
            color: "#303031"
            border.width: 1
            border.color: "#454545"
            Text { id: toastLabel; anchors.centerIn: parent; width: parent.width - 24 * win.k; wrapMode: Text.Wrap; color: "#e6e6e6"
                   text: win.toastText; font.pixelSize: Math.round(12 * win.k) }
        }
    }
}
