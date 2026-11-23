"""Локальный разбор штампов ЕСКД. Python 3.11+ / Windows."""
import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from urllib import error, request

import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter

HERE = Path(__file__).resolve().parent
API = "http://127.0.0.1:11434"
HTTP = request.build_opener(request.ProxyHandler({}))
# Bump when page recognition changes. The initial value preserves existing registries.
RECOGNITION_REVISION = "a4f84dd3984cc3a933541c0b6d7855ca0a6d3455cc4f7dd2775502275cc041f5"
FIELDS = ("designation", "sheet", "total")
STATES = ["read", "blank", "absent", "unreadable", "ambiguous"]
BLOCKS = ["full_drawing", "short", "specification", "other", "unclear",
          "no_block", "multiple_blocks", "template"]


def obj(**props):
    return dict(type="object", properties=props, required=list(props), additionalProperties=False)


field = obj(raw={"type": ["string", "null"]}, state={"enum": STATES})
READ = obj(block={"enum": BLOCKS}, **dict.fromkeys(FIELDS, field), note={"type": "string"})
LOCATE = obj(box={"type": "array", "minItems": 4, "maxItems": 4,
                  "items": {"type": "integer", "minimum": 0, "maximum": 1000}},
             clockwise={"enum": [0, 90, 180, 270]}, found={"type": "boolean"})
