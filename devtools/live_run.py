"""Прогон агента с настоящей LLM на 7 тестовых моделях (стенд с моком или сам слайсер).

    python devtools/live_run.py --url http://127.0.0.1:8765 [--only 05] [--out DIR]

Сначала запустите стенд (devtools/run_standalone.py) или откройте чат в слайсере. Для
каждой модели запрос пользователя берётся из tests/scenarios.md, STL (и фото, если о нём
говорит сценарий) загружается вложением, ход отслеживается через /api/events до `done`,
затем сохранённый проект сверяется с devtools/targets/<модель>.json. Модели кейса должны
лежать в папке models/ (см. models/README.md).

Результат: сводная таблица и по каждой модели <out>/<цель>.md с цепочкой вызовов,
ошибками, расходом токенов и итоговым ответом.

Пока идёт прогон, в чат слайсера не пишите: второй ход в той же сцене испортит результат.
"""

import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(HERE)
TARGETS_DIR = os.path.join(HERE, "targets")
SCENARIOS = os.path.join(REPO_DIR, "tests", "scenarios.md")
sys.path.insert(0, HERE)

import check_3mf  # noqa: E402
from inspect_3mf import load as load_project  # noqa: E402

HEADERS = {"X-AI-Assistant": "1"}


class Stand(object):
    def __init__(self, url):
        self.url = url.rstrip("/")

    def request(self, method, path, body=None, query=None, raw=None, timeout=60):
        url = self.url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = dict(HEADERS)
        if raw is not None:
            data = raw
            headers["Content-Type"] = "application/octet-stream"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            data = None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def new_session(self):
        return self.request("POST", "/api/sessions", body={})["session"]["id"]

    def upload(self, session, path):
        with open(path, "rb") as handle:
            data = handle.read()
        result = self.request("POST", "/api/upload", raw=data,
                              query={"session": session, "name": os.path.basename(path)}, timeout=120)
        return result["attachment"]["id"]

    def send(self, session, text, attachments):
        return self.request("POST", "/api/message", body={"session": session, "text": text,
                                                          "attachments": attachments})

    def events(self, session, since):
        return self.request("GET", "/api/events", query={"session": session, "since": since, "timeout": 25},
                            timeout=40)


def scenario_messages():
    """номер модели -> (текст запроса, нужно ли фото)."""
    result = {}
    current = None
    with open(SCENARIOS, encoding="utf-8") as handle:
        for line in handle:
            heading = re.match(r"^## (\d)\. ", line)
            if heading:
                current = int(heading.group(1))
            elif line.startswith("## "):
                current = None
            elif current and line.startswith("**Пользователь:**") and current not in result:
                text = re.search(r"«(.+?)»", line).group(1)
                result[current] = (text, ".jpg" in line and "опционально" not in line)
    return result


def run_model(stand, target_path, message, want_photo, max_wait):
    with open(target_path, encoding="utf-8") as handle:
        target = json.load(handle)
    stl = os.path.join(REPO_DIR, target["stl"])
    session = stand.new_session()
    attachments = [stand.upload(session, stl)]
    photos = glob.glob(os.path.join(os.path.dirname(stl), "*.jpg"))
    if want_photo and photos:
        attachments.append(stand.upload(session, photos[0]))
    started = time.time()
    stand.send(session, message, attachments)
    since, calls, errors, answer, usage, done = 0, [], [], [], [0, 0], None
    pending = {}
    while time.time() - started < max_wait and done is None:
        batch = stand.events(session, since)
        for event in batch["events"]:
            kind = event["type"]
            if kind == "tool_call_started":
                pending[event["call_id"]] = {"name": event["name"], "args": event.get("arguments")}
            elif kind == "tool_call_result":
                call = pending.pop(event["call_id"], {"name": event.get("name"), "args": None})
                call.update(ok=event.get("ok"), result=event.get("result"), ms=event.get("duration_ms"))
                calls.append(call)
            elif kind == "assistant_delta":
                answer.append(event.get("text", ""))
            elif kind == "error":
                errors.append("%s: %s" % (event.get("kind"), event.get("message")))
            elif kind == "usage":
                # накопительно за диалог
                usage[0] = event.get("prompt_tokens") or usage[0]
                usage[1] = event.get("completion_tokens") or usage[1]
            elif kind == "done":
                done = event.get("reason")
        since = batch.get("last_seq", since)
    project = None
    for call in calls:
        if call["name"] == "save_project" and call.get("ok"):
            try:
                project = json.loads(call["result"]).get("path")
            except (TypeError, ValueError):
                match = re.search(r'"path":\s*"([^"]+)"', call.get("result") or "")
                project = match.group(1) if match else None
    rows = check_3mf.check_target(load_project(project, None), target) if project and os.path.isfile(project) else []
    return {"done": done, "seconds": round(time.time() - started), "calls": calls, "errors": errors,
            "answer": "".join(answer), "usage": usage, "project": project, "rows": rows}


def write_report(path, name, message, res):
    lines = ["# %s" % name, "", "**Запрос:** %s" % message, "",
             "Итог: %s, %d с, вызовов %d, токены prompt/completion %d/%d" % (
                 res["done"], res["seconds"], len(res["calls"]), res["usage"][0], res["usage"][1]), ""]
    if res["rows"]:
        lines += ["## Проверка 3MF: %.0f%%" % check_3mf.total_score(res["rows"]), ""]
        for row in res["rows"]:
            lines.append("- %s %s: %s %s" % (row.get("status"), row.get("id"), row.get("actual"), row.get("note") or ""))
        lines.append("")
    lines += ["## Вызовы", ""]
    for i, call in enumerate(res["calls"], 1):
        lines.append("%d. %s `%s` %s" % (i, "✓" if call.get("ok") else "✗", call["name"],
                                          json.dumps(call.get("args"), ensure_ascii=False)[:300]))
        if not call.get("ok"):
            lines.append("   → %s" % (call.get("result") or "")[:400])
    if res["errors"]:
        lines += ["", "## Ошибки агента", ""] + ["- " + e for e in res["errors"]]
    lines += ["", "## Ответ", "", res["answer"]]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--only", help="только модели, у которых имя файла цели начинается с этого префикса")
    parser.add_argument("--out", default=os.path.join(REPO_DIR, "live_runs"))
    parser.add_argument("--max-wait", type=int, default=900, help="секунд на одну модель")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.out):
        os.makedirs(args.out)
    stand = Stand(args.url)
    messages = scenario_messages()
    summary = []
    for name in sorted(os.listdir(TARGETS_DIR)):
        if not name.endswith(".json") or (args.only and not name.startswith(args.only)):
            continue
        number = int(name[:2])
        message, want_photo = messages[number]
        res = run_model(stand, os.path.join(TARGETS_DIR, name), message, want_photo, args.max_wait)
        base = os.path.splitext(name)[0]
        write_report(os.path.join(args.out, base + ".md"), base, message, res)
        failed = sum(1 for c in res["calls"] if not c.get("ok"))
        score = "%.0f%%" % check_3mf.total_score(res["rows"]) if res["rows"] else "нет 3MF"
        summary.append((base, res["done"], res["seconds"], len(res["calls"]), failed, res["usage"][0], score))
        print("%-22s %-14s %4ds calls=%-3d failed=%-2d prompt_tok=%-7d 3MF %s" % summary[-1], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