LOC_PROMPT = """На обзоре найди основную надпись ЕСКД. Верни found и box:
[лево,верх,право,низ] всего штампа в системе 0..1000 на исходной картинке,
начало вверху слева. clockwise — на сколько градусов по часовой стрелке
повернуть вырезку для чтения. Если нет единственного штампа, found=false."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def ask(model, prompt, images, schema):
    payload = dict(model=model, stream=False, format=schema, options={"temperature": 0},
                   messages=[dict(role="user", content=prompt, images=[
                       base64.b64encode(b).decode("ascii") for b in images])])
    req = request.Request(API + "/api/chat", json.dumps(payload).encode(),
                          {"Content-Type": "application/json"})
    try:
        with HTTP.open(req, timeout=600) as res:
            return json.loads(json.load(res)["message"]["content"])
    except error.HTTPError as exc:
        raise RuntimeError(f"Ollama HTTP {exc.code}: {exc.read(600).decode(errors='replace')}") from exc


def render(page, scale, crop=(0, 0, 0, 0), clockwise=0):
    bitmap = page.render(scale=scale, crop=crop)
    try:
        image = bitmap.to_pil().convert("RGB")
        if clockwise:
            image = image.rotate(-clockwise, expand=True)
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        return stream.getvalue()
    finally:
        bitmap.close()


def inspect_page(page, model, prompt, stem, tesseract, query=ask):
    w, h = page.get_size()
    overview = render(page, 1250 / max(w, h))
    stem.with_suffix(".page.png").write_bytes(overview)
    pos = query(model, LOC_PROMPT, [overview], LOCATE)
    if pos["found"] is not True:
        raise ValueError("Не найден единственный штамп. Откройте страницу.")
    l, t, r, b = pos["box"]
    if not (0 <= l < r <= 1000 and 0 <= t < b <= 1000 and pos["clockwise"] in (0, 90, 180, 270)):
        raise ValueError("Некорректные координаты штампа")
    # Margin around the detected grid, in units of the overview.
    x0, x1 = max(0, l-8)*w/1000, min(1000, r+8)*w/1000
    y0, y1 = max(0, t-8)*h/1000, min(1000, b+8)*h/1000
    crop = (x0, h-y1, w-x1, y0)
    stamp = render(page, min(6, 2600/max(x1-x0, y1-y0)), crop, pos["clockwise"])
    path = stem.with_suffix(".stamp.png")
    path.write_bytes(stamp)
    raw = query(model, prompt, [overview, stamp], READ)
    if raw["block"] not in BLOCKS or not isinstance(raw["note"], str):
        raise ValueError("Неверная структура ответа Qwen")
    for key in FIELDS:
        f = raw[key]
        if f["state"] not in STATES or (f["state"] == "read" and
                (not isinstance(f["raw"], str) or not f["raw"].strip())):
            raise ValueError(f"Некорректное поле {key}")
        if f["state"] != "read" and f["raw"] is not None:
            raise ValueError(f"Лишнее значение в поле {key}")
    raw["box"] = pos
    evidence = ""
    if page.get_rotation() == 0 and pos["clockwise"] == 0:
        left, bottom, right, top = page.get_bbox()
        txt = page.get_textpage()
        try:
            evidence = txt.get_text_bounded(left+x0, top-y1, left+x1, top-y0).strip()
        finally:
            txt.close()
    raw["layer"] = evidence
    if tesseract:
        proc = subprocess.run([tesseract, str(path), "stdout", "-l", "rus+eng", "--psm", "6"],
                              capture_output=True, timeout=120, encoding="utf-8", errors="replace")
        if proc.returncode:
            raise ValueError("Сбой Tesseract: " + proc.stderr[-300:])
        raw["ocr"] = proc.stdout.strip()
    return raw


def config(db, key, value=None):
    if value is not None:
        db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else ""


def inventory(root):
    paths = []
    def fail(exc):
        raise exc
    for base, _, names in os.walk(root, onerror=fail):
        paths.extend(Path(base, n) for n in names if n.lower().endswith(".pdf"))
    return sorted(paths)


def expected(path):
    m = re.fullmatch(r"(.*?)(?:\s*\(([0-9]+)\))?", Path(path).stem)
    return m[1].strip(), int(m[2]) if m[2] else None


def scan(db, job, a, client=None, progress=print):
    root = Path(a.source).resolve(strict=True)
    if not root.is_dir() or job.is_relative_to(root) or root.is_relative_to(job):
        raise ValueError("Выберите две отдельные папки для архива и результата.")
    if config(db, "root") not in ("", str(root)):
        raise ValueError("Эта рабочая папка уже относится к другому архиву.")
    prompt = (HERE / "Инструкция_для_локальной_модели.txt").read_text(encoding="utf-8-sig")
    if client:
        backend = client.identity
    else:
        with HTTP.open(API + "/api/tags", timeout=10) as response:
            models = json.load(response)["models"]
        selected = next((m for m in models if m["name"] in (a.model, a.model + ":latest")), None)
        if not selected:
            raise ValueError(f"Модель отсутствует в Ollama. Выполните ollama pull {a.model}")
        backend = selected["digest"]
    signature = digest((prompt + backend + str(a.tesseract) + RECOGNITION_REVISION).encode())
    paths = inventory(root)
    config(db, "complete", "0")
    config(db, "root", str(root))
    db.execute("DELETE FROM files")
    db.commit()
    (job / "images").mkdir(exist_ok=True)
    for index, path in enumerate(paths, 1):
        rel = str(path.relative_to(root))
        progress(f"[{index}/{len(paths)}] {rel}")
        sha, count, problem = "", 0, ""
        try:
            data = path.read_bytes()
            sha = digest(data)
            with pdfium.PdfDocument(data) as doc:
                count = len(doc)
                if count == 0:
                    raise ValueError("PDF без страниц")
                for n in range(1, count+1):
                    key = digest(json.dumps([rel, sha, n, signature]).encode())
                    old = db.execute("SELECT problem,decision FROM pages WHERE id=?", (key,)).fetchone()
                    if old and (not old["problem"] or old["decision"]):
                        continue
                    problem_page, raw = "", {}
                    try:
                        page = doc[n-1]
                        try:
                            raw = inspect_page(page, a.model, prompt, job/"images"/key, a.tesseract,
                                               client.ask if client else ask)
                        finally:
                            page.close()
                    except Exception as exc:
                        problem_page = str(exc)
                        progress(f"  Страница {n}: {exc}")
                    db.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (key, rel, sha, signature, n, json.dumps(raw, ensure_ascii=False),
                                problem_page, "", "", ""))
                    db.commit()
        except (OSError, ValueError, pdfium.PdfiumError) as exc:
            problem = str(exc)
        db.execute("INSERT INTO files VALUES (?,?,?,?,?)", (rel, sha, signature, count, problem))
        db.commit()
    config(db, "complete", "1")
    db.commit()


def number(s):
    s = str(s or "").strip()
    return int(s) if re.fullmatch(r"[0-9]{1,5}", s) and int(s) > 0 else None


def analyse(db):
    rows = []
    groups = defaultdict(list)
    for f in db.execute("SELECT * FROM files ORDER BY path"):
        name, hint = expected(f["path"])
        pages = list(db.execute("SELECT * FROM pages WHERE path=? AND sha=? AND sig=? ORDER BY n",
                                (f["path"], f["sha"], f["sig"])))
        if f["problem"] or len(pages) != f["count"]:
            pages.append(dict(id="", n=0, raw="{}", problem=f["problem"] or "Неполный разбор",
                              decision="", edited="", comment=""))
        for p in pages:
            raw = json.loads(p["raw"])
            d, s, total = [raw.get(k, {}).get("raw") for k in FIELDS]
            sheet, qty = number(s), number(total)
            reasons = []
            if p["problem"]:
                reasons.append(p["problem"])
            block = raw.get("block")
            if (block in ("full_drawing", "specification") and qty == 1
                    and raw.get("sheet", {}).get("state") == "blank"):
                sheet = 1
            elif block not in ("full_drawing", "short"):
                reasons.append("Тип штампа требует проверки")
            if not d or d != name:
                reasons.append("Обозначение расходится с именем или не прочитано")
            if not sheet or (sheet == 1 and not qty):
                reasons.append("Не подтверждён номер или итог первого листа")
            if raw.get("total", {}).get("state") in ("ambiguous", "unreadable") or (total and not qty):
                reasons.append("Проверить графу Листов")
            if hint is not None and (f["count"] != 1 or sheet != hint):
                reasons.append("Проверить суффикс имени")
            if raw.get("note", "").strip():
                reasons.append(raw["note"])
            for k in ("layer", "ocr"):
                if d and (raw.get(k) or k == "ocr" and k in raw):
                    if "".join(d.split()) not in "".join(raw[k].split()):
                        reasons.append("Расхождение с " + ("текстовым слоем" if k=="layer" else "OCR"))
            original = " | ".join(reasons)
            if p["decision"] == "accept":
                d, sheet, qty = json.loads(p["edited"])
                reasons = []
            row = dict(id=p["id"], path=f["path"], sha=f["sha"], n=p["n"], name=name,
                       hint=hint, d=d, sheet=sheet, qty=qty, raw=raw, reasons=reasons,
                       original=original, decision=p["decision"], comment=p["comment"])
            rows.append(row)
            if p["decision"] != "skip":
                groups[d or name].append(row)
                if name != d and d and reasons:
                    groups[name].append(row)
    plans = {}
    for d, candidates in groups.items():
        own = [r for r in candidates if r["d"] == d]
        by_sheet = defaultdict(list)
        for r in own:
            if r["sheet"]:
                by_sheet[r["sheet"]].append(r)
        totals = {r["qty"] for r in by_sheet.get(1, []) if r["qty"]}
        qty = next(iter(totals)) if len(totals) == 1 else None
        duplicates, selected = [], []
        for n, variants in sorted(by_sheet.items()):
            if len({(r["sha"], r["n"]) for r in variants}) > 1:
                duplicates.append(n)
            selected.append(variants[0])
        errors = []
        if any(r["reasons"] for r in candidates):
            errors.append("Есть спорные страницы")
        if not qty:
            errors.append("Нет единого итога с первого листа")
        if duplicates:
            errors.append("Конкурирующие страницы")
        if qty and any(r["qty"] and r["qty"] != qty for r in own):
            errors.append("Противоречие в количестве листов")
        bound = qty or max(by_sheet, default=0)
        missing = sorted(set(range(1, bound+1))-by_sheet.keys())
        if missing:
            errors.append("Есть пропуски")
        if qty and any(n > qty for n in by_sheet):
            errors.append("Лист за пределами комплекта")
        plans[d] = dict(rows=candidates, selected=selected, qty=qty, missing=missing,
                        duplicates=duplicates, errors=errors)
    return rows, plans


def atomic_csv(path, headers, records):
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as out:
        writer = csv.writer(out, delimiter=";")
        writer.writerow(headers)
        for row in records:
            writer.writerow([("'" + x if isinstance(x, str) and x.startswith(("=", "+", "-", "@")) else x)
                             for x in row])
    os.replace(temp, path)


def seq(nums):
    return ", ".join(map(str, nums))


def report(db, job):
    rows, plans = analyse(db)
    def page_rows():
        for r in rows:
            raw, key = r["raw"], r["id"]
            stamp = job/"images"/(key+".stamp.png")
            yield (key, r["path"], r["n"], r["name"], r["hint"], raw.get("block"),
                   *(raw.get(k, {}).get("raw") for k in FIELDS), r["d"], r["sheet"], r["qty"],
                   r["original"], r["decision"], r["comment"], str(stamp) if stamp.exists() else "",
                   raw.get("layer"), raw.get("ocr"))
    atomic_csv(job/"Страницы.csv",
               "ID;Файл;Страница PDF;Из имени;Суффикс;Форма;Графа 2;Графа 7;Графа 8;Принятое обозначение;Принятый лист;Принято листов;Замечания распознавания;Решение;Комментарий;Штамп;Текст PDF;OCR".split(";"), page_rows())
    atomic_csv(job/"Комплекты.csv",
               "Обозначение;Файлов-кандидатов;Страниц-кандидатов;Ожидается;Номера по содержимому;Пропуски до известной границы;Дубли разных страниц;Статус;Порядок кандидатов (лист = файл # страница PDF)".split(";"),
               ((d, len({r["path"] for r in p["rows"]}), len(p["rows"]), p["qty"],
                 seq(r["sheet"] for r in p["selected"]), seq(p["missing"]), seq(p["duplicates"]),
                 " | ".join(p["errors"]) or "Готов к подтверждению",
                 "\n".join(f'{r["sheet"]} = {r["path"]} # {r["n"]}' for r in p["selected"]))
                for d,p in sorted(plans.items())))
    print(f"Страниц/ошибок: {len(rows)}. Готовых комплектов: {sum(not p['errors'] for p in plans.values())}/{len(plans)}. Отчёты: {job}")
    return rows, plans


def review(db, job, a):
    rows, plans = analyse(db)
    conflicts = {r["id"] for p in plans.values() if p["errors"] for r in p["rows"]}
    for r in rows:
        if a.id:
            if r["id"] != a.id:
                continue
        elif r["decision"] or (not a.all and r["id"] not in conflicts):
            continue
        print(f'\n{r["path"]} | страница PDF {r["n"]} | {r["d"]} | лист {r["sheet"]} | всего {r["qty"]}')
        print(r["original"] or "Проверка комплекта")
        if not r["id"]:
            print("Ошибка файла. Исправьте исходный PDF и повторите scan.")
            continue
        while True:
            action = input("[в] штамп, [п] обзор, [и] исходник, [д] подтвердить/исправить, [х] исключить, Enter дальше, [с] стоп: ").strip().lower()
            if action in ("в", "п", "и"):
                path = Path(config(db,"root"))/r["path"] if action=="и" else job/"images"/(r["id"]+(".stamp.png" if action=="в" else ".page.png"))
                if path.exists():
                    os.startfile(path)
                else:
                    print("Изображение отсутствует")
                continue
            if action == "с":
                return
            if not action:
                break
            if action not in ("д", "х"):
                continue
            edited = ""
            if action == "д":
                d = input(f'Обозначение [{r["d"] or ""}]: ').strip() or r["d"]
                s = number(input(f'Лист [{r["sheet"] or ""}]: ').strip() or r["sheet"])
                q = input(f'Листов [{r["qty"] or ""}], минус = пусто: ').strip()
                q = str(r["qty"] or "") if not q else q
                qty = number(q)
                if not d or not s or (s==1 and not qty) or (q not in ("", "-") and not qty) or (qty and s>qty):
                    print("Проверьте обозначение, номер и общее число.")
                    continue
                edited = json.dumps([d,s,qty], ensure_ascii=False)
            why = input("Основание решения: ").strip()
            if not why:
                print("Укажите основание для журнала.")
                continue
            db.execute("UPDATE pages SET decision=?, edited=?, comment=? WHERE id=?",
                       ("accept" if action=="д" else "skip", edited, why, r["id"]))
            db.commit()
            break
    if a.id and not any(r["id"]==a.id for r in rows):
        raise ValueError("ID страницы не найден")


def fingerprint(doc, index):
    page = doc[index]
    try:
        return page.get_size(), digest(render(page, min(1, 1200/max(page.get_size()))))
    finally:
        page.close()


def assemble(root, d, p, target):
    writer, marks, manifest = PdfWriter(), [], []
    sources = defaultdict(list)
    for r in p["selected"]:
        sources[r["path"]].append(r)
    # Add pages in sheet order; keep at most one independent reader per input.
    readers = {}
    for rel, entries in sources.items():
        data = (root/rel).read_bytes()
        if digest(data)!=entries[0]["sha"]:
            raise ValueError(f"Изменился источник {rel}")
        readers[rel] = PdfReader(io.BytesIO(data))
        with pdfium.PdfDocument(data) as doc:
            for r in entries:
                r["mark"] = fingerprint(doc, r["n"]-1)
    for r in p["selected"]:
        writer.add_page(readers[r["path"]].pages[r["n"]-1])
        marks.append(r["mark"])
        manifest.append([r["sheet"],r["path"],r["n"],r["sha"],r["id"]])
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        staging = Path(tmp)/target.name
        writer.write(str(staging))
        with pdfium.PdfDocument(staging) as doc:
            if len(doc)!=len(marks) or any(fingerprint(doc,i)!=m for i,m in enumerate(marks)):
                raise ValueError("Контроль изображений страниц не пройден: "+d)
        atomic_csv(staging.with_suffix(".csv"), ["Лист","Исходный файл","Стр. PDF","SHA256","ID"], manifest)
        staging.with_suffix(".csv").replace(target.with_suffix(".csv"))
        staging.replace(target)


def verify_inventory(db):
    root = Path(config(db,"root"))
    known = {r["path"]:r["sha"] for r in db.execute("SELECT * FROM files")}
    paths = inventory(root)
    if set(known) != {str(p.relative_to(root)) for p in paths}:
        raise ValueError("Состав архива изменился. Повторите scan.")
    for path in paths:
        rel = str(path.relative_to(root))
        if known[rel] and digest(path.read_bytes()) != known[rel]:
            raise ValueError(f"Файл изменился: {rel}. Повторите scan.")
    return root


def merge(db, job, a):
    root = verify_inventory(db)
    _, plans = report(db, job)
    ready = [(d,p) for d,p in sorted(plans.items()) if not p["errors"] and (not a.only or d==a.only)]
    if not ready:
        print("Нет готовых комплектов для выбранной сборки.")
        return
    run = job/"Сборки"/(time.strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6])
    for d,p in ready:
        print(f'\n{d}, листов {p["qty"]}')
        for r in p["selected"]:
            print(f'  {r["sheet"]}: {r["path"]}, стр. PDF {r["n"]}')
        if not a.yes and input("Сшить этот комплект? [да/Enter]: ").strip().lower() != "да":
            continue
        run.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r'[^\w. -]', "_", d)[:100].rstrip(" .")
        target = run/("КД_"+slug+"_"+digest(d.encode())[:8]+".pdf")
        assemble(root, d, p, target)
        print("Собрано:", target)


def initialize(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
      CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,sha TEXT,sig TEXT,count INTEGER,problem TEXT);
      CREATE TABLE IF NOT EXISTS pages(id TEXT PRIMARY KEY,path TEXT,sha TEXT,sig TEXT,n INTEGER,
        raw TEXT,problem TEXT,decision TEXT,edited TEXT,comment TEXT);
      CREATE INDEX IF NOT EXISTS src ON pages(path,sha,sig,n);
    """)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    cmds = parser.add_subparsers(dest="cmd", required=True)
    for cmd in ("scan", "report", "review", "merge"):
        sub = cmds.add_parser(cmd)
        sub.add_argument("--job", required=True, help="Папка результатов")
        if cmd == "scan":
            sub.add_argument("--source", required=True)
            sub.add_argument("--model", default="qwen2.5vl:7b")
            sub.add_argument("--tesseract", help="Путь к tesseract.exe с rus+eng")
        if cmd == "review":
            sub.add_argument("--all", action="store_true")
            sub.add_argument("--id")
        if cmd == "merge":
            sub.add_argument("--only", help="Одно полное обозначение")
            sub.add_argument("--yes", action="store_true", help="Подтвердить все выбранные готовые комплекты")
    a = parser.parse_args()
    job = Path(a.job).resolve()
    if a.cmd=="scan":
        job.mkdir(parents=True, exist_ok=True)
    elif not (job/"реестр.sqlite").is_file():
        raise ValueError("Сначала выполните scan.")
    # Windows releases the OS lock automatically if the process is interrupted.
    import msvcrt
    with (job/"session.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise ValueError("С этой рабочей папкой уже работает другой процесс.")
        db = sqlite3.connect(job/"реестр.sqlite")
        db.row_factory = sqlite3.Row
        try:
            initialize(db)
            if a.cmd=="scan":
                scan(db, job, a)
            if config(db,"complete")!="1":
                raise ValueError("Сканирование прервано. Повторите scan.")
            if a.cmd=="review":
                review(db, job, a)
            if a.cmd=="merge":
                merge(db, job, a)
            else:
                report(db, job)
        finally:
            db.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("Остановлено пользователем.")
    except Exception as exc:
        sys.exit(f"Ошибка: {exc}")
